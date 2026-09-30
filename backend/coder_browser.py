"""Headless browser evidence; usable by a text-only Builder and final checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from urllib.parse import urlsplit

from context_policy import DEFAULTS


def locate(page, selector):
    if isinstance(selector, str):
        return page.locator(selector)
    if not isinstance(selector, dict):
        raise ValueError("Use a CSS selector, {role,name}, or {label}")
    if selector.get("role"):
        return page.get_by_role(selector["role"],name=selector.get("name"),exact=True)
    if selector.get("label"):
        return page.get_by_label(selector["label"],exact=True)
    raise ValueError("Unknown accessible selector")


def interact(page, step, milliseconds):
    from playwright.sync_api import expect
    action, value = step["action"], step.get("value", "")
    if action == "reload":
        page.reload(wait_until="domcontentloaded",timeout=milliseconds)
        return
    target = locate(page,step["selector"])
    if action == "click": target.click(timeout=milliseconds)
    elif action == "fill": target.fill(value,timeout=milliseconds)
    elif action == "select": target.select_option(value,timeout=milliseconds)
    elif action == "press": target.press(value,timeout=milliseconds)
    elif action == "check": target.set_checked(value is not False,timeout=milliseconds)
    elif action == "scroll": target.scroll_into_view_if_needed(timeout=milliseconds)
    elif action == "exists":
        if "value" in step and not isinstance(value, bool):
            raise ValueError("Browser exists value must be true or false")
        if value is False: expect(target).to_have_count(0,timeout=milliseconds)
        else: expect(target).to_be_attached(timeout=milliseconds)
    elif action == "visible":
        if value is False: expect(target).to_be_hidden(timeout=milliseconds)
        else:
            expect(target).to_be_visible(timeout=milliseconds)
            if "value" in step and value is not True:
                expect(target).to_contain_text(value,timeout=milliseconds)
    elif action == "text":
        if not isinstance(value, str): raise ValueError("Browser text value must be a string")
        if value == "" or step.get("exact") is True:
            expect(target).to_have_text(value,timeout=milliseconds)
        else: expect(target).to_contain_text(value,timeout=milliseconds)
    else: raise ValueError("Unknown browser action: " + action)


def browser_check(root, check, evidence_dir, timeout, *, step_timeout=None, viewports=None, emit=None, startup_timeout=None, policy_version=3):
    from coder_sandbox_call import invoke, browser_options
    if "{port}" not in check.get("server_command", ""):
        raise ValueError("Preview server_command must use {port} for its listening port")
    if os.environ.get('DAEDALUS_SANDBOXED') != '1':
        Path(evidence_dir).mkdir(parents=True, exist_ok=True)
        result = invoke('coder_browser', 'browser_check', [str(root), check, str(evidence_dir), timeout],
            {'step_timeout': step_timeout, 'viewports': viewports, 'startup_timeout': startup_timeout,
             'policy_version': policy_version}, writable=[root, evidence_dir], timeout=timeout + 10)
        if emit:
            for event in result.get('timeline', []): emit(event)
        return result
    if policy_version >= 4:
        from coder_browser_schema import validate_flow
        validate_flow(check)
    from playwright.sync_api import sync_playwright
    if "{port}" not in check.get("server_command",""):
        raise ValueError("Preview server_command must use {port} for its listening port, for example python3 -m http.server {port} --bind 127.0.0.1. A fixed port cannot use the managed browser session.")
    evidence_dir = Path(evidence_dir); evidence_dir.mkdir(parents=True,exist_ok=True)
    step_timeout = step_timeout or DEFAULTS["daedalus_browser_step_seconds"]
    startup_timeout = startup_timeout or DEFAULTS['daedalus_browser_startup_seconds']
    viewports = viewports or DEFAULTS["daedalus_browser_viewports"]
    path = check.get("path","/")
    if not isinstance(path,str) or "\\" in path or urlsplit(path).scheme or urlsplit(path).netloc or path.startswith("//"):
        raise ValueError("Browser path must be project-relative")
    path="/"+path.lstrip("/")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0)); port = sock.getsockname()[1]
    start = time.monotonic()
    def remaining():
        seconds = timeout-(time.monotonic()-start)
        if seconds <= 0: raise TimeoutError("Browser command allowance exhausted")
        return seconds*1000
    timeline, observations, screenshots, traces = [], [], [], []
    errors, failures, http_errors, defects = [], [], [], []
    def event(**data):
        item={"index":len(timeline)+1,"seconds":round(time.monotonic()-start,3),**data}
        timeline.append(item)
        if emit: emit(item)
    server_log = evidence_dir/"browser-server.log"
    environment = dict(os.environ)
    environment["PATH"] = str(Path(root)/".venv"/"bin")+os.pathsep+environment.get("PATH","")
    error = ""
    ready = False
    with server_log.open("wb") as output:
        server = subprocess.Popen(["bash","-c",check["server_command"].replace("{port}",str(port))],
            cwd=root,stdout=output,stderr=subprocess.STDOUT,start_new_session=True,env=environment)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True,args=["--no-sandbox"],timeout=remaining(), **browser_options())
                try:
                    for viewport in viewports:
                        name = viewport["name"]
                        context = browser.new_context(viewport={k:viewport[k] for k in ("width","height")})
                        context.tracing.start(screenshots=True,snapshots=True,sources=True)
                        page = context.new_page()
                        page.on("pageerror",lambda e:errors.append(str(e)))
                        page.on("console",lambda m:errors.append(m.text) if m.type=="error" else None)
                        page.on("requestfailed",lambda r:failures.append({"url":r.url,"failure":r.failure}))
                        page.on("response",lambda r:http_errors.append({"url":r.url,"status":r.status}) if r.status>=400 else None)
                        try:
                            while True:
                                if server.poll() is not None: raise ValueError("Preview server exited")
                                if not ready and time.monotonic()-start >= startup_timeout:
                                    raise TimeoutError('Preview failed to start within its startup allowance')
                                try:
                                    page.goto(f"http://127.0.0.1:{port}{path}",wait_until="domcontentloaded",timeout=remaining() if ready else min(remaining(),max(1,(startup_timeout-(time.monotonic()-start))*1000)))
                                    ready = True
                                    break
                                except Exception as exc:
                                    if "ERR_CONNECTION_REFUSED" not in str(exc): raise
                                    errors.clear(); failures.clear(); http_errors.clear()
                                    remaining(); time.sleep(.2)
                            event(viewport=name,action="open",path=path,status="passed")
                            for step in check.get("steps",[]):
                                try:
                                    interact(page,step,min(remaining(),step_timeout*1000))
                                    event(viewport=name,**step,status="passed")
                                except Exception as exc:
                                    event(viewport=name,**step,status="failed",error=str(exc)); raise
                            layout = page.evaluate("""() => ({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
                                overflow:Array.from(document.querySelectorAll('body *')).filter(e=>{
                                const r=e.getBoundingClientRect();const s=getComputedStyle(e);
                                return r.width>0 && r.height>0 && s.visibility!=='hidden' && (r.right>innerWidth+2 || r.left < -2)
                                }).slice(0,20).map(e=>({tag:e.tagName,id:e.id,text:(e.innerText||'').slice(0,100)}))})""")
                            if layout["scrollWidth"] > layout["width"]+2:
                                defects.append({"summary":"Horizontal overflow at "+name,"viewport":name,"measurement":layout})
                            for candidate in check.get("layout_candidates",[]):
                                if candidate.get("viewport")!=name: continue
                                try:
                                    target=locate(page,candidate["selector"])
                                    if target.count()!=1 or not target.is_visible(): continue
                                    measured=target.evaluate("""e=>{const r=e.getBoundingClientRect();
                                        const x=r.x+r.width/2,y=r.y+r.height/2,top=document.elementFromPoint(x,y);
                                        return {x:r.x,y:r.y,width:r.width,height:r.height,viewportWidth:innerWidth,
                                        obstructed:e.matches('button,input,select,textarea,a[href]') && x>=0 && y>=0 && x<innerWidth && y<innerHeight && !!top && !e.contains(top),
                                        text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,150)}}""",timeout=min(remaining(),step_timeout*1000))
                                    clipped=measured["x"] < -2 or measured["x"]+measured["width"]>measured["viewportWidth"]+2
                                    if (candidate["category"] in {"clipping","overflow"} and clipped) or (candidate["category"]=="overlap" and measured["obstructed"]):
                                        defects.append({"summary":candidate["summary"],"viewport":name,"selector":candidate["selector"],"measurement":measured})
                                except Exception as exc:
                                    event(viewport=name,action="measure_layout",status="unverified",error=str(exc))
                            observations.append({"viewport":name,"title":page.title(),"dom":page.locator("body").aria_snapshot(),"layout":layout})
                        except Exception as exc:
                            error = str(exc)
                        finally:
                            screenshot = evidence_dir/f"{name}.png"
                            try:
                                page.screenshot(path=str(screenshot),full_page=True,timeout=step_timeout*1000)
                                screenshots.append({"viewport":name,"path":str(screenshot)})
                            except Exception as exc:
                                event(viewport=name,action="screenshot",status="failed",error=str(exc))
                            trace = evidence_dir/f"{name}-trace.zip"
                            context.tracing.stop(path=str(trace)); traces.append({"viewport":name,"path":str(trace)})
                            context.close()
                        if error: break
                finally: browser.close()
        except Exception as exc: error = str(exc)
        finally:
            try: os.killpg(server.pid,signal.SIGTERM)
            except ProcessLookupError: pass
            try: server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid,signal.SIGKILL); server.wait()
    result={"passed":not (error or errors or failures or http_errors or defects),"error":error,
        "console_errors":errors,"failed_requests":failures,"http_errors":http_errors,"layout_defects":defects,
        "observations":observations,"screenshots":screenshots,"traces":traces,"timeline":timeline,"server_log":str(server_log),
        "environment_fault":any(s in error.lower() for s in ("executable doesn't exist","missing dependencies","error while loading shared libraries"))}
    (evidence_dir/"browser.json").write_text(json.dumps(result))
    return result
