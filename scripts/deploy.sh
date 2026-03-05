#!/bin/bash
# ============================================================
# HyprChat — Deploy script
# Run as root INSIDE the LXC container
# ============================================================
set -e

echo "╔══════════════════════════════════════╗"
echo "║       HyprChat Deploy Script         ║"
echo "║   Hyprland-themed AI Chat Platform   ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ---- System packages ----
echo "[1/4] Installing system packages..."
apt update -qq
apt install -y -qq python3-pip curl > /dev/null 2>&1
echo "  ✓ System packages ready"

# ---- Create directories ----
echo "[2/4] Creating directories..."
mkdir -p /opt/hyprchat/data/{uploads/avatars,tools,knowledge_bases}
echo "  ✓ Directories created"

# ---- Install Python deps ----
echo "[3/4] Installing Python dependencies..."
cd /opt/hyprchat/backend
pip install -r requirements.txt --break-system-packages -q
echo "  ✓ Python deps installed"

# ---- Verify frontend exists ----
if [ ! -f /opt/hyprchat/frontend/dist/index.html ]; then
    echo "  ⚠ Frontend not found at /opt/hyprchat/frontend/dist/index.html"
    echo "  Make sure the project was extracted correctly."
    exit 1
fi
echo "  ✓ Frontend found"

# ---- Install systemd service ----
echo "[4/4] Installing systemd service..."
cp /opt/hyprchat/backend/hyprchat.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable hyprchat
systemctl start hyprchat
echo "  ✓ Service started"

sleep 2

echo ""
if systemctl is-active --quiet hyprchat; then
    IP=$(hostname -I | awk '{print $1}')
    echo "════════════════════════════════════════"
    echo "  ✅ HyprChat is running!"
    echo ""
    echo "  UI:          http://${IP}:8000"
    echo "  API docs:    http://${IP}:8000/docs"
    echo "  Health:      http://${IP}:8000/api/health"
    echo ""
    echo "  Logs:        journalctl -u hyprchat -f"
    echo "  Restart:     systemctl restart hyprchat"
    echo ""
    echo "  Your services:"
    echo "    Ollama:    192.168.1.110:11434"
    echo "    Codebox:   192.168.1.201:8585"
    echo "    SearXNG:   192.168.1.141:8888"
    echo "    n8n:       192.168.1.114:5678"
    echo "════════════════════════════════════════"
else
    echo "  ❌ Service failed to start!"
    echo "  Check logs: journalctl -u hyprchat -n 50"
fi
