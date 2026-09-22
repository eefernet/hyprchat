"""Opt-in local visual review. Screenshots are never sent to text-only models."""
import base64
import json
from pathlib import Path
import time


def review(store,operation_id):
    from coder_inference import local_chat,require_local_model
    from coder_worker_runtime import _json, _boundary
    from coder_evidence import resolve,attach
    payload=store.get(operation_id)["payload"]
    policy=payload.get("visual_policy",{})
    if not payload.get("ui_required"):
        return {"status":"skipped","reason":"This task does not require browser UI verification"}
    if not policy.get("enabled"):
        return {"status":"skipped","reason":"AI visual review is disabled"}
    selected=payload["settings"].get("daedalus_visual_model","") if policy.get("model_inherited") else policy.get("model")
    model=selected or payload["model"]
    screenshots=[]; observations=[]
    for check in payload.get("evidence",{}).get("checks",[]):
        if check.get("revision_id")!=payload["revision_id"]: continue
        screenshots.extend(check.get("screenshots",[]))
        observations.extend(check.get("observations",[]))
    if not screenshots:
        return {"status":"skipped","reason":"No screenshots are available for this revision","model":model}
    try:
        details=require_local_model(payload["ollama_url"],model)
        if "vision" not in details.get("capabilities",[]):
            return {"status":"skipped","reason":"The selected local model does not support vision","model":model}
        batch_size=payload["settings"]["daedalus_visual_batch_size"]
        findings=[]
        for offset in range(0,len(screenshots),batch_size):
            batch=screenshots[offset:offset+batch_size]
            images=[]; references=[]
            for shot in batch:
                record=resolve(store,store.get(operation_id)["job_id"],shot["evidence"]["id"])
                if record["revision_id"]!=payload["revision_id"]: raise ValueError("Stale screenshot")
                images.append(base64.b64encode(Path(record["path"]).read_bytes()).decode())
                references.append({"id":record["id"],"viewport":shot["viewport"]})
            instruction=("Review these screenshots against the user's request. Return JSON {findings:[{screenshot_id,category,summary,selector}]}. "
                "Categories: clipping, overlap, overflow, advisory. Keep style preferences advisory. Cite only supplied screenshot IDs. "
                "Report observable defects; do not invent interactions or failed tests. Empty findings is valid.\n"+
                json.dumps({"task":payload["original_task"],"screenshots":references,"observations":observations}))
            result=_json(local_chat(store,operation_id,"visual",[{"role":"user","content":instruction,"images":images}],model=model))
            if not isinstance(result.get("findings"),list): raise ValueError("Visual response is missing findings")
            valid_ids={ref["id"] for ref in references}
            for finding in result["findings"]:
                if not isinstance(finding,dict) or finding.get("screenshot_id") not in valid_ids or not finding.get("summary"):
                    raise ValueError("Visual findings must cite the supplied screenshots")
                # Measurements drive automatic repairs. Unsupported visual judgments
                # remain visible for the user instead of imposing a design preference.
                findings.append({**finding,"advisory":True})
        defects=[]; measurements=[]
        from coder_browser import browser_check
        from coder_repository import safe_relative
        operation=store.get(operation_id)
        for check in payload.get("evidence",{}).get("checks",[]):
            if check.get("kind")!="browser" or check.get("revision_id")!=payload["revision_id"]: continue
            shot_map={s["evidence"]["id"]:s["viewport"] for s in check.get("screenshots",[])}
            candidates=[{**f,"viewport":shot_map[f["screenshot_id"]]} for f in findings
                if f["screenshot_id"] in shot_map and f.get("selector") and f.get("category") in {"clipping","overflow","overlap"}]
            if not candidates: continue
            _boundary(store,operation_id)
            check_root=store.root/"checks"/operation["job_id"]/payload["revision_id"]
            current=store.get(operation_id)
            remaining=payload["seconds_remaining"]-(time.time()-current["started"])
            measured=browser_check(safe_relative(check_root/"workspace",check.get("cwd",".")),
                {**check,"layout_candidates":candidates},check_root/("visual-"+str(time.time_ns())),
                min(remaining,payload["settings"]["daedalus_command_seconds"]),
                step_timeout=payload["settings"]["daedalus_browser_step_seconds"],viewports=payload["settings"]["daedalus_browser_viewports"],
                emit=lambda item:store.event(operation_id,"browser_action",action=item,revision_id=payload["revision_id"]))
            attach(store,operation["job_id"],measured,payload["revision_id"]); measurements.append(measured)
            defects.extend(measured.get("layout_defects",[]))
        if any(m.get("error") for m in measurements) and not defects:
            return {"status":"skipped","reason":"Layout measurements could not be completed","model":model,"findings":findings,"measurements":measurements}
        return {"status":"failed" if defects else "passed","model":model,"revision_id":payload["revision_id"],"findings":findings,"defects":defects,"measurements":measurements}
    except InterruptedError: raise
    except Exception as error:
        return {"status":"skipped","reason":str(error),"model":model,"revision_id":payload["revision_id"]}
