#!/usr/bin/env python3
"""
HyprChat Deployment Monitor
Watches project files and deploys changes to remote servers.
Saves server config after first setup so you never re-enter IPs.
"""

import json
import os
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

# ── Config file ──
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".deploy_config.json")

# ── Remote paths ──
REMOTE_BACKEND = "/opt/hyprchat/backend/"
REMOTE_AGENTS = REMOTE_BACKEND + "agents/"
REMOTE_FRONTEND = "/opt/hyprchat/frontend/dist/"
REMOTE_OPENHANDS_WORKER = "/opt/openhands-worker/"
REMOTE_AIDER_VENV = REMOTE_OPENHANDS_WORKER + "aider-venv"
SEARXNG_PRIVACY_SCRIPT = "scripts/setup-searxng-privacy.sh"

# ── Watched files → (label, remote_dir, needs_restart) ──
# needs_restart: whether deploying this file requires restarting hyprchat service
WATCHED = {
    "backend/main.py":              ("Main Server",      REMOTE_BACKEND,            True),
    "backend/config.py":            ("Config",           REMOTE_BACKEND,            True),
    "backend/database.py":          ("Database",         REMOTE_BACKEND,            True),
    "backend/tools.py":             ("Tools",            REMOTE_BACKEND,            True),
    "backend/connectors.py":        ("Connectors",       REMOTE_BACKEND,            True),
    "backend/model_providers.py":   ("Model Providers",  REMOTE_BACKEND,            True),
    "backend/cancel_registry.py":   ("Cancel Registry",  REMOTE_BACKEND,            True),
    "backend/rag.py":               ("RAG Pipeline",     REMOTE_BACKEND,            True),
    "backend/comfyui.py":           ("ComfyUI Client",   REMOTE_BACKEND,            True),
    "backend/image_prompt_enhancer.py": ("Image Prompt Enhancer", REMOTE_BACKEND,   True),
    "backend/persona_images.py":    ("Persona Images",   REMOTE_BACKEND,            True),
    "backend/voice.py":             ("Voice",            REMOTE_BACKEND,            True),
    "backend/research.py":          ("Research",         REMOTE_BACKEND,            True),
    "backend/quick_search.py":      ("Quick Search",     REMOTE_BACKEND,            True),
    "backend/search_agent.py":      ("Search Agent",     REMOTE_BACKEND,            True),
    "backend/storage_diagnostics.py": ("Storage Diagnostics", REMOTE_BACKEND,        True),
    "backend/events.py":            ("Events",           REMOTE_BACKEND,            True),
    "backend/council.py":           ("Council",          REMOTE_BACKEND,            True),
    "backend/hf.py":                ("HuggingFace",      REMOTE_BACKEND,            True),
    "backend/hyprfit.py":           ("HyprFit",          REMOTE_BACKEND,            True),
    "backend/openhands_worker.py":  ("OpenHands Worker", REMOTE_OPENHANDS_WORKER,   False),
    "backend/agents/chat.py":           ("Chat Agent",         REMOTE_AGENTS, True),
    "backend/agents/personas.py":       ("Personas",           REMOTE_AGENTS, True),
    "backend/agents/architect.py":      ("Architect Agent",    REMOTE_AGENTS, True),
    "backend/agents/reviewer.py":       ("Reviewer Agent",     REMOTE_AGENTS, True),
    "backend/agents/acceptance.py":     ("Acceptance Agent",   REMOTE_AGENTS, True),
    "backend/agents/fixer.py":          ("Fixer Agent",        REMOTE_AGENTS, True),
    "backend/agents/aider_fixer.py":    ("Aider Fixer",        REMOTE_AGENTS, True),
    "backend/agents/project_qa.py":     ("ProjectQA Agent",    REMOTE_AGENTS, True),
    "backend/agents/project_indexer.py":("Project Indexer",    REMOTE_AGENTS, True),
    "backend/agents/language_adapters.py":("Language Adapters", REMOTE_AGENTS, True),
    "backend/agents/__init__.py":       ("Agents Init",        REMOTE_AGENTS, True),
    "backend/requirements.txt":     ("Requirements",     REMOTE_BACKEND,            True),
    "backend/hyprchat.service":     ("Systemd Service",  "/etc/systemd/system/",    True),
    # Frontend has a Vite build step now. Watch the SOURCE — a change here runs
    # `npm run build` locally and ships the whole dist/ (see FRONTEND_SRC_FILES
    # and _build_and_deploy_frontend). The built dist/index.html is no longer
    # hand-edited or watched directly.
    "frontend/src/main.jsx":        ("Frontend (build)", REMOTE_FRONTEND,           False),
    "frontend/src/vendor.js":       ("Frontend (build)", REMOTE_FRONTEND,           False),
    "frontend/src/prism-setup.js":  ("Frontend (build)", REMOTE_FRONTEND,           False),
    "frontend/index.html":          ("Frontend (build)", REMOTE_FRONTEND,           False),
    "frontend/vite.config.js":      ("Frontend (build)", REMOTE_FRONTEND,           False),
    "frontend/package.json":        ("Frontend (build)", REMOTE_FRONTEND,           False),
    "frontend/package-lock.json":   ("Frontend (build)", REMOTE_FRONTEND,           False),
    "CHANGELOG.md":                 ("Changelog",        "/opt/hyprchat/",          False),
    "README.md":                    ("README",           "/opt/hyprchat/",          False),
}

# Frontend source files: a change to any of these triggers ONE `npm run build`
# + full dist/ sync (not a per-file scp). Keep in sync with the WATCHED entries
# labelled "Frontend (build)".
FRONTEND_SRC_FILES = {
    "frontend/src/main.jsx",
    "frontend/src/vendor.js",
    "frontend/src/prism-setup.js",
    "frontend/index.html",
    "frontend/vite.config.js",
    "frontend/package.json",
    "frontend/package-lock.json",
}

CHECK_INTERVAL = 1
BATCH_WINDOW = 2  # seconds to wait for additional changes before prompting

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

    print(f"  {BLD}{M}Codebox Server{RST} {DIM}(sandbox execution){RST}")
    cb = prompt_server("",
        default_ip=cb_def.get("ip") or cb_def.get("host", ""),
        default_user=cb_def.get("user", "root"),
        default_pass=cb_def.get("pass") or cb_def.get("password", ""))

    sx = prompt_optional_searxng(sx_def)

    cfg = {"hyprchat": hypr, "codebox": cb}
    if sx:
        cfg["searxng"] = sx
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


def _default_env_text(codebox_ip, searxng_ip=""):
    """Minimal .env for a fresh HyprChat host. Existing .env files are kept."""
    searxng_url = f"http://{searxng_ip}:8888" if searxng_ip else "http://127.0.0.1:8888"
    return "\n".join([
        "HOST=0.0.0.0",
        "PORT=8000",
        "DEBUG=false",
        f"CODEBOX_URL=http://{codebox_ip}:8585",
        f"OPENHANDS_URL=http://{codebox_ip}:8586",
        f"AIDER_WORKER_URL=http://{codebox_ip}:8586",
        f"SEARXNG_URL={searxng_url}",
        "HYPRCHAT_OUTBOUND_PROXY=",
        "COMFYUI_URL=",
        "STT_URL=",
        "TTS_URL=",
        "TTS_VOICE=af_heart",
        "DATABASE_PATH=/opt/hyprchat/data/hyprchat.db",
        "UPLOAD_DIR=/opt/hyprchat/data/uploads",
        "TOOLS_DIR=/opt/hyprchat/data/tools",
        "KB_DIR=/opt/hyprchat/data/knowledge_bases",
        "SANDBOX_DIR=/opt/hyprchat/data/sandbox",
        "SETTINGS_PATH=/opt/hyprchat/data/settings.json",
        "CONNECTOR_SECRETS_PATH=/opt/hyprchat/data/connector_secrets.json",
        "QUICK_SEARCH_MODE=balanced",
        "AIDER_ENABLED=true",
        "AIDER_AUTO_TEST=true",
        "",
    ])


def _hyprchat_host_ready(hypr):
    cmd = (
        "id -u hyprchat >/dev/null 2>&1 && "
        "test -d /opt/hyprchat/backend && "
        "test -d /opt/hyprchat/frontend/dist && "
        "test -d /opt/hyprchat/data && "
        "test -f /opt/hyprchat/.env && "
        "python3 -m pip --version >/dev/null 2>&1"
    )
    return ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"], cmd, timeout=20)


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


def _bootstrap_hyprchat_host(hypr, codebox_ip, searxng_ip=""):
    env_text = shlex.quote(_default_env_text(codebox_ip, searxng_ip))
    cmd = (
        "set -u\n"
        "if command -v apt-get >/dev/null 2>&1; then\n"
        "  apt-get update -qq >/dev/null 2>&1 || true\n"
        "  apt-get install -y -qq python3-pip python3-venv curl git sqlite3 >/dev/null 2>&1 || true\n"
        "fi\n"
        "if ! id -u hyprchat >/dev/null 2>&1; then\n"
        "  useradd --system --home /opt/hyprchat --shell /usr/sbin/nologin hyprchat\n"
        "fi\n"
        "mkdir -p /opt/hyprchat/backend/agents /opt/hyprchat/frontend/dist "
        "/opt/hyprchat/data/uploads/avatars /opt/hyprchat/data/tools "
        "/opt/hyprchat/data/knowledge_bases /opt/hyprchat/data/chroma_db "
        "/opt/hyprchat/data/comfy_workflows /opt/hyprchat/data/sandbox/outputs "
        "/opt/hyprchat/data/sandbox/workspace /opt/hyprchat/data/sandbox/venv\n"
        f"if [ ! -f /opt/hyprchat/.env ]; then printf %s {env_text} > /opt/hyprchat/.env; fi\n"
        "chmod 640 /opt/hyprchat/.env 2>/dev/null || true\n"
        "chown -R hyprchat:hyprchat /opt/hyprchat/data 2>/dev/null || chown -R hyprchat /opt/hyprchat/data\n"
        "chmod -R u+rwX /opt/hyprchat/data 2>/dev/null || true\n"
    )
    return ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"], cmd, timeout=180)


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
    time.sleep(3)
    ok2, out2, err2 = ssh_cmd(
        hypr["ip"], hypr["user"], hypr["pass"],
        "systemctl is-active hyprchat 2>&1", timeout=20,
    )
    return ok2 and "active" in out2, out2, err2


def _run_first_time_searxng_setup(cfg):
    """Run optional SearXNG privacy setup and wire HyprChat to the proxy if active."""
    sx = cfg.get("searxng")
    if not _server_configured(sx):
        print(f"  {DIM}No SearXNG host configured; skipping privacy setup.{RST}")
        return

    hypr = cfg["hyprchat"]
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SEARXNG_PRIVACY_SCRIPT)
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


def _codebox_host_ready(cb):
    cmd = (
        "test -d /opt/openhands-worker && "
        "test -d /root/projects && "
        "python3 -c 'import fastapi, uvicorn, pydantic' >/dev/null 2>&1"
    )
    return ssh_cmd(cb["ip"], cb["user"], cb["pass"], cmd, timeout=20)


def _bootstrap_codebox_host(cb):
    cmd = (
        "set -u\n"
        "if command -v apt-get >/dev/null 2>&1; then\n"
        "  apt-get update -qq >/dev/null 2>&1 || true\n"
        "  apt-get install -y -qq python3-pip python3-venv curl git >/dev/null 2>&1 || true\n"
        "fi\n"
        "mkdir -p /opt/openhands-worker /root/projects /tmp/hyprchat-aider\n"
        "python3 -m pip install --break-system-packages -q -U "
        "fastapi 'uvicorn[standard]' pydantic requests >/tmp/hyprchat-worker-pip.log 2>&1 || "
        "python3 -m pip install --user -q -U fastapi 'uvicorn[standard]' pydantic requests "
        ">>/tmp/hyprchat-worker-pip.log 2>&1\n"
    )
    return ssh_cmd(cb["ip"], cb["user"], cb["pass"], cmd, timeout=300)


def _ensure_openhands_worker_service(cb):
    cmd = (
        "set -u\n"
        "if [ ! -f /etc/systemd/system/openhands-worker.service ]; then\n"
        "cat > /etc/systemd/system/openhands-worker.service <<'EOF'\n"
        "[Unit]\n"
        "Description=HyprChat OpenHands Worker\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "WorkingDirectory=/opt/openhands-worker\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "ExecStart=/usr/bin/python3 /opt/openhands-worker/openhands_worker.py\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
        "EOF\n"
        "fi\n"
        "systemctl daemon-reload\n"
        "systemctl enable openhands-worker >/dev/null 2>&1 || true\n"
    )
    return ssh_cmd(cb["ip"], cb["user"], cb["pass"], cmd, timeout=30)


def _deploy_target(filepath, remote_dir, hypr, cb):
    """Return (target_server, remote_dir) for a watched file."""
    if filepath == "backend/openhands_worker.py":
        return cb, REMOTE_OPENHANDS_WORKER
    return hypr, remote_dir


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


def _ensure_aider_worker_venv(cb):
    """Install/check Aider on Codebox, where uploaded-project fixes run.

    Aider's dependency set is not reliable on Python 3.13. Use uv to create a
    dedicated Python 3.12 venv at the path the worker checks first.
    """
    venv = REMOTE_AIDER_VENV.rstrip("/")
    aider = f"{venv}/bin/aider"
    cmd = (
        "set -u\n"
        "log=/tmp/hyprchat-aider-install.log\n"
        f"venv={shlex.quote(venv)}\n"
        f"aider={shlex.quote(aider)}\n"
        f"mkdir -p {shlex.quote(REMOTE_OPENHANDS_WORKER)}\n"
        "if [ ! -x \"$aider\" ]; then\n"
        "  rm -rf \"$venv\"\n"
        "  : > \"$log\"\n"
        "  python3 -m pip install --break-system-packages -U uv >>\"$log\" 2>&1 || "
        "python3 -m pip install --user -U uv >>\"$log\" 2>&1\n"
        "  uvbin=$(command -v uv || true)\n"
        "  if [ -z \"$uvbin\" ] && [ -x /root/.local/bin/uv ]; then uvbin=/root/.local/bin/uv; fi\n"
        "  if [ -z \"$uvbin\" ]; then\n"
        "    echo 'uv install failed'\n"
        "    tail -80 \"$log\" 2>/dev/null || true\n"
        "    exit 1\n"
        "  fi\n"
        "  \"$uvbin\" python install 3.12 >>\"$log\" 2>&1\n"
        "  \"$uvbin\" venv --seed --python 3.12 \"$venv\" >>\"$log\" 2>&1\n"
        "  \"$venv/bin/python\" -m pip install -U pip setuptools wheel >>\"$log\" 2>&1\n"
        "  \"$venv/bin/pip\" install -U aider-chat >>\"$log\" 2>&1\n"
        "fi\n"
        "if [ -x \"$aider\" ]; then\n"
        "  \"$aider\" --version 2>&1\n"
        "else\n"
        "  echo 'Aider binary missing after install attempt'\n"
        "  tail -80 \"$log\" 2>/dev/null || true\n"
        "  exit 1\n"
        "fi"
    )
    return ssh_cmd(cb["ip"], cb["user"], cb["pass"], cmd, timeout=600)


def _aider_worker_ready(cb):
    aider = f"{REMOTE_AIDER_VENV.rstrip('/')}/bin/aider"
    cmd = f"test -x {shlex.quote(aider)} && {shlex.quote(aider)} --version 2>&1"
    return ssh_cmd(cb["ip"], cb["user"], cb["pass"], cmd, timeout=20)


def _build_and_deploy_frontend(target):
    """Build the Vite frontend locally, then sync the whole dist/ to the server.

    The frontend now has a build step: editing files under frontend/src/ (or the
    Vite config/entry) requires `npm run build`, which emits index.html plus
    content-hashed assets/. The hashes change every build, so stale remote assets
    are wiped first to avoid accumulation. The HyprChat host stays dumb — it just
    serves frontend/dist/ as static files; no Node is installed there.
    """
    fe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
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
    # wipe→copy window served index.html with missing chunks).
    remote_dist = REMOTE_FRONTEND.rstrip("/")
    staging = remote_dist + ".new"
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
        f"rm -rf {shlex.quote(remote_dist)} && mv {shlex.quote(staging)} {shlex.quote(remote_dist)}",
        timeout=30,
    )
    return swap_ok, swap_err


def deploy_changes(changed, cfg):
    """Deploy changed files and restart service only if needed."""
    hypr = cfg["hyprchat"]
    sx = cfg.get("searxng") if _server_configured(cfg.get("searxng")) else None
    needs_restart = False
    needs_pip = False
    results = []

    cb = cfg["codebox"]
    ensured_dirs = set()

    print()
    print(f"  {Y}\u25b6{RST} Checking remote install prerequisites...")
    hypr_ready, _out, hypr_err = _hyprchat_host_ready(hypr)
    if not hypr_ready:
        ok, out, err = _bootstrap_hyprchat_host(hypr, cb["ip"], sx["ip"] if sx else "")
        if ok:
            print(f"  {G}\u2713{RST} HyprChat host bootstrapped")
        else:
            print(f"  {Y}!{RST} HyprChat bootstrap warning: {(err or out or hypr_err)[:300]}")
    else:
        print(f"  {G}\u2713{RST} HyprChat host ready")

    cb_ready, _out, cb_err = _codebox_host_ready(cb)
    if not cb_ready:
        ok, out, err = _bootstrap_codebox_host(cb)
        if ok:
            print(f"  {G}\u2713{RST} Codebox host bootstrapped")
        else:
            print(f"  {Y}!{RST} Codebox bootstrap warning: {(err or out or cb_err)[:300]}")
    else:
        print(f"  {G}\u2713{RST} Codebox host ready")

    frontend_built = False
    for filepath, (label, remote_dir, restart_flag) in changed:
        # Frontend source changes are handled by a single build + dist/ sync,
        # regardless of how many src files changed in this batch.
        if filepath in FRONTEND_SRC_FILES:
            if frontend_built:
                continue
            frontend_built = True
            ok, err = _build_and_deploy_frontend(hypr)
            results.append(("Frontend (build)", filepath, ok, err, hypr))
            continue

        target, remote_dir = _deploy_target(filepath, remote_dir, hypr, cb)

        dir_key = (target["ip"], remote_dir)
        if dir_key not in ensured_dirs:
            dir_ok, dir_err = _ensure_remote_dir(target, remote_dir)
            ensured_dirs.add(dir_key)
            if not dir_ok:
                results.append((label, filepath, False,
                                f"Could not create remote dir {remote_dir}: {dir_err}",
                                target))
                continue

        ok, err = scp(filepath, target["ip"], remote_dir, target["user"], target["pass"])
        results.append((label, filepath, ok, err, target))
        if restart_flag:
            needs_restart = True

        if filepath == "backend/requirements.txt":
            needs_pip = True

    # Print results
    print()
    for label, filepath, ok, err, target in results:
        icon = f"{G}\u2713{RST}" if ok else f"{R}\u2717{RST}"
        server_tag = f"{M}codebox{RST}" if target is cb else f"{C}hyprchat{RST}"
        print(f"  {icon}  {BLD}{label:18}{RST} {DIM}{filepath}{RST}  → {server_tag}")
        if err:
            print(f"       {R}{err}{RST}")

    if needs_pip:
        print()
        print(f"  {Y}\u25b6{RST} Installing new dependencies...")
        ok, out, err = ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"],
            "cd /opt/hyprchat/backend && pip install -r requirements.txt --break-system-packages -q 2>&1 | tail -3")
        if ok:
            print(f"  {G}\u2713{RST} Dependencies updated")
        else:
            print(f"  {R}\u2717{RST} pip install failed: {err}")

    # Reload systemd if service file changed
    if any(fp == "backend/hyprchat.service" for fp, *_ in changed):
        print()
        print(f"  {Y}\u25b6{RST} Reloading systemd daemon...")
        ok, out, err = ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"],
            "systemctl daemon-reload && systemctl enable hyprchat >/dev/null 2>&1")
        if ok:
            print(f"  {G}\u2713{RST} Daemon reloaded; hyprchat enabled")
        else:
            print(f"  {R}\u2717{RST} daemon-reload failed: {err}")

    if needs_restart:
        print()
        print(f"  {Y}\u25b6{RST} Repairing HyprChat data permissions...")
        ok_perm, _, err_perm = _repair_hyprchat_data_permissions(hypr)
        if ok_perm:
            print(f"  {G}\u2713{RST} Data directory writable for hyprchat")
        else:
            print(f"  {Y}!{RST} Data permission repair may have failed: {err_perm}")

        print()
        print(f"  {Y}\u25b6{RST} Restarting hyprchat service...")
        ok, out, err = ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"],
            "systemctl restart hyprchat 2>&1", timeout=150)
        if ok:
            # Bind to a specific interface (e.g. Tailscale IP) can take a few seconds.
            time.sleep(3)
            ok2, out2, _ = ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"],
                "systemctl is-active hyprchat 2>&1")
            if ok2 and "active" in out2:
                print(f"  {G}\u2713{RST} Service running")
            else:
                print(f"  {Y}!{RST} Service may not be active: {out2}")
                _show_journal(hypr, "hyprchat")
        else:
            print(f"  {R}\u2717{RST} Restart failed: {err}")
            print(f"  {Y}\u25b6{RST} Attempting start fallback...")
            ok_start, out_start, err_start = ssh_cmd(
                hypr["ip"], hypr["user"], hypr["pass"],
                "systemctl reset-failed hyprchat 2>/dev/null || true; systemctl start hyprchat 2>&1",
                timeout=90,
            )
            time.sleep(3)
            ok2, out2, _ = ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"],
                "systemctl is-active hyprchat 2>&1")
            if ok_start and ok2 and "active" in out2:
                print(f"  {G}\u2713{RST} Service running after start fallback")
            else:
                print(f"  {R}\u2717{RST} Start fallback failed: {(err_start or out_start or out2)[:300]}")
                _show_journal(hypr, "hyprchat")

    worker_deployed = any(fp == "backend/openhands_worker.py" for fp, *_ in changed)
    aider_ready, aider_out, _ = _aider_worker_ready(cb)
    if worker_deployed or not aider_ready:
        print()
        print(f"  {Y}\u25b6{RST} Checking Aider worker venv on Codebox...")
        ok, out, err = _ensure_aider_worker_venv(cb)
        if ok:
            print(f"  {G}\u2713{RST} Aider ready: {out.splitlines()[-1] if out else 'installed'}")
        else:
            print(f"  {Y}!{RST} Aider install/check failed: {(err or out)[:300]}")
            print(f"     {DIM}Worker will still run; /aider/health reports missing until this is fixed.{RST}")

    if worker_deployed:
        print()
        print(f"  {Y}\u25b6{RST} Ensuring OpenHands worker service...")
        svc_ok, svc_out, svc_err = _ensure_openhands_worker_service(cb)
        if svc_ok:
            print(f"  {G}\u2713{RST} OpenHands worker service ready")
        else:
            print(f"  {Y}!{RST} Worker service setup failed: {(svc_err or svc_out)[:300]}")

        print()
        print(f"  {Y}\u25b6{RST} Restarting OpenHands worker on Codebox...")
        ok, out, err = ssh_cmd(cb["ip"], cb["user"], cb["pass"],
            "systemctl restart openhands-worker 2>&1", timeout=30)
        if ok:
            time.sleep(3)
            ok2, out2, _ = ssh_cmd(cb["ip"], cb["user"], cb["pass"],
                "systemctl is-active openhands-worker 2>&1")
            if ok2 and "active" in out2:
                print(f"  {G}\u2713{RST} OpenHands worker running")
            else:
                print(f"  {Y}!{RST} Worker may not be active: {out2}")
                _show_journal(cb, "openhands-worker")
        else:
            print(f"  {R}\u2717{RST} Worker restart failed: {err}")
            _show_journal(cb, "openhands-worker")

    print()
    print(f"  {bar('═', G)}")
    now = datetime.now().strftime("%H:%M:%S")
    print(f"  {BLD}{G}Deploy complete{RST} {DIM}at {now}{RST}")
    print(f"  {bar('═', G)}")
    input(f"\n  {DIM}Press Enter to continue...{RST}")


# ── Main UI ──

def draw_monitor(file_states, prev_times, cfg, last_event=""):
    """Draw the full monitor screen (clears terminal first)."""
    clear()
    w = min(cols(), 58)
    hypr = cfg["hyprchat"]
    cb   = cfg["codebox"]

    # Header
    print(box([
        f"{BLD}     HyprChat Deploy Monitor{RST}",
        f"{DIM}    Watching {len(WATCHED)} files for changes{RST}",
    ], C, w))
    print()

    # Server info
    print(f"  {BLD}Servers{RST}")
    print(f"  {bar()}")
    print(f"  {C}\u25cf{RST} {BLD}HyprChat{RST}  {G}{hypr['user']}@{hypr['ip']}{RST}")
    print(f"  {M}\u25cf{RST} {BLD}Codebox{RST}   {G}{cb['user']}@{cb['ip']}{RST}")
    sx = cfg.get("searxng")
    if _server_configured(sx):
        print(f"  {Y}\u25cf{RST} {BLD}SearXNG{RST}   {G}{sx['user']}@{sx['ip']}{RST}")
    print(f"  {bar()}")
    print()

    # File list
    print(f"  {BLD}Watched Files{RST}")
    print(f"  {bar()}")

    for filepath, (label, remote_dir, _restart) in WATCHED.items():
        mtime = prev_times.get(filepath, 0)
        state = file_states.get(filepath, "idle")

        if state == "changed":
            icon = f"{Y}\u25cf{RST}"
            suffix = f" {Y}modified{RST}"
        elif state == "deployed":
            icon = f"{G}\u2713{RST}"
            suffix = f" {G}deployed{RST}"
        elif not os.path.exists(filepath):
            icon = f"{R}\u25cb{RST}"
            suffix = f" {DIM}missing{RST}"
        else:
            icon = f"{DIM}\u25cb{RST}"
            suffix = ""

        print(f"  {icon} {label:18} {DIM}{filepath}{RST}{suffix}")

    print(f"  {bar()}")
    print()

    if last_event:
        print(f"  {last_event}")
        print()

    now = datetime.now().strftime("%H:%M:%S")
    print(f"  {DIM}{now}  |  {BLD}p{RST}{DIM} push all  |  {BLD}r{RST}{DIM} reconfigure  |  {BLD}q{RST}{DIM} quit{RST}")


def push_all(cfg, file_states):
    """Push ALL files to the server regardless of change detection."""
    all_files = [(fp, info) for fp, info in WATCHED.items() if os.path.exists(fp)]
    if not all_files:
        print(f"  {R}No local files found to push.{RST}")
        input(f"\n  {DIM}Press Enter to continue...{RST}")
        return

    clear()
    print()
    print(box([
        f"{BLD}{M}  Push All Files{RST}",
        f"{DIM}  Uploading {len(all_files)} files to server{RST}",
    ], M))
    print()

    deploy_changes(all_files, cfg)
    for fp, *_ in all_files:
        file_states[fp] = "deployed"


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
        cb   = cfg["codebox"]
        sx   = cfg.get("searxng")
        sx_lines = []
        if _server_configured(sx):
            sx_lines = [f"  {Y}SearXNG{RST}   {sx['user']}@{sx['ip']}"]
        print(box([
            f"{BLD}     HyprChat Deploy Monitor{RST}",
            "",
            f"  {C}HyprChat{RST}  {hypr['user']}@{hypr['ip']}",
            f"  {M}Codebox{RST}   {cb['user']}@{cb['ip']}",
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
            file_states = {}
            for filepath in WATCHED:
                file_states[filepath] = "idle"
            push_all(cfg, file_states)

    # Initialize tracking
    prev_times  = {}
    file_states = {}
    for filepath in WATCHED:
        prev_times[filepath] = os.path.getmtime(filepath) if os.path.exists(filepath) else 0
        file_states[filepath] = "idle"

    if first_setup:
        choice = input(f"  {BLD}Run first-time full deploy now? {RST}[{G}Y{RST}/{R}n{RST}] > ").strip().lower()
        if choice in ("", "y", "yes"):
            push_all(cfg, file_states)
            _run_first_time_searxng_setup(cfg)

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
            # Non-blocking check for keyboard input
            if select.select([sys.stdin], [], [], 0)[0]:
                key = sys.stdin.read(1).lower()
                if key == "p":
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                    push_all(cfg, file_states)
                    last_event = f"{G}\u2713 Pushed all at {datetime.now().strftime('%H:%M:%S')}{RST}"
                    # Re-snapshot mtimes so pushed files don't re-trigger
                    for filepath in WATCHED:
                        if os.path.exists(filepath):
                            prev_times[filepath] = os.path.getmtime(filepath)
                    tty.setcbreak(sys.stdin.fileno())
                    draw_monitor(file_states, prev_times, cfg, last_event)
                elif key == "r":
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                    cfg = setup_servers()
                    tty.setcbreak(sys.stdin.fileno())
                    last_event = f"{G}\u2713 Config updated{RST}"
                    draw_monitor(file_states, prev_times, cfg, last_event)
                elif key == "q":
                    raise KeyboardInterrupt

            time.sleep(CHECK_INTERVAL)

            # Check for changes
            changed = []
            for filepath, info in WATCHED.items():
                if not os.path.exists(filepath):
                    continue
                mtime = os.path.getmtime(filepath)
                if mtime != prev_times.get(filepath, 0):
                    changed.append((filepath, info))
                    file_states[filepath] = "changed"
                    prev_times[filepath] = mtime

            if changed:
                # Wait briefly to collect any additional file changes
                last_event = f"{Y}\u25b6 Change detected, collecting...{RST}"
                draw_monitor(file_states, prev_times, cfg, last_event)
                time.sleep(BATCH_WINDOW)

                # Re-scan for any files that changed during the window
                for filepath, info in WATCHED.items():
                    if not os.path.exists(filepath):
                        continue
                    mtime = os.path.getmtime(filepath)
                    if mtime != prev_times.get(filepath, 0):
                        if not any(fp == filepath for fp, *_ in changed):
                            changed.append((filepath, info))
                        file_states[filepath] = "changed"
                        prev_times[filepath] = mtime

                last_event = f"{Y}\u25b6 {len(changed)} file(s) changed{RST}"
                draw_monitor(file_states, prev_times, cfg, last_event)

                # Show change summary
                print()
                print(box([
                    f"{BLD}{Y}  Changes Detected{RST}",
                    "",
                ] + [
                    f"  {Y}\u2022{RST} {info[0]:18} {DIM}{fp}{RST}"
                    for fp, info in changed
                ], Y))
                print()

                # Restore terminal for input prompt
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                choice = input(f"  {BLD}Deploy? {RST}[{G}y{RST}/{R}n{RST}] > ").strip().lower()
                tty.setcbreak(sys.stdin.fileno())

                if choice in ("y", "yes", ""):
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                    deploy_changes(changed, cfg)
                    tty.setcbreak(sys.stdin.fileno())
                    for fp, *_ in changed:
                        file_states[fp] = "deployed"
                    last_event = f"{G}\u2713 Last deploy: {datetime.now().strftime('%H:%M:%S')}{RST}"
                else:
                    for fp, *_ in changed:
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
