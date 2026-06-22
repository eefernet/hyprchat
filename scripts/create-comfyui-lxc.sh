#!/bin/bash
# ============================================================
# HyprChat — ComfyUI + Voice (STT/TTS) LXC Creator
# Run this on the Proxmox HOST with the GPUs (pve2), not inside a container.
#
# Creates a privileged Ubuntu 24.04 LXC with NVIDIA GPU passthrough (cgroup
# device sharing — same mechanism as the Ollama LXC) and installs:
#   - ComfyUI (SDXL image generation)        → :8188   (pinned to GPU 1)
#   - Speaches (OpenAI-compatible Whisper)   → :8001
#   - kokoro-fastapi (OpenAI-compatible TTS) → :8880
#
# Usage:
#   bash create-comfyui-lxc.sh [CTID] [IP]
#   bash create-comfyui-lxc.sh 115 192.168.1.115
#
# After it finishes, enter the container (`pct enter <CTID>`) and run the
# printed PHASE 2 steps (NVIDIA userspace libs + service installs) — those
# need the host driver version and a checkpoint download, so they are
# interactive by design.
# ============================================================
set -e

CTID=${1:-115}
IP=${2:-"192.168.1.115"}
GW="192.168.1.1"
HOSTNAME="comfyui"
MEMORY=16384
SWAP=2048
DISK=64
CORES=8
BRIDGE="vmbr0"
NAMESERVER="1.1.1.1"

echo "ComfyUI+Voice LXC: CTID=$CTID IP=$IP/24 MEM=${MEMORY}MB DISK=${DISK}GB"

if ! pveam list local | grep -q "ubuntu-24.04"; then
    echo "[!] Ubuntu 24.04 template not found. Downloading..."
    pveam download local ubuntu-24.04-standard_24.04-2_amd64.tar.zst
fi
TEMPLATE=$(pveam list local | grep "ubuntu-24.04" | tail -1 | awk '{print $1}')
echo "[*] Using template: $TEMPLATE"

if pct status $CTID &>/dev/null; then
    echo "[!] CTID $CTID already exists. Choose a different one."
    exit 1
fi

pct create $CTID "$TEMPLATE" \
    --hostname $HOSTNAME \
    --memory $MEMORY \
    --swap $SWAP \
    --cores $CORES \
    --rootfs local-lvm:$DISK \
    --net0 name=eth0,bridge=$BRIDGE,ip=$IP/24,gw=$GW \
    --nameserver $NAMESERVER \
    --unprivileged 0 \
    --features nesting=1 \
    --onboot 1

# ── GPU sharing (verify the nvidia-uvm major number: ls -l /dev/nvidia-uvm) ──
UVM_MAJOR=$(stat -c %t /dev/nvidia-uvm 2>/dev/null | xargs -I{} printf "%d" 0x{} || echo 234)
cat >> /etc/pve/lxc/$CTID.conf <<EOF
lxc.cgroup2.devices.allow: c 195:* rwm
lxc.cgroup2.devices.allow: c ${UVM_MAJOR}:* rwm
lxc.mount.entry: /dev/nvidia0 dev/nvidia0 none bind,optional,create=file
lxc.mount.entry: /dev/nvidia1 dev/nvidia1 none bind,optional,create=file
lxc.mount.entry: /dev/nvidiactl dev/nvidiactl none bind,optional,create=file
lxc.mount.entry: /dev/nvidia-uvm dev/nvidia-uvm none bind,optional,create=file
lxc.mount.entry: /dev/nvidia-uvm-tools dev/nvidia-uvm-tools none bind,optional,create=file
EOF

pct start $CTID
sleep 5
pct exec $CTID -- bash -c "apt-get update && apt-get install -y git curl python3 python3-venv python3-pip build-essential"

HOST_DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)

cat <<EOF

============================================================
LXC $CTID created and started ($IP).

PHASE 2 — run inside the container (pct enter $CTID):

  # 1. NVIDIA userspace libs MATCHING the host driver ($HOST_DRIVER):
  wget https://us.download.nvidia.com/XFree86/Linux-x86_64/$HOST_DRIVER/NVIDIA-Linux-x86_64-$HOST_DRIVER.run
  bash NVIDIA-Linux-x86_64-$HOST_DRIVER.run --no-kernel-module --silent
  nvidia-smi   # must show both 3090s

  # 2. ComfyUI + SDXL (pinned to GPU 1 so Ollama keeps GPU 0 headroom):
  git clone https://github.com/comfyanonymous/ComfyUI /opt/comfyui
  python3 -m venv /opt/comfyui/venv
  # Install ALL THREE torch packages from the cu124 index BEFORE requirements.txt —
  # otherwise requirements pulls a PyPI torchaudio built for a newer CUDA
  # (libcudart.so.13 missing → ComfyUI crash-loops).
  /opt/comfyui/venv/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
  /opt/comfyui/venv/bin/pip install -r /opt/comfyui/requirements.txt
  wget -O /opt/comfyui/models/checkpoints/sd_xl_base_1.0.safetensors \\
    "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors"

  cat > /etc/systemd/system/comfyui.service <<'UNIT'
[Unit]
Description=ComfyUI
After=network.target
[Service]
Environment=HYPRCHAT_COMFY_IDLE_UNLOAD_SECONDS=300
Environment=HYPRCHAT_COMFY_WATCH_INTERVAL_SECONDS=10
Environment="HYPRCHAT_COMFY_RESTART_COMMAND=systemctl restart comfyui"
ExecStart=/opt/comfyui/venv/bin/python main.py --listen 0.0.0.0 --port 8188 --cuda-device 1 --disable-smart-memory
WorkingDirectory=/opt/comfyui
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT

  # 2b. Install HyprChat's ComfyUI control node from this repo on your dev machine.
  #     The node is optional but enables purge cleanup, explicit model unload,
  #     memory status, restart, and 5-minute idle model unload:
  #       scp scripts/comfyui_hyprchat_cleanup.py root@$IP:/opt/comfyui/custom_nodes/
  #       systemctl daemon-reload
  #       systemctl enable --now comfyui
  #       journalctl -u comfyui -n 80 --no-pager | grep "HyprChat ComfyUI" || true
  #
  #     To tune idle unload later:
  #       systemctl edit comfyui
  #       # [Service]
  #       # Environment=HYPRCHAT_COMFY_IDLE_UNLOAD_SECONDS=300
  #       # Environment=HYPRCHAT_COMFY_WATCH_INTERVAL_SECONDS=10
  #       # Environment="HYPRCHAT_COMFY_RESTART_COMMAND=systemctl restart comfyui"

  # 3. Voice services (docker is simplest; both are OpenAI-compatible).
  #    nvidia-container-toolkit needs NVIDIA's apt repo; docker-in-LXC needs
  #    no-cgroups=true and apparmor=unconfined on the containers.
  apt-get install -y docker.io curl gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \\
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \\
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update && apt-get install -y nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
  sed -i 's/^#no-cgroups = false/no-cgroups = true/' /etc/nvidia-container-runtime/config.toml
  systemctl restart docker
  docker run -d --name speaches --restart unless-stopped --security-opt apparmor=unconfined --gpus all -p 8001:8000 \\
    ghcr.io/speaches-ai/speaches:latest-cuda
  docker run -d --name kokoro --restart unless-stopped --security-opt apparmor=unconfined --gpus all -p 8880:8880 \\
    ghcr.io/remsky/kokoro-fastapi-gpu:latest
  # Speaches does NOT auto-download models — install the whisper model explicitly:
  curl -X POST "http://localhost:8001/v1/models/Systran%2Ffaster-distil-whisper-large-v3"

  systemctl enable --now comfyui

VERIFY (from any LAN box):
  curl http://$IP:8188/system_stats
  curl -X POST http://$IP:8188/hyprchat/free
  curl http://$IP:8188/hyprchat/memory
  curl -X POST http://$IP:8188/hyprchat/restart
  curl http://$IP:8001/v1/models
  curl http://$IP:8880/v1/audio/voices

Then in HyprChat → Settings → Connections set:
  ComfyUI:   http://$IP:8188
  Voice STT: http://$IP:8001
  Voice TTS: http://$IP:8880
============================================================
EOF
