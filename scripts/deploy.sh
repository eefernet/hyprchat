#!/bin/bash
# ============================================================
# HyprChat — first install on a server
#
# Run as root on the target server (Ubuntu 24.04, x86_64), from a source copy
# at /opt/hyprchat that includes a built frontend/dist/:
#     sudo bash /opt/hyprchat/scripts/deploy.sh
#
# For a first install only. Upgrade an existing install with
# scripts/staged_release.py (prepare, activate, recover); see the README.
# ============================================================
set -Eeuo pipefail

APP_ROOT="/opt/hyprchat"
DATA_ROOT="${APP_ROOT}/data"
UNIT_DIR="/etc/systemd/system"
HEPHAESTUS_ENV_DIR="/etc/hyprchat"
HEPHAESTUS_ENV="${HEPHAESTUS_ENV_DIR}/hephaestus.env"
APT_LOG="/var/log/hyprchat-install-apt.log"
TOTAL_STEPS=7
STEP=0

OK="  \033[32m✓\033[0m"
FAIL="  \033[31m✗\033[0m"
INFO="  \033[33m▶\033[0m"
DIM="\033[2m"
RST="\033[0m"
BLD="\033[1m"
GRN="\033[32m"
CYN="\033[36m"

step() { STEP=$((STEP + 1)); echo -e "${INFO} ${BLD}[${STEP}/${TOTAL_STEPS}] $1${RST}"; }
pass() { echo -e "${OK} $1"; }
fail() { echo -e "${FAIL} $1"; }
die() { fail "$1"; exit 1; }
require_file() { [ -f "$1" ] || die "Missing required file: $1"; }
trap 'fail "deploy.sh stopped at line $LINENO (exit $?)."' ERR

# Value of KEY in .env (last assignment wins, as with systemd).
env_value() { sed -n "s/^$1=//p" "$APP_ROOT/.env" 2>/dev/null | tail -1; }

# The address to reach the service from this machine.
local_url() {
    local host port
    host="$(env_value HOST)"; port="$(env_value PORT)"
    host="${host:-127.0.0.1}"; port="${port:-8000}"
    case "$host" in 0.0.0.0|::|"[::]") host="127.0.0.1" ;; esac
    case "$host" in *:*) host="[$host]" ;; esac
    echo "http://${host}:${port}"
}

service_answers() { curl -fsS --max-time 3 "$(local_url)/api/health/live" > /dev/null 2>&1; }

repair_data_permissions() {
    chown -R hyprchat:hyprchat "$DATA_ROOT" 2>/dev/null || true
    chmod -R u+rwX "$DATA_ROOT" 2>/dev/null || true
}

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║        HyprChat — first install          ║"
echo "  ║       Self-hosted AI chat platform       ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""

# ── Preflight ──
step "Checking this server and the source copy..."
[ "$(id -u)" -eq 0 ] || die "Run this script as root on the target server."
[ -d "$APP_ROOT" ] || die "Expected the HyprChat source at $APP_ROOT. Copy a clean export there first (see the README)."
python3 -c 'import platform, sys; sys.exit(not (sys.version_info[:2] == (3, 12) and platform.machine() == "x86_64"))' \
    || die "HyprChat needs Python 3.12 on x86_64 (backend/requirements.lock.txt is built for it). Ubuntu 24.04 provides it."
require_file "$APP_ROOT/.env.example"
require_file "$APP_ROOT/backend/main.py"
require_file "$APP_ROOT/backend/requirements.lock.txt"
require_file "$APP_ROOT/backend/hyprchat.service"
if [ ! -f "$APP_ROOT/frontend/dist/index.html" ]; then
    fail "No built frontend at $APP_ROOT/frontend/dist/index.html"
    echo "     frontend/dist/ is build output and is not in git. Build it on your"
    echo "     development machine (cd frontend && npm ci && npm run build) and copy"
    echo "     the whole frontend/dist/ here. This server does not need Node."
    exit 1
fi
if [ -d "$APP_ROOT/.releases" ] || { systemctl cat hyprchat > /dev/null 2>&1 && service_answers; }; then
    die "HyprChat is already installed and running. Upgrade it with scripts/staged_release.py prepare / activate instead (see the README)."
fi
if [ -f "$APP_ROOT/.env" ] && grep -nE '^[A-Za-z_][A-Za-z0-9_]*=.*[[:space:]]#' "$APP_ROOT/.env"; then
    die "$APP_ROOT/.env has a comment after a value (lines above). systemd keeps it as part of the value: move each comment onto its own line."
fi
pass "Server and source copy look right"

# ── Service user ──
step "Creating the hyprchat service user..."
getent group hyprchat > /dev/null 2>&1 || groupadd --system hyprchat
id -u hyprchat > /dev/null 2>&1 || useradd --system --gid hyprchat --home-dir "$APP_ROOT" --shell /usr/sbin/nologin hyprchat
pass "Service user ready"

# ── System packages ──
step "Installing system packages (log: $APT_LOG)..."
{
    apt-get update
    # libgl1 and libglib2.0-0t64 are needed by OpenCV, which the scanned-PDF OCR uses.
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv ca-certificates curl libgl1 libglib2.0-0t64 openssh-client
} > "$APT_LOG" 2>&1 || die "apt failed; see $APT_LOG"
pass "System packages ready"

# ── Directories, environment and permissions ──
step "Creating directories and the environment file..."
mkdir -p "$DATA_ROOT"
if [ ! -f "$APP_ROOT/.env" ]; then
    cp "$APP_ROOT/.env.example" "$APP_ROOT/.env"
    pass "Created $APP_ROOT/.env from .env.example"
else
    pass "Kept the existing $APP_ROOT/.env"
fi
chown -R root:hyprchat "$APP_ROOT"
chmod -R g+rX "$APP_ROOT"
chmod 750 "$APP_ROOT"
chmod 640 "$APP_ROOT/.env"
repair_data_permissions
# Secrets for Hephaestus (code sandbox, Daedalus, documents) live in their own
# root-only file that systemd reads before starting the service.
mkdir -p "$HEPHAESTUS_ENV_DIR"
chmod 755 "$HEPHAESTUS_ENV_DIR"
if [ ! -f "$HEPHAESTUS_ENV" ]; then
    cat > "$HEPHAESTUS_ENV" <<'EOF'
# Hephaestus settings for HyprChat (root-only; loaded by systemd).
# Fill these in when you set up Hephaestus (see docs/hephaestus.md), then
# run: systemctl restart hyprchat
# HEPHAESTUS_TOKEN=
# CODEBOX_URL=http://<hephaestus-ip>:8585
# OPENHANDS_URL=http://<hephaestus-ip>:8587
# DAEDALUS_ENGINE_URL=http://<hephaestus-ip>:8587
EOF
    pass "Created $HEPHAESTUS_ENV (empty; for Hephaestus later)"
else
    pass "Kept the existing $HEPHAESTUS_ENV"
fi
chown root:root "$HEPHAESTUS_ENV"
chmod 600 "$HEPHAESTUS_ENV"
pass "Directories and permissions ready"

# ── Python environment ──
step "Installing Python dependencies into $APP_ROOT/venv..."
python3 -m venv "$APP_ROOT/venv"
"$APP_ROOT/venv/bin/python" -m pip install --quiet --require-hashes -r "$APP_ROOT/backend/requirements.lock.txt"
"$APP_ROOT/venv/bin/python" -m pip check
pass "Python dependencies installed"

# ── systemd unit ──
step "Installing the systemd service..."
cp "$APP_ROOT/backend/hyprchat.service" "$UNIT_DIR/hyprchat.service"
chmod 644 "$UNIT_DIR/hyprchat.service"
mkdir -p "$UNIT_DIR/hyprchat.service.d"
cat > "$UNIT_DIR/hyprchat.service.d/zy-hephaestus.conf" <<EOF
[Service]
EnvironmentFile=-$HEPHAESTUS_ENV
EOF
chmod 644 "$UNIT_DIR/hyprchat.service.d/zy-hephaestus.conf"
systemctl daemon-reload
systemctl enable hyprchat > /dev/null 2>&1
pass "Service installed"

# ── Start and check ──
step "Starting HyprChat..."
repair_data_permissions
systemctl restart hyprchat
URL="$(local_url)"
for _ in $(seq 1 30); do
    service_answers && break
    sleep 1
done
if ! service_answers; then
    fail "HyprChat did not answer at $URL/api/health/live within 30 seconds."
    echo "     Last log lines:"
    journalctl -u hyprchat -n 30 --no-pager | sed 's/^/       /'
    echo "     Fix the cause (often a value in $APP_ROOT/.env), then run this script again."
    exit 1
fi
pass "HyprChat is answering"

echo ""
echo -e "  ${GRN}══════════════════════════════════════════${RST}"
echo -e "  ${BLD}${GRN}HyprChat is running${RST}"
echo ""
echo -e "  UI:        ${CYN}${URL}${RST}"
echo -e "  API docs:  ${CYN}${URL}/docs${RST}"
echo -e "  Health:    ${CYN}${URL}/api/health/live${RST}"
if [ "$(env_value HOST)" = "127.0.0.1" ] || [ -z "$(env_value HOST)" ]; then
    echo -e "  ${DIM}Bound to 127.0.0.1: reachable from this machine only.${RST}"
fi
echo ""
echo -e "  ${BLD}Next steps${RST}"
echo "   1. Edit $APP_ROOT/.env: service URLs (OLLAMA_URL, SEARXNG_URL…), model"
echo "      names, and HOST (a private address such as your Tailscale IP to use it"
echo "      from other devices). Then: systemctl restart hyprchat"
echo "   2. HTTPS for the PWA and microphone: tailscale serve --bg ${URL}"
echo "   3. Code execution and Daedalus: set up Hephaestus (docs/hephaestus.md) and"
echo "      fill in $HEPHAESTUS_ENV. Documents: run scripts/install-documents.sh"
echo "      on the Hephaestus VM (docs/documents.md)."
echo "   4. Upgrades: scripts/staged_release.py prepare / activate (README → Deployment)."
echo ""
echo -e "  ${DIM}Logs:      journalctl -u hyprchat -f${RST}"
echo -e "  ${DIM}Restart:   systemctl restart hyprchat${RST}"
echo -e "  ${GRN}══════════════════════════════════════════${RST}"
echo ""
