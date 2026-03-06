# HyprChat

Self-hosted AI chat platform with tool calling, deep research, model management, and a council of AI mode.
Built with FastAPI + single-file React SPA. No build step required. Note n8n functionality is not yet implemented.

## Architecture

```
User → HyprChat server (<YOUR_SERVER_IP>:8000)
         ├── Frontend: Single-file React SPA (frontend/dist/index.html)
         ├── Backend:  FastAPI + SSE streaming (backend/main.py)
         ├── Ollama    (<OLLAMA_IP>:11434)  — local LLM inference
         ├── Codebox   (<CODEBOX_IP>:8585)  — sandboxed code execution
         ├── SearXNG   (<SEARXNG_IP>:8888)  — web search
         └── n8n       (<N8N_IP>:5678)      — workflow automation (optional) 
```

## Configuration

Edit `backend/config.py` or set environment variables:

```python
OLLAMA_URL   = "http://<OLLAMA_IP>:11434"
CODEBOX_URL  = "http://<CODEBOX_IP>:8585"
SEARXNG_URL  = "http://<SEARXNG_IP>:8888"
N8N_URL      = "http://<N8N_IP>:5678"
```

The Ollama URL can also be changed at runtime from the Settings panel.

## Features

### Chat
- Streaming responses with live token counter and speed display
- Per-conversation model selection
- Personas with avatars, custom system prompts, and KB injection
- Conversation tags and sidebar tag filtering
- Export conversation as Markdown
- File attachments and paste-to-attach
- Prompt library with quick-insert

### Tool Suites

**CodeAgent** — Full sandbox environment (Codebox):
- Execute code in 30+ languages
- Shell commands, file read/write, project downloads
- Install packages at runtime

**Deep Research** — Multi-phase parallel web research engine:
- 5 depth levels (quick scan to exhaustive sweep)
- Compare mode (A vs B), focus areas
- Cross-references multiple sources, reads full pages
- Inline source cards with thumbnails rendered in chat

**Quick Search** — Instant SearXNG-backed search:
- Inline card grid with thumbnails, YouTube previews, favicons
- Attached to the triggering message in chat

**Conspiracy Research** — Alternate-source deep dive:
- Searches whistleblower sites, FOIA vaults, leaked doc archives
- CIA reading room, FBI vault, topic-specific gov sources
- Uncensored synthesis

**fetch_url** — Fetch and read any URL directly

### Model Manager
Dedicated panel for managing Ollama models and downloading new ones:

- **Ollama tab** — Installed models grouped by family, Use/Remove buttons, pull by name
- **HuggingFace tab** — Search GGUF models (GGUF-only filter), model detail with file selector and README preview, streaming download directly to Ollama
- Multi-part GGUF support (auto-detects and downloads all parts)
- **Downloads bar** — Collapsible pill in the top bar showing all active/queued downloads with live progress, download speed, and ETA

### Council of AI
Run multiple models in parallel on the same prompt:
- Each member responds independently
- AI peer voting — members vote for the best answer
- Points system tracks model quality over time
- Host model synthesizes all responses with vote context

### Workspaces
Group related conversations, track files across chats, analyze topics, and generate personas from knowledge bases.

### Knowledge Bases
Upload documents (PDF, Markdown, text, code, etc.) and attach them to personas for automatic injection into the system prompt.

### Personas
Named AI personalities with avatars, model config, system prompts, temperature/context settings, and linked knowledge bases and tools.

### Settings
- 14 themes, 9 fonts
- Font size, chat width, UI size sliders
- Per-model parameters (temperature, top_p, top_k, num_ctx, repeat_penalty)
- Ollama URL override at runtime

## Deployment

### First-time setup (run inside your server/LXC as root)

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

### Ongoing updates (from your dev machine)

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

## Stack

- **Backend**: Python 3.11+, FastAPI, httpx, SQLite (aiosqlite)
- **Frontend**: React 18 (Babel in-browser), no build step
- **LLM**: Ollama (native tool calling protocol)
- **Search**: SearXNG
- **Sandbox**: Codebox API
