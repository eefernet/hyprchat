"""Small document subprocess supervisor, invoked through Codebox /command.

No listening socket and no dependency on Daedalus. All job identifiers are
server-generated. The parser/converter only sees its job directory in bwrap.
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/root/hyprchat-documents/jobs")
INSTALL = Path("/opt/hyprchat-documents")


def atomic(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def identity(pid):
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def limits():
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    os.umask(0o077)


def sandbox_command(job):
    cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--cap-drop", "ALL"]
    for name in ("/usr", "/bin", "/lib", "/lib64", "/etc/fonts", "/etc/libreoffice", "/etc/ld.so.cache", str(INSTALL)):
        if Path(name).exists():
            cmd += ["--ro-bind", name, name]
    return cmd + ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(job), "/work",
                  "--chdir", "/work", "--clearenv", "--setenv", "HOME", "/work",
                  "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LANG", "C.UTF-8",
                  str(INSTALL / "venv/bin/python"), str(INSTALL / "document_worker.py"), "execute"]


def supervise(job):
    child = None
    cancelled = False

    def stop(_sig, _frame):
        nonlocal cancelled
        cancelled = True
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    atomic(job / "pid.json", {"pid": os.getpid(), "identity": identity(os.getpid())})
    deadline = time.monotonic() + 300
    try:
        with (ROOT.parent / "office.lock").open("a") as lock:
            while True:
                if cancelled or (job / "cancel").exists():
                    raise InterruptedError("Document job cancelled")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Document worker queue timed out")
                    time.sleep(.2)
            atomic(job / "status.json", {"status": "running"})
            with (job / "worker.log").open("wb") as log:
                child = subprocess.Popen(sandbox_command(job), stdout=log, stderr=log,
                                         start_new_session=True, preexec_fn=limits)
                if cancelled or (job / "cancel").exists():
                    os.killpg(child.pid, signal.SIGKILL)
                try:
                    code = child.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                    raise TimeoutError("Document processing exceeded five minutes")
            if cancelled or (job / "cancel").exists():
                raise InterruptedError("Document job cancelled")
            if code or not (job / "result.json").is_file():
                raise RuntimeError("Document worker failed; inspect its local worker.log")
            result = json.loads((job / "result.json").read_text())
            terminal = {"status": "failed" if result.get("error") else "succeeded", **result}
    except Exception as e:
        terminal = {"status": "cancelled" if cancelled or isinstance(e, InterruptedError) else "failed", "error": str(e)}
    finally:
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        atomic(job / "status.json", terminal)


def main():
    action = sys.argv[1]
    if action == "execute":
        from document_runtime import execute
        try:
            request = json.loads(Path("/work/request.json").read_text())
            result = execute(request, "/work")
            for output in result.get("outputs", []):
                path = Path("/work") / output["path"]
                if path.stat().st_size > 50 * 1024**2:
                    raise ValueError("Document output exceeds 50 MB")
                output["size_bytes"] = path.stat().st_size
        except Exception as e:
            result = {"error": str(e)[:1000]}
        atomic(Path("/work/result.json"), result)
        return
    if action == "health":
        import importlib.util
        import shutil
        missing = [x for x in ("docx", "pptx", "openpyxl", "lxml", "PIL", "olefile") if not importlib.util.find_spec(x)]
        missing += [x for x in ("bwrap", "libreoffice") if not shutil.which(x)]
        print(json.dumps({"ready": not missing, "missing": missing, "version": 1}))
        return
    job_id = sys.argv[2]
    if not re.fullmatch(r"doc-[a-f0-9]{32}", job_id):
        raise ValueError("Invalid job identifier")
    job = ROOT / job_id
    if action == "start":
        job.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Exclusive start token makes retries idempotent across HTTP disconnects.
        try:
            token = os.open(job / "started", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(token)
        except FileExistsError:
            print(json.dumps({"status": "existing"}))
            return
        if not (job / "request.json").is_file():
            raise ValueError("Missing job request")
        if (job / "cancel").exists():
            atomic(job / "status.json", {"status": "cancelled"})
            print('{"status":"cancelled"}')
            return
        atomic(job / "status.json", {"status": "queued"})
        subprocess.Popen([sys.executable, __file__, "supervise", job_id],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
        print(json.dumps({"status": "queued"}))
    elif action == "supervise":
        supervise(job)
    elif action == "status":
        print((job / "status.json").read_text() if (job / "status.json").exists() else '{"status":"queued"}')
    elif action == "cancel":
        job.mkdir(mode=0o700, parents=True, exist_ok=True)
        (job / "cancel").touch()
        if (job / "pid.json").exists():
            pid = json.loads((job / "pid.json").read_text())
            if pid.get("identity") and identity(pid["pid"]) == pid["identity"]:
                try:
                    os.kill(pid["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if not (job / "started").exists():
            print('{"status":"cancelled"}')
        else:
            print((job / "status.json").read_text() if (job / "status.json").exists() else '{"status":"cancelling"}')
    elif action == "chunk":
        relative = sys.argv[3]
        path = (job / relative).resolve()
        if not path.is_relative_to(job.resolve()) or not path.is_file():
            raise ValueError("Invalid output path")
        offset = int(sys.argv[4])
        if offset < 0:
            raise ValueError("Invalid offset")
        with path.open("rb") as handle:
            handle.seek(offset)
            print(json.dumps({"data": base64.b64encode(handle.read(192 * 1024)).decode()}))
    else:
        raise ValueError("Unknown worker operation")


if __name__ == "__main__":
    main()
