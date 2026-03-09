#!/bin/bash
# ============================================================
# HyprChat — Server Deploy Script
# Run as root INSIDE the LXC container
# ============================================================
set -e

clear

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

step() {
    echo -e "${INFO} ${BLD}$1${RST}"
}

pass() {
    echo -e "${OK} $1"
}

fail() {
    echo -e "${FAIL} $1"
}

# ── [1/5] System packages ──
step "Installing system packages..."
apt update -qq > /dev/null 2>&1
apt install -y -qq python3-pip curl > /dev/null 2>&1
pass "System packages ready"

# ── [2/5] Create directories ──
step "Creating directories..."
mkdir -p /opt/hyprchat/data/{uploads/avatars,tools,knowledge_bases}
mkdir -p /opt/hyprchat/backend/agents
pass "Directories created"

# ── [3/5] Install Python deps ──
step "Installing Python dependencies..."
cd /opt/hyprchat/backend
pip install -r requirements.txt --break-system-packages -q 2>&1 | tail -1
pass "Python deps installed (incl. pypdf, chromadb)"

# ── [4/5] Verify frontend ──
step "Checking frontend..."
if [ ! -f /opt/hyprchat/frontend/dist/index.html ]; then
    fail "Frontend not found at /opt/hyprchat/frontend/dist/index.html"
    echo "     Make sure the project was extracted correctly."
    exit 1
fi
pass "Frontend found"

# ── [5/5] Install & start service ──
step "Installing systemd service..."
cp /opt/hyprchat/backend/hyprchat.service /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload
systemctl enable hyprchat > /dev/null 2>&1
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
