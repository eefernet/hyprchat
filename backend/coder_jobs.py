"""One owner for Daedalus job lifecycle, budgets, verification, and delivery."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import time
import uuid

import httpx

import config
import context_policy
import database as db
from db import coder_jobs as store


_RUNNERS = {}
_OWNERS = {}
_CANCEL_FOLLOWUPS = set()
_CLOSING = False
_DELIVERY = asyncio.Lock()
_WRITER = asyncio.Lock()
_HTTP = None
_EVENTS = None
_RECONCILER = None


def configure(http, events):
    global _HTTP, _EVENTS, _CLOSING
    _HTTP, _EVENTS, _CLOSING = http, events, False


def worker_url(job, operation=""):
    base = config.OPENHANDS_URL.rstrip("/") + f"/jobs/{job['id']}/operations/"
    return base + (operation or job["worker_operation"])


def spawn(job_id, owner=None):
    if _CLOSING:
        return
    if job_id in _RUNNERS and not _RUNNERS[job_id].done():
        return
    async def owned():
        token = db.set_current_user_id(owner or db.current_user_id())
        try:
            async with _WRITER:
                await run(job_id)
        finally:
            db.reset_current_user_id(token)
    _OWNERS[job_id] = owner or db.current_user_id()
    task = asyncio.create_task(owned())
    _RUNNERS[job_id] = task
    def cleanup(done):
        _RUNNERS.pop(job_id, None)
        _OWNERS.pop(job_id, None)
    task.add_done_callback(cleanup)


async def recover():
    for row in await store.recoverable():
        spawn(row["id"], row["user_id"])


async def start():
    """Recover startup work and close the commit-to-dispatch cancellation gap."""
    global _RECONCILER
    await recover()
    if _RECONCILER and not _RECONCILER.done():
        return

    async def reconcile_queue():
        failed = False
        while not _CLOSING:
            await asyncio.sleep(2)
            try:
                await recover()
                failed = False
            except Exception:
                if not failed:
                    logging.getLogger(__name__).exception("Coding queue recovery failed; retrying")
                failed = True
    _RECONCILER = asyncio.create_task(reconcile_queue())


async def shutdown():
    global _CLOSING, _RECONCILER
    _CLOSING = True
    tasks = list(_RUNNERS.values())
    if _RECONCILER:
        tasks.append(_RECONCILER)
        _RECONCILER = None
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # Worker subprocesses keep running. A subsequent startup reconnects.


async def sync_settings():
    pending = []
    for job_id, owner in list(_OWNERS.items()):
        token = db.set_current_user_id(owner)
        try:
            job = await store.get(job_id)
        finally:
            db.reset_current_user_id(token)
        if not job or not job.get("worker_operation"):
            continue
        try:
            response = await _HTTP.patch(worker_url(job) + "/settings", json=context_policy.runtime_settings(), timeout=10)
            response.raise_for_status()
        except Exception:
            pending.append(job_id)
    return pending


async def create(conversation_id, task, mode="build_from_prompt", project_id="", model="", key=""):
    if not context_policy.runtime_settings()["daedalus_v3_enabled"]:
        raise ValueError("Enable the new Daedalus workflow in Settings first")
    if mode not in ("build_from_prompt", "fix_uploaded_project", "ask_uploaded_project"):
        raise ValueError("Unknown workflow mode")
    if not task.strip():
        raise ValueError("A task is required")
    source = None
    project = None
    if project_id:
        project = await db.get_coding_project(project_id)
        if not project:
            raise LookupError("Project not found")
        source_id = project.get("openhands_project_id", "")
        if source_id.startswith("cw3-"):
            source = await store.get(source_id)
            if not source or not (source.get("acceptance") or {}).get("accepted"):
                raise ValueError("Source project has no accepted revision")
    elif mode != "build_from_prompt":
        raise ValueError("Select a registered project")
    conversation = await db.get_conversation(conversation_id) if mode == "ask_uploaded_project" else None
    chosen_model = model or ((config.QA_MODEL or (conversation or {}).get("model")) if conversation is not None else None) or config.BUILDER_MODEL or config.CODER_MODEL
    if not chosen_model or chosen_model.startswith(("openai:", "anthropic:", "custom:")) or chosen_model.endswith(("-cloud", ":cloud")):
        raise ValueError("Select an installed local coding model in Settings")
    # The queue consumer may see this row as soon as it commits. Persist the
    # complete source identity with the job so recovery cannot dispatch a
    # partly initialized project after its creating request disconnects.
    job = await store.create(conversation_id, task, mode, project_id, chosen_model, key or uuid.uuid4().hex,
        source_job_id=source["id"] if source else "", source_revision_id=source["revision_id"] if source else "",
        source_project_id=(project.get("openhands_project_id") or project_id) if project and not source else "")
    spawn(job["id"])
    return job


async def cancel(job_id):
    async with _DELIVERY:
        job = await store.request_cancel(job_id)
    if not job:
        raise LookupError("Workflow not found")
    if job["state"] in store.TERMINAL:
        return job
    if job.get("worker_operation"):
        try:
            response = await _HTTP.post(worker_url(job) + "/cancel", timeout=15)
            response.raise_for_status()
            if response.json()["status"] in ("cancelled", "failed", "succeeded", "blocked", "interrupted"):
                operation = response.json()
                checkpoint = operation.get("result",{}).get("snapshot")
                if checkpoint:
                    await store.record_revision(job_id,checkpoint)
                attempt = await db.get_run(job["worker_operation"])
                if attempt and attempt.get("status") in ("queued","running","pending"):
                    await db.update_run(job["worker_operation"],status="cancelled",result_envelope=operation.get("result",{}),ended=True)
                return await store.save(job_id, state="cancelled", event="cancelled", **({"revision_id":checkpoint["revision"]} if checkpoint else {}))
        except Exception:
            job = await store.save(job_id, state="cancelling", blocker="Waiting for worker cancellation acknowledgement")
            owner = db.current_user_id()
            previous = _RUNNERS.get(job_id)
            if previous and not previous.done():
                if job_id not in _CANCEL_FOLLOWUPS:
                    _CANCEL_FOLLOWUPS.add(job_id)
                    def retry_after_exit(_):
                        _CANCEL_FOLLOWUPS.discard(job_id)
                        spawn(job_id,owner)
                    previous.add_done_callback(retry_after_exit)
            else:
                spawn(job_id,owner)
            return job
    else:
        return await store.save(job_id, state="cancelled", event="cancelled")
    return await store.get(job_id)


async def resume(job_id):
    job = await store.get(job_id)
    if not job:
        raise LookupError("Workflow not found")
    if job["state"] not in ("blocked", "waiting_for_input"):
        raise ValueError("Only a blocked workflow can be continued")
    if job.get("worker_operation"):
        try:
            previous = await _HTTP.get(worker_url(job), timeout=15)
            if previous.status_code != 404:
                previous.raise_for_status()
                operation = previous.json()
                if operation["status"] in {"queued", "starting", "running", "cancelling"}:
                    raise ValueError("The previous worker operation is still active. Wait for it to stop before continuing.")
                await db.update_run(job["worker_operation"], status=operation["status"], result_envelope=operation.get("result", {}), ended=True)
        except httpx.HTTPError as error:
            raise ValueError("Cannot confirm the previous worker has stopped. Reconnect the worker before continuing.") from error
    job = await store.save(job_id, state="queued", event="resume", allowance=job.get("allowance", 1) + 1,
                           calls_used=0, seconds_used=0, worker_operation="", operation_key="", blocker="", attempt=0, acceptance_attempt=0,
                           plan_errors=[],
                           resume_after_inspect=("" if job.get("resume_state") in {"queued", "inspecting"} else job.get("resume_state", "planning")))
    previous = _RUNNERS.get(job_id)
    if previous and not previous.done():
        owner = db.current_user_id()
        previous.add_done_callback(lambda _: spawn(job_id, owner))
    else:
        spawn(job_id)
    return job


async def _worker_request(job_id, method, url, **kwargs):
    """Reconnect to the same operation while retaining the controller's lease."""
    disconnected = False
    while True:
        current = await store.get(job_id)
        if not current or current.get("cancel_requested"):
            raise InterruptedError("Cancellation requested")
        try:
            response = await _HTTP.request(method, url, **kwargs)
            response.raise_for_status()
        except httpx.HTTPError as error:
            if isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500:
                raise
            if not disconnected:
                await store.save(job_id, event="worker_reconnecting", blocker="Reconnecting to the worker; the existing operation still owns this job")
                disconnected = True
            await asyncio.sleep(2)
            continue
        if disconnected:
            await store.save(job_id, event="worker_reconnected", blocker="")
        return response


async def uses_persistent_workflow(conversation_id):
    latest = await db.get_latest_coder_workflow(conversation_id)
    if latest and latest.get("workflow_version") == 3 and latest.get("state") in store.ACTIVE:
        return True
    if latest and latest.get("state") in {"planning","building","reviewing","fixing","accepting","running","queued","cancelling"}:
        return False
    return bool(context_policy.runtime_settings()["daedalus_v3_enabled"])


async def route_tool(name, args, conversation_id, events):
    """Return None for legacy dispatch; never turn an inspection into editing."""
    relevant = {"start_coder_workflow", "plan_project", "generate_code", "run_aider_fix", "ask_project",
                "get_coder_workflow", "cancel_coder_workflow", "run_review", "run_acceptance_review", "run_fixer",
                "download_project", "download_file", "write_file", "run_shell", "execute_code"}
    if not conversation_id or name not in relevant:
        return None
    latest = await db.get_latest_coder_workflow(conversation_id)
    job = await store.get(latest["id"]) if latest and latest.get("workflow_version") == 3 else None
    enabled = context_policy.runtime_settings()["daedalus_v3_enabled"]
    if latest and latest.get("workflow_version") != 3 and latest.get("state") in {"planning","building","reviewing","fixing","accepting","running","queued","cancelling"}:
        return None
    starts = {"start_coder_workflow", "plan_project", "generate_code", "run_aider_fix", "ask_project"}
    if not job and (not enabled or name not in starts):
        return None
    if name in starts:
        conv = await db.get_conversation(conversation_id)
        latest_user = next((m for m in reversed((conv or {}).get("messages", [])) if m.get("role") == "user"), {})
        task = args.get("task") or args.get("question") or latest_user.get("content") or ""
        key = str(latest_user.get("id") or hashlib.sha256(task.encode()).hexdigest())
        if job and (job["state"] in store.ACTIVE or job.get("chat_request_key") == key):
            return json.dumps({"workflow_id": job["id"], "state": job["state"], "message": "The background job owns execution. Report its status; do not repeat workflow tools."})
        if not enabled:
            return None
        project = await db.get_coding_project_by_conv(conversation_id)
        project_id = args.get("project_id") or (project or {}).get("id", "")
        mode = args.get("mode") or ("ask_uploaded_project" if name == "ask_project" else "fix_uploaded_project" if project_id else "build_from_prompt")
        job = await create(conversation_id, task, mode, project_id, key=key)
        job = await store.save(job["id"], chat_request_key=key)
        await events.emit(conversation_id, "tool_start", {"tool": name, "workflow_id": job["id"], "status": "Daedalus job started"})
        await events.emit(conversation_id, "tool_end", {"tool": name, "workflow_id": job["id"], "status": "Running in background; progress reconnects after refresh"})
        return json.dumps({"workflow_id": job["id"], "state": job["state"], "message": "Job started in background. The job controller handles planning, coding, checks, and acceptance. Tell the user it has started; do not launch additional tools for this job."})
    if not job:
        return None
    if name in {"cancel_coder_workflow", "get_coder_workflow"}:
        target = await store.get(args.get("workflow_id") or job["id"])
        if not target or target["conversation_id"] != conversation_id:
            return json.dumps({"error":"Workflow not found in this conversation"})
        return json.dumps(await cancel(target["id"]) if name == "cancel_coder_workflow" else target)
    if name in {"run_review", "run_acceptance_review", "run_fixer", "download_project", "download_file"}:
        return json.dumps({"workflow_id": job["id"], "state": job["state"], "artifact": job.get("artifact"), "blocker": job.get("blocker"),
                           "message": "Checks and delivery are managed by this workflow. Only its accepted artifact is deliverable."})
    if name in {"write_file", "run_shell", "execute_code"} and job["state"] in store.ACTIVE:
        return json.dumps({"error": "A coding job owns the project. Stop it before making independent edits.", "workflow_id": job["id"]})
    return None


async def _operate(job, kind, **extra):
    settings = context_policy.runtime_settings()
    seconds = settings["daedalus_job_seconds"] - job.get("seconds_used", 0)
    calls = settings["daedalus_model_calls"] - job.get("calls_used", 0)
    if seconds <= 0 or (calls <= 0 and kind in {"plan","code","accept","qa"}):
        raise TimeoutError("Execution allowance exhausted. Continue from the saved checkpoint.")
    operation_key = hashlib.sha256(json.dumps({"kind": kind, "milestone": job.get("milestone_index"),
        "attempt": job.get("attempt", 0), "allowance": job.get("allowance", 1), "revision": job.get("revision_id"),
        "extra": extra}, sort_keys=True).encode()).hexdigest()
    if job.get("operation_key") != operation_key:
        seq = job.get("operation_sequence", 0) + 1
        operation_id = f"{job['id']}-{seq}"
        job = await store.save(job["id"], worker_operation=operation_id, operation_sequence=seq,
                               operation_key=operation_key, operation_kind=kind, operation_accounted=False, worker_event_cursor=0)
    await store.start_attempt(job, job["worker_operation"], kind)
    payload = {"kind": kind, "request_key": operation_key, "task": job["user_task"],
               "original_task": job["user_task"], "source_project_id": job.get("source_project_id") or job["project_id"],
               "source_job_id":job.get("source_job_id",""), "source_revision_id":job.get("source_revision_id",""),
               "model": job["model"], "ollama_url": config.OLLAMA_URL,
               "disable_thinking": config.OPENHANDS_DISABLE_THINKING, "reasoning_effort": config.OPENHANDS_REASONING_EFFORT,
               "settings": settings, "seconds_remaining": seconds, "calls_remaining": calls,
               "revision_id": job.get("revision_id", ""), **extra}
    stage_model = {"plan": config.ARCHITECT_MODEL or config.PLANNING_MODEL,
                   "accept": config.ACCEPTANCE_MODEL or config.PLANNING_MODEL,
                   "qa": config.QA_MODEL}.get(kind)
    if stage_model:
        if stage_model.startswith(("openai:","anthropic:","custom:")) or stage_model.endswith(("-cloud", ":cloud")):
            raise ValueError(f"Select a local {kind} model in Settings")
        payload["model"] = stage_model
    response = await _worker_request(job["id"], "POST", worker_url(job), json=payload, timeout=30)
    response.raise_for_status()
    started = time.monotonic()
    deadline_exhausted = False
    while True:
        current = await store.get(job["id"])
        if not current or current.get("cancel_requested"):
            await cancel(job["id"])
            raise InterruptedError("Cancellation requested")
        response = await _worker_request(job["id"], "GET", worker_url(job), timeout=30)
        response.raise_for_status()
        operation = response.json()
        progress = await _worker_request(job["id"], "GET", worker_url(job) + "/events", params={"after": current.get("worker_event_cursor", 0)}, timeout=30)
        progress.raise_for_status()
        batch = progress.json()
        if batch:
            context = next((e["data"]["context"] for e in reversed(batch) if e.get("data", {}).get("context")), current.get("context"))
            await store.save(job["id"], event="progress", worker_event_cursor=batch[-1]["seq"],
                             context=context, operation_calls=operation.get("calls",0),
                             operation_seconds=max(0, time.time()-(operation.get("started") or time.time())),
                             activity=batch[-1].get("type"))
        if operation["status"] not in ("queued", "starting", "running", "cancelling"):
            break
        if not deadline_exhausted and time.monotonic() - started > seconds:
            deadline_exhausted = True
            await store.save(job["id"], event="allowance_exhausted", blocker="Execution allowance exhausted; waiting for the worker to stop at its checkpoint")
            await _worker_request(job["id"], "POST", worker_url(job) + "/cancel", timeout=15)
            # Keep the writer lease through cancellation acknowledgement.
            # The normal result path records final usage and the checkpoint.
        await asyncio.sleep(2)
    result = operation["result"]
    await db.update_run(job["worker_operation"], status=operation["status"], result_envelope=result, ended=True)
    if not current.get("operation_accounted"):
        elapsed = max(0, (operation.get("ended") or operation["started"]) - operation["started"])
        await store.save(job["id"], calls_used=current.get("calls_used", 0) + operation.get("calls", 0),
                         seconds_used=current.get("seconds_used", 0) + elapsed, operation_accounted=True,
                         last_operation_status=operation["status"], operation_calls=0, operation_seconds=0)
    if result.get("snapshot"):
        await store.record_revision(job["id"], result["snapshot"])
    if operation["status"] != "succeeded" or deadline_exhausted:
        changes = {}
        if result.get("snapshot"):
            changes["revision_id"] = result["snapshot"]["revision"]
        if result.get("checks"):
            changes["checks"] = result["checks"]
        if changes:
            await store.save(job["id"], **changes)
        if deadline_exhausted:
            raise TimeoutError("Execution allowance exhausted; worker stopped at its checkpoint")
        raise RuntimeError(result.get("error") or f"Worker operation {operation['status']}")
    return result, await store.get(job["id"])


def _checks(milestones, discovered=None):
    checks, seen = [], set()
    for milestone in milestones:
        requested = list(milestone.get("checks", []))
        if discovered is not None:
            for flow in milestone.get("browser_flows", []):
                preview = next((item for item in discovered if item.get("kind") == "browser" and item.get("cwd", ".") == flow.get("cwd", ".")), None)
                if not preview:
                    raise ValueError(f"No preview server discovered for browser flow in {flow.get('cwd','.')}")
                requested.append({**{k:v for k,v in preview.items() if k != "id"}, **flow, "kind":"browser", "server_command":preview["server_command"]})
        for check in requested:
            if not isinstance(check, dict) or not (check.get("command") or (check.get("kind") == "browser" and check.get("server_command"))):
                raise ValueError("Each check needs an executable command or browser flow")
            candidate = {**check, "cwd":check.get("cwd",".")}
            key = json.dumps({k:v for k,v in candidate.items() if k not in {"id","origin"}},sort_keys=True)
            if key not in seen:
                seen.add(key)
                candidate["id"] = check.get("id") or "check-" + hashlib.sha256(key.encode()).hexdigest()[:12]
                checks.append(candidate)
    return checks


async def _deliver(job, result):
    accepted = job.get("acceptance") or {}
    if not accepted.get("accepted") or accepted.get("revision_id") != job["revision_id"] or result["revision_id"] != job["revision_id"]:
        raise ValueError("Artifact revision does not match accepted revision")
    artifact_id = "artifact-" + hashlib.sha256(f"{job['id']}:{job['revision_id']}".encode()).hexdigest()[:24]
    filename = f"{job['id']}-{job['revision_id']}.tar.gz"
    destination = Path(config.SANDBOX_OUTPUTS_DIR) / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".part")
    digest = hashlib.sha256()
    try:
        async with _HTTP.stream("GET", worker_url(job) + "/artifact", timeout=120) as response:
            response.raise_for_status()
            with partial.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    latest = await store.get(job["id"])
                    if latest.get("cancel_requested"):
                        raise InterruptedError("Cancellation requested")
                    output.write(chunk)
                    digest.update(chunk)
        if digest.hexdigest() != result["sha256"]:
            raise ValueError("Artifact checksum mismatch")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    try:
        artifact = await store.finish_artifact(job["id"], job["revision_id"], artifact_id=artifact_id,
            filename=filename, url=f"/api/downloads/{filename}", kind="archive", mime_type="application/gzip",
            storage_path=str(destination), size_bytes=destination.stat().st_size, sha256=result["sha256"], exists_status="present", status="accepted",
            metadata={"revision_id": job["revision_id"], "acceptance": accepted, "workflow_version": 3})
        if not artifact:
            raise RuntimeError("Artifact registration failed")
    except BaseException:
        # Cancellation may arrive immediately after SQLite committed. Preserve
        # an already-published file, and keep it on an ambiguous DB failure.
        try:
            latest = await asyncio.shield(store.get(job["id"]))
        except BaseException:
            latest = None
        if latest and (latest.get("artifact") or {}).get("id") != artifact_id:
            destination.unlink(missing_ok=True)
        raise
    return artifact


async def run(job_id):
    try:
        while True:
            job = await store.get(job_id)
            if not job or job["state"] in store.TERMINAL or job["state"] in ("blocked", "waiting_for_input"):
                return
            if job.get("cancel_requested"):
                cancelled = await cancel(job_id)
                if cancelled["state"] != "cancelled":
                    await asyncio.sleep(2)
                    continue
                return
            state = job["state"]
            if state in ("queued", "inspecting"):
                job = await store.save(job_id, state="inspecting")
                result, job = await _operate(job, "inspect")
                next_state = job.get("resume_after_inspect") or ("baselining" if result["inventory"]["files"] and job["mode"] != "ask_uploaded_project" else "planning")
                if job.get("revision_id") and result["snapshot"]["revision"] != job["revision_id"] and job.get("milestones"):
                    next_state = "checking"
                await store.save(job_id, state=next_state, inventory=result["inventory"],
                                 baseline_revision=job.get("baseline_revision") or result["snapshot"]["revision"],
                                 revision_id=result["snapshot"]["revision"], workspace=result["workspace"], verification=result.get("verification",{}), resume_after_inspect="")
            elif state == "baselining":
                checks = job.get("verification",{}).get("checks",[])
                if checks:
                    result, job = await _operate(job,"check",checks=checks,baseline=True)
                    await store.record_check(job_id,job["worker_operation"],job["revision_id"],result)
                    await store.save(job_id,state="blocked" if result.get("environment_fault") else "planning",
                                     resume_state="baselining", baseline_checks=result["checks"],
                                     blocker="Verification environment is unavailable; inspect dependency setup and command logs" if result.get("environment_fault") else "")
                else:
                    await store.save(job_id,state="planning",baseline_checks=[],baseline_note="No deterministic check command discovered")
            elif state == "planning":
                if job["mode"] == "ask_uploaded_project":
                    result, job = await _operate(job, "qa")
                    await store.save(job_id, state="completed", answer=result.get("answer", ""))
                    return
                evidence = {"inventory": job.get("inventory", {}), "verification":job.get("verification",{}), "baseline_checks":job.get("baseline_checks",[])}
                if job.get("plan_feedback"):
                    evidence["plan_feedback"] = job["plan_feedback"]
                result, job = await _operate(job, "plan", evidence=evidence)
                _checks(result["milestones"])
                # Separate persisted operation boundary: the Architect has
                # returned before any generate_code operation is dispatched.
                await store.save(job_id, state="coding", event="plan_returned", milestones=result["milestones"], milestone_index=0, attempt=0, blocker="")
                await asyncio.sleep(0)
            elif state == "coding":
                milestone = job["milestones"][job["milestone_index"]]
                result, job = await _operate(job, "code", task=milestone["task"], milestone=milestone,
                    milestone_id=str(job["milestone_index"]), evidence={"checks": job.get("checks", []), "issues": job.get("issues", [])})
                await store.save(job_id, state="checking", revision_id=result["snapshot"]["revision"],
                                 inventory=result["inventory"], verification=result.get("verification",{}), acceptance=None, builder_finished=result["agent_finished"], builder_note=result.get("incomplete_reason", ""))
            elif state == "checking":
                # Re-run the accumulated milestone checks to catch regressions.
                selected = job["milestones"][:job["milestone_index"] + 1]
                try:
                    checks = _checks([{"checks":job.get("verification",{}).get("checks",[])},*selected],discovered=job.get("verification",{}).get("checks",[]))
                except ValueError as error:
                    # Plans are advisory. Return an incompatible verification
                    # plan to the Architect with actual source evidence instead
                    # of making Continue replay an impossible check forever.
                    problem = str(error)
                    previous = job.get("plan_errors", [])
                    await store.save(job_id, state="blocked" if problem in previous else "planning",
                        resume_state="planning", event="verification_plan_invalid",
                        plan_errors=list(dict.fromkeys([*previous, problem])),
                        plan_feedback={"error":problem,"previous_milestones":job["milestones"],
                            "instruction":"Inspect the current project and correct its verification plan. Preserve the original requested behavior. Use browser flows only for requested browser behavior; do not add a web interface to a library or command-line project."},
                        blocker=f"The verification plan does not match the project: {problem}. Continue to replan from this checkpoint." if problem in previous else "")
                    continue
                result, job = await _operate(job, "check", checks=checks)
                await store.record_check(job_id, job["worker_operation"], job["revision_id"], result)
                if result.get("environment_fault"):
                    await store.save(job_id,state="blocked",resume_state="checking",blocker="Verification environment is unavailable; inspect dependency setup and command logs",checks=result["checks"])
                    continue
                if not result["passed"]:
                    failures = {check["id"] for check in result["checks"] if not check["passed"]}
                    previous_failures = set(job.get("failed_check_ids",[]))
                    improved = bool(previous_failures) and failures < previous_failures
                    attempts = 0 if improved else job.get("attempt", 0) + 1
                    issues = [c for c in result["checks"] if not c["passed"]]
                    if attempts >= 3:
                        await store.save(job_id, state="blocked", resume_state="coding", blocker="Milestone still fails after verification and repair attempts", checks=result["checks"], issues=issues)
                    else:
                        # The third attempt receives an explicit diagnosis/replan
                        # request; research remains optional and evidence-driven.
                        if attempts == 2:
                            issues.append({"summary": "Reassess the cause and implementation approach before the final repair attempt."})
                        await store.save(job_id, state="coding", attempt=attempts, checks=result["checks"], issues=issues, failed_check_ids=sorted(failures))
                elif job["milestone_index"] + 1 < len(job["milestones"]):
                    await store.save(job_id, state="coding", milestone_index=job["milestone_index"] + 1, attempt=0, checks=result["checks"], issues=[])
                else:
                    # A finish-tool call is a model claim, not verification.
                    # On the final milestone the independent Acceptance pass
                    # can judge the tested revision even when the SDK used up
                    # its attempt turns before emitting that specific call.
                    await store.save(job_id, state="accepting", checks=result["checks"], issues=[],
                                     completion_basis="builder_finish" if job.get("builder_finished") else "independent_acceptance_required")
            elif state == "accepting":
                result, job = await _operate(job, "accept", evidence={"milestones": job["milestones"], "checks": job["checks"], "revision_id": job["revision_id"]})
                if result.get("accepted") is True and all(check["passed"] for check in job["checks"]):
                    await store.save(job_id, state="packaging", acceptance=result)
                else:
                    attempts = job.get("acceptance_attempt",0) + 1
                    await store.save(job_id, state="blocked" if attempts >= 3 else "coding", resume_state="coding", acceptance=result,
                                     acceptance_attempt=attempts, issues=result.get("issues", []),
                                     blocker=(result.get("summary") or "Acceptance found unmet requirements") if attempts >= 3 else "")
            elif state == "packaging":
                result, job = await _operate(job, "package")
                artifact = await _deliver(job, result)
                if _EVENTS:
                    await _EVENTS.emit(job["conversation_id"], "file_ready", {**artifact, "workflow_id": job_id})
                return
            else:
                raise ValueError(f"Unknown workflow state: {state}")
    except asyncio.CancelledError:
        raise
    except InterruptedError:
        # Keep the writer lease until the old worker acknowledges Stop. A
        # queued job must not overtake an operation whose cancellation is
        # still pending across a network interruption.
        while await store.get(job_id):
            if (await cancel(job_id))["state"] in store.TERMINAL:
                break
            await asyncio.sleep(2)
    except Exception as error:
        job = await store.get(job_id)
        if job and job["state"] not in store.TERMINAL and not job.get("cancel_requested"):
            await store.save(job_id, state="blocked", resume_state=job["state"], blocker=f"{type(error).__name__}: {error}")
