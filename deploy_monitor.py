#!/usr/bin/env python3
"""
HyprChat Deployment Monitor
Monitors project files and deploys changes to remote server when detected.
"""

import os
import time
import subprocess
from pathlib import Path
from datetime import datetime

# === COLOR CODES ===
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
RED = "\033[31m"

# === CONFIGURATION ===
REMOTE_BACKEND_DIR = "/opt/hyprchat/backend/"
REMOTE_FRONTEND_DIR = "/opt/hyprchat/frontend/dist/"
CHECK_INTERVAL = 1  # seconds
REMOTE_HOST = None  # set at runtime

# Files to monitor
WATCHED_FILES = {
    "Backend Main": "backend/main.py",
    "Backend Config": "backend/config.py",
    "Backend Database": "backend/database.py",
    "Frontend Index": "frontend/dist/index.html",
}


def prompt_for_host():
    """Prompt the user for the remote server IP/hostname."""
    print()
    print(f"{BOLD}{CYAN}╔═══════════════════════════════════════════════════╗")
    print(f"║{CYAN}     HyprChat Deployment Monitor{RESET}                 {CYAN}║")
    print(f"╚═══════════════════════════════════════════════════╝{RESET}")
    print()
    while True:
        ip = input(f"{BOLD}Remote server IP or hostname{RESET} (e.g. 192.168.1.100): ").strip()
        if ip:
            user = input(f"{BOLD}SSH user{RESET} [root]: ").strip() or "root"
            return f"{user}@{ip}"
        print(f"  {RED}Please enter a valid IP or hostname.{RESET}")

# === UTILITY FUNCTIONS ===
def print_header():
    """Print formatted header."""
    print()
    print(f"{BOLD}{CYAN}Target:{RESET} {GREEN}{REMOTE_HOST}{RESET}")
    print(f"{DIM}Backend → {REMOTE_HOST}:{REMOTE_BACKEND_DIR}")
    print(f"Frontend → {REMOTE_HOST}:{REMOTE_FRONTEND_DIR}{RESET}")
    print()

def print_status():
    """Print status of watched files."""
    print(f"{BOLD}{BLUE}👁️  Monitoring Files:{RESET}")
    print(f"{DIM}{'─' * 52}{RESET}")
    
    for label, filepath in WATCHED_FILES.items():
        status, was_modified = file_states.get(filepath, (False, False))
        
        if was_modified:
            checkmark = f"{GREEN}✓{RESET}"
            status_text = f"{YELLOW}[CHANGED]{RESET}"
        else:
            checkmark = " "
            status_text = ""
        
        print(f"  {checkmark} {label:20} → {filepath}{status_text}")
    
    print(f"{DIM}{'─' * 52}{RESET}")
    print(f"{BOLD}{DIM}Tip: Type '{CYAN}stop{RESET}' to exit program{RESET}")
    print()

def get_file_mtime(filepath):
    """Get file modification time."""
    try:
        return os.path.getmtime(filepath)
    except OSError:
        return 0

def check_file_changes():
    """Check for file changes."""
    global file_states
    new_states = {}
    changed_files = []
    
    for label, filepath in WATCHED_FILES.items():
        current_mtime = get_file_mtime(filepath)
        old_state = file_states.get(filepath, (False, False))
        prev_mtime = prev_times.get(filepath, current_mtime)
        
        is_modified = current_mtime != prev_times.get(filepath, current_mtime)
        
        new_states[filepath] = (is_modified, old_state[1])
        prev_times[filepath] = current_mtime
        
        if is_modified:
            changed_files.append((label, filepath))
    
    file_states = new_states
    return changed_files

def display_changes(changed_files):
    """Display list of changed files."""
    print()
    print(f"{BOLD}{YELLOW}╔═══════════════════════════════════════════════════╗")
    print(f"║{YELLOW}          🚀 Changes Detected!{RESET}                 ║")
    print(f"{YELLOW}╚═══════════════════════════════════════════════════╝" + RESET)
    print()
    
    print(f"{BOLD}Modified files:{RESET}")
    for label, filepath in changed_files:
        print(f"  {GREEN}✓{RESET} {label:20} → {filepath}")
    print()

def get_user_choice():
    """Get user deployment choice."""
    while True:
        choice = input(
            f"{BOLD}{CYAN}Deploy changes? {RESET}[{GREEN}y{RESET}/{YELLOW}n{RESET}/{MAGENTA}s top{RESET}] > ").strip().lower()
        
        if choice == "y":
            return "deploy"
        elif choice == "n":
            return "skip"
        elif choice == "stop":
            return "quit"
        else:
            print(f"  {RED}Invalid input.{RESET} Please enter 'y', 'n', or 'stop'")

def print_deploy_info(label, filepath, is_backend=True):
    """Print deployment info."""
    if is_backend:
        dest = f"{REMOTE_HOST}:{REMOTE_BACKEND_DIR}"
        file = filepath
    else:
        dest = f"{REMOTE_HOST}:{REMOTE_FRONTEND_DIR}index.html"
        file = "frontend/dist/index.html"
    
    print()
    print(f"{DIM}→ {BOLD}Uploading{RESET}: {GREEN}{label}{RESET}")
    print(f"  {DIM}From:{RESET} {CYAN}{file}{RESET}")
    print(f"  {DIM}To:{RESET} {MAGENTA}{dest}{RESET}")
    print()
    print(f"  {YELLOW}[Running]{RESET} scp {file} {dest}")
    print()

def run_command(cmd, label, is_backend=True):
    """Run command and display output."""
    print_deploy_info(label, cmd[5] if is_backend else WATCHED_FILES["Frontend Index"], is_backend)
    
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print(f"  {GREEN}✓ Upload successful{RESET}")
        else:
            print(f"  {RED}✗ Upload failed:{RESET} {result.stderr}")
    except Exception as e:
        print(f"  {RED}✗ Error:{RESET} {e}")

def deploy_backend():
    """Deploy backend files."""
    if "Backend Main" in [f[0] for f in changed_files]:
        run_command(
            f"scp backend/main.py {REMOTE_HOST}:{REMOTE_BACKEND_DIR}",
            "Backend Main",
            True
        )

    if "Backend Config" in [f[0] for f in changed_files]:
        run_command(
            f"scp backend/config.py {REMOTE_HOST}:{REMOTE_BACKEND_DIR}",
            "Backend Config",
            True
        )

    if "Backend Database" in [f[0] for f in changed_files]:
        run_command(
            f"scp backend/database.py {REMOTE_HOST}:{REMOTE_BACKEND_DIR}",
            "Backend Database",
            True
        )

    if "Frontend Index" in [f[0] for f in changed_files]:
        run_command(
            f"scp frontend/dist/index.html {REMOTE_HOST}:{REMOTE_FRONTEND_DIR}index.html",
            "Frontend Index",
            False
        )
    
    print()
    print(f"{BOLD}{BLUE}🔄 Restarting Service...{RESET}")
    print(f"  {YELLOW}[Running]{RESET} ssh {REMOTE_HOST} \"systemctl restart hyprchat && systemctl status hyprchat\"")
    
    try:
        result = subprocess.run(
            f"ssh {REMOTE_HOST} 'systemctl restart hyprchat && systemctl status hyprchat'",
            shell=True,
            capture_output=True,
            text=True
        )
        
        print()
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(f"{RED}{result.stderr}{RESET}")
    except Exception as e:
        print(f"  {RED}✗ Error:{RESET} {e}")
    
    print()
    print(f"{GREEN}{'═' * 54}{RESET}")
    print(f"  {GREEN}Deployment complete!{RESET}")
    print(f"{GREEN}{'═' * 54}{RESET}")
    print()

def main():
    """Main loop."""
    global changed_files, prev_times, file_states, REMOTE_HOST

    REMOTE_HOST = prompt_for_host()

    changed_files = []
    prev_times = {}
    file_states = {}

    print_header()
    
    # Initialize file states
    for label, filepath in WATCHED_FILES.items():
        prev_times[filepath] = get_file_mtime(filepath)
        file_states[filepath] = (False, False)
    
    print(f"{BOLD}{DIM}Started monitoring at{RESET} {CYAN}{datetime.now().strftime('%H:%M:%S')}{RESET}")
    print()
    
    try:
        while True:
            print_status()
            changed_files = check_file_changes()
            
            if changed_files:
                display_changes(changed_files)
                choice = get_user_choice()
                
                if choice == "quit":
                    print()
                    print(f"{BOLD}{CYAN}👋 Goodbye!{RESET}")
                    print()
                    break
                elif choice == "deploy":
                    print()
                    deploy_backend()
                
                # Reset change states after deployment decision
                for label, filepath in changed_files:
                    file_states[filepath] = (False, False)
            
            time.sleep(CHECK_INTERVAL)
            
    except KeyboardInterrupt:
        print()
        print(f"{BOLD}{CYAN}👋 Goodbye!{RESET}")
        print()
        os._exit(0)

if __name__ == "__main__":
    main()
