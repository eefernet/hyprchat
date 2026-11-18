#!/usr/bin/env python3
"""
HyprChat Deployment Monitor
Watches project files and deploys changes to remote servers.
Saves server config after first setup so you never re-enter IPs.
"""

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime

# ── ANSI ──
RST = "\033[0m"
BLD = "\033[1m"
DIM = "\033[2m"
R   = "\033[31m"
G   = "\033[32m"
Y   = "\033[33m"
B   = "\033[34m"
M   = "\033[35m"
C   = "\033[36m"
W   = "\033[37m"
BG_R = "\033[41m"
BG_G = "\033[42m"
BG_B = "\033[44m"
BG_C = "\033[46m"
BG_M = "\033[45m"

# ── Paths ──
# Every glob and file read is anchored on the script's own folder, so the
# monitor behaves the same whatever directory it is started from. Watched
# paths are stored as repo-relative strings ("backend/main.py").
ROOT = Path(__file__).resolve().parent
CONFIG_FILE = str(ROOT / ".deploy_config.json")


def _abs(rel):
    """Absolute local path of a repo-relative watched path."""
    return str(ROOT / rel)


# ── Remote paths ──
REMOTE_BACKEND = "/opt/hyprchat/backend/"
REMOTE_FRONTEND = "/opt/hyprchat/frontend/dist/"
REMOTE_HEPHAESTUS = "/srv/hephaestus/releases/"
# The live HyprChat service runs from a staged release since 2026-10-20
# (/opt/hyprchat/.releases/<stamp>/, created by scripts/staged_release.py; the
# unit drop-in zz-hyprchat-release.conf points WorkingDirectory/ExecStart at it).
# Every REMOTE_* path above is written against the legacy root and is rebased
# onto the active release root at deploy time (see _resolve_hyprchat_root).
# /opt/hyprchat/data and /opt/hyprchat/.env stay fixed (HYPRCHAT_DATA_DIR +
# EnvironmentFile).
HYPRCHAT_ROOT = "/opt/hyprchat"
HYPRCHAT_SERVICE = "hyprchat"
_LIVE_ROOT = {"root": HYPRCHAT_ROOT, "release": False, "checked": False}
SEARXNG_PRIVACY_SCRIPT = "scripts/setup-searxng-privacy.sh"
HEALTH_PATH = "/api/health/live"
HEALTH_SECONDS = 30

# ── Watch rules ──
# backend/<these>/*.py are hot-patched and restart the service; seed_kb/ does not.
BACKEND_RESTART_DIRS = ("agents", "routes", "db", "tooling")
BACKEND_NO_RESTART_DIRS = ("seed_kb",)
# The HyprChat backend's own dependency files: a change needs a staged release
# (scripts/staged_release.py prepare/activate builds a new venv).
DEPENDENCY_FILES = ("backend/requirements.txt", "backend/requirements.lock.txt")
# Frontend build inputs outside frontend/src and frontend/public.
FRONTEND_CONFIG_FILES = (
    "frontend/index.html", "frontend/vite.config.js",
    "frontend/package.json", "frontend/package-lock.json",
)
DOCS_FILES = ("CHANGELOG.md", "README.md")
FRONTEND_LABEL = "Frontend (build)"
# Explicit labels; every other file gets a label from its folder.
LABELS = {
    "backend/main.py": "Main Server",
    "backend/config.py": "Config",
    "backend/database.py": "Database",
    "backend/tools.py": "Tools",
    "backend/route_policy.json": "Route Policy",
    "backend/requirements.txt": "Requirements",
    "backend/requirements.lock.txt": "Backend Lock",
    "backend/seed_kb/seed_coder_kb.py": "Coder Docs Seeder",
    "backend/seed_kb/coder_sources.py": "Coder Docs Sources",
    "CHANGELOG.md": "Changelog",
    "README.md": "README",
}
FOLDER_LABELS = {
    "agents": "Agents", "routes": "Routes", "db": "Database",
    "tooling": "Tooling", "seed_kb": "Coder Docs",
}
VM_LABEL = "Hephaestus VM"
RELEASE_LABEL = "Needs release"


def _rel_files(pattern):
    """Repo-relative paths of the files matching a glob under ROOT."""
    out = set()
    for p in ROOT.glob(pattern):
        if not p.is_file() or p.name.startswith("."):
            continue
        out.add(p.relative_to(ROOT).as_posix())
    return out


def _is_test_or_fixture(rel):
    name = rel.rsplit("/", 1)[-1]
    parts = rel.split("/")
    return (".test." in name or ".spec." in name or ".fixtures." in name
            or name.endswith(".fixtures.json")
            or any(p in ("__tests__", "__fixtures__", "fixtures", "tests") for p in parts[:-1]))


def frontend_files():
    """Every frontend build input: src/**, public/** (no tests/fixtures) + config."""
    files = set()
    for base in ("frontend/src", "frontend/public"):
        files |= {p for p in _rel_files(base + "/**/*") if not _is_test_or_fixture(p)}
    files |= {p for p in FRONTEND_CONFIG_FILES if (ROOT / p).is_file()}
    return files


def worker_files():
    """Execution source the Hephaestus VM receives only as a tested immutable release."""
    files = {p for p in _rel_files("backend/coder_*.py")
             if Path(p).stem not in {"coder_jobs", "coder_presentation"}}
    files |= _rel_files("backend/hephaestus_*.py")
    files |= _rel_files("backend/engine-*.txt")
    return files


def document_worker_files():
    return ({p for p in ("backend/document_worker.py", "backend/document_runtime.py") if (ROOT / p).is_file()}
            | _rel_files("backend/document-requirements*.txt"))


WORKER_SHARED = {"backend/context_policy.py"}
DOCUMENT_SHARED = {"backend/document_formats.py"}
# Filled by refresh_watch(); module-level so callers and tests can read them.
WORKER_FILES = set()
DOCUMENT_WORKER_FILES = set()
FRONTEND_SRC_FILES = set()
WATCHED = {}


def release_only_files():
    """Files the monitor never hot-patches (they ship in a release)."""
    return WORKER_FILES | WORKER_SHARED | DOCUMENT_WORKER_FILES | DOCUMENT_SHARED


def backend_files():
    """(path, remote_dir, restart) for every runtime backend file."""
    out = {}
    for p in _rel_files("backend/*.py") | _rel_files("backend/*.json"):
        out[p] = (REMOTE_BACKEND, True)
    for sub in BACKEND_RESTART_DIRS:
        for p in _rel_files(f"backend/{sub}/*.py"):
            out[p] = (f"{REMOTE_BACKEND}{sub}/", True)
    for sub in BACKEND_NO_RESTART_DIRS:
        for p in _rel_files(f"backend/{sub}/*.py"):
            out[p] = (f"{REMOTE_BACKEND}{sub}/", False)
    for p in DEPENDENCY_FILES:
        if (ROOT / p).is_file():
            out[p] = (REMOTE_BACKEND, True)
    return out


# ── Which release-only files the HyprChat backend itself imports ──
# A static trace from backend/main.py over every import statement (also the
# ones inside functions, and constant importlib.import_module/__import__
# calls). Cached per file by mtime, so re-tracing on every scan is cheap.
_IMPORT_CACHE = {}


def _module_file(dotted):
    """backend/<dotted>.py or backend/<dotted>/__init__.py, if it exists."""
    if not dotted:
        return None
    base = ROOT / "backend" / Path(*dotted.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand.relative_to(ROOT).as_posix()
    return None


def _file_imports(rel):
    """Backend files one file imports (resolved, repo-relative)."""
    path = ROOT / rel
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return set()
    key = str(path)
    hit = _IMPORT_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        _IMPORT_CACHE[key] = (mtime, set())
        return set()
    # Package that relative imports resolve against.
    pkg = Path(rel).parent.relative_to("backend").parts
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                names.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = pkg[:len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                base = ".".join([*anchor, *(node.module.split(".") if node.module else [])])
            else:
                base = node.module or ""
            if base:
                parts = base.split(".")
                names.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
            for a in node.names:
                if a.name != "*":
                    names.add(f"{base}.{a.name}" if base else a.name)
        elif isinstance(node, ast.Call) and node.args:
            fn = node.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            arg = node.args[0]
            if fname in ("import_module", "__import__") and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.add(arg.value)
    files = {f for f in (_module_file(n) for n in names) if f and f != rel}
    _IMPORT_CACHE[key] = (mtime, files)
    return files


def backend_import_closure(entry="backend/main.py"):
    """Every backend file reachable by import from the service entrypoint."""
    seen, todo = set(), [entry]
    while todo:
        rel = todo.pop()
        if rel in seen or not (ROOT / rel).is_file():
            continue
        seen.add(rel)
        todo.extend(_file_imports(rel) - seen)
    return seen


def release_split():
    """(vm_only, backend_imported) partition of the release-only files.

    vm_only: the HyprChat backend never imports them; they live on the
    Hephaestus VM only, so a batch just leaves them out.
    backend_imported: the backend imports them too, so a change to one must
    reach the HyprChat host in the same step as the code that uses it, which
    only a staged release does: the batch's backend part is held.
    """
    rel_only = release_only_files()
    used = backend_import_closure() & rel_only
    return rel_only - used, used


def _label(rel, rel_vm, rel_backend):
    if rel in LABELS:
        return LABELS[rel]
    if rel in rel_vm:
        return VM_LABEL
    if rel in rel_backend:
        return RELEASE_LABEL
    if rel.startswith("frontend/"):
        return FRONTEND_LABEL
    parts = rel.split("/")
    if parts[0] == "backend" and len(parts) == 3:
        return FOLDER_LABELS.get(parts[1], parts[1].title())
    if parts[0] == "backend":
        return "Backend"
    return rel


def watch_list():
    """Fresh {path: (label, remote_dir, needs_restart)} from the current tree."""
    vm_only, used = release_split()
    out = {}
    for p, (remote, restart) in backend_files().items():
        if p in vm_only:
            remote, restart = REMOTE_HEPHAESTUS, False
        out[p] = (_label(p, vm_only, used), remote, restart)
    for p in vm_only:  # non-.py VM files (engine/document requirement locks)
        out.setdefault(p, (VM_LABEL, REMOTE_HEPHAESTUS, False))
    for p in FRONTEND_SRC_FILES:
        out[p] = (FRONTEND_LABEL, REMOTE_FRONTEND, False)
    for p in DOCS_FILES:
        if (ROOT / p).is_file():
            out[p] = (_label(p, vm_only, used), HYPRCHAT_ROOT + "/", False)
    return dict(sorted(out.items()))


def refresh_watch():
    """Re-glob the tree. Updates the module-level sets/dict in place."""
    new_worker = worker_files()
    new_doc = document_worker_files()
    new_fe = frontend_files()
    WORKER_FILES.clear(); WORKER_FILES.update(new_worker)
    DOCUMENT_WORKER_FILES.clear(); DOCUMENT_WORKER_FILES.update(new_doc)
    FRONTEND_SRC_FILES.clear(); FRONTEND_SRC_FILES.update(new_fe)
    fresh = watch_list()
    WATCHED.clear(); WATCHED.update(fresh)
    return WATCHED


def is_dependency(rel):
    return rel in DEPENDENCY_FILES


def classify_batch(paths, holding=False):
    """Split a batch of changed paths.

    Returns {"deploy", "vm_only", "hold", "blockers"} (lists, input order):
    - vm_only: Hephaestus-only files, left out (ship with the VM release);
    - blockers: release-only files the backend imports + dependency files;
    - hold: when there is a blocker (or an earlier hold is still open,
      `holding`), the blockers and every other backend/ file of the batch;
    - deploy: the rest (frontend, docs, and backend when nothing blocks).
    """
    vm_only, used = release_split()
    out = {"deploy": [], "vm_only": [], "hold": [], "blockers": []}
    blockers = [p for p in paths if p in used or is_dependency(p)]
    hold_backend = bool(blockers) or holding
    for p in paths:
        if p in vm_only:
            out["vm_only"].append(p)
        elif p in blockers or (hold_backend and p.startswith("backend/")):
            out["hold"].append(p)
        else:
            out["deploy"].append(p)
    out["blockers"] = blockers
    return out


# Backend files waiting for a staged release: {path: digest at hold time}.
# Not re-baselined, so they are still "changed" against what is live, but a
# held file is not offered again until its content changes.
HELD = {}


def blocking_holds(held=None):
    held = HELD if held is None else held
    _vm, used = release_split()
    return sorted(p for p in held if p in used or is_dependency(p))


refresh_watch()

CHECK_INTERVAL = 1


def file_digest(path):
    """SHA-256 of a watched file's contents (repo-relative or absolute); None if unreadable."""
    try:
        h = hashlib.sha256()
        with open(_abs(path), "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _exists(rel):
    return os.path.exists(_abs(rel))


def _mtime(rel):
    try:
        return os.path.getmtime(_abs(rel))
    except OSError:
        return 0

# ── Terminal helpers ──

def cols():
    return shutil.get_terminal_size((60, 24)).columns

def clear():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

def bar(char="─", color=DIM):
    w = min(cols(), 60)
    return f"{color}{char * w}{RST}"

def box(lines, color=C, width=None):
    w = width or min(cols(), 58)
    inner = w - 4
    out = [f"{BLD}{color}╔{'═' * (w - 2)}╗{RST}"]
    for line in lines:
        stripped = line
        # rough visible length (strip ANSI)
        vis = len(re.sub(r'\033\[[0-9;]*m', '', stripped))
        pad = inner - vis
        out.append(f"{BLD}{color}║{RST} {stripped}{' ' * max(0, pad)} {BLD}{color}║{RST}")
    out.append(f"{BLD}{color}╚{'═' * (w - 2)}╝{RST}")
    return "\n".join(out)


# ── Config persistence ──

def normalize_config(cfg):
    """Accept both old deploy_monitor keys and the AGENTS.md template keys."""
    if not cfg:
        return cfg
    if "hephaestus" in cfg and "codebox" not in cfg:
        cfg["codebox"] = {**cfg["hephaestus"], "runtime": "hephaestus"}
    for key in ("hyprchat", "codebox", "searxng"):
        server = cfg.get(key)
        if not isinstance(server, dict):
            continue
        if not server.get("ip") and server.get("host"):
            server["ip"] = server["host"]
        if not server.get("pass") and server.get("password"):
            server["pass"] = server["password"]
        server.setdefault("user", "root")
        if key == "searxng":
            server.setdefault("dev_ip", server.get("dev") or server.get("dev_host") or "")
    return cfg


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return normalize_config(json.load(f))
        except Exception:
            pass
    return None

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def prompt_server(label, default_ip="", default_user="root", default_pass=""):
    """Prompt for a single server's connection info."""
    print(f"  {BLD}{label}{RST}")
    ip   = input(f"    {DIM}IP address{RST} [{C}{default_ip}{RST}]: ").strip() or default_ip
    user = input(f"    {DIM}User{RST}       [{C}{default_user}{RST}]: ").strip() or default_user
    pw   = input(f"    {DIM}Password{RST}   [{C}{'*' * len(default_pass) if default_pass else ''}{RST}]: ").strip() or default_pass
    print()
    return {"ip": ip, "user": user, "pass": pw}


def prompt_optional_searxng(default=None):
    """Prompt for the optional SearXNG host. Empty on first setup means skip."""
    default = default or {}
    has_default = bool(default.get("ip") or default.get("host"))
    prompt = "[Y/n]" if has_default else "[y/N]"
    choice = input(
        f"  {BLD}{Y}Configure optional SearXNG privacy host?{RST} {prompt} > "
    ).strip().lower()
    if not choice and not has_default:
        print()
        return None
    if choice in ("n", "no", "skip", "s"):
        print()
        return None

    sx = prompt_server(
        "SearXNG Server (existing install)",
        default_ip=default.get("ip") or default.get("host", ""),
        default_user=default.get("user", "root"),
        default_pass=default.get("pass") or default.get("password", ""),
    )
    if not sx.get("ip"):
        return None
    dev_default = default.get("dev_ip") or default.get("dev", "")
    dev_ip = input(
        f"    {DIM}Optional dev IP allowed to use :8888{RST} [{C}{dev_default}{RST}]: "
    ).strip() or dev_default
    sx["dev_ip"] = dev_ip
    print()
    return sx


def setup_servers():
    """Interactive first-time setup or reconfigure."""
    clear()
    print()
    print(box([
        f"{BLD}    HyprChat Deploy Setup{RST}",
        f"{DIM}  Configure your server connections{RST}",
    ], C))
    print()

    cfg = normalize_config(load_config() or {})
    hypr_def = cfg.get("hyprchat", {})
    cb_def   = cfg.get("codebox", {})
    sx_def   = cfg.get("searxng", {})

    print(f"  {BLD}{Y}HyprChat Server{RST} {DIM}(backend + frontend){RST}")
    hypr = prompt_server("",
        default_ip=hypr_def.get("ip") or hypr_def.get("host", ""),
        default_user=hypr_def.get("user", "root"),
        default_pass=hypr_def.get("pass") or hypr_def.get("password", ""))

    print(f"  {BLD}{M}Hephaestus Docker VM{RST} {DIM}(sandbox execution){RST}")
    cb = {**cb_def, **prompt_server("",
        default_ip=cb_def.get("ip") or cb_def.get("host", ""),
        default_user=cb_def.get("user", "root"),
        default_pass=cb_def.get("pass") or cb_def.get("password", ""))}
    if cb_def.get("runtime") == "hephaestus":
        cb["runtime"] = "hephaestus"

    sx = prompt_optional_searxng(sx_def)

    cfg = {**cfg, "hyprchat": hypr, "codebox": cb}
    if cb.get("runtime") == "hephaestus":
        cfg["hephaestus"] = dict(cb)
    if sx:
        cfg["searxng"] = sx
    else:
        cfg.pop("searxng", None)
    save_config(cfg)
    print(f"  {G}Config saved to {CONFIG_FILE}{RST}")
    print()
    return cfg


# ── SCP / SSH with sshpass ──

# When a password is supplied we MUST stop ssh/scp from offering ssh-agent
# keys first: every offered key counts against the server's MaxAuthTries
# (default 6), so on a host without our key installed a loaded agent can burn
# all attempts on pubkeys and get "Permission denied (publickey,password)"
# before the password is ever tried — intermittently, depending on agent
# state and how many connections a deploy made just before. Forcing
# password-only auth makes the configured password the first and only method.
_PW_AUTH_OPTS = [
    "-o", "PubkeyAuthentication=no",
    "-o", "PreferredAuthentications=password",
    "-o", "NumberOfPasswordPrompts=1",
]

_SSH_RETRYABLE_ERRORS = (
    "Permission denied (publickey,password)",
    "Connection reset by",
    "Connection closed by",
    "kex_exchange_identification",
)


def _ssh_retryable_failure(stdout, stderr):
    msg = f"{stdout or ''}\n{stderr or ''}"
    return any(token in msg for token in _SSH_RETRYABLE_ERRORS)


def scp(local, remote_host, remote_path, user, password):
    """Copy a file to remote via scp. Returns (ok, msg)."""
    dest = f"{user}@{remote_host}:{remote_path}"
    if password:
        cmd = [
            "sshpass", "-p", password,
            "scp", "-o", "StrictHostKeyChecking=no", *_PW_AUTH_OPTS, "-q",
            local, dest
        ]
    else:
        cmd = ["scp", "-o", "StrictHostKeyChecking=no", "-q", local, dest]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, ""
        return False, r.stderr.strip()
    except FileNotFoundError:
        # sshpass not installed, fall back to plain scp
        cmd2 = ["scp", "-o", "StrictHostKeyChecking=no", "-q", local, dest]
        r = subprocess.run(cmd2, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, ""
        return False, r.stderr.strip()
    except Exception as e:
        return False, str(e)


def scp_recursive(local, remote_host, remote_path, user, password, timeout=120):
    """Recursively copy a directory's contents to remote via `scp -r`.

    `local` should be a directory (use a trailing '/.' to copy its contents
    into remote_path). Returns (ok, msg). Used to ship the built frontend
    dist/ tree (index.html + hashed assets/) in one shot.
    """
    dest = f"{user}@{remote_host}:{remote_path}"
    base = ["scp", "-o", "StrictHostKeyChecking=no", "-q", "-r", local, dest]
    try:
        cmd = (["sshpass", "-p", password,
                "scp", "-o", "StrictHostKeyChecking=no", *_PW_AUTH_OPTS,
                "-q", "-r", local, dest]) if password else base
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return True, ""
        return False, r.stderr.strip()
    except FileNotFoundError:
        r = subprocess.run(base, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return True, ""
        return False, r.stderr.strip()
    except Exception as e:
        return False, str(e)


def ssh_cmd(host, user, password, command, timeout=30, attempts=3):
    """Run a command on remote via ssh. Returns (ok, stdout, stderr)."""
    if password:
        cmd = [
            "sshpass", "-p", password,
            "ssh", "-o", "StrictHostKeyChecking=no", *_PW_AUTH_OPTS,
            f"{user}@{host}", command
        ]
    else:
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", f"{user}@{host}", command]
    try:
        last = None
        for attempt in range(max(1, attempts)):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            out = r.stdout.strip()
            err = r.stderr.strip()
            if r.returncode == 0:
                return True, out, err
            last = (out, err)
            if not password or attempt >= attempts - 1 or not _ssh_retryable_failure(out, err):
                break
            time.sleep(1 + attempt)
        out, err = last or ("", "")
        return False, out, err
    except FileNotFoundError:
        if password:
            return False, "", "sshpass not installed; install sshpass or configure SSH keys and remove the password for this host"
        cmd2 = ["ssh", "-o", "StrictHostKeyChecking=no", f"{user}@{host}", command]
        r = subprocess.run(cmd2, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return False, "", str(e)


# ── Deploy logic ──

def _show_journal(target, unit, lines=20):
    """Print the last N journalctl lines for a unit on the given target."""
    ok, out, _ = ssh_cmd(target["ip"], target["user"], target["pass"],
        f"journalctl -u {unit} -n {lines} --no-pager 2>&1")
    if ok and out:
        print(f"  {DIM}── journalctl -u {unit} (last {lines} lines) ──{RST}")
        for line in out.splitlines():
            print(f"  {DIM}{line}{RST}")


def _server_configured(server):
    return isinstance(server, dict) and bool(server.get("ip"))


def _resolve_hyprchat_root(hypr):
    """Return the directory the live hyprchat service runs from.

    Reads the unit's WorkingDirectory: `<root>/backend`. With the staged-release
    drop-in that is /opt/hyprchat/.releases/<stamp>/backend; without it (or when
    the lookup fails) the legacy /opt/hyprchat root is used. Resolved once per
    deploy batch and shown in the monitor header.
    """
    ok, out, _err = ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"],
                            f"systemctl show {HYPRCHAT_SERVICE} -p WorkingDirectory --value",
                            timeout=20)
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    workdir = lines[-1] if ok and lines else ""
    if workdir.endswith("/backend") and "/.releases/" in workdir:
        _LIVE_ROOT.update(root=workdir[:-len("/backend")], release=True, checked=True)
    else:
        _LIVE_ROOT.update(root=HYPRCHAT_ROOT, release=False, checked=True)
    return _LIVE_ROOT["root"]


def _rebase(remote_dir, root):
    """Map a legacy /opt/hyprchat/... path onto the active release root."""
    if root == HYPRCHAT_ROOT:
        return remote_dir
    if remote_dir.rstrip("/") == HYPRCHAT_ROOT:
        return root + "/"
    if remote_dir.startswith(HYPRCHAT_ROOT + "/"):
        return root + remote_dir[len(HYPRCHAT_ROOT):]
    return remote_dir


def _hyprchat_host_ready(hypr, root=HYPRCHAT_ROOT):
    """An install the monitor may hot-patch: service user, tree, data, .env and
    the venv interpreter the service runs (<root>/venv/bin/python). The monitor
    never creates an install; scripts/deploy.sh does."""
    cmd = (
        "id -u hyprchat >/dev/null 2>&1 && "
        f"test -d {shlex.quote(root + '/backend')} && "
        f"test -d {shlex.quote(root + '/frontend/dist')} && "
        "test -d /opt/hyprchat/data && "
        "test -f /opt/hyprchat/.env && "
        f"test -x {shlex.quote(root + '/venv/bin/python')}"
    )
    return ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"], cmd, timeout=20)


def _health_poll_cmd(seconds=None):
    """Shell run on the HyprChat host: poll /api/health/live for `seconds`.

    The address comes from /opt/hyprchat/.env (HOST/PORT, 0.0.0.0 → 127.0.0.1),
    because the service may bind a single interface (e.g. a Tailscale IP)."""
    seconds = HEALTH_SECONDS if seconds is None else int(seconds)
    return (
        "env=/opt/hyprchat/.env\n"
        "val() { sed -n \"s/^$1=//p\" \"$env\" 2>/dev/null | tail -n 1 | sed 's/[[:space:]]#.*$//' | tr -d \"\\\"' \"; }\n"
        "host=$(val HOST); port=$(val PORT)\n"
        "case \"$host\" in ''|0.0.0.0|::) host=127.0.0.1;; esac\n"
        "port=${port:-8000}\n"
        f"url=\"http://$host:$port{HEALTH_PATH}\"\n"
        f"end=$(( $(date +%s) + {seconds} ))\n"
        "while [ \"$(date +%s)\" -lt \"$end\" ]; do\n"
        "  if curl -fsS --max-time 3 \"$url\" >/dev/null 2>&1; then echo \"healthy $url\"; exit 0; fi\n"
        "  sleep 1\n"
        "done\n"
        "echo \"no answer from $url after " + str(seconds) + " s\"\n"
        "exit 1\n"
    )


def _wait_healthy(hypr, seconds=None):
    """(ok, message) after polling the live endpoint on the host."""
    seconds = HEALTH_SECONDS if seconds is None else seconds
    ok, out, err = ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"],
                           _health_poll_cmd(seconds), timeout=seconds + 30, attempts=1)
    return ok, (out or err or "").strip()


def _hyprchat_data_permissions_cmd():
    return (
        "mkdir -p /opt/hyprchat/data/uploads/avatars /opt/hyprchat/data/tools "
        "/opt/hyprchat/data/knowledge_bases /opt/hyprchat/data/chroma_db "
        "/opt/hyprchat/data/comfy_workflows /opt/hyprchat/data/sandbox/outputs "
        "/opt/hyprchat/data/sandbox/workspace /opt/hyprchat/data/sandbox/venv\n"
        "if id -u hyprchat >/dev/null 2>&1; then\n"
        "  chown -R hyprchat:hyprchat /opt/hyprchat/data 2>/dev/null || chown -R hyprchat /opt/hyprchat/data 2>/dev/null || true\n"
        "  chmod -R u+rwX /opt/hyprchat/data 2>/dev/null || true\n"
        "fi\n"
    )


def _repair_hyprchat_data_permissions(hypr):
    return ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"], _hyprchat_data_permissions_cmd(), timeout=90)


def _set_hyprchat_env(hypr, key, value):
    """Set or replace one key in /opt/hyprchat/.env on the HyprChat host."""
    cmd = (
        "set -u\n"
        "env=/opt/hyprchat/.env\n"
        f"key={shlex.quote(key)}\n"
        f"value={shlex.quote(value)}\n"
        "touch \"$env\"\n"
        "cp -a \"$env\" \"$env.bak.$(date +%Y%m%d%H%M%S)\" 2>/dev/null || true\n"
        "tmp=\"${env}.tmp.$$\"\n"
        "awk -v k=\"$key\" -v v=\"$value\" 'BEGIN{done=0} "
        "$0 ~ \"^\" k \"=\" {print k \"=\" v; done=1; next} "
        "{print} END{if(!done) print k \"=\" v}' \"$env\" > \"$tmp\"\n"
        "mv \"$tmp\" \"$env\"\n"
        "chmod 640 \"$env\" 2>/dev/null || true\n"
        "chown hyprchat:hyprchat \"$env\" 2>/dev/null || chown hyprchat \"$env\" 2>/dev/null || true\n"
    )
    return ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"], cmd, timeout=30)


def _restart_hyprchat(hypr):
    _repair_hyprchat_data_permissions(hypr)
    ok, out, err = ssh_cmd(
        hypr["ip"], hypr["user"], hypr["pass"],
        "systemctl restart hyprchat 2>&1", timeout=150,
    )
    if not ok:
        return False, out, err
    healthy, msg = _wait_healthy(hypr)
    return healthy, msg, ""


def _run_first_time_searxng_setup(cfg):
    """Run optional SearXNG privacy setup and wire HyprChat to the proxy if active."""
    sx = cfg.get("searxng")
    if not _server_configured(sx):
        print(f"  {DIM}No SearXNG host configured; skipping privacy setup.{RST}")
        return

    hypr = cfg["hyprchat"]
    script_path = _abs(SEARXNG_PRIVACY_SCRIPT)
    if not os.path.exists(script_path):
        print(f"  {Y}!{RST} SearXNG privacy script missing: {SEARXNG_PRIVACY_SCRIPT}")
        return

    print()
    print(f"  {Y}\u25b6{RST} Running first-time SearXNG privacy setup...")
    remote_script = f"/root/{os.path.basename(SEARXNG_PRIVACY_SCRIPT)}"
    ok, err = scp(script_path, sx["ip"], "/root/", sx["user"], sx.get("pass", ""))
    if not ok:
        print(f"  {Y}!{RST} Could not copy SearXNG setup script: {err[:300]}")
        return

    env_parts = [
        f"HYPRCHAT_IP={shlex.quote(hypr['ip'])}",
        f"DEV_IP={shlex.quote(sx.get('dev_ip', ''))}",
        "PROTON_OPENVPN=auto",
    ]
    cmd = " ".join(env_parts + ["bash", shlex.quote(remote_script)])
    ok, out, err = ssh_cmd(sx["ip"], sx["user"], sx.get("pass", ""), cmd, timeout=300)
    setup_output = "\n".join(x for x in (out, err) if x).strip()
    if setup_output:
        for line in setup_output.splitlines()[-24:]:
            print(f"  {DIM}{line}{RST}")
    if not ok:
        print(f"  {Y}!{RST} SearXNG privacy setup failed; HyprChat deploy remains usable.")
        return

    env_changed = False
    searxng_url = f"http://{sx['ip']}:8888"
    ok_env, env_out, env_err = _set_hyprchat_env(hypr, "SEARXNG_URL", searxng_url)
    if ok_env:
        env_changed = True
        print(f"  {G}\u2713{RST} HyprChat SEARXNG_URL set to {searxng_url}")
    else:
        print(f"  {Y}!{RST} Could not set SEARXNG_URL: {(env_err or env_out)[:300]}")

    proxy_active = any(line.strip() == "HYPRCHAT_PROXY_ACTIVE=1" for line in setup_output.splitlines())
    if proxy_active:
        proxy_url = f"http://{sx['ip']}:8899"
        ok_env, env_out, env_err = _set_hyprchat_env(hypr, "HYPRCHAT_OUTBOUND_PROXY", proxy_url)
        if ok_env:
            env_changed = True
            print(f"  {G}\u2713{RST} HyprChat outbound proxy set to {proxy_url}")
        else:
            print(f"  {Y}!{RST} Could not set HYPRCHAT_OUTBOUND_PROXY: {(env_err or env_out)[:300]}")
    else:
        print(f"  {Y}!{RST} SearXNG proxy was not activated; outbound proxy was not set.")

    if env_changed:
        print(f"  {Y}\u25b6{RST} Restarting HyprChat after SearXNG env update...")
        ok_restart, restart_out, restart_err = _restart_hyprchat(hypr)
        if ok_restart:
            print(f"  {G}\u2713{RST} HyprChat restarted")
        else:
            print(f"  {Y}!{RST} HyprChat restart check failed: {(restart_err or restart_out)[:300]}")










def _deploy_target(filepath, remote_dir, hypr, cb, root=HYPRCHAT_ROOT):
    """Resolve application hotfix paths; runtime files require a full release."""
    if filepath in release_only_files() or is_dependency(filepath):
        raise ValueError(f"{filepath} ships only in a release, never as a hot patch")
    return hypr, _rebase(remote_dir, root)


def _ensure_remote_dir(target, remote_dir):
    """Create the destination directory before scp, useful for new agents."""
    ok, _out, err = ssh_cmd(
        target["ip"],
        target["user"],
        target["pass"],
        f"mkdir -p {shlex.quote(remote_dir)}",
        timeout=20,
    )
    return ok, err






def _build_and_deploy_frontend(target, root=HYPRCHAT_ROOT):
    """Build the Vite frontend locally, then sync the whole dist/ to the server.

    The frontend now has a build step: editing files under frontend/src/ (or the
    Vite config/entry) requires `npm run build`, which emits index.html plus
    content-hashed assets/. The hashes change every build, so stale remote assets
    are wiped first to avoid accumulation. The HyprChat host stays dumb — it just
    serves frontend/dist/ as static files; no Node is installed there.
    """
    fe = _abs("frontend")
    npm = shutil.which("npm")
    if not npm:
        return False, "npm not found on the dev machine — cannot build frontend"
    try:
        build = subprocess.run(
            [npm, "run", "build"], cwd=fe,
            capture_output=True, text=True, timeout=600,
        )
    except Exception as e:
        return False, f"npm run build failed to launch: {e}"
    if build.returncode != 0:
        return False, "npm run build failed:\n" + (build.stderr or build.stdout or "")[-1000:]

    dist = os.path.join(fe, "dist")
    if not os.path.isdir(dist):
        return False, "build produced no dist/ directory"

    # Upload to a staging dir, then swap. Wiping the live assets/ BEFORE the
    # copy meant a failed scp left the server with no frontend at all (and the
    # wipe→copy window served index.html with missing chunks). The previous
    # dist is kept beside it as dist.prev (one copy) for a quick rollback.
    remote_dist = _rebase(REMOTE_FRONTEND, root).rstrip("/")
    staging = remote_dist + ".new"
    previous = remote_dist + ".prev"
    ssh_cmd(
        target["ip"], target["user"], target["pass"],
        f"rm -rf {shlex.quote(staging)} && mkdir -p {shlex.quote(staging)}",
        timeout=30,
    )
    ok, err = scp_recursive(dist + "/.", target["ip"], staging + "/",
                            target["user"], target["pass"])
    if not ok:
        return False, err
    swap_ok, _out, swap_err = ssh_cmd(
        target["ip"], target["user"], target["pass"],
        f"rm -rf {shlex.quote(previous)} && "
        f"{{ test -d {shlex.quote(remote_dist)} && mv {shlex.quote(remote_dist)} {shlex.quote(previous)} || true; }} && "
        f"mv {shlex.quote(staging)} {shlex.quote(remote_dist)}",
        timeout=30,
    )
    return swap_ok, swap_err


def _cleanup_staged_tmp(entries):
    """Best-effort removal of staged .deploy-tmp files, batched per host."""
    by_host = {}
    for e in entries:
        by_host.setdefault(e["target"]["ip"], []).append(e)
    for host_entries in by_host.values():
        target = host_entries[0]["target"]
        for i in range(0, len(host_entries), 40):
            chunk = host_entries[i:i + 40]
            rm_cmd = "rm -f " + " ".join(shlex.quote(e["tmp"]) for e in chunk)
            ssh_cmd(target["ip"], target["user"], target["pass"], rm_cmd, timeout=30)


PREV_SUFFIX = ".deploy-prev"


def _activate_cmd(entries):
    """Keep the live copy as <final>.deploy-prev, then move the staged file in."""
    steps = []
    for e in entries:
        final, tmp, prev = (shlex.quote(e["final"]), shlex.quote(e["tmp"]),
                            shlex.quote(e["final"] + PREV_SUFFIX))
        steps.append(f"{{ rm -f {prev}; if [ -e {final} ]; then cp -p {final} {prev}; fi; }} "
                     f"&& mv -f {tmp} {final}")
    return " && ".join(steps)


def _restore_cmd(entries):
    """Put each <final>.deploy-prev back; a file that was new is removed."""
    steps = []
    for e in entries:
        final, prev, tmp = (shlex.quote(e["final"]), shlex.quote(e["final"] + PREV_SUFFIX),
                            shlex.quote(e["tmp"]))
        steps.append(f"if [ -e {prev} ]; then mv -f {prev} {final}; else rm -f {final}; fi; rm -f {tmp}")
    return "; ".join(steps)


def _by_host(entries):
    out = {}
    for e in entries:
        out.setdefault(e["target"]["ip"], []).append(e)
    return out.values()


def _run_chunks(entries, make_cmd, timeout=60):
    """Run make_cmd(chunk) per host in chunks of 40. Returns (ok, err)."""
    for host_entries in _by_host(entries):
        target = host_entries[0]["target"]
        for i in range(0, len(host_entries), 40):
            chunk = host_entries[i:i + 40]
            ok, _out, err = ssh_cmd(target["ip"], target["user"], target["pass"],
                                    make_cmd(chunk), timeout=timeout)
            if not ok:
                return False, err
    return True, ""


def _restore_previous(entries):
    return _run_chunks(entries, _restore_cmd)


def _discard_previous(entries):
    return _run_chunks(entries, lambda chunk: "rm -f " + " ".join(
        shlex.quote(e["final"] + PREV_SUFFIX) for e in chunk), timeout=30)


def _restart_and_check(hypr):
    """Restart the service and poll /api/health/live. Returns (ok, message)."""
    ok, out, err = ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"],
                           "systemctl restart hyprchat 2>&1", timeout=150)
    if not ok:
        ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"],
                "systemctl reset-failed hyprchat 2>/dev/null || true; systemctl start hyprchat 2>&1",
                timeout=90)
    healthy, msg = _wait_healthy(hypr)
    if not ok and not healthy:
        msg = f"restart failed: {(err or out)[:200]}; {msg}"
    return healthy, msg


def _print_split(plan):
    """Explain the files of a batch that are not hot-patched."""
    if plan["vm_only"]:
        print(f"  {M}●{RST} Left out ({len(plan['vm_only'])}), Hephaestus VM only: "
              f"{DIM}{', '.join(plan['vm_only'])}{RST}")
        print(f"    {DIM}They ship with the Hephaestus release installer (docs/hephaestus.md).{RST}")
    if plan["hold"]:
        blockers = plan["blockers"] or blocking_holds()
        print(f"  {Y}●{RST} Held ({len(plan['hold'])}), needs a staged release: "
              f"{DIM}{', '.join(plan['hold'])}{RST}")
        print(f"    {DIM}Blocked by {', '.join(blockers)}: the backend imports release-only files or "
              f"its dependencies changed.{RST}")
        print(f"    {DIM}Ship with scripts/staged_release.py prepare/activate, then press c. "
              f"Frontend changes still deploy.{RST}")


def deploy_changes(changed, cfg, plan=None):
    """Deploy one batch of changed files; returns {path: digest actually sent}.

    The batch is split first (classify_batch): Hephaestus-only files are left
    out, and when a release-only file the backend imports or a dependency file
    is in it, every backend file is held for a staged release; the rest
    deploys. Backend files deploy in two phases: every file is staged to
    `<final>.deploy-tmp`, and only when the whole batch staged cleanly does a
    per-host batched `mv` flip them live, keeping each replaced file as
    `<final>.deploy-prev`. After a restart /api/health/live is polled for
    HEALTH_SECONDS; when it does not answer, the previous files are put back
    and the service restarted again.
    """
    changed = list(changed)
    info_by_path = dict(changed)
    if plan is None:
        plan = classify_batch([p for p, _ in changed], holding=bool(blocking_holds()))
    hypr = cfg["hyprchat"]
    cb = cfg.get("codebox") or {}
    pushed = {}  # filepath -> sha256 of the content actually sent

    print()
    _print_split(plan)
    todo = [(p, info_by_path[p]) for p in plan["deploy"] if p in info_by_path]
    if not todo:
        print()
        print(f"  {DIM}Nothing in this batch can be hot-patched.{RST}")
        input(f"\n  {DIM}Press Enter to continue...{RST}")
        return pushed

    print()
    print(f"  {Y}▶{RST} Checking the HyprChat install...")
    root = _resolve_hyprchat_root(hypr)
    if _LIVE_ROOT["release"]:
        print(f"  {G}✓{RST} Live HyprChat release: {DIM}{root}{RST}")
    else:
        print(f"  {Y}!{RST} No staged release active; using the install at {DIM}{root}{RST}")
    ready, _out, ready_err = _hyprchat_host_ready(hypr, root)
    if not ready:
        print(f"  {R}✗{RST} No working HyprChat install at {root} "
              f"(service user, backend/, frontend/dist, data/, .env, venv/bin/python).")
        if ready_err:
            print(f"    {DIM}{ready_err[:300]}{RST}")
        print(f"    {DIM}Install a server with scripts/deploy.sh; the monitor only updates an existing install.{RST}")
        input(f"\n  {DIM}Press Enter to continue...{RST}")
        return pushed
    print(f"  {G}✓{RST} HyprChat install ready")

    results = []       # (label, filepath, ok, err)
    frontend_built = False
    staged = []        # entries staged and awaiting activation
    stage_failed = []  # (label, filepath, err)
    ensured_dirs = set()
    for filepath, (label, remote_dir, restart_flag) in todo:
        # Frontend source changes are handled by a single build + dist/ sync,
        # regardless of how many src files changed in this batch.
        if filepath in FRONTEND_SRC_FILES:
            if frontend_built:
                continue
            frontend_built = True
            # Digest every changed src file BEFORE the build reads them, so all
            # batch members re-baseline (not just the one that triggered the build).
            fe_digests = {fp2: file_digest(fp2) for fp2, _i in todo if fp2 in FRONTEND_SRC_FILES}
            ok, err = _build_and_deploy_frontend(hypr, root)
            results.append((FRONTEND_LABEL, f"{len(fe_digests)} frontend file(s)", ok, err))
            if ok:
                pushed.update({fp2: d2 for fp2, d2 in fe_digests.items() if d2 is not None})
            continue

        target, remote_dir = _deploy_target(filepath, remote_dir, hypr, cb, root)
        dir_key = (target["ip"], remote_dir)
        if dir_key not in ensured_dirs:
            dir_ok, dir_err = _ensure_remote_dir(target, remote_dir)
            ensured_dirs.add(dir_key)
            if not dir_ok:
                stage_failed.append((label, filepath, f"Could not create remote dir {remote_dir}: {dir_err}"))
                continue

        final = remote_dir.rstrip("/") + "/" + os.path.basename(filepath)
        tmp = final + ".deploy-tmp"
        digest = file_digest(filepath)
        ok, err = scp(_abs(filepath), target["ip"], tmp, target["user"], target["pass"])
        if ok:
            staged.append({"filepath": filepath, "label": label, "target": target,
                           "tmp": tmp, "final": final, "digest": digest,
                           "restart_flag": restart_flag})
        else:
            stage_failed.append((label, filepath, err))

    activated = []
    restart_msg = None
    if stage_failed:
        # Abort the whole backend batch: the live tree stays untouched,
        # staged tmp files are removed, and no digests are recorded.
        results += [(label, fp, False, err) for label, fp, err in stage_failed]
        results += [(e["label"], e["filepath"], False, "staged ok but batch aborted, tmp removed")
                    for e in staged]
        _cleanup_staged_tmp(staged)
    elif staged:
        ok, err = _run_chunks(staged, _activate_cmd)
        if not ok:
            _restore_previous(staged)
            results += [(e["label"], e["filepath"], False, f"activation failed, previous files restored: {err}")
                        for e in staged]
        else:
            activated = staged

    needs_restart = any(e["restart_flag"] for e in activated)
    rolled_back = False
    if needs_restart:
        print()
        print(f"  {Y}▶{RST} Repairing HyprChat data permissions...")
        ok_perm, _, err_perm = _repair_hyprchat_data_permissions(hypr)
        if not ok_perm:
            print(f"  {Y}!{RST} Data permission repair may have failed: {err_perm}")
        print(f"  {Y}▶{RST} Restarting hyprchat, waiting up to {HEALTH_SECONDS} s for {HEALTH_PATH}...")
        healthy, restart_msg = _restart_and_check(hypr)
        if not healthy:
            rolled_back = True
            print(f"  {R}✗{RST} Not healthy: {restart_msg}")
            _show_journal(hypr, "hyprchat")
            print(f"  {Y}▶{RST} Restoring the previous backend files...")
            ok_restore, err_restore = _restore_previous(activated)
            healthy2, msg2 = _restart_and_check(hypr)
            if ok_restore and healthy2:
                print(f"  {G}✓{RST} Previous files restored, service healthy again")
            else:
                print(f"  {R}✗{RST} Rollback incomplete: {(err_restore or msg2)[:300]}")
                _show_journal(hypr, "hyprchat")
            results += [(e["label"], e["filepath"], False, "rolled back: service not healthy")
                        for e in activated]
    if activated and not rolled_back:
        _discard_previous(activated)
        for e in activated:
            results.append((e["label"], e["filepath"], True, ""))
            if e["digest"] is not None:
                pushed[e["filepath"]] = e["digest"]

    print()
    for label, filepath, ok, err in results:
        icon = f"{G}✓{RST}" if ok else f"{R}✗{RST}"
        print(f"  {icon}  {BLD}{label:18}{RST} {DIM}{filepath}{RST}")
        if err:
            print(f"       {R}{err}{RST}")
    if needs_restart and not rolled_back:
        print(f"  {G}✓{RST} Service healthy {DIM}({restart_msg}){RST}")

    print()
    if stage_failed:
        print(f"  {bar('═', R)}")
        print(f"  {BLD}{R}DEPLOY ABORTED{RST}: {len(stage_failed)} file(s) failed to stage.")
        print(f"  {R}No live files were replaced and no service was restarted.{RST}")
        print(f"  {bar('═', R)}")
    elif rolled_back:
        print(f"  {bar('═', R)}")
        print(f"  {BLD}{R}DEPLOY ROLLED BACK{RST}: the service did not answer {HEALTH_PATH}.")
        print(f"  {bar('═', R)}")
    else:
        print(f"  {bar('═', G)}")
        print(f"  {BLD}{G}Deploy complete{RST} {DIM}at {datetime.now().strftime('%H:%M:%S')}{RST}")
        print(f"  {bar('═', G)}")
    input(f"\n  {DIM}Press Enter to continue...{RST}")
    return pushed


# ── Main UI ──

STATE_LOOK = {
    "changed": (Y, "●", "modified"),
    "deployed": (G, "✓", "deployed"),
    "held": (Y, "○", "held: needs a staged release"),
    "release-only": (M, "○", "Hephaestus release only"),
    "failed": (R, "✗", "deploy failed"),
}


def draw_monitor(file_states, prev_times, cfg, last_event=""):
    """Draw the full monitor screen (clears terminal first)."""
    clear()
    w = min(cols(), 58)
    hypr = cfg["hyprchat"]
    cb   = cfg.get("codebox") or {}

    print(box([
        f"{BLD}     HyprChat Deploy Monitor{RST}",
        f"{DIM}    Watching {len(WATCHED)} files for changes{RST}",
    ], C, w))
    print()

    print(f"  {BLD}Servers{RST}")
    print(f"  {bar()}")
    print(f"  {C}●{RST} {BLD}HyprChat{RST}  {G}{hypr['user']}@{hypr['ip']}{RST}")
    if _LIVE_ROOT["checked"]:
        kind = "staged release" if _LIVE_ROOT["release"] else "no staged release"
        print(f"    {DIM}live {kind}: {_LIVE_ROOT['root']}{RST}")
    if cb.get("ip"):
        print(f"  {M}●{RST} {BLD}Hephaestus{RST}   {G}{cb.get('user', 'root')}@{cb['ip']}{RST}")
    sx = cfg.get("searxng")
    if _server_configured(sx):
        print(f"  {Y}●{RST} {BLD}SearXNG{RST}   {G}{sx['user']}@{sx['ip']}{RST}")
    print(f"  {bar()}")
    print()

    print(f"  {BLD}Watched Files{RST}")
    print(f"  {bar()}")
    for filepath, (label, _remote_dir, _restart) in WATCHED.items():
        state = file_states.get(filepath, "idle")
        if state in STATE_LOOK:
            color, mark, word = STATE_LOOK[state]
            icon, suffix = f"{color}{mark}{RST}", f" {color}{word}{RST}"
        elif not _exists(filepath):
            icon, suffix = f"{R}○{RST}", f" {DIM}missing{RST}"
        else:
            icon, suffix = f"{DIM}○{RST}", ""
        print(f"  {icon} {label:18} {DIM}{filepath}{RST}{suffix}")
    print(f"  {bar()}")
    print()

    blockers = blocking_holds()
    if HELD:
        print(f"  {Y}Held for a staged release:{RST} {len(HELD)} backend file(s), "
              f"blocked by {', '.join(blockers) or 'nothing'}")
        print(f"  {DIM}Ship with scripts/staged_release.py, then press c.{RST}")
        print()
    if last_event:
        print(f"  {last_event}")
        print()

    now = datetime.now().strftime("%H:%M:%S")
    print(f"  {DIM}{now}  |  {BLD}p{RST}{DIM} push all  |  {BLD}c{RST}{DIM} clear held  |  "
          f"{BLD}r{RST}{DIM} reconfigure  |  {BLD}q{RST}{DIM} quit{RST}")


def push_all(cfg, file_states):
    """Push every hot-patchable file to the live staged release.

    Only for a staged release: a server without one is installed with
    scripts/deploy.sh and upgraded with scripts/staged_release.py. Release-only
    and dependency files are never pushed; only files actually sent are marked
    deployed. Returns {path: digest sent}.
    """
    refresh_watch()
    root = _resolve_hyprchat_root(cfg["hyprchat"])
    if not _LIVE_ROOT["release"]:
        print()
        print(f"  {Y}!{RST} No staged release is active on {cfg['hyprchat']['ip']} ({root}).")
        print(f"    {DIM}Push all only updates a staged release. Install a new server with "
              f"scripts/deploy.sh; upgrade with scripts/staged_release.py prepare/activate.{RST}")
        input(f"\n  {DIM}Press Enter to continue...{RST}")
        return {}

    files = [(fp, info) for fp, info in WATCHED.items() if _exists(fp)]
    skip = release_only_files() | set(DEPENDENCY_FILES)
    sendable = [(fp, info) for fp, info in files if fp not in skip]
    skipped = [fp for fp, _ in files if fp in skip]
    if not sendable:
        print(f"  {R}No local files found to push.{RST}")
        input(f"\n  {DIM}Press Enter to continue...{RST}")
        return {}

    clear()
    print()
    print(box([
        f"{BLD}{M}  Push All Files{RST}",
        f"{DIM}  Uploading {len(sendable)} files to {root}{RST}",
    ], M))
    print()
    if skipped:
        print(f"  {DIM}Not pushed ({len(skipped)} release-only or dependency files): they ship with "
              f"scripts/staged_release.py or the Hephaestus release installer.{RST}")

    plan = {"deploy": [fp for fp, _ in sendable], "vm_only": [], "hold": [], "blockers": []}
    pushed = deploy_changes(sendable, cfg, plan=plan) or {}
    for fp, _ in sendable:
        file_states[fp] = "deployed" if fp in pushed else "failed"
    return pushed


def scan_for_changes(prev_times, baseline_hash, file_states, pending, held=None):
    """Re-glob the tree, then mtime pre-filter + content-hash confirm.

    Appends genuinely changed files to `pending` (deduped) and returns the
    list of newly added paths. A file that appeared since the last scan has
    no baseline, so it counts as changed. While a hold for a staged release
    is open, backend changes join the hold instead of prompting; once the
    last blocker is gone (reverted, or cleared with c), the other held files
    come back as pending changes.
    """
    held = HELD if held is None else held
    refresh_watch()
    vm_only, _used = release_split()
    new = []

    def add(fp):
        if not any(p == fp for p, _ in pending):
            pending.append((fp, WATCHED[fp]))
            new.append(fp)
        file_states[fp] = "changed"

    for filepath in list(WATCHED):
        if not _exists(filepath):
            continue
        mtime = _mtime(filepath)
        if mtime == prev_times.get(filepath, 0):
            continue
        prev_times[filepath] = mtime  # always advance the cheap filter
        digest = file_digest(filepath)
        if digest is None:
            continue
        if digest == baseline_hash.get(filepath):
            if filepath in held:  # reverted to what is live
                del held[filepath]
            if file_states.get(filepath) in ("changed", "held"):
                file_states[filepath] = "idle"
            continue  # touched, but content identical
        if held.get(filepath) == digest:
            continue  # held and unchanged since: do not offer it again
        if (blocking_holds(held) and filepath.startswith("backend/")
                and filepath not in vm_only):
            held[filepath] = digest
            file_states[filepath] = "held"
            continue
        add(filepath)

    if held and not blocking_holds(held):
        for fp in sorted(held):
            del held[fp]
            if fp in WATCHED and file_digest(fp) != baseline_hash.get(fp):
                add(fp)
            else:
                file_states[fp] = "idle"
    return new


def apply_outcome(plan, pushed, prev_times, baseline_hash, file_states, held=None):
    """Record what one deploy did, per file of the batch."""
    held = HELD if held is None else held
    for fp in plan["vm_only"]:
        # Acknowledged: they ship with the Hephaestus release.
        baseline_hash[fp] = file_digest(fp)
        prev_times[fp] = _mtime(fp)
        file_states[fp] = "release-only"
    for fp in plan["hold"]:
        # Not re-baselined: still different from what is live.
        held[fp] = file_digest(fp)
        prev_times[fp] = _mtime(fp)
        file_states[fp] = "held"
    for fp in plan["deploy"]:
        if fp in pushed:
            baseline_hash[fp] = pushed[fp]
            cur = file_digest(fp)
            if cur is not None and cur != pushed[fp]:
                prev_times[fp] = 0  # edited while the deploy ran: detect again
                file_states[fp] = "changed"
            else:
                prev_times[fp] = _mtime(fp)
                file_states[fp] = "deployed"
        else:
            prev_times[fp] = 0  # failed: offered again on the next scan
            file_states[fp] = "changed"


def clear_held(prev_times, baseline_hash, file_states, held=None):
    """The owner shipped a staged release: treat held files as live."""
    held = HELD if held is None else held
    count = len(held)
    for fp in list(held):
        baseline_hash[fp] = file_digest(fp)
        prev_times[fp] = _mtime(fp)
        file_states[fp] = "idle"
        del held[fp]
    return count


def main():
    cfg = load_config()
    first_setup = False
    if not cfg or not cfg.get("hyprchat", {}).get("ip"):
        cfg = setup_servers()
        first_setup = True
    else:
        clear()
        print()
        hypr = cfg["hyprchat"]
        cb   = cfg.get("codebox") or {}
        sx   = cfg.get("searxng")
        sx_lines = []
        if _server_configured(sx):
            sx_lines = [f"  {Y}SearXNG{RST}   {sx['user']}@{sx['ip']}"]
        print(box([
            f"{BLD}     HyprChat Deploy Monitor{RST}",
            "",
            f"  {C}HyprChat{RST}  {hypr['user']}@{hypr['ip']}",
            f"  {M}Hephaestus{RST}   {cb.get('user', 'root')}@{cb.get('ip', '-')}",
            *sx_lines,
            "",
            f"  {BLD}Enter{RST}  Start watching        ",
            f"  {BLD}p{RST}      Push all files now     ",
            f"  {BLD}r{RST}      Reconfigure servers    ",
        ], C))
        print()
        choice = input(f"  {BLD}>{RST} ").strip().lower()
        if choice == "r":
            cfg = setup_servers()
        elif choice == "p":
            push_all(cfg, {})

    # Initialize tracking
    refresh_watch()
    prev_times    = {}
    baseline_hash = {}
    file_states   = {}
    for filepath in WATCHED:
        exists = _exists(filepath)
        prev_times[filepath] = _mtime(filepath) if exists else 0
        baseline_hash[filepath] = file_digest(filepath) if exists else None
        file_states[filepath] = "idle"

    if first_setup:
        choice = input(f"  {BLD}Push all files to the existing install now? {RST}[{G}Y{RST}/{R}n{RST}] > ").strip().lower()
        if choice in ("", "y", "yes"):
            pushed = push_all(cfg, file_states)
            if _LIVE_ROOT["release"]:
                for fp, digest in pushed.items():
                    baseline_hash[fp] = digest
                _run_first_time_searxng_setup(cfg)

    if not _LIVE_ROOT["checked"]:
        _resolve_hyprchat_root(cfg["hyprchat"])
    last_event = f"{DIM}Watching for changes...{RST}"
    draw_monitor(file_states, prev_times, cfg, last_event)

    # Use non-blocking stdin reads via select on Unix
    import select
    import tty
    import termios

    old_settings = termios.tcgetattr(sys.stdin)

    try:
        tty.setcbreak(sys.stdin.fileno())

        while True:
            if select.select([sys.stdin], [], [], 0)[0]:
                key = sys.stdin.read(1).lower()
                if key == "p":
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                    pushed = push_all(cfg, file_states) or {}
                    # Re-baseline only the files that were actually sent.
                    for filepath, digest in pushed.items():
                        prev_times[filepath] = _mtime(filepath)
                        baseline_hash[filepath] = digest
                        HELD.pop(filepath, None)
                    last_event = (f"{G}✓ Pushed {len(pushed)} file(s) at "
                                  f"{datetime.now().strftime('%H:%M:%S')}{RST}")
                    tty.setcbreak(sys.stdin.fileno())
                    draw_monitor(file_states, prev_times, cfg, last_event)
                elif key == "c":
                    n = clear_held(prev_times, baseline_hash, file_states)
                    last_event = f"{G}✓ {n} held file(s) marked as released{RST}"
                    draw_monitor(file_states, prev_times, cfg, last_event)
                elif key == "r":
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                    cfg = setup_servers()
                    tty.setcbreak(sys.stdin.fileno())
                    last_event = f"{G}✓ Config updated{RST}"
                    draw_monitor(file_states, prev_times, cfg, last_event)
                elif key == "q":
                    raise KeyboardInterrupt

            time.sleep(CHECK_INTERVAL)

            held_before = len(HELD)
            pending = []
            scan_for_changes(prev_times, baseline_hash, file_states, pending)
            if len(HELD) != held_before and not pending:
                last_event = f"{Y}○ {len(HELD)} backend file(s) held for a staged release{RST}"
                draw_monitor(file_states, prev_times, cfg, last_event)

            if pending:
                def show_pending_box():
                    last = f"{Y}▶ {len(pending)} file(s) changed{RST}"
                    draw_monitor(file_states, prev_times, cfg, last)
                    print()
                    print(box([
                        f"{BLD}{Y}  Changes Detected{RST}",
                        "",
                    ] + [
                        f"  {Y}•{RST} {info[0]:18} {DIM}{fp}{RST}"
                        for fp, info in pending
                    ] + [
                        "",
                        f"  {DIM}Still watching — new changes join this batch{RST}",
                    ], Y))
                    print()
                    print(f"  {BLD}Deploy?{RST} [{G}y{RST}{DIM}/Enter{RST} deploy · {R}n{RST} skip]")
                show_pending_box()

                decision = None
                while decision is None:
                    if select.select([sys.stdin], [], [], 0)[0]:
                        key = sys.stdin.read(1).lower()
                        if key in ("y", "\n", "\r"):
                            decision = True
                        elif key == "n":
                            decision = False
                        continue
                    time.sleep(CHECK_INTERVAL)
                    if scan_for_changes(prev_times, baseline_hash, file_states, pending):
                        show_pending_box()

                if decision:
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                    plan = classify_batch([fp for fp, _ in pending], holding=bool(blocking_holds()))
                    pushed = deploy_changes(pending, cfg, plan=plan) or {}
                    tty.setcbreak(sys.stdin.fileno())
                    apply_outcome(plan, pushed, prev_times, baseline_hash, file_states)
                    last_event = f"{G}✓ Last deploy: {datetime.now().strftime('%H:%M:%S')}{RST}"
                else:
                    for fp, *_ in pending:
                        # Declining acknowledges the current content
                        baseline_hash[fp] = file_digest(fp)
                        file_states[fp] = "idle"
                    last_event = f"{DIM}Skipped deploy at {datetime.now().strftime('%H:%M:%S')}{RST}"

                draw_monitor(file_states, prev_times, cfg, last_event)

    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        clear()
        print()
        print(f"  {BLD}{C}HyprChat Deploy Monitor stopped.{RST}")
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
