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

# ── Watched files → (label, remote_dir, needs_restart) ──
# needs_restart: whether deploying this file requires restarting hyprchat service
WATCHED = {
    "backend/main.py":              ("Main Server",      REMOTE_BACKEND,            True),
    "backend/config.py":            ("Config",           REMOTE_BACKEND,            True),
    "backend/database.py":          ("Database",         REMOTE_BACKEND,            True),
    "backend/tools.py":             ("Tools",            REMOTE_BACKEND,            True),
    "backend/cancel_registry.py":   ("Cancel Registry",  REMOTE_BACKEND,            True),
    "backend/rag.py":               ("RAG Pipeline",     REMOTE_BACKEND,            True),
    "backend/research.py":          ("Research",         REMOTE_BACKEND,            True),
    "backend/quick_search.py":      ("Quick Search",     REMOTE_BACKEND,            True),
    "backend/search_agent.py":      ("Search Agent",     REMOTE_BACKEND,            True),
    "backend/events.py":            ("Events",           REMOTE_BACKEND,            True),
    "backend/council.py":           ("Council",          REMOTE_BACKEND,            True),
    "backend/hf.py":                ("HuggingFace",      REMOTE_BACKEND,            True),
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
    "frontend/dist/index.html":     ("Frontend",         REMOTE_FRONTEND,           False),
    "CHANGELOG.md":                 ("Changelog",        "/opt/hyprchat/",          False),
    "README.md":                    ("README",           "/opt/hyprchat/",          False),
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
    for key in ("hyprchat", "codebox"):
        server = cfg.get(key)
        if not isinstance(server, dict):
            continue
        if not server.get("ip") and server.get("host"):
            server["ip"] = server["host"]
        if not server.get("pass") and server.get("password"):
            server["pass"] = server["password"]
        server.setdefault("user", "root")
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

    cfg = {"hyprchat": hypr, "codebox": cb}
    save_config(cfg)
    print(f"  {G}Config saved to {CONFIG_FILE}{RST}")
    print()
    return cfg


# ── SCP / SSH with sshpass ──

def scp(local, remote_host, remote_path, user, password):
    """Copy a file to remote via scp. Returns (ok, msg)."""
    dest = f"{user}@{remote_host}:{remote_path}"
    if password:
        cmd = [
            "sshpass", "-p", password,
            "scp", "-o", "StrictHostKeyChecking=no", "-q",
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


def ssh_cmd(host, user, password, command, timeout=30):
    """Run a command on remote via ssh. Returns (ok, stdout, stderr)."""
    if password:
        cmd = [
            "sshpass", "-p", password,
            "ssh", "-o", "StrictHostKeyChecking=no",
            f"{user}@{host}", command
        ]
    else:
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", f"{user}@{host}", command]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
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
    """Install/check Aider on Codebox, where uploaded-project fixes run."""
    venv = REMOTE_AIDER_VENV.rstrip("/")
    aider = f"{venv}/bin/aider"
    pip = f"{venv}/bin/pip"
    cmd = (
        "set -u\n"
        "log=/tmp/hyprchat-aider-install.log\n"
        f"mkdir -p {shlex.quote(REMOTE_OPENHANDS_WORKER)}\n"
        f"if [ ! -x {shlex.quote(aider)} ]; then\n"
        f"  rm -rf {shlex.quote(venv)}\n"
        "  : > \"$log\"\n"
        f"  if ! python3 -m venv {shlex.quote(venv)} >>\"$log\" 2>&1; then\n"
        "    if command -v apt-get >/dev/null 2>&1; then\n"
        "      apt-get update -qq >>\"$log\" 2>&1 || true\n"
        "      apt-get install -y -qq python3-venv >>\"$log\" 2>&1 || true\n"
        "    fi\n"
        f"    python3 -m venv {shlex.quote(venv)} >>\"$log\" 2>&1\n"
        "  fi\n"
        f"  {shlex.quote(pip)} install -U pip setuptools wheel >>\"$log\" 2>&1\n"
        f"  {shlex.quote(pip)} install -U aider-chat >>\"$log\" 2>&1\n"
        "fi\n"
        f"if [ -x {shlex.quote(aider)} ]; then\n"
        f"  {shlex.quote(aider)} --version 2>&1\n"
        "else\n"
        "  echo 'Aider binary missing after install attempt'\n"
        "  tail -80 \"$log\" 2>/dev/null || true\n"
        "  exit 1\n"
        "fi"
    )
    return ssh_cmd(cb["ip"], cb["user"], cb["pass"], cmd, timeout=600)


def deploy_changes(changed, cfg):
    """Deploy changed files and restart service only if needed."""
    hypr = cfg["hyprchat"]
    needs_restart = False
    needs_pip = False
    results = []

    cb = cfg["codebox"]
    ensured_dirs = set()

    for filepath, (label, remote_dir, restart_flag) in changed:
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
        ok, out, err = ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"], "systemctl daemon-reload")
        if ok:
            print(f"  {G}\u2713{RST} Daemon reloaded")
        else:
            print(f"  {R}\u2717{RST} daemon-reload failed: {err}")

    if needs_restart:
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

    # Restart openhands worker on codebox if it was deployed
    if any(fp == "backend/openhands_worker.py" for fp, *_ in changed):
        print()
        print(f"  {Y}\u25b6{RST} Checking Aider worker venv on Codebox...")
        ok, out, err = _ensure_aider_worker_venv(cb)
        if ok:
            print(f"  {G}\u2713{RST} Aider ready: {out.splitlines()[-1] if out else 'installed'}")
        else:
            print(f"  {Y}!{RST} Aider install/check failed: {(err or out)[:300]}")
            print(f"     {DIM}Worker will still run; /aider/health reports missing until this is fixed.{RST}")

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
    if not cfg or not cfg.get("hyprchat", {}).get("ip"):
        cfg = setup_servers()
    else:
        clear()
        print()
        hypr = cfg["hyprchat"]
        cb   = cfg["codebox"]
        print(box([
            f"{BLD}     HyprChat Deploy Monitor{RST}",
            "",
            f"  {C}HyprChat{RST}  {hypr['user']}@{hypr['ip']}",
            f"  {M}Codebox{RST}   {cb['user']}@{cb['ip']}",
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
