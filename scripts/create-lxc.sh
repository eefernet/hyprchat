#!/bin/bash
# ============================================================
# HyprChat — Proxmox LXC Creator
# Run this on the Proxmox HOST (not inside a container)
#
# Usage:
#   bash create-lxc.sh <CTID> <IP> [GATEWAY]
#   bash create-lxc.sh 120 10.0.0.120          # gateway defaults to <first three octets>.1
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTID=${1:-}
IP=${2:-}
if [ -z "$CTID" ] || [ -z "$IP" ]; then
    echo "Usage: bash create-lxc.sh <CTID> <IP> [GATEWAY]" >&2
    exit 2
fi
GW=${3:-"${IP%.*}.1"}
HOSTNAME="hyprchat"
# Change if your storage differs.
STORAGE="local-lvm"
MEMORY=2048
SWAP=512
DISK=20
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

# Ubuntu 24.04 provides the Python 3.12 that HyprChat's dependency lock is built for.
if ! pveam list local | grep -q "ubuntu-24.04-standard"; then
    echo "[!] Ubuntu 24.04 template not found. Downloading..."
    pveam update > /dev/null
    TEMPLATE_NAME=$(pveam available --section system | awk '/ubuntu-24.04-standard/ {print $2}' | sort -V | tail -1)
    [ -n "$TEMPLATE_NAME" ] || { echo "[!] No Ubuntu 24.04 template is available from pveam." >&2; exit 1; }
    pveam download local "$TEMPLATE_NAME"
fi

# Find the exact template name
TEMPLATE=$(pveam list local | grep "ubuntu-24.04-standard" | sort -V | tail -1 | awk '{print $1}')
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

echo "[4/5] Updating packages inside the LXC..."
pct exec $CTID -- bash -c "
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates > /dev/null
    echo 'Packages updated.'
"

echo "[5/5] Creating /opt/hyprchat..."
pct exec $CTID -- mkdir -p /opt/hyprchat

echo ""
read -r -p "Create companion Hephaestus Docker VM now? [y/N] " CREATE_HEPHAESTUS
if [[ "$CREATE_HEPHAESTUS" =~ ^[Yy]$ ]]; then
    bash "$SCRIPT_DIR/create-hephaestus-vm.sh"
fi

read -r -p "Create companion ComfyUI + Voice LXC now? [y/N] " CREATE_COMFY
if [[ "$CREATE_COMFY" =~ ^[Yy]$ ]]; then
    read -r -p "ComfyUI CTID [115]: " COMFY_CTID
    read -r -p "ComfyUI IP: " COMFY_IP
    COMFY_CTID=${COMFY_CTID:-115}
    if [ -z "$COMFY_IP" ]; then
        echo "[!] No ComfyUI IP given; run scripts/create-comfyui-lxc.sh <CTID> <IP> later."
        CREATE_COMFY=n
    fi
    if [ -z "$COMFY_IP" ]; then
        :
    elif [ -x "$SCRIPT_DIR/create-comfyui-lxc.sh" ] || [ -f "$SCRIPT_DIR/create-comfyui-lxc.sh" ]; then
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
echo "  1. On your dev machine, build the frontend and make a clean export."
echo "     git archive leaves out .env, .deploy_config.json, data/ and anything"
echo "     else that is not committed; frontend/dist/ is added after it:"
echo ""
echo "     ( cd frontend && npm ci && npm run build )"
echo "     git archive --format=tar -o hyprchat-project.tar HEAD"
echo "     tar rf hyprchat-project.tar frontend/dist"
echo "     gzip -f hyprchat-project.tar"
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
echo "  3. deploy.sh prints the address HyprChat answers on. It binds to"
echo "     127.0.0.1 until you set HOST in /opt/hyprchat/.env (for example to"
echo "     ${IP} or a Tailscale address) and restart it."
echo ""
echo "════════════════════════════════════════════════"
