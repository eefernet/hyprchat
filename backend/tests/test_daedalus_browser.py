"""Opt-in real browser test: DAEDALUS_BROWSER_TEST=1 pytest this file."""
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

import pytest

from context_policy import DEFAULTS,ROLES,resolve


@pytest.mark.skipif(os.environ.get('DAEDALUS_BROWSER_TEST')!='1', reason='Opt-in Vite/Chromium integration test')
@pytest.mark.parametrize('width',[1440,390])
def test_settings_and_job_refresh_in_browser(tmp_path,width):
    from playwright.sync_api import sync_playwright,expect
    frontend=Path(__file__).resolve().parents[2]/'frontend'
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    log=(tmp_path/'vite.log').open('w')
    server=subprocess.Popen(['node','node_modules/vite/bin/vite.js','--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=frontend,stdout=log,stderr=subprocess.STDOUT)
    settings={**DEFAULTS,'daedalus_v3_enabled':True,'resolved_contexts':{role:resolve(role,DEFAULTS).as_dict() for role in ROLES}}
    job={'id':'fixture-job','workflow_version':3,'state':'coding','event_sequence':5,'model':'coder:local',
         'workspace':'/fixture','milestones':[{'title':'Implement behavior'}],'milestone_index':0,'inventory':{'files':4000,'lines':1000000},'calls_used':2,'checks':[{'id':'syntax','passed':True,'log':'/fixture/check.log','log_tail':'tail'}]}
    saved=[];errors=[];inspections=[];snapshot_unavailable=False
    def api(route):
        nonlocal settings,job,snapshot_unavailable
        request=route.request
        if '/api/coder/projects?' in request.url:
            return route.fulfill(json={'projects':[{'id':'cp-fixture','name':'Existing app'}],'active_project_id':'cp-fixture'})
        if request.url.endswith('/api/settings'):
            if request.method=='PATCH':
                body=request.post_data_json;saved.append(body);settings={**settings,**body}
                settings['resolved_contexts']={role:resolve(role,settings).as_dict() for role in ROLES}
            return route.fulfill(json=settings)
        if '/events?' in request.url:
            return route.fulfill(content_type='text/event-stream',body='data: '+json.dumps({'seq':4,'data':{'state':'planning'}})+'\n\n')
        if request.url.split('?')[0].endswith('/cancel'):
            job={**job,'state':'cancelled','event_sequence':6};snapshot_unavailable=True;return route.fulfill(json=job)
        if request.url.endswith('/inspect'):
            body=request.post_data_json;inspections.append(body)
            if body.get('operation')=='search':
                return route.fulfill(json={'items':[{'path':'src/main.py','line':300000,'sha256':'source-hash','text':'def late_function():','cursor':'late'}],'next_cursor':None})
            if body.get('operation')=='read':
                return route.fulfill(json={'path':body['path'],'start':body['start'],'sha256':'source-hash','content':'def late_function():' if body['start']==300000 else '    return 42','next_line':300100 if body['start']==300000 else None})
            if request.post_data_json.get('operation')=='log':
                return route.fulfill(json={'content':'first page' if not request.post_data_json.get('offset') else 'last page','next_offset':10 if not request.post_data_json.get('offset') else None})
            return route.fulfill(json={'items':[{'path':'src/main.py','cursor':'src/main.py'}],'next_cursor':None})
        if snapshot_unavailable:return route.fulfill(status=503,json={'detail':'Temporary reconnect failure'})
        return route.fulfill(json=job)
    try:
        url=f'http://127.0.0.1:{port}/tests/daedalus-harness.html'
        for _ in range(100):
            try:
                urllib.request.urlopen(url,timeout=1).close();break
            except Exception:time.sleep(.05)
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':width,'height':900})
            page.context.tracing.start(screenshots=True,snapshots=True,sources=True)
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.route('**/api/**',api)
            page.goto(url)
            page.get_by_label('Daedalus project',exact=True).select_option('new')
            page.get_by_label('AI visual review for this request',exact=True).select_option('off')
            expect(page.get_by_label('Request options')).to_contain_text('"daedalus_new_project":true')
            expect(page.get_by_label('Request options')).to_contain_text('"daedalus_visual_review":false')
            page.get_by_label('Daedalus project',exact=True).select_option('cp-fixture')
            expect(page.get_by_label('Request options')).to_contain_text('"daedalus_project_id":"cp-fixture"')
            page.get_by_label('Daedalus local vision model').fill('qwen3.5:4b')
            expect(page.get_by_label('Daedalus context window',exact=True)).to_have_value('32768')
            page.get_by_label('Daedalus context window',exact=True).fill('300000')
            page.get_by_label('Daedalus compaction',exact=True).select_option('off')
            page.get_by_label('Browser interaction timeout (seconds)',exact=True).focus()
            page.get_by_label('Browser interaction timeout (seconds)',exact=True).press('ArrowUp')
            expect(page.get_by_label('Browser interaction timeout (seconds)',exact=True)).to_have_value('16')
            page.get_by_text('Stage context overrides and effective values',exact=True).click()
            page.get_by_label('acceptance context override',exact=True).fill('196608')
            page.get_by_label('reviewer output override',exact=True).fill('16384')
            page.get_by_label('reviewer thinking override',exact=True).select_option('on')
            page.get_by_label('Excluded directory names',exact=True).fill('node_modules')
            page.get_by_label('Excluded directory names',exact=True).press('End')
            page.get_by_label('Excluded directory names',exact=True).press_sequentially(', generated')
            page.get_by_role('button',name='Save coding settings',exact=True).click()
            expect(page.get_by_text('Saved. Active jobs use these values at the next model request.',exact=True)).to_be_visible()
            assert saved[-1]['openhands_num_ctx']==300000
            assert saved[-1]['daedalus_visual_model']=='qwen3.5:4b'
            assert saved[-1]['daedalus_visual_review'] is False
            assert saved[-1]['daedalus_role_contexts']['acceptance']==196608
            assert saved[-1]['daedalus_role_outputs']['reviewer']==16384
            assert saved[-1]['daedalus_role_thinking']['reviewer']=='on'
            assert saved[-1]['daedalus_compaction']=='off'
            assert saved[-1]['daedalus_browser_step_seconds']==16
            assert saved[-1]['daedalus_exclude_dirs']==['node_modules','generated']
            expect(page.get_by_label('Daedalus coding job').get_by_role('status')).to_have_text('Building')
            page.get_by_role('button',name='View details',exact=True).click()
            page.get_by_role('button',name='Files & logs',exact=True).click()
            page.get_by_text('Browse project evidence',exact=True).click()
            expect(page.get_by_role('button',name='src/main.py',exact=True)).to_be_visible()
            page.get_by_label('Evidence type',exact=True).select_option('search')
            page.get_by_label('Search project evidence',exact=True).fill('late_function')
            page.get_by_role('button',name='Search',exact=True).click()
            page.get_by_role('button',name='src/main.py:300000',exact=True).click()
            expect(page.get_by_text('src/main.py · line 300000',exact=False)).to_be_visible()
            assert inspections[-1]['operation']=='read' and inspections[-1]['start']==300000
            assert inspections[-1]['sha256']=='source-hash'
            page.get_by_role('button',name='Next page',exact=True).click()
            expect(page.get_by_text('return 42',exact=False)).to_be_visible()
            assert inspections[-1]['start']==300100 and inspections[-1]['sha256']=='source-hash'
            page.get_by_role('button',name='Checks',exact=True).click()
            page.get_by_role('button',name='Read full log',exact=True).click()
            expect(page.get_by_text('first page',exact=True)).to_be_visible()
            page.get_by_role('button',name='Next log page',exact=True).click()
            expect(page.get_by_text('last page',exact=True)).to_be_visible()
            page.screenshot(path=str(tmp_path/'daedalus-settings.png'),full_page=True)
            page.get_by_role('button',name='Close job details',exact=True).click()
            page.get_by_role('button',name='Stop',exact=True).click()
            expect(page.get_by_label('Daedalus coding job').get_by_role('status')).to_have_text('Stopped')
            expect(page.get_by_text('Reconnecting to progress. Last confirmed status is shown.')).to_be_visible()
            expect(page.get_by_label('Daedalus coding job').get_by_role('status')).to_have_text('Stopped')
            snapshot_unavailable=False
            page.reload()
            expect(page.get_by_label('Daedalus context window',exact=True)).to_have_value('300000')
            expect(page.get_by_label('Daedalus coding job').get_by_role('status')).to_have_text('Stopped')
            job={**job,'state':'blocked','event_sequence':7,
                 'probe_audits':[{'cache_key':'audit-1','check_id':'check-r1','revision_id':'revision123','round':2,'disposition':'ambiguous',
                     'reason':'Text includes the adjacent Delete control','request_basis':'Keep Delete usable','source_basis':'app.js renders the control','failure_basis':'The whole row includes both labels'}],
                 'blocker':'ConversationRunError: Model-call allowance exhausted; continue from the checkpoint\n\nConversation logs are stored at: /worker/session\nPlease file a public bug report.'}
            page.reload()
            expect(page.get_by_text('The model-call allowance is used up.',exact=False)).to_be_visible()
            expect(page.get_by_role('button',name='Continue',exact=True)).to_be_visible()
            expect(page.get_by_text('Please file a public bug report.',exact=False)).to_have_count(0)
            page.get_by_role('button',name='View details',exact=True).click()
            expect(page.get_by_role('dialog')).to_be_visible()
            expect(page.get_by_text('Please file a public bug report.',exact=False)).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.get_by_role('button',name='View details',exact=True)).to_be_focused()
            page.screenshot(path=str(tmp_path/f'progress-{width}.png'),full_page=True)
            page.context.tracing.stop(path=str(tmp_path/f'progress-{width}-trace.zip'))
            assert not errors
            browser.close()
    finally:
        server.terminate();server.wait(timeout=10);log.close()


@pytest.mark.skipif(os.environ.get('DAEDALUS_BROWSER_TEST')!='1', reason='Opt-in Chromium integration test')
def test_slow_preview_startup_is_not_an_application_failure(tmp_path):
    import shlex
    import sys
    from coder_checks import browser_check
    (tmp_path/'index.html').write_text('<!doctype html><title>Ready</title><h1>Loaded</h1>')
    evidence=tmp_path/'evidence';evidence.mkdir()
    program='import time,http.server; time.sleep(2); http.server.ThreadingHTTPServer(("127.0.0.1", {port}),http.server.SimpleHTTPRequestHandler).serve_forever()'
    check={'server_command':shlex.quote(sys.executable)+' -c '+shlex.quote(program),
           'path':'/','steps':[{'action':'text','selector':'h1','value':'Loaded'}]}
    result=browser_check(tmp_path,check,evidence,15)
    assert result['passed'] and result['title']=='Ready'
    assert result['failed_requests']==[] and result['console_errors']==[]
    assert Path(result['screenshot']).is_file()


@pytest.mark.skipif(os.environ.get('DAEDALUS_BROWSER_TEST')!='1', reason='Opt-in Chromium integration test')
@pytest.mark.parametrize('step',[
    {'action':'visible','selector':'#missing'},
    {'action':'visible','selector':'h1','value':'Wrong content'},
])
def test_browser_assertions_use_the_configured_step_allowance(tmp_path,step):
    from playwright.sync_api import TimeoutError as BrowserTimeout
    from coder_checks import browser_check
    (tmp_path/'index.html').write_text('<!doctype html><h1>Actual content</h1>')
    evidence=tmp_path/'evidence';evidence.mkdir()
    check={'server_command':'python3 -m http.server {port} --bind 127.0.0.1','path':'/','steps':[step]}
    started=time.monotonic()
    with pytest.raises((BrowserTimeout,AssertionError)):
        browser_check(tmp_path,check,evidence,30,step_timeout=1)
    assert time.monotonic()-started<8


@pytest.mark.skipif(os.environ.get('DAEDALUS_BROWSER_TEST')!='1', reason='Opt-in Chromium integration test')
def test_browser_deletion_and_empty_text_assertions_reject_remaining_content():
    from playwright.sync_api import sync_playwright
    from coder_browser import interact
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        page=browser.new_page()
        page.set_content('<ul id="items"><li>Keep me</li></ul><button id="delete" onclick="document.querySelector(\'li\').remove()">Delete</button>')
        absent={'action':'exists','selector':'li','value':False}
        empty={'action':'text','selector':'#items','value':''}
        for step in (absent,empty,{'action':'visible','selector':'li','value':False},
                     {'action':'text','selector':'li','value':'Keep','exact':True}):
            with pytest.raises(AssertionError): interact(page,step,100)
        interact(page,{'action':'text','selector':'li','value':'Keep'},100)
        interact(page,{'action':'click','selector':'#delete'},100)
        interact(page,absent,100)
        interact(page,empty,100)
        interact(page,{'action':'visible','selector':'li','value':False},100)
        # Absence is a count assertion, so multiple matches must also fail.
        page.set_content('<li>A</li><li>B</li>')
        with pytest.raises(AssertionError): interact(page,absent,100)
        with pytest.raises(ValueError,match='true or false'):
            interact(page,{**absent,'value':'false'},100)
        browser.close()


@pytest.mark.skipif(os.environ.get('DAEDALUS_BROWSER_TEST')!='1', reason='Opt-in Chromium integration test')
def test_policy4_composite_text_keeps_delete_actionable():
    from playwright.sync_api import sync_playwright,TimeoutError
    from coder_browser import interact
    from coder_browser_schema import validate_steps
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        page=browser.new_page()
        page.set_content('<ul><li>Entered text<button onclick="this.parentNode.remove()">Delete</button></li></ul>')
        steps=[{'action':'text','selector':'li','value':'Entered text'},
               {'action':'click','selector':{'role':'button','name':'Delete'}},
               {'action':'exists','selector':'li','value':False}]
        validate_steps(steps)
        for step in steps: interact(page,step,200)
        page.set_content('<ul><li>Entered text<button style="width:0;height:0;padding:0;border:0;overflow:hidden" onclick="this.parentNode.remove()">Delete</button></li></ul>')
        with pytest.raises(TimeoutError): interact(page,steps[1],200)
        assert page.locator('li').count()==1
        browser.close()
