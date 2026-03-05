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
    mkdir -p /opt/hyprchat/{backend,frontend/dist,scripts,data/{uploads/avatars,tools,knowledge_bases}}
"

echo ""
echo "════════════════════════════════════════════════"
echo "  LXC $CTID created and ready!"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Copy the project files to the container:"
echo "     From your local machine (where you have the tar.gz):"
echo ""
echo "     # Upload tar to Proxmox host first"
echo "     scp hyprchat-project-v4.tar.gz root@<PROXMOX_IP>:/tmp/"
echo ""
echo "     # Then from Proxmox host, push into the LXC:"
echo "     pct push $CTID /tmp/hyprchat-project-v4.tar.gz /tmp/hyprchat-project-v4.tar.gz"
echo "     pct exec $CTID -- bash -c 'cd /opt && tar xzf /tmp/hyprchat-project-v4.tar.gz --strip-components=1 -C /opt/hyprchat'"
echo ""
echo "  2. Run the deploy script inside the container:"
echo "     pct exec $CTID -- bash /opt/hyprchat/scripts/deploy.sh"
echo ""
echo "  3. Access HyprChat at: http://${IP}:8000"
echo "     API docs at:        http://${IP}:8000/docs"
echo ""
echo "════════════════════════════════════════════════"
