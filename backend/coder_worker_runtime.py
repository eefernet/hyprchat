"""Durable Hephaestus operations, isolated from HTTP connections and restarts."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import tempfile
import uuid
from fastapi import Request

from coder_repository import Repository, safe_relative, validate_links
from context_policy import DEFAULTS, resolve, estimate_tokens, operation_settings
from coder_verification import digest, source_context


class WorkerStore:
    def __init__(self, root, *, controller_owned=True):
        self.root = Path(root).resolve()
        from hephaestus_inventory import managed_root,read,control
        # SDK child bookkeeping is operation-private and cannot access the
        # controller's registry or admission locks through its read-only mount.
        managed=self.managed_root=managed_root(self.root) if controller_owned else None
        if managed and any(self.root.is_relative_to(managed/p) for p in read(control(managed)/'retired-stores.json',{'paths':[]})['paths']):
            raise ValueError('This test environment has been retired. Create a new environment to run more tests.')
        self.root.mkdir(parents=True, exist_ok=True)
        from hephaestus_inventory import register_store
        if managed:
            register_store(self.root)
        self.path = self.root / "operations.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                    pid INTEGER, process_started REAL, started REAL, ended REAL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                    operation_id TEXT, seq INTEGER, payload TEXT,
                    PRIMARY KEY(operation_id,seq));
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, operation_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            result["result"] = json.loads(result["result"])
            return result

    def event(self, operation_id, kind, **data):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO events VALUES(?,COALESCE((SELECT MAX(seq)+1 FROM events WHERE operation_id=?),1),?)",
                       (operation_id, operation_id, json.dumps({"type": kind, "data": data, "at": time.time()})))

    def events(self, operation_id, after=0, limit=100):
        with self.connect() as db:
            return [{"seq": row["seq"], **json.loads(row["payload"])} for row in db.execute(
                "SELECT seq,payload FROM events WHERE operation_id=? AND seq>? ORDER BY seq LIMIT ?", (operation_id,after,limit))]

    def has_event(self, operation_id, kind):
        """One boolean without decoding the ledger: a failed build used to load every agent event (tens of MB)."""
        with self.connect() as db:
            return db.execute("SELECT 1 FROM events WHERE operation_id=? AND json_extract(payload,'$.type')=? LIMIT 1",
                              (operation_id, kind)).fetchone() is not None

    def update(self, operation_id, **values):
        allowed = {"status", "result", "payload", "pid", "process_started", "started", "ended", "cancel_requested", "calls"}
        if set(values) - allowed:
            raise ValueError("Unknown operation update")
        with self.connect() as db:
            db.execute("UPDATE operations SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                       [json.dumps(value) if key in ("payload", "result") else value for key, value in values.items()] + [operation_id])

    def create(self, operation_id, job_id, kind, payload):
        from hephaestus_inventory import managed_root, read, control, lock
        root = self.managed_root
        if root:
            with lock(root, 'admission', exclusive=False, blocking=False):
                retired_stores=read(control(root)/'retired-stores.json',{'paths':[]})['paths']
                if any(self.root.is_relative_to(root/p) for p in retired_stores):
                    raise ValueError('This test environment has been retired and cannot accept new work.')
                retired=read(control(root) / 'retired-jobs.json', {'jobs': [],'projects': []})
                if job_id in retired['jobs'] or payload.get('project_id') in retired.get('projects',[]):
                    raise ValueError('This VM workspace was deliberately removed. Restore the saved project archive to continue.')
                return self._create(operation_id, job_id, kind, payload)
        return self._create(operation_id, job_id, kind, payload)

    def _create(self, operation_id, job_id, kind, payload):
        for value in (operation_id, job_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise ValueError("Invalid operation identity")
        if kind not in ("inspect", "code", "check", "package", "qa"):
            raise ValueError("Unknown operation kind")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if old:
                # A durable cancellation can arrive before the delayed start POST.
                if old['job_id'] == job_id and old['kind'] == 'cancel' and old['cancel_requested']:
                    return False
                if old["job_id"] != job_id or old["kind"] != kind or json.loads(old["payload"]).get("request_key") != payload.get("request_key"):
                    raise ValueError("Operation identity reused for another request")
                return False
            db.execute("INSERT INTO operations(id,job_id,kind,status,payload) VALUES(?,?,?,'queued',?)",
                       (operation_id, job_id, kind, json.dumps(payload)))
        return True


    def cancel_before_dispatch(self, operation_id, job_id):
        """Acknowledge an absent operation while fencing every later delivery of it."""
        for value in (operation_id, job_id):
            if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
                raise ValueError('Invalid operation identity')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT job_id FROM operations WHERE id=?', (operation_id,)).fetchone()
            if old and old['job_id'] != job_id:
                raise ValueError('Operation belongs to another job')
            if not old:
                db.execute("INSERT INTO operations(id,job_id,kind,status,payload,result,cancel_requested,ended) VALUES(?,?,'cancel','cancelled',?,?,1,?)",
                    (operation_id,job_id,json.dumps({'settings':DEFAULTS}),json.dumps({'error':'Cancelled before dispatch'}),time.time()))
        return self.get(operation_id)


def operation_repo(store, operation):
    root = store.root / "jobs" / operation["job_id"] / "workspace"
    repository = Repository(root, store.root / "jobs" / operation["job_id"] / "repository",
                      operation["payload"]["settings"].get("daedalus_exclude_dirs", DEFAULTS["daedalus_exclude_dirs"]))
    if operation['payload'].get('deliverable_contract') is not None:
        protected = list(operation['payload'].get('protected_files', []))
        protected += [row['path'] for row in operation['payload']['deliverable_contract']['criteria']
                      if Path(row['path']).suffix.lower() not in {'.jar', '.war', '.zip'}]
        from coder_environment_publish import atomic_json
        atomic_json(repository.state / 'protected-source.json', protected)
    return repository


def _alive(operation):
    if not operation.get("pid"):
        return False
    try:
        import psutil
        process = psutil.Process(operation["pid"])
        return abs(process.create_time() - operation["process_started"]) < 0.1 and process.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def launch(store, operation_id):
    """Claim the operation before spawning, so duplicate POSTs cannot race."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        claimed = db.execute("UPDATE operations SET status='starting',started=? WHERE id=? AND status='queued' AND cancel_requested=0",
                             (time.time(), operation_id)).rowcount
    if not claimed:
        return
    log_dir = store.root / "logs"
    log_dir.mkdir(exist_ok=True)
    try:
        with (log_dir / f"{operation_id}.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--execute", operation_id, "--state", str(store.root)],
                                       stdout=log, stderr=log, start_new_session=True)
        import psutil
        store.update(operation_id, pid=process.pid, process_started=psutil.Process(process.pid).create_time())
        # Reap children without tying execution to the request or keeping a
        # zombie after completion. The SQLite ledger owns results.
        threading.Thread(target=process.wait, daemon=True).start()
    except Exception as error:
        store.update(operation_id, status="failed", result={"error": str(error)}, ended=time.time())


def cancel(store, operation_id):
    operation = store.get(operation_id)
    if not operation:
        raise LookupError("Operation not found")
    if operation["status"] in ("succeeded", "failed", "cancelled", "blocked", "interrupted"):
        return operation
    store.update(operation_id, cancel_requested=1, status="cancelling")
    if _alive(operation):
        import psutil
        parent = psutil.Process(operation["pid"])
        children = parent.children(recursive=True)
        for process in reversed(children):
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        parent.terminate()
        _, alive = psutil.wait_procs([parent, *children], timeout=5)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=5)
        if alive:
            return store.get(operation_id)
    result = {"error": "Cancelled by user", "unverified_changes": operation["kind"] == "code"}
    try:
        repository = operation_repo(store, operation)
        result["snapshot"] = repository.snapshot("Cancelled operation", parent=operation["payload"].get("revision_id", ""))
    except (ValueError, RuntimeError):
        pass
    store.update(operation_id, status="cancelled", result=result, ended=time.time())
    return store.get(operation_id)


def reconcile(store, operation_id):
    operation = store.get(operation_id)
    if operation and operation["status"] in ("running", "starting", "cancelling"):
        # Allow the startup handshake to store its PID before calling it lost.
        if time.time() - (operation.get("started") or time.time()) > 10 and not _alive(operation):
            result = {"error":"Worker interrupted; inspect checkpoint before continuation", "unverified_changes":operation["kind"] == "code"}
            if operation['payload'].get('engine') == 'openhands159':
                from coder_engine_resources import failure
                result.update(failure(operation['payload'].get('_resource_cgroup')) or {})
                result['failure'] = {'category': result.get('failure_category', 'worker_interrupted'),
                                     'stage': operation['kind'], 'next_action': 'Inspect the retained checkpoint and resume with the existing allowance.'}
            with store.connect() as db:
                db.execute("UPDATE operations SET status='interrupted',ended=?,result=? WHERE id=? AND status IN ('running','starting','cancelling')",
                           (time.time(),json.dumps(result),operation_id))
            operation = store.get(operation_id)
    return operation


def _boundary(store, operation_id, *, model_call=False):
    operation = store.get(operation_id)
    if operation["cancel_requested"]:
        raise InterruptedError("Cancellation requested")
    payload = operation["payload"]
    if payload.get('_preparation_failure'):
        raise ValueError('Dependency environment preparation failed; project files are saved.')
    now = time.time()
    preparing = max(0, now - payload.get('_preparation_active_started', now))
    if payload.get('_preparation_deadline') and now >= payload['_preparation_deadline']:
        raise TimeoutError('Environment preparation allowance exhausted')
    if now - payload.get('_active_started', operation["started"]) - preparing >= payload["seconds_remaining"]:
        raise TimeoutError("Execution allowance exhausted; continue from the checkpoint")
    if payload.get('engine') == 'openhands159' and now - payload.get('_usage_checkpoint', {}).get('at', 0) >= 1:
        checkpoint = {'at': now, 'active_seconds': max(0, now - payload.get('_active_started', operation['started']) - preparing)}
        with store.connect() as connection:
            connection.execute("UPDATE operations SET payload=json_set(payload,'$._usage_checkpoint',json(?)) WHERE id=?",
                               (json.dumps(checkpoint), operation_id))
    if payload.get('engine') == 'openhands159' and now - getattr(store, '_storage_checked_at', 0) >= 30:
        store._storage_checked_at = now
        enforce_storage(store, payload)
    if model_call:
        with store.connect() as db:
            cursor = db.execute("UPDATE operations SET calls=calls+1 WHERE id=? AND calls<? AND cancel_requested=0",
                                (operation_id, payload["calls_remaining"]))
            if not cursor.rowcount:
                raise TimeoutError("Model-call allowance exhausted; continue from the checkpoint")
    return operation


def evidence_catalog(evidence, budget):
    """Page collections instead of overflowing context with accumulated logs."""
    collections = []
    def describe(value, path=""):
        if isinstance(value,list):
            page = {"items":[],"total":len(value),"next_cursor":0 if value else None,"evidence_path":path}
            collections.append((page,value))
            return page
        if isinstance(value,dict):
            return {key:describe(item,f"{path}.{key}" if path else key) for key,item in value.items()}
        return value
    result = describe(evidence)
    for page,values in collections:
        for index,value in enumerate(values):
            # Complete logs remain on disk and are accessible through log pages.
            item = {k:v for k,v in value.items() if k != "log_tail" or not value.get("passed") } if isinstance(value,dict) else value
            page["items"].append(item)
            if estimate_tokens(result)>budget:
                page["items"].pop()
                break
            page["next_cursor"] = index+1 if index+1<len(values) else None
    return result


def evidence_page(evidence, path, cursor=0, limit=100):
    if cursor<0 or limit<1:
        raise ValueError("Invalid evidence page")
    value=evidence
    for key in path.split(".") if path else []:
        if not isinstance(value,dict) or key not in value:
            raise ValueError("Unknown evidence collection")
        value=value[key]
    if not isinstance(value,list):
        return {"value":value}
    items=[{"cursor":index+1,"value":item} for index,item in enumerate(value[cursor:cursor+limit],cursor)]
    return {"items":items,"total":len(value),"next_cursor":cursor+len(items) if cursor+len(items)<len(value) else None}


def bounded_context(repository, task, policy, extra=None):
    """Repository context is navigable; a preview never claims full coverage."""
    context = {"task": task, "evidence": evidence_catalog(extra or {},policy.input_budget//2), "inventory": repository.inventory(limit=1)["stats"], "root_files": [],
               "files": [], "source": [], "instructions": "Inventory/source are paginated. Request more inspection when evidence is missing."}
    if estimate_tokens(context) > policy.input_budget:
        raise ValueError("Required task/evidence exceeds configured context; increase context in Settings or split the milestone")
    # Keep root manifests/docs/tests visible even when a large directory
    # occupies the first alphabetical page. The normal file cursor remains
    # unchanged; this is an overview, not a claim to list the whole project.
    with repository.connect() as db:
        for row in db.execute("SELECT path,package,lines,sha256 FROM files WHERE instr(path,'/')=0 ORDER BY path"):
            context["root_files"].append(dict(row))
            if estimate_tokens(context) > policy.compact_at:
                context["root_files"].pop()
                break
    page = repository.inventory(limit=100)
    for row in page["items"]:
        context["files"].append({"path": row["path"], "package": row["package"], "lines": row["lines"], "sha256": row["sha256"]})
        if estimate_tokens(context) > policy.compact_at:
            context["files"].pop()
            break
    context["files_next_cursor"] = context["files"][-1]["path"] if context["files"] else ""
    return context


def _json(text):
    """Accept ordinary text-model prose/fences around the structured result."""
    decoder = json.JSONDecoder()
    fallback = None
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if not isinstance(value,dict):
            continue
        if set(value) & {"inspect","milestones","accepted","answer"}:
            return value
        if fallback is None:
            fallback = value
    if fallback is not None:
        return fallback
    raise ValueError("Expected a JSON object in the model response")


def validate_qa(answer, repository=None):
    text = answer.get('answer')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Return a nonempty answer grounded in the inspected source')
    if repository is None:
        return
    references = re.findall(r"([\w./-]+):(?:L)?([1-9]\d*)", text)
    if not references:
        raise ValueError('Cite inspected source as relative/path:line; do not invent a source answer')
    for path, line in references:
        try:
            content = repository.read(path, start=int(line), limit=1).get('content')
        except (OSError, ValueError) as exc:
            raise ValueError('Invalid source citation: ' + path + ':' + line) from exc
        if not content:
            raise ValueError('Source citation does not identify a current source line: ' + path + ':' + line)


def _read_only(store, operation_id, repository, instruction, extra, validate=None, schema=None):
    """The model can navigate evidence, but receives no write/shell tools."""
    from coder_inference import local_chat, summarize_history
    operation = store.get(operation_id)
    role = "qa"
    task = operation["payload"]["task"]
    observations, checkpoint, corrections = [], "", 0
    inspections, repeats, invalid_repeats = [], {}, {}
    recovery = None
    if operation['payload'].get('policy_version', 1) >= 5:
        if schema is None:
            from coder_response_recovery import DEFINITIONS, JSON_VALUE
            schema = {'type':'object', 'additionalProperties':JSON_VALUE, '$defs':DEFINITIONS}
        from coder_response_recovery import Recovery, ResponseFormatError
        recovery = Recovery(store, operation_id, instruction, extra, schema)
        if 'answer' in recovery.state:
            if validate:
                validate(recovery.state['answer'])
            return recovery.state['answer']
        observations = recovery.state['observations']
        inspections = recovery.state['inspections']
        repeats = recovery.state['repeats']
        checkpoint = recovery.state['checkpoint']
    changed_paths=[]
    baseline=operation["payload"].get("baseline_revision","")
    revision=operation["payload"].get("revision_id","")
    if baseline and revision and baseline!=revision:
        if not all(re.fullmatch(r"[a-f0-9]{40,64}",v) for v in (baseline,revision)):
            raise ValueError("Invalid source revision")
        changed_paths=repository.git("diff","--name-only",baseline,revision).decode().splitlines()
        extra={**extra,"changed_paths":changed_paths}
        if operation['payload'].get('policy_version', 1) >= 6:
            diff_dir = store.root / 'checks' / operation['job_id']
            diff_dir.mkdir(parents=True, exist_ok=True)
            diff_path = diff_dir / f'{operation_id}-source-diff.log'
            diff_path.write_bytes(repository.git('diff', '--no-ext-diff', baseline, revision))
            extra['source_diff'] = {'log':str(diff_path), 'bytes':diff_path.stat().st_size,
                                    'instruction':'Read relevant ranges with the log inspector'}
    while True:
        operation = _boundary(store, operation_id)
        payload = operation["payload"]
        policy = resolve(role, operation_settings(payload))
        context = bounded_context(repository, task, policy, extra)
        if payload.get('policy_version',1)>=5 and extra.get('requirement'):
            subject = extra.get('original_check') or extra.get('candidate_probe') or {}
            paths = [subject['path']] if subject.get('runner')=='file' else [subject['target']['path']] if subject.get('target') else [b['path'] for b in subject.get('bindings',[])]
            context['source'] = source_context(repository, extra['requirement'].get('text',''), policy.input_budget//5, paths, include_root=False)
        elif payload.get("policy_version",1)>=2:
            context["source"] = source_context(repository,task,policy.input_budget//5,changed_paths)
        if observations and policy.compaction and estimate_tokens({**context, "observations": observations, "checkpoint": checkpoint}) > policy.compact_at:
            checkpoint = summarize_history(store, operation_id, {"checkpoint": checkpoint, "observations": observations})
            observations = []
            if recovery:
                recovery.state.update(observations=observations, checkpoint=checkpoint)
                recovery.save()
        context["observations"] = observations
        context["checkpoint"] = checkpoint
        prompt = instruction + "\nReturn one JSON object. To inspect more return {\"inspect\":{\"operation\":\"files|symbols|dependencies|search|read|bytes|log|evidence\",\"query\":\"...\",\"path\":\"...\",\"cursor\":\"...\",\"start\":1}}. Inspection is read-only. Files/symbols accept literal substrings or * and ? wildcards; files match the full relative path or basename. Search finds literal source text.\n" + json.dumps(context)
        if estimate_tokens(prompt) > policy.input_budget:
            raise ValueError("Inspection evidence exceeds context. Increase the window or narrow the milestone in Settings.")
        if recovery: recovery.before_call()
        try:
            raw = local_chat(store, operation_id, role, [{"role":"user", "content":prompt}], **({'schema':schema} if schema else {}))
        except ValueError as error:
            if recovery and isinstance(error, ResponseFormatError):
                recovery.reject(error, error.response, error.failure_category)
                continue
            if recovery: recovery.observed()
            raise
        try:
            answer = _json(raw)
        except (ValueError, TypeError):
            if recovery:
                recovery.reject('Return one valid JSON object matching the supplied schema', raw)
                continue
            corrections += 1
            store.event(operation_id, "invalid_response", response=raw)
            if payload.get('policy_version',1)>=3:
                signature=digest({'invalid_json':raw})
                invalid_repeats[signature]=invalid_repeats.get(signature,0)+1
                if schema:
                    mode=payload.get('structured_mode','schema')
                    next_mode={'schema':'json','json':'text','text':'text'}[mode]
                    if next_mode!=mode:
                        store.update(operation_id,payload={**store.get(operation_id)['payload'],'structured_mode':next_mode})
                        store.event(operation_id,'structured_output_fallback',reason='Runtime returned invalid JSON despite requested format',mode=next_mode)
                if invalid_repeats[signature]>=3:
                    raise ValueError('Identical invalid JSON response repeated three times')
            if corrections >= payload["settings"]["daedalus_attempt_turns"]:
                raise ValueError("Model repeatedly returned invalid inspection JSON; change the selected model or continue")
            observations.append({"error": "Return one valid JSON object matching the requested schema; the previous response was invalid."})
            continue
        inspection = answer.get("inspect")
        if not inspection:
            if validate:
                try:
                    validate(answer)
                except (ValueError, KeyError, TypeError, SyntaxError) as error:
                    if recovery:
                        recovery.reject(error, answer)
                        continue
                    corrections += 1
                    store.event(operation_id,"invalid_plan",response=answer,error=str(error))
                    signature=digest({"response":answer,"error":str(error)})
                    invalid_repeats[signature]=invalid_repeats.get(signature,0)+1
                    if payload.get("policy_version",1)>=2 and invalid_repeats[signature]>=3:
                        raise ValueError(f"Identical invalid response repeated three times: {error}") from error
                    if corrections >= payload["settings"]["daedalus_attempt_turns"]:
                        raise ValueError(f"Plan validation failed: {error}") from error
                    observations.append({"invalid_response":answer,"error":str(error),"instruction":"Correct the response using the requested schema and observed evidence. Preserve the original requirements."})
                    continue
            if recovery: recovery.accepted(answer)
            return answer
        try:
            if not isinstance(inspection,dict):
                raise ValueError("Inspection must be an object")
            kind = inspection.get("operation")
            if kind == "evidence":
                result = evidence_page(extra,inspection.get("path",""),int(inspection.get("cursor") or 0),int(inspection.get("limit") or 100))
            elif kind == "log":
                result = read_check_log(store, operation, inspection["path"], int(inspection.get("offset") or 0), policy.input_budget)
            elif kind == "bytes":
                result = repository.read_bytes(inspection["path"], offset=int(inspection.get("offset") or 0), length=policy.input_budget)
            elif kind == "dependencies":
                result = repository.dependencies(inspection.get("path", ""), query=inspection.get("query", ""), cursor=int(inspection.get("cursor") or 0))
            elif kind == "read":
                result = repository.read(inspection["path"], start=int(inspection.get("start") or 1), limit=int(inspection.get("limit") or 200))
                if estimate_tokens(result) > policy.input_budget // 2:
                    result = {"error": "Source range is too large. Use bytes with a next_offset cursor, or request fewer lines.", "path":inspection["path"]}
            elif kind in ("files", "symbols"):
                result = repository.inventory(query=inspection.get("query", ""), cursor=inspection.get("cursor", ""), symbols=kind == "symbols")
            elif kind == "search":
                result = repository.search(inspection["query"], cursor=inspection.get("cursor", ""))
            else:
                result = {"error": "Unknown read-only inspection"}
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError, ValueError, KeyError, TypeError) as error:
            result = {"error":f"{type(error).__name__}: {error}",
                      "instruction":"Correct the inspection arguments or list/search the repository to find the current path. This inspection did not succeed."}
            requested = inspection.get("path") if isinstance(inspection,dict) else None
            if isinstance(error,FileNotFoundError) and isinstance(requested,str) and requested:
                query = Path(requested).name
                matches = repository.inventory(query=query)
                result.update(items=matches["items"], next_cursor=matches["next_cursor"], matching_query=query)
        if estimate_tokens(result) > policy.input_budget // 2:
            items = result.get("items", [])
            while items and estimate_tokens(result) > policy.input_budget // 2:
                items.pop()
            if items:
                result.update(next_cursor=items[-1]["cursor"], truncated=True)
            else:
                result = {"error":"Inspection result exceeds this context. Narrow the query or request a byte range."}
        identity = digest({"request":inspection,"result":result})
        repeats[identity] = repeats.get(identity,0)+1
        if payload.get("policy_version",1)>=2 and repeats[identity]>=3:
            raise ValueError("Inspection repeated identical evidence three times without progress; continue with a different approach or model")
        record = {"id":"inspection-"+identity[:16], "request":inspection, "result":result}
        if not result.get("error"):
            inspections.append(record)
        store.event(operation_id, "inspection", **record)
        observations.append(record)
        if recovery: recovery.observed()





def changed_check_sources(archive, root, exclusions=None):
    """Setup may create dependencies, but cannot rewrite the tested source."""
    changed, members = [], set()
    with tarfile.open(archive) as bundle:
        for member in bundle:
            members.add(member.name)
            path = root / member.name
            if member.isfile():
                if path.is_symlink() or not path.is_file():
                    changed.append(member.name)
                    continue
                with bundle.extractfile(member) as source, path.open("rb") as actual:
                    while True:
                        expected = source.read(1024 * 1024)
                        if actual.read(len(expected) or 1) != expected:
                            changed.append(member.name)
                            break
                        if not expected:
                            break
            elif member.issym() and (not path.is_symlink() or os.readlink(path) != member.linkname):
                changed.append(member.name)
    if exclusions is not None:
        for directory,dirs,files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in exclusions and not name.endswith('.egg-info')]
            for name in files:
                path=Path(directory)/name
                relative=path.relative_to(root).as_posix()
                if relative not in members and path.suffix in {'.py','.js','.mjs','.cjs','.ts','.tsx','.jsx','.html','.css','.rs','.go','.java'}:
                    changed.append(relative)
    return changed


def read_check_log(store, operation, name, offset=0, length=65536):
    root = (store.root / "checks" / operation["job_id"]).resolve()
    path = Path(name).resolve()
    allowed = path.is_relative_to(root)
    if operation.get('payload', {}).get('policy_version', 1) >= 7:
        with store.connect() as db:
            identities = [row['id'] for row in db.execute('SELECT id FROM operations WHERE job_id=?', (operation['job_id'],))]
        allowed = allowed or any(path.is_relative_to((store.root / kind / identity).resolve())
            for identity in identities for kind in ('execution', 'patches'))
        allowed = allowed or any(path == (store.root / 'logs' / (identity + '.log')).resolve() for identity in identities)
    if not allowed or path.suffix != ".log":
        raise ValueError("Expected a verification log belonging to this job")
    if offset < 0 or length < 1:
        raise ValueError("Invalid log range")
    length = min(length, 65536)
    with path.open("rb") as source:
        source.seek(offset)
        data = source.read(length)
    size = path.stat().st_size
    return {"path":name,"offset":offset,"content":data.decode("utf-8","replace"),"size":size,
            "next_offset":offset+len(data) if offset+len(data)<size else None}




def storage_usage(root):
    total = 0
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    pass
    return total


def enforce_storage(store, payload):
    store._storage_checked_at = time.time()
    limit = payload["settings"]["daedalus_storage_mb"] * 1024 * 1024
    if payload.get('engine') == 'openhands159':
        from coder_engine_cache import collect
        collect(store, limit_bytes=limit, reserve_bytes=payload['settings'].get('daedalus_min_free_mb', 0)*1024*1024)
    if storage_usage(store.root) > limit:
        raise ValueError("Worker storage allowance exceeded by retained data or active caches. Increase storage in Settings or remove archived jobs.")
    if shutil.disk_usage(store.root).free < payload["settings"].get("daedalus_min_free_mb",0)*1024*1024:
        raise ValueError("Worker free disk space is below the configured reserve; free space before continuing")


def stop_descendants():
    import psutil
    children = psutil.Process().children(recursive=True)
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(children, timeout=3)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=3)


def execute(store, operation_id):
    operation = store.get(operation_id)
    payload, kind = operation["payload"], operation["kind"]
    if payload.get('engine') != 'openhands159' or kind not in {'inspect','code','check','package','qa'}:
        store.update(operation_id,status='blocked',ended=time.time(),result={
            'error':'This engine or operation has been retired; start a new OpenHands 1.59 job.',
            'failure_category':'engine_retired'})
        return
    if os.environ.get('HEPHAESTUS_EXECUTOR')=='docker':
        metadata=payload.get('_execution_scope',{})
        if not metadata.get('owner'):
            store.update(operation_id,status='blocked',result={'error':'Authenticated execution scope is missing'},ended=time.time())
            return
        os.environ['HEPHAESTUS_EXECUTION_IDENTITY']=json.dumps({**metadata,'operation':operation_id,
            'job':operation['job_id'],'revision':payload.get('revision_id','')})
    store.update(operation_id, status="running")
    result, status = {}, "succeeded"
    alarm = threading.current_thread() is threading.main_thread()
    modern = True
    if modern:
        payload = {**payload, '_usage_checkpoint': {'at': time.time(), 'active_seconds': 0}}
        store.update(operation_id, payload=payload)
        from coder_engine_resources import enter_operation
        try:
            group = enter_operation(operation_id)
            if group:
                payload = {**payload, '_resource_cgroup': group}
                store.update(operation_id, payload=payload)
                store.event(operation_id, 'resource_limits', cgroup=group, memory_bytes=3*1024**3, tasks=256)
        except Exception as error:
            store.update(operation_id, status='blocked', result={'error': str(error),
                'failure_category': 'resource_preparation', 'stage': 'preparation'}, ended=time.time())
            return
    if modern and kind == 'code':
        # Loading the model is bounded independently and never consumes coding time.
        preparation_start = time.time()
        store.update(operation_id, payload={**store.get(operation_id)['payload'],
            '_preparation_active_started': preparation_start,
            '_preparation_deadline': preparation_start + payload['settings'].get('daedalus_preparation_seconds', 600)})
        preparation_alarm = None
        try:
            if alarm:
                def preparation_expired(signum, frame):
                    raise TimeoutError('Model preparation allowance exhausted')
                preparation_alarm = signal.signal(signal.SIGALRM, preparation_expired)
                signal.setitimer(signal.ITIMER_REAL, payload['settings'].get('daedalus_preparation_seconds', 600))
            from coder_engine_ops import prepare
            prepare(store, operation_id)
        except Exception as error:
            payload = {**store.get(operation_id)['payload'], '_preparation_seconds': time.time() - preparation_start}
            payload.pop('_preparation_active_started', None); payload.pop('_preparation_deadline', None)
            status = 'cancelled' if store.get(operation_id)['cancel_requested'] else 'blocked'
            store.update(operation_id, status=status, payload=payload,
                result={'error': str(error), 'failure_category': 'environment', 'stage': 'preparation'}, ended=time.time())
            store.event(operation_id, 'finished', status=status, stage='preparation', error=str(error))
            return
        finally:
            if preparation_alarm is not None:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, preparation_alarm)
        payload = {**store.get(operation_id)['payload'], '_active_started': time.time(),
                   '_preparation_seconds': time.time() - preparation_start}
        payload.pop('_preparation_active_started', None); payload.pop('_preparation_deadline', None)
        store.update(operation_id, payload=payload)
    if alarm:
        def expired(signum, frame):
            raise TimeoutError("Execution allowance exhausted; continue from checkpoint")
        previous_alarm = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, max(0.001, payload["seconds_remaining"] + (payload['settings'].get('daedalus_preparation_seconds', 600) if modern else 0) - (time.time() - payload.get('_active_started', operation["started"]))))
    from contextlib import ExitStack
    cache_leases = ExitStack()
    try:
        if modern:
            from coder_engine_cache import lease, scope
            cache_leases.enter_context(lease(store.root / 'project-environments' / scope(payload.get('project_id') or operation['job_id'])))
            cache_leases.enter_context(lease(store.root / 'operation-caches' / operation_id))
        enforce_storage(store, payload)
        _boundary(store, operation_id)
        workspace = store.root / "jobs" / operation["job_id"] / "workspace"
        if not workspace.exists():
            if kind != "inspect":
                raise ValueError("Job workspace is not initialized")
            source_id = payload.get("source_project_id")
            if payload.get("source_job_id"):
                source_job = payload["source_job_id"]
                if not re.fullmatch(r"cw3-[a-f0-9]+", source_job):
                    raise ValueError("Invalid source job identity")
                baseline_bundle = store.root/"jobs"/operation["job_id"]/"baseline.tar.gz"
                baseline_bundle.parent.mkdir(parents=True,exist_ok=True)
                if payload.get("source_archive_sha256"):
                    uploaded = store.root/"sources"/operation["job_id"]/(payload["source_archive_sha256"]+".tar.gz")
                    shutil.copyfile(uploaded,baseline_bundle)
                else:
                    source_repository = Repository(store.root/"jobs"/source_job/"workspace", store.root/"jobs"/source_job/"repository",
                                                   payload["settings"]["daedalus_exclude_dirs"])
                    source_repository.archive(payload["source_revision_id"],baseline_bundle)
                staging = workspace.with_name("copying")
                shutil.rmtree(staging,ignore_errors=True)
                staging.mkdir()
                with tarfile.open(baseline_bundle) as bundle:
                    bundle.extractall(staging,filter="data")
                # A review candidate's archive carries its "not accepted" report; it is never project source.
                for report in staging.glob("DAEDALUS_REVIEW_*.json"):
                    report.unlink()
                baseline_bundle.unlink()
                staging.rename(workspace)
            elif source_id:
                if not re.fullmatch(r"[A-Za-z0-9_-]+", source_id):
                    raise ValueError("Invalid registered project identity")
                projects_root = Path(payload["projects_root"]).resolve()
                if os.environ.get("HEPHAESTUS_EXECUTOR")=="docker":
                    from hephaestus_scope import scope, workspace_root
                    with scope(**payload["_execution_scope"]):
                        with scope(project=source_id):
                            projects_root=workspace_root()/"root/projects"
                source = safe_relative(projects_root, source_id)
                source_links = validate_links(source, payload["settings"]["daedalus_exclude_dirs"])
                remaining = payload["settings"]["daedalus_storage_mb"] * 1024 * 1024 - storage_usage(store.root)
                def copy_file(src, dst):
                    nonlocal remaining
                    _boundary(store, operation_id)
                    remaining -= os.stat(src).st_size
                    if remaining < 0:
                        raise ValueError("Worker storage allowance exceeded while copying project")
                    return shutil.copy2(src, dst)
                staging = workspace.with_name("copying")
                shutil.rmtree(staging, ignore_errors=True)
                shutil.copytree(source, staging, symlinks=True, copy_function=copy_file,
                                ignore=shutil.ignore_patterns(*payload["settings"]["daedalus_exclude_dirs"]))
                for link in source_links:
                    if Path(link["target"]).is_absolute():
                        target = Path(link["target"]).relative_to(source)
                        copied = staging / link["path"]
                        copied.unlink()
                        copied.symlink_to(os.path.relpath(staging/target,copied.parent))
                staging.rename(workspace)
            else:
                workspace.mkdir(parents=True)
        repository = operation_repo(store, operation)
        if kind in {"code", "qa"}:
            from coder_inference import require_local_model
            require_local_model(payload["ollama_url"],payload["model"])
        if kind == "inspect":
            pinned = payload.get('pinned_revision')
            if payload.get('policy_version', 1) >= 6 and pinned:
                if not re.fullmatch(r'[a-f0-9]{40,64}', pinned):
                    raise ValueError('Invalid candidate revision')
                observed = repository.snapshot('Workspace before candidate continuation', parent=payload.get('revision_id', ''))
                if observed['revision'] != pinned:
                    # Explicit candidate continuation selects the immutable source.
                    # Save divergent work before restoring, so it is never lost.
                    bundle_path = repository.state / 'candidate-restore.tar.gz'
                    repository.archive(pinned, bundle_path)
                    staging = workspace.with_name('candidate-restore')
                    shutil.rmtree(staging, ignore_errors=True); staging.mkdir()
                    with tarfile.open(bundle_path) as bundle:
                        bundle.extractall(staging, filter='data')
                    backup = workspace.with_name('before-candidate-' + operation_id)
                    workspace.rename(backup)
                    try:
                        staging.rename(workspace)
                    except BaseException:
                        backup.rename(workspace)
                        raise
                    store.event(operation_id, 'candidate_restored', revision_id=pinned,
                                retained_snapshot=observed, retained_workspace=str(backup))
            result = {"inventory": repository.refresh(), "snapshot": repository.snapshot("Project baseline", parent=payload.get("revision_id", "")), "workspace": str(workspace)}
            if payload.get('policy_version', 1) >= 6:
                from coder_profiles import discover as profiles
                result['verification'] = profiles(repository, payload.get('execution_commands'), payload.get('proposed_commands'))
        elif modern and kind in {'code', 'check'}:
            from coder_engine_ops import execute as engine_execute
            result = engine_execute(store, operation_id, repository)
        elif kind == 'qa':
            from coder_execution_utils import tree_hashes
            original_hashes = tree_hashes(repository.root, repository.excludes)
            result = _read_only(store, operation_id, repository,
                'Answer using current source evidence and file:line citations. Return {"answer":"..."}. Never modify the project.',
                payload.get('evidence', {}),
                validate=lambda answer: validate_qa(answer, repository))
            if tree_hashes(repository.root, repository.excludes) != original_hashes:
                raise ValueError('Source changed during read-only Q&A; answer was not published')
            result['revision_id'] = payload.get('revision_id', '')
        elif kind == "package":
            observed = repository.snapshot("Delivery source check",parent=payload["revision_id"])
            if observed["revision"] != payload["revision_id"]:
                raise ValueError("Source changed after Acceptance; verify the new revision before delivery")
            archive_owner = operation_id if payload.get('candidate') and payload.get('policy_version', 1) >= 7 else operation['job_id']
            output = store.root / "artifacts" / f"{'candidate-' if payload.get('candidate') else ''}{archive_owner}-{payload['revision_id']}.tar.gz"
            output.parent.mkdir(exist_ok=True)
            result = {**repository.archive(payload["revision_id"], output), "path": str(output), "revision_id": payload["revision_id"]}
            if modern and payload.get('verified_outputs'):
                from coder_engine_delivery import append_products
                from coder_repository import file_hash
                append_products(output, payload['verified_outputs'], payload['revision_id'], store.root)
                result.update(sha256=file_hash(output), size=output.stat().st_size)
            if payload.get('candidate'):
                import io
                from coder_repository import file_hash
                temporary = output.with_suffix('.tmp')
                name = f"DAEDALUS_REVIEW_{payload['revision_id']}.json"
                report = json.dumps({'label':'Review candidate; not accepted', 'revision_id':payload['revision_id'],
                    'verification_summary':payload.get('verification_summary', {}),
                    'run_instructions':payload.get('run_instructions', []),
                    'build_products': [{k: row[k] for k in ('name', 'revision_id', 'sha256', 'size')}
                                       for row in payload.get('verified_outputs', [])]}, indent=2).encode()
                with tarfile.open(output) as source, tarfile.open(temporary, 'w:gz') as target:
                    for member in source:
                        if member.name == name:
                            raise ValueError('Candidate report path collides with project source')
                        target.addfile(member, source.extractfile(member) if member.isfile() else None)
                    member = tarfile.TarInfo(name); member.size = len(report); member.mode = 0o644
                    target.addfile(member, io.BytesIO(report))
                temporary.replace(output)
                result.update(sha256=file_hash(output), size=output.stat().st_size)
    except (ValueError, TimeoutError) as error:
        result, status = {**store.get(operation_id)["result"], "error": str(error)}, "blocked"
    except InterruptedError as error:
        result, status = {"error": str(error)}, "cancelled"
    except Exception as error:
        result, status = {"error": f"{type(error).__name__}: {error}", "unverified_changes": kind == "code"}, "failed"
    finally:
        if alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_alarm)
        # Re-read: launch() stores the PID after spawning, so the row read at start-up may not have it yet
        # and every child process (dev servers, Chromium) would be left running.
        if (store.get(operation_id) or {}).get("pid") == os.getpid():
            stop_descendants()
        cache_leases.close()
    if kind == "code" and "snapshot" not in result:
        try:
            checkpoint_repo = operation_repo(store, operation)
            if modern and payload.get('engine_behavior_version', 0) >= 1:
                from coder_execution_utils import sanitize_workspace
                from coder_execution_utils import restore_protected
                sanitize_workspace(checkpoint_repo.root, checkpoint_repo.excludes)
                result['protected_restored'] = restore_protected(checkpoint_repo,
                    payload.get('baseline_revision') or payload.get('revision_id', ''), payload.get('protected_files', []))
                result['inventory'] = checkpoint_repo.refresh()
            result["snapshot"] = checkpoint_repo.snapshot("Interrupted coding checkpoint", parent=payload.get("revision_id", ""))
        except Exception as checkpoint_error:
            result['checkpoint_error'] = str(checkpoint_error)
    if modern and kind == 'code' and payload.get('engine_behavior_version', 0) >= 1:
        try:
            from coder_engine_runtime import repair_count
            path = store.root / 'agent-runtime' / operation['job_id'] / 'bookkeeping' / 'engine' / 'stop.json'
            result['hook_denials'] = repair_count(path, payload.get('repair_round', 0))
        except (ValueError, OSError) as repair_error:
            result['hook_denials'] = 2  # Corrupt bookkeeping must not grant more repairs.
            result['repair_recovery_error'] = str(repair_error)
    if status in {'blocked','failed'}:
        from coder_failures import classify_failure
        result['failure']=classify_failure(result.get('error','Operation failed'),kind,
            recovery_attempted=store.has_event(operation_id, 'tool_mode_fallback'))
        if modern:
            with store.connect() as connection:
                event = connection.execute(
                    "SELECT payload FROM events WHERE operation_id=? AND json_extract(payload,'$.type') IN ('model_failure','model_response') ORDER BY seq DESC LIMIT 1",
                    (operation_id,)).fetchone()
            record = json.loads(event['payload']) if event else {}
            if record.get('type') == 'model_failure':
                detail = record['data']
                result['failure'].update(category=detail['category'], request_id=detail['request_id'],
                    upstream_status=detail['upstream_status'], recovery_attempted=detail['attempt'] > 1,
                    next_action='Inspect the inference request and termination reason before resuming the retained conversation.')
                result['failure'].update({key: detail[key] for key in ('bridge_status', 'response_requirement', 'usage') if key in detail})
                result['transport_failure'] = detail
    preparation_failure = store.get(operation_id)['payload'].get('_preparation_failure')
    if preparation_failure:
        result['failure'] = {'category': 'environment', 'stage': kind, 'preparation': preparation_failure,
            'next_action': 'Restore dependency preparation, then start a fresh project or resume the saved checkpoint.'}
        result['error'] = 'Dependency setup failed; your project files are saved.'
        status = 'blocked'
    store.update(operation_id, status=status, result=result, ended=time.time())
    store.event(operation_id, "finished", status=status, result=result)


def install_routes(app, projects_root, *, engine="openhands159"):
    from fastapi import Body, HTTPException
    from fastapi.responses import FileResponse
    class LazyStore:
        instance = None
        def __getattr__(self, name):
            if self.instance is None:
                self.instance = WorkerStore(os.environ.get("DAEDALUS_STATE_DIR", str(Path(projects_root).parent / ".daedalus")))
            return getattr(self.instance, name)
    store = LazyStore()

    def source_path(job_id, checksum):
        if not re.fullmatch(r"cw3-[a-f0-9]+",job_id) or not re.fullmatch(r"[a-f0-9]{64}",checksum):
            raise HTTPException(422,"Invalid source identity")
        return store.root/"sources"/job_id/(checksum+".tar.gz")

    @app.get("/jobs/{job_id}/source/{checksum}")
    def has_source(job_id: str, checksum: str):
        if not source_path(job_id,checksum).is_file(): raise HTTPException(404,"Source not uploaded")
        return {"sha256":checksum}

    @app.put("/jobs/{job_id}/source/{checksum}")
    async def upload_source(job_id: str, checksum: str, request: Request,
                            upload_mb: int = DEFAULTS["daedalus_upload_mb"],extracted_mb: int = DEFAULTS["daedalus_extracted_mb"],free_mb: int = DEFAULTS["daedalus_min_free_mb"]):
        if min(upload_mb,extracted_mb,free_mb)<1: raise HTTPException(422,"Invalid storage allowance")
        path=source_path(job_id,checksum); path.parent.mkdir(parents=True,exist_ok=True)
        # A lost response can overlap a receipt probe and a retry. Each stream
        # owns its temporary file; only a validated exact checksum is published.
        temporary=path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
        try:
            size=0; hasher=hashlib.sha256()
            with temporary.open("wb") as output:
                async for chunk in request.stream():
                    size+=len(chunk)
                    if size>upload_mb*1024*1024: raise HTTPException(413,"Source archive too large")
                    required = len(chunk) + free_mb * 1024 * 1024
                    if engine == 'openhands159' and shutil.disk_usage(store.root).free < required:
                        from coder_engine_cache import collect
                        await asyncio.to_thread(collect, store, required_bytes=len(chunk),
                                                reserve_bytes=free_mb * 1024 * 1024)
                    if shutil.disk_usage(store.root).free < required:
                        raise HTTPException(507,"Insufficient worker disk space")
                    hasher.update(chunk); output.write(chunk)
            if hasher.hexdigest()!=checksum: raise HTTPException(422,"Source checksum mismatch")
            with tarfile.open(temporary) as archive:
                expanded=0
                for member in archive:
                    if Path(member.name).is_absolute() or ".." in Path(member.name).parts:
                        raise HTTPException(422,"Unsafe archive path")
                    expanded+=member.size
                    if expanded>extracted_mb*1024*1024: raise HTTPException(413,"Expanded source too large")
            temporary.replace(path)
            return {"sha256":checksum}
        finally: temporary.unlink(missing_ok=True)

    @app.get("/jobs/{job_id}/evidence/{identity}")
    def evidence(job_id: str, identity: str):
        from coder_evidence import resolve
        try: record=resolve(store,job_id,identity)
        except (ValueError,OSError): raise HTTPException(404,"Evidence unavailable") from None
        return FileResponse(record["path"],media_type="image/png" if record["kind"]=="screenshot" else "application/zip")

    @app.get("/jobs/{job_id}/operations/{operation_id}/checkpoint")
    def checkpoint(job_id: str, operation_id: str, revision: str):
        operation=operation_status(job_id,operation_id)
        if not re.fullmatch(r"[a-f0-9]{40,64}",revision): raise HTTPException(422,"Invalid revision")
        output=store.root/"artifacts"/f"unverified-{job_id}-{revision}.tar.gz"; output.parent.mkdir(exist_ok=True)
        try: operation_repo(store,operation).archive(revision,output)
        except (ValueError,OSError,subprocess.CalledProcessError): raise HTTPException(404,"Checkpoint unavailable") from None
        return FileResponse(output,media_type="application/gzip",filename=output.name)

    @app.post("/jobs/{job_id}/operations/{operation_id}", status_code=202)
    def start_operation(job_id: str, operation_id: str, body: dict = Body(...)):
        try:
            payload = {**body, "projects_root": str(projects_root)}
            if os.environ.get('HEPHAESTUS_EXECUTOR')=='docker':
                from hephaestus_scope import current, claim_job
                metadata=current()
                if payload.get('source_job_id') and not payload.get('source_archive_sha256'):
                    claim_job(payload['source_job_id'],metadata['owner'])
                if not metadata.get('owner'): raise ValueError('Authenticated execution scope is missing')
                payload['_execution_scope']={**metadata,'project':payload.get('project_id') or job_id,
                                             'conversation':payload.get('conversation_id','')}
            if engine and payload.get('engine') != engine:
                raise ValueError('This worker accepts only ' + engine + ' jobs')
            for key in ("seconds_remaining", "calls_remaining", "settings"):
                if key not in payload:
                    raise ValueError(f"Missing {key}")
            resolve("builder", payload["settings"])
            if any(str(payload.get("model", "")).startswith(prefix) for prefix in ("openai:", "anthropic:", "custom:")) or str(payload.get("model", "")).endswith(("-cloud", ":cloud")):
                raise ValueError("Daedalus requires a local model")
            store.create(operation_id, job_id, body["kind"], payload)
            launch(store, operation_id)
            return store.get(operation_id)
        except (ValueError, KeyError) as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/jobs/{job_id}/operations/{operation_id}")
    def operation_status(job_id: str, operation_id: str):
        operation = reconcile(store, operation_id)
        if not operation or operation["job_id"] != job_id:
            raise HTTPException(404, "Operation not found")
        return operation

    @app.get("/jobs/{job_id}/operations/{operation_id}/events")
    def operation_events(job_id: str, operation_id: str, after: int = 0):
        operation_status(job_id, operation_id)
        return store.events(operation_id, after)

    @app.post("/jobs/{job_id}/operations/{operation_id}/cancel")
    def cancel_operation(job_id: str, operation_id: str):
        try:
            store.cancel_before_dispatch(operation_id, job_id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return cancel(store, operation_id)

    @app.patch("/jobs/{job_id}/operations/{operation_id}/settings")
    def update_policy(job_id: str, operation_id: str, body: dict = Body(...)):
        from context_policy import validate_patch
        operation = operation_status(job_id, operation_id)
        try:
            settings = {**operation["payload"]["settings"], **validate_patch(body, operation["payload"]["settings"])}
        except (ValueError,TypeError) as error:
            raise HTTPException(422,str(error)) from error
        if "context_compaction" in body:
            if body["context_compaction"] not in {"on","off"}:
                raise HTTPException(422,"Invalid global compaction mode")
            settings["context_compaction"] = body["context_compaction"]
        store.update(operation_id, payload={**operation["payload"], "settings": settings})
        return {"settings_version": resolve(settings=settings).settings_version}

    @app.get("/jobs/{job_id}/operations/{operation_id}/artifact")
    def artifact(job_id: str, operation_id: str):
        operation = operation_status(job_id, operation_id)
        if operation["kind"] != "package" or operation["status"] != "succeeded":
            raise HTTPException(409, "Artifact is not ready")
        return FileResponse(operation["result"]["path"], media_type="application/gzip")

    @app.post("/jobs/{job_id}/operations/{operation_id}/inspect")
    def inspect(job_id: str, operation_id: str, body: dict = Body(...)):
        operation = operation_status(job_id, operation_id)
        repository = operation_repo(store, operation)
        try:
            if body.get("refresh") and body.get("operation") in {"files", "symbols", "search", "dependencies"}:
                repository.refresh()
            if body.get("operation") == "screenshot":
                import base64
                path = Path(body["path"]).resolve()
                root = (store.root/"checks"/job_id).resolve()
                if not path.is_relative_to(root) or path.suffix != ".png":
                    raise ValueError("Expected a browser screenshot belonging to this job")
                return {"image":"data:image/png;base64,"+base64.b64encode(path.read_bytes()).decode()}
            if body.get("operation") == "log":
                return read_check_log(store, operation, body["path"], int(body.get("offset",0)), int(body.get("length",65536)))
            if body.get("operation") == "bytes":
                return repository.read_bytes(body["path"], offset=int(body.get("offset", 0)), length=int(body.get("length", 65536)), expected_hash=body.get("sha256", ""))
            if body.get("operation") == "dependencies":
                return repository.dependencies(body.get("path", ""), query=body.get("query", ""), cursor=int(body.get("cursor") or 0), limit=int(body.get("limit", 100)))
            if body.get("operation") == "read":
                return repository.read(body["path"], start=int(body.get("start", 1)), limit=int(body.get("limit", 200)), expected_hash=body.get("sha256", ""))
            if body.get("operation") == "search":
                return repository.search(body["query"], cursor=body.get("cursor", ""), limit=int(body.get("limit", 100)))
            return repository.inventory(cursor=body.get("cursor", ""), limit=int(body.get("limit", 100)), query=body.get("query", ""), symbols=body.get("operation") == "symbols")
        except (ValueError, TypeError, KeyError, OSError) as error:
            raise HTTPException(422, str(error)) from error
    return store


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", required=True)
    parser.add_argument("--state", required=True)
    arguments = parser.parse_args()
    execute(WorkerStore(arguments.state), arguments.execute)
