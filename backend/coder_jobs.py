"""One owner for Daedalus job lifecycle, budgets, verification, and delivery."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from pathlib import Path
import time
import uuid
from contextvars import ContextVar

import httpx

import config
import context_policy
import database as db
from db import coder_jobs as store
from coder_verification import digest, probe_checks
from coder_loop import POLICY_VERSION


_RUNNERS = {}
_OWNERS = {}
_CANCEL_FOLLOWUPS = set()
_CLOSING = False
_DELIVERY = asyncio.Lock()
_WRITER = asyncio.Lock()
_HTTP = None
_EVENTS = None
_RECONCILER = None
REQUEST_OPTIONS = ContextVar("daedalus_request_options", default={})


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


def inherited_protection(source, requested):
    """A follow-up edit inherits the files its parent's API caller protected, not the ones read from the
    parent's request text: "do not modify README.md" must not forbid a later "now update README.md"."""
    grounded = set((source or {}).get('grounded_protected_files', []))
    return list(dict.fromkeys([*[p for p in (source or {}).get('protected_files', []) if p not in grounded], *requested]))


async def create(conversation_id, task, mode="build_from_prompt", project_id="", model="", key="", *, visual_review=None, visual_model=None, execution_commands=None, protected_files=None):
    if not context_policy.runtime_settings()["daedalus_v3_enabled"]:
        raise ValueError("Enable the new Daedalus workflow in Settings first")
    mode = "edit_project" if mode == "fix_uploaded_project" else mode
    if mode not in ("build_from_prompt", "edit_project", "ask_uploaded_project"):
        raise ValueError("Unknown workflow mode")
    if not task.strip():
        raise ValueError("A task is required")
    source = None
    project = None
    if visual_review is not None and not isinstance(visual_review, bool):
        raise ValueError("Visual review must be true, false, or inherit")
    settings = context_policy.runtime_settings()
    selected_visual = settings["daedalus_visual_model"] if visual_model is None else visual_model
    from coder_profiles import validate_commands
    execution_commands = validate_commands(execution_commands or {})
    protected_files = protected_files or []
    if not isinstance(protected_files, list) or any(not isinstance(p, str) or not p or Path(p).is_absolute() or '..' in Path(p).parts for p in protected_files):
        raise ValueError('Protected files must be project-relative paths')
    context_policy.validate_patch({"daedalus_visual_model":selected_visual}, settings)
    if mode == "build_from_prompt" and project_id:
        raise ValueError("A new build cannot inherit an existing project; use edit_project for changes")
    if not project_id and mode != "build_from_prompt":
        active = await db.get_coding_project_by_conv(conversation_id)
        project_id = (active or {}).get("id", "")
    if project_id:
        project = await db.get_coding_project(project_id)
        if not project:
            raise LookupError("Project not found")
        source_id = project.get("accepted_job_id") or project.get("openhands_project_id") or ""   # a NULL column is None, not ""
        if source_id.startswith("cw3-"):
            source = await store.get(source_id)
            if not source or not (source.get("acceptance") or {}).get("accepted"):
                raise ValueError("Source project has no accepted revision")
        elif not source_id:
            raise ValueError("This project has no accepted revision yet; continue its existing job")
    elif mode != "build_from_prompt":
        raise ValueError("Select a registered project")
    # Policy 7 qualified on repairs and edits first; from-scratch builds keep the default policy
    # until they qualify too. Jobs keep the policy they were created with.
    policy = 7 if POLICY_VERSION >= 7 or (mode == "edit_project" and settings.get("daedalus_policy7_edits")) or (
        mode == "build_from_prompt" and settings.get("daedalus_policy7_builds")) else POLICY_VERSION
    if policy >= 7 and source:
        protected_files = inherited_protection(source, protected_files)
    if source and not execution_commands.get('packages'):
        # A follow-up edit runs the same project: the test command given with the upload still applies.
        # Live run 2026-09-22: without it, tests/check_ledger.py (not a test_*.py name) was never executed, both
        # outcomes demanded test evidence that could not exist, and a correct edit parked as "Build incomplete".
        execution_commands = validate_commands(source.get('execution_commands') or {})
    conversation = await db.get_conversation(conversation_id) if mode == "ask_uploaded_project" else None
    chosen_model = model or ((config.QA_MODEL or (conversation or {}).get("model")) if conversation is not None else None) or config.BUILDER_MODEL or config.CODER_MODEL
    if not chosen_model or chosen_model.startswith(("openai:", "anthropic:", "custom:")) or chosen_model.endswith(("-cloud", ":cloud")):
        raise ValueError("Select an installed local coding model in Settings")
    # The queue consumer may see this row as soon as it commits. Persist the
    # complete source identity with the job so recovery cannot dispatch a
    # partly initialized project after its creating request disconnects.
    job = await store.create(conversation_id, task, mode, project_id, chosen_model, key or uuid.uuid4().hex,
        source_job_id=source["id"] if source else "", source_revision_id=source["revision_id"] if source else "",
        source_project_id=(project.get("openhands_project_id") or project_id) if project and not source else "",
        policy_version=policy,
        visual_policy={"enabled":settings["daedalus_visual_review"] if visual_review is None else visual_review,
                       "model":selected_visual,"model_inherited":visual_model is None},
        request_options={"visual_review":visual_review,"visual_model":visual_model,
                         "execution_commands":execution_commands,"protected_files":protected_files},
        parent_artifact_id=((source or {}).get("artifact") or {}).get("id", ""),
        # Only a policy-7 parent's outcomes carry policy-7 evidence kinds.
        inherited=((source or {}).get('brief', {}).get('outcomes', []) if (source or {}).get('policy_version', 1) >= 7 else []) if policy >= 7 else None)
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


async def resume(job_id, *, visual_review=None, visual_model=None, candidate_revision=None):
    job = await store.get(job_id)
    if not job:
        raise LookupError("Workflow not found")
    from coder_policy7_evidence import DURABLE_LIMITS
    if job.get('policy_version', 1) >= 7 and job.get('stop_limit') in DURABLE_LIMITS:
        raise ValueError(job.get('blocker') or 'Retained repair limits prevent further progress on this request')
    if job["state"] not in (("blocked", "waiting_for_input", "ready_for_review", "cancelled") if job.get('policy_version', 1) >= 7 else ("blocked", "waiting_for_input", "ready_for_review")):
        raise ValueError("Only a blocked workflow can be continued")
    if job.get('candidate_artifact'):
        expected = job['candidate_artifact'].get('metadata', {}).get('revision_id')
        if not candidate_revision or candidate_revision != expected or candidate_revision != job['revision_id']:
            raise ValueError('Continue this candidate requires its exact immutable revision')
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
    changes = {}
    if visual_review is not None or visual_model is not None:
        policy = dict(job.get("visual_policy") or {"enabled":False,"model":""})
        if visual_review is not None:
            if not isinstance(visual_review, bool):
                raise ValueError("Visual review must be a boolean")
            policy["enabled"] = visual_review
        if visual_model is not None:
            context_policy.validate_patch({"daedalus_visual_model":visual_model}, context_policy.runtime_settings())
            policy["model"] = visual_model
            policy["model_inherited"] = False
        changes.update(visual_policy=policy, visual_review={"status":"pending"}, acceptance=None)
        if job.get("resume_state") in {"accepting", "packaging", "visual_review"}:
            changes["resume_state"] = "visual_review"
    if job.get('policy_version', 1) >= 6:
        changes.update(candidate_revision=candidate_revision or '', delivery_status='building', acceptance=None, review=None, stop_limit='')
        if job.get('resume_state') == 'packaging' and 'visual_policy' not in changes:
            # Only publication failed (disk reserve, checksum, worker restart). The verdict still stands:
            # _deliver and finish_artifact re-check it against the inspected revision before publishing.
            changes.pop('acceptance'); changes.pop('review')
        if job.get('candidate_artifact'):
            changes.update(candidate_history=[*job.get('candidate_history', []), job['candidate_artifact']], candidate_artifact=None)
    job = await store.save(job_id, state="queued", event="resume", allowance=job.get("allowance", 1) + 1,
                           calls_used=0, seconds_used=0, worker_operation="", operation_key="", blocker="", attempt=0, acceptance_attempt=0,
                           plan_errors=[], visual_attempt=0, probe_corrections=0, probe_diagnoses=[], failure=None,
                           resume_after_inspect=("" if job.get("resume_state") in {"queued", "inspecting"} else changes.get("resume_state", job.get("resume_state", "planning"))), **changes)
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
            if not (method == "GET" and "/source/" in url and response.status_code == 404):
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
        options = REQUEST_OPTIONS.get()
        target = options.get("project_id")
        project_id = target or args.get("project_id") or (project or {}).get("id", "")
        mode = args.get("mode") or ("ask_uploaded_project" if name == "ask_project" else "edit_project" if project_id else "build_from_prompt")
        if options.get("new_project") and name != "ask_project":
            mode = "build_from_prompt"
        if mode == "build_from_prompt":
            project_id = ""
        job = await create(conversation_id, task, mode, project_id, key=key,
                           visual_review=options.get("visual_review", args.get("visual_review")),
                           visual_model=options.get("visual_model"))
        origin = next((m for m in reversed((conv or {}).get('messages', [])) if m.get('role') == 'assistant'), {})
        job = await store.save(job["id"], chat_request_key=key, origin_message_id=origin.get('id'))
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
                           **({'candidate_artifact':job.get('candidate_artifact'), 'delivery_status':job.get('delivery_status'),
                               'verification_summary':job.get('verification_summary')} if job.get('policy_version',1)>=6 else {}),
                           "message": "Checks and delivery are managed by this workflow. A review candidate requires review and does not replace the accepted version." if job.get('policy_version',1)>=6 else "Checks and delivery are managed by this workflow. Only its accepted artifact is deliverable."})
    if name in {"write_file", "run_shell", "execute_code"} and job["state"] in store.ACTIVE:
        return json.dumps({"error": "A coding job owns the project. Stop it before making independent edits.", "workflow_id": job["id"]})
    return None


def verification_reserve(settings):
    """Model calls a policy-7 build may not spend on coding: the audit and review still need them."""
    return max(20, settings["daedalus_model_calls"] // 4)


async def _operate(job, kind, *, reserve_calls=0, **extra):
    settings = context_policy.runtime_settings()
    seconds = settings["daedalus_job_seconds"] - job.get("seconds_used", 0)
    calls = settings["daedalus_model_calls"] - job.get("calls_used", 0) - reserve_calls
    if seconds <= 0:
        raise TimeoutError("Execution allowance exhausted. Continue from the saved checkpoint.")
    if calls <= 0 and kind in {"plan","verify","code","accept","qa","visual"}:   # every kind that calls a model
        raise TimeoutError(f"Model-call allowance exhausted ({job.get('calls_used', 0)} of {settings['daedalus_model_calls']} used). Continue from the saved checkpoint.")
    operation_key = hashlib.sha256(json.dumps({"kind": kind, "milestone": job.get("milestone_index"),
        "attempt": job.get("attempt", 0), "allowance": job.get("allowance", 1), "revision": job.get("revision_id"),
        "extra": extra}, sort_keys=True).encode()).hexdigest()
    if job.get("operation_key") != operation_key:
        seq = job.get("operation_sequence", 0) + 1
        operation_id = f"{job['id']}-{seq}"
        job = await store.save(job["id"], worker_operation=operation_id, operation_sequence=seq,
                               operation_key=operation_key, operation_kind=kind, operation_accounted=False, worker_event_cursor=0,
                               call_limit=settings["daedalus_model_calls"])
    await store.start_attempt(job, job["worker_operation"], kind)
    payload = {"kind": kind, "request_key": operation_key, "task": job["user_task"],
               "original_task": job["user_task"], "source_project_id": job.get("source_project_id", "") if job.get("policy_version",1)>=2 else job.get("source_project_id") or job["project_id"],
               "source_job_id":job.get("source_job_id",""), "source_revision_id":job.get("source_revision_id",""),
               "model": job["model"], "ollama_url": config.OLLAMA_URL,
               "disable_thinking": config.OPENHANDS_DISABLE_THINKING, "reasoning_effort": config.OPENHANDS_REASONING_EFFORT,
               "policy_version":job.get("policy_version",1), "requirements":job.get("requirements",[]),
               "ui_required":job.get("ui_required",False), "visual_policy":job.get("visual_policy",{}),
               "visual_review":job.get("visual_review",{}), "verification_plan":job.get("verification_plan",{}),
               "settings": settings, "seconds_remaining": seconds, "calls_remaining": calls,
               "revision_id": job.get("revision_id", ""), **extra}
    payload["baseline_revision"] = job.get("baseline_revision","")
    stage_model = {"plan": config.ARCHITECT_MODEL or config.PLANNING_MODEL,
                   "verify": config.REVIEWER_MODEL or config.ACCEPTANCE_MODEL or config.PLANNING_MODEL,
                   "accept": config.ACCEPTANCE_MODEL or config.PLANNING_MODEL,
                   "qa": config.QA_MODEL}.get(kind)
    if stage_model:
        if stage_model.startswith(("openai:","anthropic:","custom:")) or stage_model.endswith(("-cloud", ":cloud")):
            raise ValueError(f"Select a local {kind} model in Settings")
        payload["model"] = stage_model
    if kind == "inspect" and job.get("policy_version",1)>=2 and job.get("source_job_id"):
        source = await store.get(job["source_job_id"])
        artifact = (source or {}).get("artifact") or {}
        path = Path(artifact.get("storage_path", ""))
        expected = artifact.get("sha256", "")
        if not path.is_file() or len(expected) != 64:
            raise ValueError("Accepted source archive is unavailable; restore the artifact before editing this project")
        source_url = config.OPENHANDS_URL.rstrip("/") + f"/jobs/{job['id']}/source/{expected}"
        present = await _worker_request(job["id"], "GET", source_url, timeout=15)
        if present.status_code == 404:
            async def chunks():
                with path.open("rb") as source_file:
                    while chunk := source_file.read(1024 * 1024):
                        yield chunk
            # A failed upload blocks safely; Continue opens a fresh stream.
            uploaded = await _HTTP.put(source_url, content=chunks(),timeout=120,params={
                "upload_mb":settings["daedalus_upload_mb"],"extracted_mb":settings["daedalus_extracted_mb"],"free_mb":settings["daedalus_min_free_mb"]})
            uploaded.raise_for_status()
        else:
            present.raise_for_status()
        payload["source_archive_sha256"] = expected
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
            timeline = list(current.get("browser_timeline",[]))
            verification_events = list(current.get('verification_events',[]))
            for entry in batch:
                if entry["type"] == "browser_action":
                    timeline.append({"operation_id":job["worker_operation"],**entry["data"]})
                if entry['type'] in {'probe_diagnosis','probe_proposed','probe_rejected','response_format_recovery','raw_probe_review'}:
                    verification_events.append({'operation_id':job['worker_operation'], **entry})
            await store.save(job["id"], event="progress", worker_event_cursor=batch[-1]["seq"],
                             worker_contact_at=store.now(),
                             context=context, operation_calls=operation.get("calls",0),
                             operation_seconds=max(0, time.time()-(operation.get("started") or time.time())),
                             activity=batch[-1].get("type"), browser_timeline=timeline,
                             verification_events=verification_events)
        if operation["status"] not in ("queued", "starting", "running", "cancelling"):
            if len(batch) >= 100:
                continue  # Drain persisted evidence before completing the operation.
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
        # An operation cancelled while still queued has no start time.
        begun = operation.get("started") or operation.get("ended") or 0
        elapsed = max(0, (operation.get("ended") or begun) - begun)
        stage_usage = [r for r in current.get('stage_usage',[]) if r['operation_id']!=job['worker_operation']]
        stage_usage.append({'operation_id':job['worker_operation'],'stage':kind,'seconds':elapsed,
            'calls':operation.get('calls',0),'status':operation['status']})
        await store.save(job["id"], calls_used=current.get("calls_used", 0) + operation.get("calls", 0),
                         seconds_used=current.get("seconds_used", 0) + elapsed, operation_accounted=True,
                         last_operation_status=operation["status"], operation_calls=0, operation_seconds=0,
                         stage_usage=stage_usage)
    if result.get("snapshot"):
        await store.record_revision(job["id"], result["snapshot"])
    if operation["status"] != "succeeded" or deadline_exhausted:
        changes = {}
        if result.get("snapshot"):
            changes["revision_id"] = result["snapshot"]["revision"]
        if result.get("checks"):
            changes["checks"] = result["checks"]
        if result.get('failure'):
            changes['failure']=result['failure']
        if changes:
            await store.save(job["id"], **changes)
        if deadline_exhausted:
            raise TimeoutError("Execution allowance exhausted; worker stopped at its checkpoint")
        if job.get('policy_version', 1) >= 6 and kind in {'verify', 'accept'} and (
                (result.get('failure') or {}).get('category') in {'response_format', 'verification_response', 'invalid_probe'}
                or 'Inspection repeated identical evidence' in result.get('error', '')):
            raise ValueError(result.get('error') or 'Independent review could not resolve its checks')
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


async def _deliver(job, result, *, candidate=False):
    from coder_policy7_evidence import runnable
    accepted = job.get("acceptance") or {}
    if result['revision_id'] != job['revision_id'] or (not candidate and (not accepted.get('accepted') or accepted.get('revision_id') != job['revision_id'])):
        raise ValueError("Artifact revision does not match accepted revision")
    artifact_id = "artifact-" + hashlib.sha256(f"{job['id']}:{job['revision_id']}".encode()).hexdigest()[:24]
    filename = f"{job['id']}-{job['revision_id']}.tar.gz"
    if candidate:
        # Continue can review unchanged source with new execution evidence. Each
        # package operation owns an immutable report/archive; retrying that same
        # operation retains its identity without overwriting a prior candidate.
        publication = hashlib.sha256(f"candidate:{job['id']}:{job['revision_id']}:{job['worker_operation']}".encode()).hexdigest()[:24]
        artifact_id = 'artifact-' + publication
        filename = f'candidate-{publication}-{filename}'
    destination = Path(config.SANDBOX_OUTPUTS_DIR) / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".part")
    digest = hashlib.sha256()
    try:
        async with _HTTP.stream("GET", worker_url(job) + "/artifact", timeout=120) as response:
            response.raise_for_status()
            with partial.open("wb") as output:
                checked = 0.0
                async for chunk in response.aiter_bytes():
                    # One SQLite connection per ~64 KiB chunk was thousands per artifact; Stop is polled once a second.
                    if time.monotonic() - checked >= 1:
                        checked = time.monotonic()
                        latest = await store.get(job["id"])
                        if not latest or latest.get("cancel_requested"):
                            raise InterruptedError("Cancellation requested")
                    if job.get("policy_version",1)>=2 and shutil.disk_usage(destination.parent).free < len(chunk)+context_policy.runtime_settings()["daedalus_min_free_mb"]*1024*1024:
                        raise ValueError("Artifact storage is below the configured free-space reserve; free space and continue")
                    output.write(chunk)
                    digest.update(chunk)
        if digest.hexdigest() != result["sha256"]:
            raise ValueError("Artifact checksum mismatch")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    try:
        publish = store.finish_candidate if candidate else store.finish_artifact
        artifact = await publish(job["id"], job["revision_id"], artifact_id=artifact_id,
            filename=filename, url=f"/api/downloads/{filename}", kind="archive", mime_type="application/gzip",
            storage_path=str(destination), size_bytes=destination.stat().st_size, sha256=result["sha256"], exists_status="present", status="review_candidate" if candidate else "accepted",
            metadata={"revision_id": job["revision_id"], "acceptance": accepted, "workflow_version": 3,
                "policy_version":job.get("policy_version",1),"project_id":job["project_id"],
                "parent_artifact_id":job.get("parent_artifact_id",""),"source_revision_id":job.get("source_revision_id",""),
                "requirements":job.get("requirements",[]),"visual_review":job.get("visual_review",{}),
                **({'verification_summary':job.get('verification_summary',{}), 'run_instructions':job.get('verification',{}).get('profiles',[]),
                    'runnable':runnable(job) if job.get('policy_version', 1) >= 7 else any(c.get('execution_succeeded') for c in job.get('checks',[])), 'delivery_status':'review_candidate'} if candidate else {})})
        if not artifact:
            raise RuntimeError("Artifact registration failed")
    except BaseException:
        # Cancellation may arrive immediately after SQLite committed. Preserve
        # an already-published file, and keep it on an ambiguous DB failure.
        try:
            latest = await asyncio.shield(store.get(job["id"]))
        except BaseException:
            latest = None
        if latest and (latest.get("candidate_artifact" if candidate else "artifact") or {}).get("id") != artifact_id:
            destination.unlink(missing_ok=True)
        raise
    return artifact


async def run(job_id):
    try:
        while True:
            job = await store.get(job_id)
            if not job or job["state"] in store.TERMINAL or job["state"] in ("blocked", "waiting_for_input", "ready_for_review"):
                return
            if job.get("cancel_requested"):
                cancelled = await cancel(job_id)
                if cancelled["state"] != "cancelled":
                    await asyncio.sleep(2)
                    continue
                return
            state = job["state"]
            if job.get('policy_version', 1) >= 6:
                import sys
                if job.get('policy_version', 1) >= 7:
                    from coder_policy7_controller import step
                else:
                    from coder_loop import step
                if await step(job, sys.modules[__name__]):
                    return
                continue
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
                await store.save(job_id, state="verifying" if job.get("policy_version",1)>=2 else "coding", event="plan_returned",
                    milestones=result["milestones"], requirements=result.get("requirements",[]), ui_required=result.get("ui_required",False),
                    milestone_index=0, attempt=0, blocker="")
                await asyncio.sleep(0)
            elif state == "verifying":
                result, job = await _operate(job, "verify", evidence={"requirements":job["requirements"], "milestones":job["milestones"]})
                await store.save(job_id,state="coding",verification_plan=result, verification_hash=digest(result))
            elif state == "coding":
                milestone = job["milestones"][job["milestone_index"]]
                result, job = await _operate(job, "code", task=milestone["task"], milestone=milestone,
                    milestone_id=str(job["milestone_index"]), evidence={"checks": job.get("checks", []), "issues": job.get("issues", []),
                        **({'verification_audits':job.get('probe_audits',[])[-2:]} if job.get('policy_version',1)>=4 else {})})
                await store.save(job_id, state="checking", revision_id=result["snapshot"]["revision"],
                                 inventory=result["inventory"], verification=result.get("verification",{}), acceptance=None,
                                 visual_review={"status":"pending"}, builder_finished=result["agent_finished"], builder_note=result.get("incomplete_reason", ""))
            elif state == "checking":
                # Re-run the accumulated milestone checks to catch regressions.
                selected = job["milestones"][:job["milestone_index"] + 1]
                try:
                    checks = _checks([{"checks":job.get("verification",{}).get("checks",[])},*selected],discovered=job.get("verification",{}).get("checks",[]))
                    if job.get("policy_version",1)>=2:
                        if not job.get("ui_required"):
                            checks = [c for c in checks if c.get("kind")!="browser"]
                        covered = set().union(*(set(m["requirement_ids"]) for m in selected))
                        for check in probe_checks(job["verification_plan"]["checks"]):
                            if not set(check["requirement_ids"])<=covered: continue
                            if check.get("kind")=="project_tests":
                                tests=[c for c in job.get("verification",{}).get("checks",[]) if c.get("is_test")]
                                if tests:
                                    checks.extend({**c,"id":check["id"]+"-"+digest(c)[:12],"origin":"independent",
                                        **({'execution_alias':c['id'],'execution_origin':'project_tests'} if job.get('policy_version',1)>=5 else {}),
                                        "requirement_ids":check["requirement_ids"],"contract_hash":check["contract_hash"]} for c in tests)
                                else:
                                    checks.append({**check,"command":"printf 'Requested runnable project tests are missing. Add tests and a discoverable test command.\\n' >&2; exit 1"})
                                continue
                            if check.get("kind")=="browser":
                                if not job.get("ui_required"): raise ValueError("Browser probes require a UI task")
                                preview=next((c for c in job.get("verification",{}).get("checks",[]) if c.get("kind")=="browser" and c.get("cwd",".")==check.get("cwd",".")),None)
                                if not preview: raise ValueError("No preview server discovered for independent browser probe")
                                check={**check,"server_command":preview["server_command"]}
                            checks.append(check)
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
                    from coder_failures import classify_failure
                    await store.save(job_id,state="blocked",resume_state="checking",blocker="Verification environment is unavailable; inspect dependency setup and command logs",checks=result["checks"],
                        **({'failure':classify_failure('environment unavailable','checking')} if job.get('policy_version',1)>=4 else {}))
                    continue
                if not result["passed"]:
                    independent = [c for c in result["checks"] if not c["passed"] and c.get("origin")=="independent"]
                    # Diagnose executable evidence independently before asking the
                    # Builder to contort correct code around a faulty probe.
                    signature=digest({"checks":[c["id"] for c in independent],"plan":job.get("verification_hash")})
                    if job.get('policy_version',1)>=4:
                        from coder_probe_audit import audit_failures
                        job,recheck=await audit_failures(job,result,_operate,store.save)
                        if recheck: continue
                    elif independent and signature not in job.get("probe_diagnoses",[]):
                        job=await store.save(job_id,checks=result["checks"])
                        prior=job["verification_plan"]
                        diagnosis,job=await _operate(job,"verify",diagnose_probes=prior,
                            evidence={"requirements":job["requirements"],"previous_probes":prior,"failed_checks":independent})
                        revised=digest(diagnosis["checks"])!=digest(prior["checks"])
                        attempts=job.get("probe_corrections",0)+int(revised)
                        await store.save(job_id,state="blocked" if attempts>2 else "checking",resume_state="checking",
                            checks=result["checks"],verification_plan=diagnosis,verification_hash=digest(diagnosis),probe_corrections=attempts,
                            probe_diagnoses=[*job.get("probe_diagnoses",[]),signature,
                                digest({"checks":[c["id"] for c in independent],"plan":digest(diagnosis)})],
                            event="verification_diagnosed",blocker="Independent probes still require correction; inspect their recorded rationale before continuing" if attempts>2 else "")
                        if revised: continue
                    failures = {check["id"] for check in result["checks"] if not check["passed"]}
                    previous_failures = set(job.get("failed_check_ids",[]))
                    improved = bool(previous_failures) and failures < previous_failures
                    attempts = 0 if improved else job.get("attempt", 0) + 1
                    issues = [c for c in result["checks"] if not c["passed"]]
                    if attempts >= 3:
                        from coder_failures import classify_failure
                        await store.save(job_id, state="blocked", resume_state="coding", blocker="Milestone still fails after verification and repair attempts", checks=result["checks"], issues=issues,
                            **({'failure':classify_failure('test failed','checking',recovery_attempted=True)} if job.get('policy_version',1)>=4 else {}))
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
                    await store.save(job_id, state="visual_review" if job.get("policy_version",1)>=2 else "accepting", checks=result["checks"], issues=[],
                                     completion_basis="builder_finish" if job.get("builder_finished") else "independent_acceptance_required")
            elif state == "visual_review":
                result, job = await _operate(job,"visual",evidence={"checks":job["checks"]})
                if result.get("status")!="passed" and not result.get("defects") and any(r["kind"]=="visual" for r in job.get("requirements",[])):
                    await store.save(job_id,state="waiting_for_input",resume_state="visual_review",visual_review=result,
                        blocker="The request explicitly requires visual verification. Enable a supported local vision model and continue; this requirement is still unverified.")
                    continue
                defects = result.get("defects",[])
                attempts = job.get("visual_attempt",0) + bool(defects)
                await store.save(job_id,state=("blocked" if attempts>=3 else "coding") if defects else "accepting",
                    resume_state="coding",visual_review=result,visual_attempt=attempts,issues=defects,
                    blocker="Visual checks still find concrete layout defects after repairs" if attempts>=3 else "")
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
            # Spent model calls are a bounded endpoint as well: packaging the checkpoint needs none.
            out_of_calls = (job.get('policy_version', 1) >= 7 and 'model-call allowance' in str(error).lower()
                            and job.get('revision_id') and job['state'] in {'coding', 'checking'})
            if job.get('policy_version', 1) >= 6 and job.get('brief') and (out_of_calls or isinstance(error, ValueError) and job['state'] in {'reviewing', 'accepting', 'auditing'}
                    or job.get('policy_version', 1) >= 7 and 'model-call allowance' in str(error).lower() and job['state'] in {'reviewing', 'accepting', 'auditing', 'visual_review'}):
                # Invalid/ambiguous review output has a bounded endpoint too.
                import sys
                if job.get('policy_version', 1) >= 7:
                    from coder_policy7_controller import step
                    from coder_policy7_evidence import acceptance
                    summary = acceptance(job['brief']['outcomes'], job.get('checks', []), {}, job['revision_id'])
                    job = await store.save(job_id, state='candidate_packaging', verification_summary=summary,
                        acceptance=None, delivery_status='review_candidate', blocker=str(error),
                        stop_limit='model_calls' if 'model-call allowance' in str(error).lower() else job.get('stop_limit', ''),
                        resume_state='checking' if out_of_calls else 'accepting')
                else:
                    from coder_loop import candidate, step
                    job = await candidate(job, store, str(error))
                try:
                    await step(job, sys.modules[__name__])
                    return
                except InterruptedError:
                    raise
                except Exception as packaging_error:
                    # Every failure parks the job visibly. A narrower list let a failed worker package
                    # operation (RuntimeError) escape with the job still recoverable: a silent respawn loop.
                    error = packaging_error
                    job = await store.get(job_id)
                    if not job:
                        return
            from coder_failures import classify_failure
            await store.save(job_id, state="blocked", resume_state=job["state"], blocker=f"{type(error).__name__}: {error}",
                failure=job.get('failure') or classify_failure(error,job['state']))
