#!/usr/bin/env python3
"""
HyprChat Deployment Monitor
Watches project files and deploys changes to remote servers.
Saves server config after first setup so you never re-enter IPs.
"""

import json
import os
import re
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
REMOTE_BACKEND  = "/opt/hyprchat/backend/"
REMOTE_FRONTEND = "/opt/hyprchat/frontend/dist/"

# ── Watched files → (label, remote_dir, is_backend) ──
WATCHED = {
    "backend/main.py":              ("Main Server",      REMOTE_BACKEND),
    "backend/config.py":            ("Config",           REMOTE_BACKEND),
    "backend/database.py":          ("Database",         REMOTE_BACKEND),
    "backend/tools.py":             ("Tools",            REMOTE_BACKEND),
    "backend/rag.py":               ("RAG Pipeline",     REMOTE_BACKEND),
    "backend/research.py":          ("Research",         REMOTE_BACKEND),
    "backend/events.py":            ("Events",           REMOTE_BACKEND),
    "backend/council.py":           ("Council",          REMOTE_BACKEND),
    "backend/hf.py":                ("HuggingFace",      REMOTE_BACKEND),
    "backend/openhands_worker.py":  ("OpenHands",        REMOTE_BACKEND),
    "backend/agents/chat.py":       ("Chat Agent",       REMOTE_BACKEND + "agents/"),
    "backend/agents/personas.py":   ("Personas",         REMOTE_BACKEND + "agents/"),
    "backend/agents/__init__.py":   ("Agents Init",      REMOTE_BACKEND + "agents/"),
    "backend/requirements.txt":     ("Requirements",     REMOTE_BACKEND),
    "backend/hyprchat.service":     ("Systemd Service",  "/etc/systemd/system/"),
    "frontend/dist/index.html":     ("Frontend",         REMOTE_FRONTEND),
    "CHANGELOG.md":                 ("Changelog",        "/opt/hyprchat/"),
    "README.md":                    ("README",           "/opt/hyprchat/"),
}

CHECK_INTERVAL = 1

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

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
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

    cfg = load_config() or {}
    hypr_def = cfg.get("hyprchat", {})
    cb_def   = cfg.get("codebox", {})

    print(f"  {BLD}{Y}HyprChat Server{RST} {DIM}(backend + frontend){RST}")
    hypr = prompt_server("",
        default_ip=hypr_def.get("ip", ""),
        default_user=hypr_def.get("user", "root"),
        default_pass=hypr_def.get("pass", ""))

    print(f"  {BLD}{M}Codebox Server{RST} {DIM}(sandbox execution){RST}")
    cb = prompt_server("",
        default_ip=cb_def.get("ip", ""),
        default_user=cb_def.get("user", "root"),
        default_pass=cb_def.get("pass", ""))

    cfg = {"hyprchat": hypr, "codebox": cb}
    save_config(cfg)
    print(f"  {G}Config saved to {CONFIG_FILE}{RST}")
    print()
    return cfg


# ── SCP / SSH with sshpass ──

def scp(local, remote_host, remote_path, user, password):
    """Copy a file to remote via scp. Returns (ok, msg)."""
    dest = f"{user}@{remote_host}:{remote_path}"
    cmd = [
        "sshpass", "-p", password,
        "scp", "-o", "StrictHostKeyChecking=no", "-q",
        local, dest
    ]
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
    cmd = [
        "sshpass", "-p", password,
        "ssh", "-o", "StrictHostKeyChecking=no",
        f"{user}@{host}", command
    ]
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

def deploy_changes(changed, cfg):
    """Deploy changed files and restart service."""
    hypr = cfg["hyprchat"]
    needs_restart = False
    needs_pip = False
    results = []

    cb = cfg["codebox"]

    for filepath, (label, remote_dir) in changed:
        # openhands_worker.py goes to codebox server
        if filepath == "backend/openhands_worker.py":
            target = cb
            remote_dir = "/opt/openhands-worker/"
        else:
            target = hypr

        ok, err = scp(filepath, target["ip"], remote_dir, target["user"], target["pass"])
        status = f"{G}OK{RST}" if ok else f"{R}FAIL{RST}"
        results.append((label, filepath, ok, err, target))
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
    if any(fp == "backend/hyprchat.service" for fp, _ in changed):
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
            "systemctl restart hyprchat 2>&1", timeout=90)
        if ok:
            time.sleep(1)
            ok2, out2, _ = ssh_cmd(hypr["ip"], hypr["user"], hypr["pass"],
                "systemctl is-active hyprchat 2>&1")
            if ok2 and "active" in out2:
                print(f"  {G}\u2713{RST} Service running")
            else:
                print(f"  {Y}!{RST} Service may not be active: {out2}")
        else:
            print(f"  {R}\u2717{RST} Restart failed: {err}")

    # Restart openhands worker on codebox if it was deployed
    if any(fp == "backend/openhands_worker.py" for fp, _ in changed):
        print()
        print(f"  {Y}\u25b6{RST} Restarting OpenHands worker on Codebox...")
        ok, out, err = ssh_cmd(cb["ip"], cb["user"], cb["pass"],
            "systemctl restart openhands-worker 2>&1", timeout=30)
        if ok:
            time.sleep(1)
            ok2, out2, _ = ssh_cmd(cb["ip"], cb["user"], cb["pass"],
                "systemctl is-active openhands-worker 2>&1")
            if ok2 and "active" in out2:
                print(f"  {G}\u2713{RST} OpenHands worker running")
            else:
                print(f"  {Y}!{RST} Worker may not be active: {out2}")
        else:
            print(f"  {R}\u2717{RST} Worker restart failed: {err}")

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

    for filepath, (label, remote_dir) in WATCHED.items():
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
    for fp, _ in all_files:
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
                    for fp, _ in changed:
                        file_states[fp] = "deployed"
                    last_event = f"{G}\u2713 Last deploy: {datetime.now().strftime('%H:%M:%S')}{RST}"
                else:
                    for fp, _ in changed:
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
