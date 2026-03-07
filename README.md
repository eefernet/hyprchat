## 📝 This program is still very much under construction with missing features, bad code and more. Check releases for a "stable" alpha build
# 🧠 HyprChat

**Self-hosted AI chat platform** with tool calling, deep research, model management, and a council of AI mode.

Built with FastAPI + a single-file React SPA — no build step required.

---

## ✨ Features

### 💬 Chat
- SSE streaming responses with live token counter & speed display
- Per-conversation model selection & system prompts
- Personas with avatars, custom configs, and knowledge base injection
- Conversation tags & sidebar filtering
- File attachments & paste-to-attach
- Prompt library with quick-insert ⚡
- Export conversation as Markdown

### 🛠️ Tool Suites

| Tool | Description |
|------|-------------|
| **CodeAgent** | Sandboxed code execution in 30+ languages, shell commands, file I/O, package installs |
| **generate_code** | Agentic code generation via OpenHands SDK — writes, tests, and fixes code automatically in the sandbox |
| **Deep Research** | Multi-phase parallel web research with 5 depth levels, compare mode, cross-referencing |
| **Quick Search** | Instant SearXNG-backed search with inline card grid, thumbnails & YouTube previews |
| **Conspiracy Research** | Alt-source deep dive — FOIA vaults, whistleblower sites, CIA reading room, FBI vault |
| **fetch_url** | Fetch and read any URL directly |

### 📦 Model Manager
- **Ollama tab** — Installed models grouped by family, Use/Remove buttons, pull by name
- **HuggingFace tab** — Search GGUF models, model detail with file selector & README preview, streaming download → Ollama
- **Multi-part GGUF** — Auto-detects and downloads all split parts
- **Downloads bar** — Live progress, speed, ETA for all active downloads

### 🏛️ Council of AI
- Run multiple models in parallel on the same prompt
- AI peer voting — members vote for the best answer
- Points system tracks model quality over time
- Host model synthesizes all responses with vote context

### 📚 Knowledge Bases
Upload documents (PDF, Markdown, text, code) and attach them to personas for automatic system prompt injection.

### 🎭 Personas
Named AI personalities with avatars, model config, system prompts, temperature/context settings, and linked KBs & tools.

### 🗂️ Workspaces
Group related conversations, track files across chats, analyze topics, and generate personas from knowledge bases.

### ⚙️ Settings
- 🎨 14 themes & 9 fonts
- Font size, chat width, UI size sliders
- Per-model parameters (temperature, top_p, top_k, num_ctx, repeat_penalty)
- Runtime Ollama URL override

---

## 🏗️ Architecture

```
User → HyprChat server (<SERVER_IP>:8000)
         ├── Frontend: Single-file React SPA (frontend/dist/index.html)
         ├── Backend:  FastAPI + SSE streaming (backend/main.py)
         ├── Ollama    (<OLLAMA_IP>:11434)  — local LLM inference
         ├── Codebox   (<CODEBOX_IP>:8585)  — sandboxed code execution
         │     └── OpenHands Worker (:8586) — agentic code generation (OpenHands SDK)
         ├── SearXNG   (<SEARXNG_IP>:8888)  — web search
         └── n8n       (<N8N_IP>:5678)      — workflow automation (optional)
```

## 🔧 Configuration

Edit `backend/config.py` or set environment variables:

```python
OLLAMA_URL   = "http://<OLLAMA_IP>:11434"
CODEBOX_URL  = "http://<CODEBOX_IP>:8585"
SEARXNG_URL  = "http://<SEARXNG_IP>:8888"
N8N_URL      = "http://<N8N_IP>:5678"
CODER_MODEL  = ""                          # empty = use chat model
OPENHANDS_ENABLED = True                   # toggle agentic code generation
OPENHANDS_MAX_ROUNDS = 8                   # max agent iterations per task
```

The Ollama URL, coder model, and OpenHands settings can also be changed at runtime from the Settings panel.

---

## 🚀 Deployment

### First-time setup

```bash
apt update && apt install -y python3 python3-pip
mkdir -p /opt/hyprchat/{backend,frontend/dist,data}
cp -r backend/* /opt/hyprchat/backend/
cp frontend/dist/index.html /opt/hyprchat/frontend/dist/
cd /opt/hyprchat/backend && pip install -r requirements.txt --break-system-packages
cp backend/hyprchat.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now hyprchat
```

Or use the included deploy script:

```bash
bash scripts/deploy.sh
```

### Updating

```bash
scp backend/main.py backend/config.py backend/database.py root@<SERVER_IP>:/opt/hyprchat/backend/
scp frontend/dist/index.html root@<SERVER_IP>:/opt/hyprchat/frontend/dist/
ssh root@<SERVER_IP> "systemctl restart hyprchat"
```

---

## 📋 Logs & Service Management

```bash
journalctl -u hyprchat -f        # live logs
systemctl restart hyprchat       # restart
systemctl status hyprchat        # status
```

---

## 🧰 Stack

| Layer | Tech |
|-------|------|
| **Backend** | Python 3.11+, FastAPI, httpx, SQLite (aiosqlite) |
| **Frontend** | React 18 (Babel in-browser), zero build step |
| **LLM** | Ollama (native tool calling protocol) |
| **Search** | SearXNG |
| **Sandbox** | Codebox API (LXC container) |
| **Agentic Coding** | OpenHands SDK v1.12 (runs inside Codebox LXC) |
