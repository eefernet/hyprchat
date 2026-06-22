#!/bin/bash
# ============================================================
# HyprChat — Server Deploy Script
# Run as root INSIDE the LXC container
# ============================================================
set -euo pipefail

clear || true

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║      HyprChat Server Deploy Script       ║"
echo "  ║    Hyprland-themed AI Chat Platform       ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""

# ── Helpers ──
OK="  \033[32m✓\033[0m"
FAIL="  \033[31m✗\033[0m"
INFO="  \033[33m▶\033[0m"
DIM="\033[2m"
RST="\033[0m"
BLD="\033[1m"
GRN="\033[32m"
CYN="\033[36m"
APP_ROOT="/opt/hyprchat"
DATA_ROOT="${APP_ROOT}/data"

step() {
    echo -e "${INFO} ${BLD}$1${RST}"
}

pass() {
    echo -e "${OK} $1"
}

fail() {
    echo -e "${FAIL} $1"
}

die() {
    fail "$1"
    exit 1
}

require_file() {
    if [ ! -f "$1" ]; then
        die "Missing required file: $1"
    fi
}

repair_data_permissions() {
    if id -u hyprchat > /dev/null 2>&1; then
        chown -R hyprchat:hyprchat "$DATA_ROOT" 2>/dev/null || chown -R hyprchat "$DATA_ROOT" 2>/dev/null || true
        chmod -R u+rwX "$DATA_ROOT" 2>/dev/null || true
    fi
}

# ── [1/7] Preflight ──
step "Checking repository layout..."
[ "$(id -u)" -eq 0 ] || die "Run this script as root inside the target server/container."
[ -d "$APP_ROOT" ] || die "Expected HyprChat at $APP_ROOT. Clone or copy the repo there first."
require_file "$APP_ROOT/.env.example"
require_file "$APP_ROOT/backend/main.py"
require_file "$APP_ROOT/backend/requirements.txt"
require_file "$APP_ROOT/backend/hyprchat.service"
if [ ! -f "$APP_ROOT/frontend/dist/index.html" ]; then
    fail "Frontend not found at $APP_ROOT/frontend/dist/index.html"
    echo "     frontend/dist/ is BUILD OUTPUT and is not committed to git."
    echo "     Build it on your dev machine before packaging/deploying:"
    echo "         cd frontend && npm install && npm run build"
    echo "     then include frontend/dist/ in what you copy to this host."
    echo "     (The server stays Node-free by design — do not install npm here.)"
    exit 1
fi
pass "Required files found"

# ── [2/7] Service user ──
step "Creating service user..."
if ! getent group hyprchat > /dev/null 2>&1; then
    groupadd --system hyprchat
fi
if ! id -u hyprchat > /dev/null 2>&1; then
    useradd --system --gid hyprchat --home-dir "$APP_ROOT" --shell /usr/sbin/nologin hyprchat
fi
pass "Service user ready"

# ── [3/7] System packages ──
step "Installing system packages..."
apt update -qq > /dev/null 2>&1
apt install -y -qq python3-pip curl > /dev/null 2>&1
pass "System packages ready"

# ── [4/7] Create directories and environment ──
step "Creating directories..."
mkdir -p "$DATA_ROOT"/{uploads/avatars,tools,knowledge_bases,chroma_db,comfy_workflows}
mkdir -p "$DATA_ROOT"/sandbox/{outputs,workspace,venv}
mkdir -p "$APP_ROOT/backend/agents"
if [ ! -f "$APP_ROOT/.env" ]; then
    cp "$APP_ROOT/.env.example" "$APP_ROOT/.env"
    pass "Created $APP_ROOT/.env from .env.example"
else
    pass "Existing $APP_ROOT/.env preserved"
fi
chown -R root:hyprchat "$APP_ROOT"
chmod -R g+rX "$APP_ROOT"
chmod 750 "$APP_ROOT"
chown root:hyprchat "$APP_ROOT/.env"
chmod 640 "$APP_ROOT/.env"
repair_data_permissions
pass "Directories and ownership ready"

# ── [5/7] Install Python deps ──
step "Installing Python dependencies..."
cd "$APP_ROOT/backend"
pip install -r requirements.txt --break-system-packages -q 2>&1 | tail -1
pass "Python deps installed (incl. pypdf, chromadb)"

# ── [6/7] Verify frontend ──
step "Checking frontend..."
if [ ! -f "$APP_ROOT/frontend/dist/index.html" ]; then
    fail "Frontend not found at $APP_ROOT/frontend/dist/index.html"
    echo "     frontend/dist/ is BUILD OUTPUT and is not committed to git."
    echo "     Build it on your dev machine before packaging/deploying:"
    echo "         cd frontend && npm install && npm run build"
    echo "     then include frontend/dist/ in what you copy to this host."
    echo "     (The server stays Node-free by design — do not install npm here.)"
    exit 1
fi
pass "Frontend found"

# ── [7/7] Install & start service ──
step "Installing systemd service..."
cp "$APP_ROOT/backend/hyprchat.service" /etc/systemd/system/hyprchat.service
chmod 644 /etc/systemd/system/hyprchat.service
systemctl daemon-reload
systemctl enable hyprchat > /dev/null 2>&1
repair_data_permissions
systemctl restart hyprchat
pass "Service started"

sleep 2

echo ""
if systemctl is-active --quiet hyprchat; then
    IP=$(hostname -I | awk '{print $1}')
    echo -e "  ${GRN}══════════════════════════════════════════${RST}"
    echo -e "  ${BLD}${GRN}HyprChat is running!${RST}"
    echo ""
    echo -e "  UI:          ${CYN}http://${IP}:8000${RST}"
    echo -e "  API docs:    ${CYN}http://${IP}:8000/docs${RST}"
    echo -e "  Health:      ${CYN}http://${IP}:8000/api/health${RST}"
    echo ""
    echo -e "  ${DIM}Logs:        journalctl -u hyprchat -f${RST}"
    echo -e "  ${DIM}Restart:     systemctl restart hyprchat${RST}"
    echo -e "  ${GRN}══════════════════════════════════════════${RST}"
else
    fail "Service failed to start!"
    echo "     Check logs: journalctl -u hyprchat -n 50"
fi
echo ""
