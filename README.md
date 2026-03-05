# HyprChat

Hyprland-themed AI chat platform with integrated tool calling.
Self-hosted on any Linux server or Proxmox LXC with Ollama, Codebox, and SearXNG.

## Architecture

```
User → HyprChat server (<YOUR_SERVER_IP>:8000)
         ├── Frontend: Single-file React (index.html)
         ├── Backend: FastAPI + SSE streaming
         ├── Ollama (<OLLAMA_IP>:11434) — local LLM inference
         ├── Codebox (<CODEBOX_IP>:8585) — Code execution sandbox
         └── SearXNG (<SEARXNG_IP>:8888) — Web search
```

## Configuration

Edit `backend/config.py` (or set environment variables) to point to your services:

```python
OLLAMA_URL   = "http://<OLLAMA_IP>:11434"
CODEBOX_URL  = "http://<CODEBOX_IP>:8585"
SEARXNG_URL  = "http://<SEARXNG_IP>:8888"
N8N_URL      = "http://<N8N_IP>:5678"
```

## Tool Suites

### ⚡ CodeAgent
Code execution (30+ languages), shell commands, file management,
web research, URL fetching, file downloads.

### 🔬 Deep Research
Multi-phase parallel research engine with 5 depth levels.
Includes compare mode and focus areas.

### ⚡ Quick Search
SearXNG-backed instant search with inline YouTube, image, and web previews.

### ⚖️ Council of AI
Run multiple models in parallel on the same prompt. Models earn points
based on response quality. A host model synthesizes all answers.

## Deployment

### First-time setup (run inside your server/LXC as root)

```bash
apt update && apt install -y python3 python3-pip
mkdir -p /opt/hyprchat/{backend,frontend/dist,data}
cp backend/* /opt/hyprchat/backend/
cp frontend/dist/index.html /opt/hyprchat/frontend/dist/
cd /opt/hyprchat/backend && pip install -r requirements.txt --break-system-packages
cp backend/hyprchat.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now hyprchat
```

Or use the included deploy script:
```bash
bash scripts/deploy.sh
```

### Ongoing updates (from your dev machine)

Run the deploy monitor — it watches for file changes and prompts you to push:
```bash
python3 deploy_monitor.py
```

Or push manually:
```bash
scp backend/main.py backend/config.py backend/database.py root@<SERVER_IP>:/opt/hyprchat/backend/
scp frontend/dist/index.html root@<SERVER_IP>:/opt/hyprchat/frontend/dist/
ssh root@<SERVER_IP> "systemctl restart hyprchat"
```

## Logs & Service Management

```bash
journalctl -u hyprchat -f        # live logs
systemctl restart hyprchat       # restart
systemctl status hyprchat        # status
```
