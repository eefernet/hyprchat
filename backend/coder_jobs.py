"""One owner for Daedalus job lifecycle, budgets, verification, and delivery."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
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
from coder_verification import digest
POLICY_VERSION = 7
ENGINE = "openhands159"
RETIRED_ENGINE = "This coding engine has been retired. Download retained source and start a new OpenHands 1.59 project."


def require_modern(job):
    if job.get("engine") != ENGINE:
        raise ValueError(RETIRED_ENGINE)


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


def worker_base(job):
    if job.get('engine') == 'openhands159':
        value = os.environ.get('DAEDALUS_ENGINE_URL', '').strip()
        if not value:
            raise ValueError('The isolated OpenHands 1.59 worker is not configured')
        return value.rstrip('/')
    raise ValueError(RETIRED_ENGINE)


def worker_url(job, operation=""):
    base = worker_base(job) + f"/jobs/{job['id']}/operations/"
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
        token = db.set_current_user_id(row['user_id'])
        try:
            job = await store.get(row['id'])
            if job and job.get('engine') != ENGINE:
                await store.save(row['id'], state='blocked', blocker=RETIRED_ENGINE,
                    failure={'category':'engine_retired','stage':job['state']})
                continue
            spawn(row['id'], row['user_id'])
        finally:
            db.reset_current_user_id(token)


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


async def create(conversation_id, task, mode="build_from_prompt", project_id="", model="", key="", *, visual_review=None, visual_model=None, execution_commands=None, protected_files=None, source_pointer="", engine="", ui_checks=None, original_request=None, chat_request_key=None, origin_message_id=None):
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
    source_revision = ""
    if project_id:
        project = await db.get_coding_project(project_id)
        if not project:
            raise LookupError("Project not found")
        source, source_pointer, source_revision = await resolve_source(project, source_pointer)
        from hephaestus_scope import retired_project
        if not source and retired_project(project):
            raise ValueError("This project workspace was discarded when Codebox retired. Upload its source or start from a retained artifact.")
    elif mode != "build_from_prompt":
        raise ValueError("Select a registered project")
    from coder_ui_checks import validate as validate_ui_checks
    ui_checks = validate_ui_checks(ui_checks if ui_checks is not None else (source or {}).get('ui_checks', []))
    if engine and engine != ENGINE:
        raise ValueError(RETIRED_ENGINE)
    if source and source.get('engine') != ENGINE:
        raise ValueError(RETIRED_ENGINE)
    engine = ENGINE
    if engine == 'openhands159':
        worker_base({'engine': engine})
        # Explicit follow-up flows add coverage; they cannot silently remove
        # controller-owned regression interactions inherited from the source.
        ui_checks = validate_ui_checks(list({json.dumps(flow, sort_keys=True): flow
            for flow in [*(source or {}).get('ui_checks', []), *ui_checks]}.values()))
    policy = POLICY_VERSION
    if policy >= 7 and source:
        protected_files = inherited_protection(source, protected_files)
    if source and not execution_commands.get('packages'):
        # A follow-up edit runs the same project: the test command given with the upload still applies.
        # Live run 2026-09-22: without it, tests/check_ledger.py (not a test_*.py name) was never executed, both
        # outcomes demanded test evidence that could not exist, and a correct edit parked as "Build incomplete".
        execution_commands = validate_commands(source.get('execution_commands') or {})
    conversation = await db.get_conversation(conversation_id)
    chosen_model = model or (config.QA_MODEL if mode == "ask_uploaded_project" else "") or config.CODER_MODEL or (conversation or {}).get("model")
    if not chosen_model or chosen_model.startswith(("openai:", "anthropic:", "custom:")) or chosen_model.endswith(("-cloud", ":cloud")):
        raise ValueError("Select an installed local coding model in Settings")
    # The queue consumer may see this row as soon as it commits. Persist the
    # complete source identity with the job so recovery cannot dispatch a
    # partly initialized project after its creating request disconnects.
    contract_options = {}
    if engine == 'openhands159':
        from coder_engine_contract import freeze as freeze_deliverables
        literal_request = task if original_request is None else original_request
        from coder_ui_contract import VERSION, freeze, recipes, original_source
        contract_source = await original_source(source, store.get)
        contract_options = {'deliverable_contract_version': 1, 'deliverable_contract': freeze_deliverables(literal_request, (source or {}).get('deliverable_contract')), 'original_user_request': literal_request, 'plan_finalization_version': 1, 'ui_preflight_version': 1, 'engine_behavior_version': 2, 'ui_contract_version': VERSION,
            'ui_contract': freeze(literal_request, contract_source, mode=mode, source_revision=source_revision),
            'ui_contract_context': recipes((source or {}).get('checks', [])) or (source or {}).get('ui_contract_context', [])}
    if chat_request_key is not None:
        contract_options.update(chat_request_key=chat_request_key, origin_message_id=origin_message_id)
    job = await store.create(conversation_id, task, mode, project_id, chosen_model, key or uuid.uuid4().hex,
        source_job_id=source["id"] if source else "", source_revision_id=source_revision if source else "",
        source_project_id=(project.get("openhands_project_id") or project_id) if project and not source else "",
        policy_version=policy, engine=engine, source_pointer=source_pointer if source else ("upload" if project else ""),
        visual_policy={"enabled":settings["daedalus_visual_review"] if visual_review is None else visual_review,
                       "model":selected_visual,"model_inherited":visual_model is None},
        request_options={"visual_review":visual_review,"visual_model":visual_model,
                         "execution_commands":execution_commands,"protected_files":protected_files,
                         **({'ui_checks': ui_checks} if ui_checks else {}), **contract_options},
        parent_artifact_id=((source or {}).get("artifact") or {}).get("id", ""),
        # Only a policy-7 parent's outcomes carry policy-7 evidence kinds.
        inherited=((source or {}).get('brief', {}).get('outcomes', []) if (source or {}).get('policy_version', 1) >= 7 else []) if policy >= 7 else None)
    spawn(job["id"])
    return job


async def resolve_source(project, requested=""):
    """(source job, pointer, revision) a new job on this project starts from.

    Follow-ups start from the working version (the latest delivered build, review candidates included) so
    "build X, now add Y" works before anything is accepted; ``requested="accepted"`` starts from the accepted
    version instead. Uploaded projects have neither pointer and seed from the upload (no source job).

    An accepted version whose job was deleted with its conversation is reported, never silently replaced by the
    raw upload: an edit seeded from the upload would drop the accepted fix the owner chose. The accepted archive
    stays pinned and downloadable, so the message says to upload it.
    """
    order = ("accepted",) if requested == "accepted" else ("working", "accepted")
    for pointer in order:
        job_id = project.get(pointer + "_job_id") or (project.get("openhands_project_id") if pointer == "accepted" else "") or ""
        revision = project.get(pointer + "_revision_id") or ""
        if not job_id.startswith("cw3-"):
            continue
        source = await store.get(job_id)
        if not source:
            continue
        if pointer == "accepted" and not revision:
            if not (source.get("acceptance") or {}).get("accepted"):
                continue
            revision = source["revision_id"]
        if revision:
            return source, pointer, revision
    upload = project.get("openhands_project_id") or ""
    upload = "" if upload.startswith("cw3-") else upload
    from db.coder_promotion import accepted_job_deleted
    if await accepted_job_deleted(project["id"]):
        raise ValueError("The job that produced the accepted version was deleted with its conversation; "
                         "upload the accepted archive to continue from it")
    if requested == "accepted":
        raise ValueError("This project has no accepted version yet; edit its latest build instead")
    if upload:
        return None, "upload", ""
    raise ValueError("This project has no delivered build yet; continue its existing job")


async def cancel(job_id):
    job = await store.get(job_id)
    if not job:
        raise LookupError("Workflow not found")
    require_modern(job)
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
                job = await store.account_operation(job_id, job['worker_operation'], job.get('operation_kind', operation.get('kind', '')), operation)
                checkpoint = operation.get("result",{}).get("snapshot")
                if checkpoint:
                    await store.record_revision(job_id,checkpoint)
                attempt = await db.get_run(job["worker_operation"])
                if attempt and attempt.get("status") in ("queued","running","pending"):
                    await db.update_run(job["worker_operation"],status="cancelled",result_envelope=operation.get("result",{}),ended=True)
                return await store.save(job_id, state="cancelled", event="cancelled", **({"revision_id":checkpoint["revision"]} if checkpoint else {}))
        except Exception:
            latest = await store.get(job_id)
            if latest and latest['state'] in store.TERMINAL:
                return latest  # A simultaneous Stop or publication already committed.
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


async def resume(job_id, *, visual_review=None, visual_model=None, candidate_revision=None, clarification=None, budget_mode=None):
    job = await store.get(job_id)
    if not job:
        raise LookupError("Workflow not found")
    require_modern(job)
    if job.get('worker_workspace_available') is False:
        raise ValueError('This worker workspace was discarded during the Hephaestus migration. Download the retained artifact and start a new job to continue it.')
    modern = job.get('engine') == 'openhands159'
    budget_mode = budget_mode or ('preserve' if modern else 'new_allowance')
    if budget_mode not in {'preserve', 'new_allowance'}:
        raise ValueError('budget_mode must be preserve or new_allowance')
    from coder_check_evidence import DURABLE_LIMITS, EVIDENCE_VERSION
    if job.get('policy_version', 1) >= 7 and job.get('stop_limit') in DURABLE_LIMITS:
        raise ValueError(job.get('blocker') or 'Retained repair limits prevent further progress on this request')
    from coder_presentation import nothing_to_continue, NOTHING_TO_CONTINUE
    if nothing_to_continue(job):
        raise ValueError(NOTHING_TO_CONTINUE)
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
    previous = _RUNNERS.get(job_id)
    if previous and not previous.done():
        # Worker acknowledgement can arrive before the old controller handles
        # its cancelled result. Keep the terminal state until that owner exits,
        # otherwise its exception handler can overwrite the resumed job.
        _, pending = await asyncio.wait({previous}, timeout=30)
        if pending:
            raise ValueError('The previous controller is still stopping. Retry Continue shortly.')
        job = await store.get(job_id)
    changes = {}
    if clarification is not None and (not isinstance(clarification, str) or not clarification.strip() or len(clarification) > 10000):
        raise ValueError('Clarification must contain between 1 and 10000 characters')
    if job.get('scope_question') and not clarification:
        raise ValueError('Answer the scope question before continuing')
    if clarification:
        if job.get('policy_version', 1) < 7 or not job.get('scope_question'):
            raise ValueError('This workflow is not waiting for a scope clarification')
        changes.update(scope_clarifications=[*job.get('scope_clarifications', []), {'text': clarification.strip(), 'at': store.now()}],
                       resume_state='coding', scope_question='')
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
            # Policy 7 captures on managed services during checks. Enabling vision
            # after a skipped capture must recreate the current revision's evidence.
            changes["resume_state"] = "checking" if job.get('policy_version', 1) >= 7 else "visual_review"
    if job.get('policy_version', 1) >= 6:
        changes.update(candidate_revision=candidate_revision or '', delivery_status='building', acceptance=None, review=None, stop_limit='')
        if job.get('resume_state') == 'packaging' and 'visual_policy' not in changes:
            # Only publication failed (disk reserve, checksum, worker restart). The verdict still stands:
            # _deliver and finish_artifact re-check it against the inspected revision before publishing.
            changes.pop('acceptance'); changes.pop('review')
        if job.get('candidate_artifact'):
            changes.update(candidate_history=[*job.get('candidate_history', []), job['candidate_artifact']], candidate_artifact=None)
    new_allowance = budget_mode == 'new_allowance'
    if modern and job.get('engine_behavior_version', 0) >= 1:
        changes.update(recovery_failure=None, failed_checkpoint=None)
    budget = dict(allowance=job.get('allowance', 1) + int(new_allowance),
                  calls_used=0 if new_allowance else job.get('calls_used', 0),
                  seconds_used=0 if new_allowance else job.get('seconds_used', 0),
                  previous_allowances_calls=job.get('previous_allowances_calls', 0) + (job.get('calls_used', 0) if new_allowance else 0),
                  previous_allowances_seconds=job.get('previous_allowances_seconds', 0) + (job.get('seconds_used', 0) if new_allowance else 0),
                  last_resume_budget_mode=budget_mode)
    job = await store.save(job_id, state="queued", event="resume", **budget,
                           worker_operation="", operation_key="", blocker="", attempt=job.get('attempt', 0) if modern else 0, acceptance_attempt=job.get('acceptance_attempt', 0) if modern else 0,
                           plan_errors=[], visual_attempt=0, probe_corrections=0, probe_diagnoses=[], failure=None,
                           resume_after_inspect=("" if job.get("resume_state") in {"queued", "inspecting"} else changes.get("resume_state", job.get("resume_state", "coding"))), **changes)
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
    return True


async def route_tool(name, args, conversation_id, events):
    """Route project work to modern jobs; never turn an inspection into editing."""
    relevant = {"start_coder_workflow", "plan_project", "generate_code", "ask_project",
                "get_coder_workflow", "cancel_coder_workflow", "run_review", "run_acceptance_review", "run_fixer",
                "download_project", "download_file", "write_file", "run_shell", "execute_code"}
    if not conversation_id or name not in relevant:
        return None
    latest = await db.get_latest_coder_workflow(conversation_id)
    job = await store.get(latest["id"]) if latest and latest.get("workflow_version") == 3 else None
    enabled = True
    starts = {"start_coder_workflow", "plan_project", "generate_code", "ask_project"}
    if not job and (not enabled or name not in starts):
        return None
    if name in {'run_aider_fix', 'run_review', 'run_acceptance_review', 'run_fixer'}:
        return json.dumps({'error': 'Retired workflow tool. Use start_coder_workflow; its controller owns checks and repairs.'})
    if job and job.get('engine') != ENGINE and name in starts:
        return json.dumps({'error': RETIRED_ENGINE, 'workflow_id': job['id']})
    if name in starts:
        conv = await db.get_conversation(conversation_id)
        options = REQUEST_OPTIONS.get()
        request_id = options.get('user_message_id')
        latest_user = next((m for m in reversed((conv or {}).get("messages", [])) if m.get("role") == "user"
                            and (request_id is None or str(m.get('id')) == str(request_id))), {})
        if request_id is not None and not latest_user:
            return json.dumps({'error': 'The original user message is unavailable; no job was started.'})
        task = args.get("task") or args.get("question") or latest_user.get("content") or ""
        key = str(latest_user.get("id") or hashlib.sha256(task.encode()).hexdigest())
        existing = await store.get(store.request_identity(conversation_id, key))
        if existing:
            return json.dumps({"workflow_id": existing["id"], "state": existing["state"],
                               "message": "This user message already has a saved job. Reattach to it; do not dispatch again."})
        if job and job["state"] in store.ACTIVE:
            return json.dumps({"workflow_id": job["id"], "state": job["state"], "request_started": False,
                               "message": "An earlier background job is still active. No new job was started for this message."})
        if not enabled:
            return None
        project = await db.get_coding_project_by_conv(conversation_id)
        target = options.get("project_id")
        project_id = target or args.get("project_id") or (project or {}).get("id", "")
        mode = ("ask_uploaded_project" if name == "ask_project" else
                args.get("mode") or ("edit_project" if project_id else "build_from_prompt"))
        if options.get("new_project") and name != "ask_project":
            mode = "build_from_prompt"
        if target and not options.get('new_project') and mode == 'build_from_prompt':
            return json.dumps({'error': 'A project is selected for this request. Use edit_project, or explicitly select New project.'})
        if mode == "build_from_prompt":
            project_id = ""
        origin = next((m for m in reversed((conv or {}).get('messages', [])) if m.get('role') == 'assistant'), {})
        job = await create(conversation_id, task, mode, project_id, key=key, original_request=latest_user.get('content') or '',
                           chat_request_key=key, origin_message_id=options.get('origin_message_id', origin.get('id')),
                           visual_review=options.get("visual_review", args.get("visual_review")),
                           visual_model=options.get("visual_model"),
                           source_pointer="accepted" if options.get("source_pointer") == "accepted" else "")
        if events: await events.emit(conversation_id, "tool_start", {"tool": name, "workflow_id": job["id"], "status": "Daedalus job started"})
        if events: await events.emit(conversation_id, "tool_end", {"tool": name, "workflow_id": job["id"], "status": "Running in background; progress reconnects after refresh"})
        return json.dumps({"workflow_id": job["id"], "state": job["state"], "message": "Job started in background. The job controller handles planning, coding, checks, and candidate delivery for explicit owner acceptance. Tell the user it has started; do not launch additional tools for this job."})
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




async def _operate(job, kind, *, reserve_calls=0, **extra):
    require_modern(job)
    if kind not in {'inspect', 'code', 'check', 'package', 'qa'}:
        raise ValueError('Retired worker operation: ' + str(kind))
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
    payload = {"kind": kind, "engine": job.get('engine', 'legacy'),
               'conversation_id':job.get('conversation_id',''), 'project_id':job.get('project_id',''),
               'progress_version': (job.get('progress') or {}).get('version', 0),
               'plan_finalization_version': job.get('plan_finalization_version', 0),
               'ui_preflight_version': job.get('ui_preflight_version', 0),
               'deliverable_contract': job.get('deliverable_contract'),
               'engine_behavior_version': job.get('engine_behavior_version', 0), 'model_digest': job.get('model_digest', ''),
               "request_key": operation_key, "task": job["user_task"], "mode": job.get("mode", ""),
               "original_task": job["user_task"], "source_project_id": job.get("source_project_id", "") if job.get("policy_version",1)>=2 else job.get("source_project_id") or job["project_id"],
               "source_job_id":job.get("source_job_id",""), "source_revision_id":job.get("source_revision_id",""),
               "model": job["model"], "ollama_url": config.OLLAMA_URL,
               "disable_thinking": config.OPENHANDS_DISABLE_THINKING,
               "policy_version":job.get("policy_version",1), "requirements":job.get("requirements",[]),
               "ui_required":job.get("ui_required",False), "visual_policy":job.get("visual_policy",{}),
               "visual_review":job.get("visual_review",{}), "verification_plan":job.get("verification_plan",{}),
               "settings": settings, "seconds_remaining": seconds, "calls_remaining": calls,
               "revision_id": job.get("revision_id", ""), **extra}
    payload["baseline_revision"] = job.get("baseline_revision","")
    if job.get('policy_version', 1) >= 7:
        from coder_scope import effective_task
        payload.update(task=effective_task(job), original_task=effective_task(job))
    stage_model = config.QA_MODEL if kind == 'qa' else ''
    if stage_model:
        if stage_model.startswith(("openai:","anthropic:","custom:")) or stage_model.endswith(("-cloud", ":cloud")):
            raise ValueError(f"Select a local {kind} model in Settings")
        payload["model"] = stage_model
    if kind == "inspect" and job.get("policy_version",1)>=2 and job.get("source_job_id"):
        source = await store.get(job["source_job_id"])
        from db.coder_promotion import candidate_for
        artifact = candidate_for(source or {}, job.get("source_revision_id", "")) or {}
        path = Path(artifact.get("storage_path", ""))
        expected = artifact.get("sha256", "")
        if not path.is_file() or len(expected) != 64:
            if (source or {}).get('worker_workspace_available') is False:
                raise ValueError('The retired worker workspace is unavailable and this revision has no retained artifact. Upload its source to start a new job.')
            if (source or {}).get('engine', 'legacy') != job.get('engine', 'legacy'):
                raise ValueError('An immutable source archive is required when moving a project between engines')
            if job.get("source_pointer") == "accepted" and not (source or {}).get("candidate_history") and not (source or {}).get("candidate_artifact"):
                raise ValueError("Accepted source archive is unavailable; restore the artifact before editing this project")
            # No local archive for this exact revision: the worker archives it from the source job's repository.
            expected = ""
        if expected:
            source_url = worker_base(job) + f"/jobs/{job['id']}/source/{expected}"
            from coder_source_upload import ensure_source
            await ensure_source(store, _HTTP, job['id'], source_url, path, expected, settings)
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
            identity = next((e['data'] for e in batch if e['type'] == 'model_identity'), None)
            if identity:
                await store.save(job['id'], model_digest=identity['digest'], model_identity=identity)
            context = next((e["data"]["context"] for e in reversed(batch) if e.get("data", {}).get("context")), current.get("context"))
            timeline = list(current.get("browser_timeline",[]))
            verification_events = list(current.get('verification_events',[]))
            for entry in batch:
                if entry["type"] == "browser_action":
                    timeline.append({"operation_id":job["worker_operation"],**entry["data"]})
                if entry['type'] in {'probe_diagnosis','probe_proposed','probe_rejected','response_format_recovery','raw_probe_review'}:
                    verification_events.append({'operation_id':job['worker_operation'], **entry})
            await store.save(job["id"], event="progress", worker_events=batch, worker_operation=job["worker_operation"],
                             worker_contact_at=store.now(),
                             context=context, operation_calls=operation.get("calls",0),
                             operation_seconds=max(0, time.time()-(operation.get("started") or time.time())),
                             activity=batch[-1].get("type"), browser_timeline=timeline,
                             verification_events=verification_events)
        if operation["status"] not in ("queued", "starting", "running", "cancelling"):
            if len(batch) >= 100:
                continue  # Drain persisted evidence before completing the operation.
            break
        preparation = settings.get('daedalus_preparation_seconds', 600) if job.get('engine') == 'openhands159' else 0
        if not deadline_exhausted and time.monotonic() - started > seconds + preparation:
            deadline_exhausted = True
            await store.save(job["id"], event="allowance_exhausted", blocker="Execution allowance exhausted; waiting for the worker to stop at its checkpoint")
            await _worker_request(job["id"], "POST", worker_url(job) + "/cancel", timeout=15)
            # Keep the writer lease through cancellation acknowledgement.
            # The normal result path records final usage and the checkpoint.
        await asyncio.sleep(2)
    result = operation["result"]
    await db.update_run(job["worker_operation"], status=operation["status"], result_envelope=result, ended=True)
    await store.account_operation(job['id'], job['worker_operation'], kind, operation)
    if result.get("snapshot"):
        await store.record_revision(job["id"], result["snapshot"])
    if operation["status"] != "succeeded" or deadline_exhausted:
        changes = {}
        if job.get('engine') == 'openhands159' and kind == 'code' and 'hook_denials' in result:
            changes['repair_round'] = max(job.get('repair_round', 0), min(2, result['hook_denials']))
            if job.get('engine_behavior_version', 0) >= 1 and changes['repair_round'] >= 2:
                changes['stop_limit'] = 'application_repairs'
        if result.get("snapshot"):
            changes["revision_id"] = result["snapshot"]["revision"]
            if job.get('engine_behavior_version', 0) >= 1 and kind == 'code':
                changes['inventory'] = result.get('inventory', job.get('inventory', {}))
                changes['failed_checkpoint'] = {'operation_id': job['worker_operation'],
                    'revision_id': result['snapshot']['revision'], 'inventory': result.get('inventory', {})}
        if result.get("checks"):
            changes["checks"] = result["checks"]
        if result.get('failure'):
            changes['failure']={**result['failure'], **({'checkpoint_error':result['checkpoint_error']} if result.get('checkpoint_error') else {})}
        if changes:
            await store.save(job["id"], **changes)
        if deadline_exhausted:
            raise TimeoutError("Execution allowance exhausted; worker stopped at its checkpoint")
        if job.get('policy_version', 1) >= 6 and kind in {'verify', 'accept'} and (
                (result.get('failure') or {}).get('category') in {'response_format', 'verification_response', 'invalid_probe'}
                or 'Inspection repeated identical evidence' in result.get('error', '')):
            raise ValueError(result.get('error') or 'Independent review could not resolve its checks')
        detail = result.get("error") or f"Worker operation {operation['status']}"
        if result.get('checkpoint_error'):
            detail += '. Checkpoint could not be saved: ' + result['checkpoint_error']
        raise RuntimeError(detail)
    return result, await store.get(job["id"])




async def _readable_name(job, archive, candidate):
    """Name the download after the project; the stored file, its URL and artifacts.filename stay as they are."""
    try:
        from coder_naming import base_for, download_name, title_of
        from db.coder_download_names import assign
        base, version = await assign(job.get("project_id") or "", job["id"], await asyncio.to_thread(base_for, job, archive))
        return {"download_name": download_name(base, "review" if candidate else "accepted", version),
                "download_base": base, "download_version": version, "project_title": title_of(base)}
    except Exception as error:
        # A name is a convenience. It must never cost a delivery.
        print(f"[CODER] readable download name unavailable for {job.get('id')}: {type(error).__name__}: {error}")
        return {}


async def _deliver(job, result, *, candidate=False):
    from coder_check_evidence import runnable
    accepted = job.get("acceptance") or {}
    if job.get('policy_version', 1) >= 7 and not candidate:
        summary = job.get('verification_summary') or {}
        if summary.get('evidence_version') != 4 or not summary.get('accepted') or summary.get('revision_id') != job['revision_id']:
            raise ValueError('Automatic completion requires confirmed acceptance examples that passed on this revision; recheck it')
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
    readable = await _readable_name(job, destination, candidate)
    try:
        publish = store.finish_candidate if candidate else store.finish_artifact
        artifact = await publish(job["id"], job["revision_id"], artifact_id=artifact_id,
            filename=filename, url=f"/api/downloads/{filename}", kind="archive", mime_type="application/gzip",
            **({"title": readable["download_name"]} if readable else {}),
            storage_path=str(destination), size_bytes=destination.stat().st_size, sha256=result["sha256"], exists_status="present", status="review_candidate" if candidate else "accepted",
            metadata={"revision_id": job["revision_id"], "acceptance": accepted, "workflow_version": 3, **readable,
                "policy_version":job.get("policy_version",1),"project_id":job["project_id"],
                "parent_artifact_id":job.get("parent_artifact_id",""),"source_revision_id":job.get("source_revision_id",""),
                "requirements":job.get("requirements",[]),"visual_review":job.get("visual_review",{}),
                **({'verification_summary':job.get('verification_summary',{}), 'run_instructions':job.get('verification',{}).get('profiles',[]),
                    'runnable':runnable(job) if job.get('policy_version', 1) >= 7 else any(c.get('execution_succeeded') for c in job.get('checks',[])), 'delivery_status':'review_candidate',
                    'checks_passed': bool((job.get('verification_summary') or {}).get('checks_passed')),
                    'exercised': bool((job.get('verification_summary') or {}).get('exercised'))} if candidate else ({'accepted_by': 'examples'} if job.get('policy_version', 1) >= 7 else {}))})
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
            if job.get('engine') == 'openhands159':
                import sys
                from coder_engine_controller import step
                if await step(job, sys.modules[__name__]):
                    return
                continue
            await store.save(job_id, state='blocked', blocker=RETIRED_ENGINE,
                failure={'category':'engine_retired','stage':state})
            return
    except asyncio.CancelledError:
        raise
    except InterruptedError:
        # Retain the writer lease until Stop is acknowledged.
        while await store.get(job_id):
            if (await cancel(job_id))["state"] in store.TERMINAL:
                break
            await asyncio.sleep(2)
    except Exception as error:
        job = await store.get(job_id)
        if job and job["state"] not in store.TERMINAL and not job.get("cancel_requested"):
            if job.get('engine') == 'openhands159' and job.get('engine_behavior_version', 0) >= 1:
                import sys
                from coder_engine_recovery import recover_candidate
                if await recover_candidate(job, sys.modules[__name__], error):
                    return
                job = await store.get(job_id)
            # Spent model calls are a bounded endpoint as well: packaging the checkpoint needs none.
            out_of_calls = (job.get('policy_version', 1) >= 7 and 'model-call allowance' in str(error).lower()
                            and job.get('revision_id') and job['state'] in {'coding', 'checking'})
            if job.get('engine') == 'openhands159' and out_of_calls:
                import sys
                from coder_engine_controller import step
                from coder_engine_ops import summarize
                summary = summarize(job.get('checks', []), job['revision_id'])
                summary.update(checks_passed=False, accepted=False, incomplete_reason=str(error))
                job = await store.save(job_id, state='candidate_packaging', verification_summary=summary,
                    acceptance=None, delivery_status='review_candidate', blocker=str(error),
                    stop_limit='model_calls', resume_state='checking')
                try:
                    await step(job, sys.modules[__name__])
                    return
                except InterruptedError:
                    raise
                except Exception as packaging_error:
                    error = packaging_error
                    job = await store.get(job_id)
                    if not job:
                        return
            from coder_failures import classify_failure
            await store.save(job_id, state="blocked", resume_state=job["state"], blocker=f"{type(error).__name__}: {error}",
                failure=job.get('failure') or classify_failure(error,job['state']))
