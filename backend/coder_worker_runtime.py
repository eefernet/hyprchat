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

from coder_repository import Repository, safe_relative, validate_links
from context_policy import DEFAULTS, resolve, estimate_tokens
from coder_checks import discover, browser_check, validate_plan


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
        if kind not in ("inspect", "plan", "code", "check", "accept", "package", "qa"):
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


def _read_only(store, operation_id, repository, instruction, extra, validate=None):
    """The model can navigate evidence, but receives no write/shell tools."""
    from coder_inference import local_chat, summarize_history
    operation = store.get(operation_id)
    role = {"plan": "architect", "accept": "acceptance", "qa": "qa"}[operation["kind"]]
    task = operation["payload"]["task"]
    observations, checkpoint, corrections = [], "", 0
    while True:
        operation = _boundary(store, operation_id)
        payload = operation["payload"]
        policy = resolve(role, payload["settings"])
        context = bounded_context(repository, task, policy, extra)
        if observations and policy.compaction and estimate_tokens({**context, "observations": observations, "checkpoint": checkpoint}) > policy.compact_at:
            checkpoint = summarize_history(store, operation_id, {"checkpoint": checkpoint, "observations": observations})
            observations = []
        context["observations"] = observations
        context["checkpoint"] = checkpoint
        prompt = instruction + "\nReturn one JSON object. To inspect more return {\"inspect\":{\"operation\":\"files|symbols|dependencies|search|read|bytes|log|evidence\",\"query\":\"...\",\"path\":\"...\",\"cursor\":\"...\",\"start\":1}}. Inspection is read-only. Files/symbols accept literal substrings or * and ? wildcards; files match the full relative path or basename. Search finds literal source text.\n" + json.dumps(context)
        if estimate_tokens(prompt) > policy.input_budget:
            raise ValueError("Inspection evidence exceeds context. Increase the window or narrow the milestone in Settings.")
        raw = local_chat(store, operation_id, role, [{"role":"user", "content":prompt}])
        try:
            answer = _json(raw)
        except (ValueError, TypeError):
            corrections += 1
            store.event(operation_id, "invalid_response", response=raw)
            if corrections >= payload["settings"]["daedalus_attempt_turns"]:
                raise ValueError("Model repeatedly returned invalid inspection JSON; change the selected model or continue")
            observations.append({"error": "Return one valid JSON object matching the requested schema; the previous response was invalid."})
            continue
        inspection = answer.get("inspect")
        if not inspection:
            if validate:
                try:
                    validate(answer)
                except ValueError as error:
                    corrections += 1
                    store.event(operation_id,"invalid_plan",response=answer,error=str(error))
                    if corrections >= payload["settings"]["daedalus_attempt_turns"]:
                        raise ValueError(f"Plan validation failed: {error}") from error
                    observations.append({"invalid_plan":answer,"error":str(error),"instruction":"Return a corrected plan preserving the original requested behavior. Fix the verification command syntax."})
                    continue
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
        store.event(operation_id, "inspection", request=inspection, result=result)
        observations.append({"request": inspection, "result": result})



def changed_check_sources(archive, root):
    """Setup may create dependencies, but cannot rewrite the tested source."""
    changed = []
    with tarfile.open(archive) as bundle:
        for member in bundle:
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
    return changed


def read_check_log(store, operation, name, offset=0, length=65536):
    root = (store.root / "checks" / operation["job_id"]).resolve()
    path = Path(name).resolve()
    if not path.is_relative_to(root) or path.suffix != ".log":
        raise ValueError("Expected a verification log belonging to this job")
    if offset < 0 or length < 1:
        raise ValueError("Invalid log range")
    with path.open("rb") as source:
        source.seek(offset)
        data = source.read(length)
    size = path.stat().st_size
    return {"path":name,"offset":offset,"content":data.decode("utf-8","replace"),"size":size,
            "next_offset":offset+len(data) if offset+len(data)<size else None}


def _run_check(store, operation_id, repository, payload):
    revision = payload["revision_id"]
    check_dir = store.root / "checks" / store.get(operation_id)["job_id"] / revision
    check_dir.mkdir(parents=True, exist_ok=True)
    archive = check_dir / "source.tar.gz"
    repository.archive(revision, archive)
    root = check_dir / "workspace"
    if root.exists() and changed_check_sources(archive, root):
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
        cached = check_dir / (check_key + ".json")
        if cached.exists():
            previous = json.loads(cached.read_text())
            if previous.get("passed"):
                results.append({**previous,"reused":True})
                continue
        log_path = check_dir / f"check-{check_key}.log"
        started = time.time()
        remaining = payload["seconds_remaining"] - (time.time() - store.get(operation_id)["started"])
        timeout = min(remaining, payload["settings"]["daedalus_command_seconds"])
        if check.get("kind") == "browser":
            browser_dir = check_dir / f"browser-{check_key}"
            browser_dir.mkdir(exist_ok=True)
            try:
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
        results.append({**check, "exit_code": exit_code, "passed": exit_code == 0, "seconds": time.time() - started,
                        "environment_fault": exit_code == 127 or bool(check.get("setup") and any(marker in tail.lower() for marker in ("temporary failure in name resolution", "eai_again", "enotfound", "connection refused", "network is unreachable"))), "log": str(log_path), "log_tail": preview, "log_bytes": log_path.stat().st_size, "revision_id": revision})
        persist(check_key)
        if results[-1]["environment_fault"]:
            break
    changed = changed_check_sources(archive, root)
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
                source_repository = Repository(store.root/"jobs"/source_job/"workspace", store.root/"jobs"/source_job/"repository",
                                               payload["settings"]["daedalus_exclude_dirs"])
                baseline_bundle = store.root/"jobs"/operation["job_id"]/"baseline.tar.gz"
                baseline_bundle.parent.mkdir(parents=True,exist_ok=True)
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
        if kind in {"plan", "code", "accept", "qa"}:
            from coder_inference import require_local_model
            require_local_model(payload["ollama_url"],payload["model"])
        if kind == "inspect":
            result = {"inventory": repository.refresh(), "snapshot": repository.snapshot("Project baseline", parent=payload.get("revision_id", "")), "workspace": str(workspace)}
            result["verification"] = discover(repository)
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
            enforce_storage(store, payload)
            result["inventory"] = repository.refresh()
            result["verification"] = discover(repository)
            result["snapshot"] = repository.snapshot("Coding checkpoint", parent=payload.get("revision_id", ""))
        elif kind == "check":
            result = _run_check(store, operation_id, repository, payload)
        elif kind in ("accept", "qa"):
            if kind == "accept":
                observed = repository.snapshot("Acceptance source check",parent=payload["revision_id"])
                if observed["revision"] != payload["revision_id"]:
                    raise ValueError("Source changed after verification; inspect the checkpoint and rerun checks")
            instruction = ("Assess the requested behavior against the exact revision and executable evidence. Inspect missing source before deciding. "
                           "Return {\"accepted\":true|false,\"issues\":[{\"summary\":\"...\",\"file\":\"...\"}],\"summary\":\"...\"}. "
                           "Do not claim unexecuted behavior passed." if kind == "accept" else
                           "Answer using current source evidence and file:line citations. Return {\"answer\":\"...\"}. Never modify the project.")
            result = _read_only(store, operation_id, repository, instruction, payload.get("evidence", {}))
            result["revision_id"] = payload.get("revision_id", "")
        elif kind == "package":
            observed = repository.snapshot("Delivery source check",parent=payload["revision_id"])
            if observed["revision"] != payload["revision_id"]:
                raise ValueError("Source changed after Acceptance; verify the new revision before delivery")
            output = store.root / "artifacts" / f"{operation['job_id']}-{payload['revision_id']}.tar.gz"
            output.parent.mkdir(exist_ok=True)
            result = {**repository.archive(payload["revision_id"], output), "path": str(output), "revision_id": payload["revision_id"]}
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
        if operation.get("pid") == os.getpid():
            stop_descendants()
    if kind == "code" and "snapshot" not in result:
        try:
            result["snapshot"] = operation_repo(store, operation).snapshot("Interrupted coding checkpoint", parent=payload.get("revision_id", ""))
        except Exception:
            pass
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
