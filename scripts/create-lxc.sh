#!/bin/bash
# ============================================================
# HyprChat — Proxmox LXC Creator
# Run this on the Proxmox HOST (not inside a container)
#
# Usage:
#   bash create-lxc.sh [CTID] [IP]
#   bash create-lxc.sh 120 192.168.1.120
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTID=${1:-120}
IP=${2:-"192.168.1.120"}
GW="192.168.1.1"
HOSTNAME="hyprchat"
STORAGE="local-lvm"      # Change if your storage differs
TEMPLATE="local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"
MEMORY=2048
SWAP=512
DISK=8
CORES=2
BRIDGE="vmbr0"
NAMESERVER="1.1.1.1"

echo "╔══════════════════════════════════════════════╗"
echo "║     HyprChat — Proxmox LXC Creator          ║"
echo "║                                              ║"
echo "║  CTID:     $CTID                             "
echo "║  IP:       $IP/24                            "
echo "║  Gateway:  $GW                               "
echo "║  Memory:   ${MEMORY}MB                       "
echo "║  Disk:     ${DISK}GB                         "
echo "║  Cores:    $CORES                            "
echo "╚══════════════════════════════════════════════╝"
echo ""

# Check if template exists
if ! pveam list local | grep -q "ubuntu-24.04"; then
    echo "[!] Ubuntu 24.04 template not found. Downloading..."
    pveam download local ubuntu-24.04-standard_24.04-2_amd64.tar.zst
fi

# Find the exact template name
TEMPLATE=$(pveam list local | grep "ubuntu-24.04" | tail -1 | awk '{print $1}')
echo "[*] Using template: $TEMPLATE"

# Check CTID not in use
if pct status $CTID &>/dev/null; then
    echo "[!] CTID $CTID already exists. Choose a different one."
    exit 1
fi

# Create the container
echo "[1/5] Creating LXC container..."
pct create $CTID $TEMPLATE \
    --hostname $HOSTNAME \
    --memory $MEMORY \
    --swap $SWAP \
    --cores $CORES \
    --rootfs ${STORAGE}:${DISK} \
    --net0 name=eth0,bridge=${BRIDGE},ip=${IP}/24,gw=${GW} \
    --nameserver $NAMESERVER \
    --unprivileged 1 \
    --features nesting=1 \
    --onboot 1 \
    --start 0

echo "[2/5] Starting container..."
pct start $CTID
sleep 3

# Wait for container to be ready
echo "[3/5] Waiting for container to be ready..."
for i in {1..30}; do
    if pct exec $CTID -- test -f /etc/os-release 2>/dev/null; then
        break
    fi
    sleep 1
done

echo "[4/5] Installing system packages inside LXC..."
pct exec $CTID -- bash -c "
    apt update -qq
    apt install -y -qq python3-pip curl wget > /dev/null 2>&1
    echo 'Packages installed.'
"

echo "[5/5] Creating project directory structure..."
pct exec $CTID -- bash -c "
    mkdir -p /opt/hyprchat/{backend,frontend/dist,scripts,data/{uploads/avatars,tools,knowledge_bases,comfy_workflows}}
    mkdir -p /opt/hyprchat/data/sandbox/{outputs,workspace}
"

echo ""
read -r -p "Create companion ComfyUI + Voice LXC now? [y/N] " CREATE_COMFY
if [[ "$CREATE_COMFY" =~ ^[Yy]$ ]]; then
    read -r -p "ComfyUI CTID [115]: " COMFY_CTID
    read -r -p "ComfyUI IP [192.168.1.115]: " COMFY_IP
    COMFY_CTID=${COMFY_CTID:-115}
    COMFY_IP=${COMFY_IP:-192.168.1.115}
    if [ -x "$SCRIPT_DIR/create-comfyui-lxc.sh" ] || [ -f "$SCRIPT_DIR/create-comfyui-lxc.sh" ]; then
        bash "$SCRIPT_DIR/create-comfyui-lxc.sh" "$COMFY_CTID" "$COMFY_IP"
    else
        echo "[!] scripts/create-comfyui-lxc.sh not found. Run it later from this repo to create the companion media LXC."
    fi
else
    echo "[*] Skipping companion ComfyUI + Voice LXC."
fi

echo ""
echo "════════════════════════════════════════════════"
echo "  LXC $CTID created and ready!"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Build the frontend, then copy the project files to the container."
echo "     frontend/dist/ is build output (gitignored) — build it on your dev"
echo "     machine BEFORE creating the tarball, or deploy.sh will abort:"
echo ""
echo "     ( cd frontend && npm install && npm run build )"
echo "     tar czf hyprchat-project.tar.gz --exclude node_modules ."
echo ""
echo "     # Upload tar to Proxmox host first"
echo "     scp hyprchat-project.tar.gz root@<PROXMOX_IP>:/tmp/"
echo ""
echo "     # Then from Proxmox host, push into the LXC:"
echo "     pct push $CTID /tmp/hyprchat-project.tar.gz /tmp/hyprchat-project.tar.gz"
echo "     pct exec $CTID -- bash -c 'cd /opt && tar xzf /tmp/hyprchat-project.tar.gz -C /opt/hyprchat'"
echo ""
echo "  2. Run the deploy script inside the container:"
echo "     pct exec $CTID -- bash /opt/hyprchat/scripts/deploy.sh"
echo ""
echo "  3. Access HyprChat at: http://${IP}:8000"
echo "     API docs at:        http://${IP}:8000/docs"
echo ""
echo "════════════════════════════════════════════════"
