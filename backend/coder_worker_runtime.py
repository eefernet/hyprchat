"""Durable Codebox operations, isolated from HTTP connections and restarts."""
from __future__ import annotations

import argparse
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
from fastapi import Request

from coder_repository import Repository, safe_relative, validate_links
from context_policy import DEFAULTS, resolve, estimate_tokens, operation_settings
from coder_checks import discover, browser_check, validate_plan
from coder_verification import (digest, source_context, test_count, validate_requirements,
    validate_probes, validate_probe_correction, validate_acceptance, PLAN_INSTRUCTION, PROBE_INSTRUCTION, ACCEPT_INSTRUCTION)


class WorkerStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
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
                       (operation_id, operation_id, json.dumps({"type": kind, "data": data})))

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
        for value in (operation_id, job_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise ValueError("Invalid operation identity")
        if kind not in ("inspect", "plan", "verify", "code", "check", "visual", "accept", "package", "qa"):
            raise ValueError("Unknown operation kind")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if old:
                if old["job_id"] != job_id or old["kind"] != kind or json.loads(old["payload"]).get("request_key") != payload.get("request_key"):
                    raise ValueError("Operation identity reused for another request")
                return False
            db.execute("INSERT INTO operations(id,job_id,kind,status,payload) VALUES(?,?,?,'queued',?)",
                       (operation_id, job_id, kind, json.dumps(payload)))
        return True


def operation_repo(store, operation):
    root = store.root / "jobs" / operation["job_id"] / "workspace"
    return Repository(root, store.root / "jobs" / operation["job_id"] / "repository",
                      operation["payload"]["settings"].get("daedalus_exclude_dirs", DEFAULTS["daedalus_exclude_dirs"]))


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
    if time.time() - operation["started"] >= payload["seconds_remaining"]:
        raise TimeoutError("Execution allowance exhausted; continue from the checkpoint")
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


def _read_only(store, operation_id, repository, instruction, extra, validate=None, schema=None):
    """The model can navigate evidence, but receives no write/shell tools."""
    from coder_inference import local_chat, summarize_history
    operation = store.get(operation_id)
    role = {"plan": "architect", "verify":"reviewer", "accept": "acceptance", "qa": "qa"}[operation["kind"]]
    task = operation["payload"]["task"]
    observations, checkpoint, corrections = [], "", 0
    inspections, repeats, invalid_repeats = [], {}, {}
    recovery = None
    if operation['payload'].get('policy_version', 1) >= 5:
        if schema is None:
            from coder_check_schema import DEFINITIONS, JSON_VALUE
            schema = {'type':'object', 'additionalProperties':JSON_VALUE, '$defs':DEFINITIONS}
        from coder_response_recovery import Recovery, ResponseFormatError
        recovery = Recovery(store, operation_id, instruction, extra, schema)
        if 'answer' in recovery.state:
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
        if operation["kind"]=="verify":
            prompt+='\nTo check JavaScript fixture construction or language semantics, return {"inspect":{"operation":"evaluate","expression":"a JavaScript expression, or an IIFE returning data"}}. This evaluates in an empty VM context without modules, require, process, or project files. Use it when an expected value or fixture might be wrong.'
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
            if operation["kind"] == "accept" and 2 <= payload.get("policy_version",1) < 6:
                if payload.get('policy_version',1)>=3:
                    from coder_contracts import attach_acceptance_evidence
                    answer=attach_acceptance_evidence(answer,extra.get('checks',[]),payload['revision_id'])
                validate = lambda value: validate_acceptance(value,payload["requirements"],extra.get("checks",[]),
                    payload["revision_id"],repository,inspections,payload.get("visual_review"))
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
            if kind == "evaluate" and operation["kind"]=="verify":
                result=javascript_experiment(inspection["expression"],min(payload["settings"]["daedalus_command_seconds"],
                    payload["seconds_remaining"]-(time.time()-operation["started"])),policy.input_budget)
            elif kind == "evidence":
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


def javascript_experiment(expression,timeout,budget):
    """Check language semantics without exposing the project to the expression."""
    if not isinstance(expression,str) or not expression.strip(): raise ValueError("Expression is required")
    if estimate_tokens(expression)>budget: raise ValueError("Experiment exceeds configured input budget")
    if timeout<=0: raise TimeoutError("Execution allowance exhausted")
    program='''const vm=require('node:vm');const fs=require('node:fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const context=vm.createContext(Object.create(null),{codeGeneration:{strings:false,wasm:false}});
const result=vm.runInContext('JSON.stringify({value:('+input.expression+')})',context,{timeout:input.milliseconds});
process.stdout.write(result);
'''
    with tempfile.TemporaryDirectory(prefix="daedalus-semantics-") as directory:
        result=subprocess.run(['node','--max-old-space-size=128','-e',program],
            input=json.dumps({"expression":expression,"milliseconds":max(1,int(timeout*1000))}),
            cwd=directory,env={"PATH":os.environ.get("PATH","")},capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        return {"error":result.stderr[-budget:],"exit_code":result.returncode}
    if estimate_tokens(result.stdout)>budget:return {"error":"Experiment result is too large; return fewer values"}
    return {"experiment":json.loads(result.stdout),"note":"Language experiment only; this does not verify the project"}



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


def _run_check(store, operation_id, repository, payload):
    if payload.get('policy_version', 1) >= 6:
        from coder_native_checks import run_checks
        return run_checks(store, operation_id, repository, payload)
    revision = payload["revision_id"]
    check_dir = store.root / "checks" / store.get(operation_id)["job_id"] / revision
    check_dir.mkdir(parents=True, exist_ok=True)
    archive = check_dir / "source.tar.gz"
    repository.archive(revision, archive)
    root = check_dir / "workspace"
    exclusions=payload["settings"]["daedalus_exclude_dirs"] if payload.get("policy_version",1)>=2 else None
    if root.exists() and changed_check_sources(archive, root, exclusions):
        shutil.rmtree(root)
        for cached in check_dir.glob("*.json"):
            cached.unlink()
    if not root.exists():
        staging = check_dir / "copying"
        shutil.rmtree(staging,ignore_errors=True)
        staging.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(staging, filter="data")
        staging.rename(root)
    results = []
    def persist(check_key):
        target = check_dir / (check_key + ".json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(results[-1]))
        temporary.replace(target)
        store.update(operation_id,result={"revision_id":revision,"checks":results,"passed":False})
        store.event(operation_id,"check",check=results[-1])
    for index, check in enumerate(payload.get("checks", [])):
        _boundary(store, operation_id)
        cwd = root if check.get("cwd", ".") == "." else safe_relative(root, check["cwd"])
        check_key = hashlib.sha256(json.dumps(check,sort_keys=True).encode()).hexdigest()
        if payload.get('policy_version',1)>=5:
            check_key = digest({'check':check,'settings':payload['settings'],
                'environment':dict(os.environ),'baseline':payload.get('baseline_revision')})
        cached = check_dir / (check_key + ".json")
        if cached.exists():
            previous = json.loads(cached.read_text())
            if previous.get("passed"):
                results.append({**previous,"reused":True})
                continue
        log_path = check_dir / f"check-{check_key}-{operation_id}.log"
        started = time.time()
        remaining = payload["seconds_remaining"] - (time.time() - store.get(operation_id)["started"])
        timeout = min(remaining, payload["settings"]["daedalus_command_seconds"])
        if payload.get('policy_version',1)>=5 and check.get('execution_alias'):
            previous = next((r for r in results if r['id']==check['execution_alias']
                and r.get('is_test') and all(r.get(k)==check.get(k) for k in ('command','cwd','test_runner'))), None)
            if previous:
                results.append({**previous, **check, 'reused':True, 'reused_from':previous['id'],
                    'execution_origin':'project_tests', 'seconds':0})
                persist(check_key)
                continue
        if payload.get('policy_version',1)>=5 and check.get('runner')=='file':
            from coder_file_probe import execute_file
            try:
                evidence = execute_file(repository, root, check, payload.get('baseline_revision'))
            except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
                evidence = {'passed':False,'error':str(error),'failure_kind':'invalid_file_binding'}
            results.append({**check,**evidence,'revision_id':revision,'seconds':time.time()-started})
            persist(check_key)
            continue
        if payload.get('policy_version',1)>=4 and (check.get('runner')=='api' or check.get('bindings')):
            from coder_api_probe import source_bindings
            try:
                check={**check,'source_bindings':source_bindings(cwd,check)}
            except (ValueError,FileNotFoundError) as error:
                results.append({**check,'passed':False,'revision_id':revision,'error':str(error),
                    'failure_kind':'missing_source_binding','seconds':time.time()-started})
                persist(check_key)
                continue
        if check.get("kind") == "browser":
            browser_dir = check_dir / f"browser-{check_key}-{operation_id}"
            browser_dir.mkdir(exist_ok=True)
            try:
                if payload.get("policy_version",1)>=2:
                    from coder_browser import browser_check as verified_browser
                    from coder_evidence import attach
                    browser = verified_browser(cwd,check,browser_dir,timeout,
                        policy_version=payload.get('policy_version',1),
                        step_timeout=payload["settings"]["daedalus_browser_step_seconds"],viewports=payload["settings"]["daedalus_browser_viewports"],
                        startup_timeout=payload['settings'].get('daedalus_browser_startup_seconds',DEFAULTS['daedalus_browser_startup_seconds']),
                        emit=lambda item:store.event(operation_id,"browser_action",action=item,revision_id=revision))
                    attach(store,store.get(operation_id)["job_id"],browser,revision)
                else:
                    browser = browser_check(cwd, check, browser_dir, timeout,
                        step_timeout=payload["settings"].get("daedalus_browser_step_seconds",DEFAULTS["daedalus_browser_step_seconds"]))
            except Exception as error:
                browser = {"passed":False,"error":str(error),"server_log":str(browser_dir/"browser-server.log"),
                           "environment_fault":any(marker in str(error).lower() for marker in ("executable doesn't exist","host system is missing dependencies","error while loading shared libraries"))}
            results.append({**check,**browser,"revision_id":revision,"seconds":time.time()-started})
            persist(check_key)
            if browser.get("environment_fault"):
                break
            continue
        with log_path.open("wb") as output:
            environment = {k:v for k,v in os.environ.items() if k not in {"PYTHONPATH","PYTHONHOME","VIRTUAL_ENV","PIP_TARGET","PIP_PREFIX","PIP_CONSTRAINT","PIP_REQUIRE_VIRTUALENV","PIP_CONFIG_FILE","npm_config_prefix","NPM_CONFIG_PREFIX","NPM_CONFIG_USERCONFIG","NPM_CONFIG_GLOBALCONFIG","GOFLAGS"}}
            environment["PATH"] = os.pathsep.join([str(cwd/".venv"/"bin"),str(root/".venv"/"bin"),environment.get("PATH","")])
            environment["PIP_CONFIG_FILE"] = os.devnull
            process = subprocess.Popen(["bash", "-c", check["command"]], cwd=cwd, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,env=environment)
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                exit_code = -1
        # Logs are evidence references, not a hidden permanent truncation.
        policy = resolve("reviewer", payload["settings"])
        with log_path.open("rb") as source:
            source.seek(max(0, log_path.stat().st_size - policy.input_budget))
            tail = source.read().decode("utf-8", "replace")
        preview_chars = policy.input_budget // max(1,len(payload.get("checks", [])))
        preview = tail[-preview_chars:] if preview_chars else ""
        count = test_count(check.get("test_runner") or check["command"],tail)
        false_assertion = False
        if check.get("is_test") or "node" in check["command"]:
            with log_path.open(errors="replace") as full_log:
                false_assertion=any("Assertion failed" in line for line in full_log)
        results.append({**check, "exit_code": exit_code, "passed": exit_code == 0 and (payload.get("policy_version",1)<2 or (count!=0 and not false_assertion)),
                        "test_count":count, "assertion_failure":false_assertion,"seconds": time.time() - started,
                        "environment_fault": exit_code == 127 or bool(check.get("setup") and any(marker in tail.lower() for marker in ("temporary failure in name resolution", "eai_again", "enotfound", "connection refused", "network is unreachable"))), "log": str(log_path), "log_tail": preview, "log_bytes": log_path.stat().st_size, "revision_id": revision})
        persist(check_key)
        if results[-1]["environment_fault"]:
            break
    changed = changed_check_sources(archive, root, exclusions)
    if changed:
        results.append({"id":"verification-source-integrity", "passed":False, "revision_id":revision,
                        "summary":"Verification modified or removed source from the tested revision", "paths":changed})
        persist("source-integrity")
    return {"revision_id": revision, "checks": results, "environment_fault": any(r.get("environment_fault") for r in results), "passed": bool(results) and all(item["passed"] for item in results)}


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
    limit = payload["settings"]["daedalus_storage_mb"] * 1024 * 1024
    if storage_usage(store.root) > limit:
        raise ValueError("Worker storage allowance exceeded. Increase storage in Settings or remove archived jobs.")
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
    store.update(operation_id, status="running")
    result, status = {}, "succeeded"
    alarm = threading.current_thread() is threading.main_thread()
    if alarm:
        def expired(signum, frame):
            raise TimeoutError("Execution allowance exhausted; continue from checkpoint")
        previous_alarm = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, max(0.001, payload["seconds_remaining"] - (time.time() - operation["started"])))
    try:
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
                baseline_bundle.unlink()
                staging.rename(workspace)
            elif source_id:
                if not re.fullmatch(r"[A-Za-z0-9_-]+", source_id):
                    raise ValueError("Invalid registered project identity")
                projects_root = Path(payload["projects_root"]).resolve()
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
        if kind in {"plan", "verify", "code", "accept", "qa"}:
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
            else:
                result["verification"] = discover(repository)
        elif kind in {'plan', 'verify', 'accept', 'code', 'check'} and payload.get('policy_version', 1) >= 7:
            from coder_policy7_ops import execute_operation
            result = execute_operation(store, operation_id, repository)
        elif kind in {'plan', 'verify', 'accept'} and payload.get('policy_version', 1) >= 6:
            from coder_review import plan, author, judge
            observed = repository.snapshot('Independent review source check', parent=payload['revision_id'])
            if observed['revision'] != payload['revision_id']:
                raise ValueError('Source changed after checkpoint; rerun checks before reviewing')
            result = {'plan': plan, 'verify': author, 'accept': judge}[kind](store, operation_id, repository, _read_only)
        elif kind in {"plan","verify"} and payload.get("policy_version",1)>=3:
            from coder_contracts import run_contract
            result = run_contract(store,operation_id,repository,_read_only)
        elif kind == "plan" and payload.get("policy_version",1)>=2:
            result = _read_only(store,operation_id,repository,PLAN_INSTRUCTION,payload.get("evidence",{}),
                                validate=lambda answer: validate_requirements(answer,payload["original_task"]))
        elif kind == "verify":
            previous=payload.get("diagnose_probes")
            instruction=PROBE_INSTRUCTION
            if previous:
                instruction+='\nDiagnose the supplied failing verification probes before any further code repair. Inspect the real source. A test can be wrong: check fixture construction, language semantics, assumed filenames, and expected results against the original request. Return the COMPLETE checks list. Keep valid checks unchanged. Correct only demonstrated probe defects; never adapt requested behavior to the current implementation or weaken assertions to make a failure pass. Preserve all IDs and requirement mappings. Include corrections:[{check_id,reason,source_quote}] for any changed check. If the code is wrong, return the original checks unchanged and corrections:[]; the Builder will repair it.'
            result = _read_only(store,operation_id,repository,instruction,payload.get("evidence",{}),
                validate=(lambda answer:validate_probe_correction(answer,payload["requirements"],previous,payload["original_task"])) if previous else
                         (lambda answer:validate_probes(answer,payload["requirements"])))
        elif kind == "plan":
            result = _read_only(store, operation_id, repository,
                "Plan the requested work as observable milestones. Keep implementation, tests, and docs together for a small utility. "
                "Inspect existing source and package manifests before planning changes. The controller discovers and runs real build, lint, and test commands after coding. "
                "For new projects, OMIT the checks field: do not invent test-module filenames or inline shell/Python programs. "
                "For existing projects, optional checks may reference commands already present in inspected package scripts. "
                "Prefer standard-library unittest for Python utilities that need no external dependencies. "
                "Return {\"milestones\":[{\"id\":\"m1\",\"title\":\"...\",\"task\":\"...\",\"criteria\":[\"...\"]}]}. "
                "For UI behavior include browser_flows:[{cwd:\".\",path:\"/\",steps:[{action:click|fill|visible|text|reload,selector,value}]}] on the milestone. "
                "The controller supplies the preview server command from the project. Include steps that exercise requested behavior, including reload for persistence. "
                "Keep all requested behavior, integration, tests, and documentation requirements. File layouts are advisory.",
                payload.get("evidence", {}), validate=validate_plan)
        elif kind == "code":
            from coder_sdk_runtime import run_coder
            result = run_coder(store, operation_id, repository)
            # cargo new / git init leave nested repositories that break the checkpoint snapshot.
            for nested in sorted(repository.root.rglob('.git')):
                if nested.parent != repository.root and 'node_modules' not in nested.parts:
                    shutil.rmtree(nested, ignore_errors=True) if nested.is_dir() else nested.unlink(missing_ok=True)
            enforce_storage(store, payload)
            result["inventory"] = repository.refresh()
            if payload.get('policy_version', 1) >= 6:
                from coder_profiles import discover as profiles
                result['verification'] = profiles(repository, payload.get('execution_commands'), payload.get('proposed_commands'))
            else:
                result["verification"] = discover(repository)
            result["snapshot"] = repository.snapshot("Coding checkpoint", parent=payload.get("revision_id", ""))
        elif kind == "check":
            result = _run_check(store, operation_id, repository, payload)
        elif kind == "visual":
            from coder_visual import review
            result = review(store,operation_id)
        elif kind in ("accept", "qa"):
            if kind == "accept":
                observed = repository.snapshot("Acceptance source check",parent=payload["revision_id"])
                if observed["revision"] != payload["revision_id"]:
                    raise ValueError("Source changed after verification; inspect the checkpoint and rerun checks")
            instruction = ("Assess the requested behavior against the exact revision and executable evidence. Inspect missing source before deciding. "
                           "Return {\"accepted\":true|false,\"issues\":[{\"summary\":\"...\",\"file\":\"...\"}],\"summary\":\"...\"}. "
                           "Do not claim unexecuted behavior passed." if kind == "accept" else
                           "Answer using current source evidence and file:line citations. Return {\"answer\":\"...\"}. Never modify the project.")
            if kind == "accept" and payload.get("policy_version",1)>=2:
                instruction = ACCEPT_INSTRUCTION
                if payload.get('policy_version',1)>=3:
                    instruction+='\nThe controller supplies check_ids using the actual requirement/revision mapping. Omit check_ids from your output. You still independently decide status and explain each judgment; unexecuted or failed evidence can never establish success.'
            result = _read_only(store, operation_id, repository, instruction,
                {**payload.get("evidence", {}),"requirements":payload.get("requirements",[]),"visual_review":payload.get("visual_review",{})})
            result["revision_id"] = payload.get("revision_id", "")
        elif kind == "package":
            observed = repository.snapshot("Delivery source check",parent=payload["revision_id"])
            if observed["revision"] != payload["revision_id"]:
                raise ValueError("Source changed after Acceptance; verify the new revision before delivery")
            archive_owner = operation_id if payload.get('candidate') and payload.get('policy_version', 1) >= 7 else operation['job_id']
            output = store.root / "artifacts" / f"{'candidate-' if payload.get('candidate') else ''}{archive_owner}-{payload['revision_id']}.tar.gz"
            output.parent.mkdir(exist_ok=True)
            result = {**repository.archive(payload["revision_id"], output), "path": str(output), "revision_id": payload["revision_id"]}
            if payload.get('candidate'):
                import io
                from coder_repository import file_hash
                temporary = output.with_suffix('.tmp')
                name = f"DAEDALUS_REVIEW_{payload['revision_id']}.json"
                report = json.dumps({'label':'Review candidate; not accepted', 'revision_id':payload['revision_id'],
                    'verification_summary':payload.get('verification_summary', {}),
                    'run_instructions':payload.get('run_instructions', [])}, indent=2).encode()
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
    if kind == "code" and "snapshot" not in result:
        try:
            result["snapshot"] = operation_repo(store, operation).snapshot("Interrupted coding checkpoint", parent=payload.get("revision_id", ""))
        except Exception:
            pass
    if status in {'blocked','failed'}:
        from coder_failures import classify_failure
        result['failure']=classify_failure(result.get('error','Operation failed'),kind,
            recovery_attempted=store.has_event(operation_id, 'tool_mode_fallback'))
    store.update(operation_id, status=status, result=result, ended=time.time())
    store.event(operation_id, "finished", status=status, result=result)


def install_routes(app, projects_root):
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
        temporary=path.with_suffix(".part")
        try:
            size=0; hasher=hashlib.sha256()
            with temporary.open("wb") as output:
                async for chunk in request.stream():
                    size+=len(chunk)
                    if size>upload_mb*1024*1024: raise HTTPException(413,"Source archive too large")
                    if shutil.disk_usage(store.root).free<len(chunk)+free_mb*1024*1024: raise HTTPException(507,"Insufficient worker disk space")
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
        operation_status(job_id, operation_id)
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
