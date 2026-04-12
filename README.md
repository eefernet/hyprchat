# 🧠 HyprChat

**Self-hosted AI chat platform** — tool calling, agentic code generation, deep research, multi-model councils, workflow automation, and full model management. All running on your own hardware.

Built with FastAPI + a single-file React SPA. No build step, no cloud dependencies.

> ⚠️ Alpha software — actively developed, expect rough edges. Check [releases](https://github.com/eefernet/hyprchat/releases) for stable builds.

---

## ✨ Core Features

### 💬 Chat
- SSE streaming with live token counter, speed display, and thinking token visualization
- Per-conversation model selection, system prompts, and parameter overrides
- Conversation forking — branch from any message to explore alternatives
- Full-text search (FTS5) across all messages with highlighted snippets
- Conversation tags, pinning, and sidebar filtering
- Auto-generated titles via LLM after first exchange
- Export as Markdown or JSON (with reimport)
- Keyboard shortcuts — `Ctrl+K` search, `Ctrl+N` new chat, `Ctrl+/` toggle sidebar

### 📄 File Attachments
- Drag-and-drop or paste files directly into chat
- **PDF support** — text extracted server-side via `pypdf`, displayed as a compact badge in chat while full content is sent to the model
- Project archives (`.zip`, `.tar.gz`) route to Coder Bot automatically
- Text files attached inline with syntax highlighting

### 🤖 Coder Bot
- **Plan-first architecture** — configurable planning model analyzes the task before writing code
- **Smart routing** — automatically decides between direct tool use and the full OpenHands agent based on project complexity
- **Overseer verification** — reviews agent output against user specs, re-prompts if needed
- **Project uploads** — drop a `.zip`/`.tar.gz` and the agent works inside your existing codebase
- Live progress pills with real-time status from the coding agent
- Sandboxed execution via Codebox (LXC) with 30+ language support
- Code-block rescue, error recovery hints, dev-server detection, and context pruning

### 🛠️ Tool Suites

| Tool | Description |
|------|-------------|
| `execute_code` | Sandboxed code execution in 30+ languages with package installs |
| `generate_code` | Agentic project generation via OpenHands — writes, tests, and fixes code |
| `plan_project` | Architecture planning with dedicated thinking model |
| `deep_research` | Multi-phase parallel web research with 5 depth levels and cross-referencing |
| `quick_search` | Instant SearXNG search with OG image cards, YouTube previews, and favicon badges |
| `research` | Web search + full page reading for grounded answers |
| `conspiracy_research` | Alt-source deep dive — FOIA vaults, CIA reading room, FBI vault, whistleblower sites |
| `fetch_url` | Fetch and read any URL directly |
| `write_file` | Write files to the sandbox |
| `read_file` | Read files from sandbox projects |
| `search_files` | Grep/regex across project files |
| `run_shell` | Execute shell commands in the sandbox |

### 📦 Model Manager
- **Ollama tab** — installed models grouped by family with size tags, capability badges (Vision, Thinking, Code, Tools), and Use/Remove buttons
- **HuggingFace tab** — search GGUF models, model detail with file selector and README preview, streaming download → Ollama
- **Multi-part GGUF** — auto-detects and downloads all split parts
- **Downloads bar** — live progress, speed, and ETA for all active downloads
- Clear error handling for missing/corrupt models

### 🏛️ Council of AI
- Run multiple models in parallel on the same prompt
- **Preset councils** — Philosophers, Visionaries, Scientists, Debaters (one-click setup)
- **Debate rounds** — configurable rebuttal rounds where members read and respond to each other
- **AI peer voting** — members vote for the best answer after debate
- Points system and performance analytics with win rates and recommendations
- Host model synthesizes all responses with full debate and vote context
- Expandable round-by-round history in chat

### 📚 Knowledge Bases & RAG
- Upload documents (PDF, Markdown, text, code) and attach to personas
- Sentence-aware chunking with code-aware splitting for Python/JS/TS
- ChromaDB vector storage with cosine similarity search
- Research tool results auto-indexed into per-persona memory
- Configurable chunk size, overlap, top_k, and embed model

### 🎭 Personas
- Named AI personalities with avatars, model config, system prompts, and temperature/context settings
- Linked knowledge bases and tool sets
- Persona avatar and name displayed in chat messages
- Seed bots: Coder Bot, Conspiracy Bot, Based Bot

### 🗂️ Workspaces
- Group related conversations and track files across chats
- AI-powered topic analysis using configurable workspace model
- Generate personas from workspace knowledge

### ⚡ Workflow Automation
- **5 step types** — `tool`, `ai_completion`, `parallel`, `loop`, `run_workflow`
- **Conditionals** — skip steps based on previous results
- **Variables** — `{{input}}`, `{{steps.N.result}}`, `{{vars.name}}`, `{{loop.item}}`, `{{webhook.field}}`
- **Retry & error handling** — per-step retry with exponential backoff
- **Cron scheduling** — automatic execution with enable/disable and run tracking
- **Webhook triggers** — unique URL per workflow for external integrations (GitHub, Home Assistant, n8n)
- **Chat trigger** — `/run Workflow Name input text`
- Visual step editor and seed presets (Deep Research, System Health Check, Scrape & Analyze, Multi-URL Scraper)

### 📊 Token Analytics
- Cumulative usage tracking per model, persona, and day
- Summary cards with CSS bar charts
- Configurable date range (7d / 30d / 90d)

### 🔍 Prompt Library
- Save and organize reusable prompts by category
- Quick-insert via ⚡ button in input bar
- Apply as system prompt templates without creating a persona

### ⚙️ Settings
- 🎨 14 themes (Terminal, Cyberpunk, Solarized, Dracula, Material Ocean, and more) and 9 monospace fonts
- Font size, chat width, and UI size sliders
- Per-model parameters (temperature, top_p, top_k, num_ctx, repeat_penalty)
- Configurable workspace analysis model, planning model, and coder model
- Runtime Ollama URL override
- Thinking mode control (Auto / On / Off)
- Auto-title toggle, scanline effect toggle, nav rail labels
- Danger zone: bulk delete all chats, purge all RAG collections

---

## 🏗️ Architecture

```
User → HyprChat Server (:8000)
         ├── Frontend:  Single-file React SPA (inline Babel, no build step)
         ├── Backend:   FastAPI + SSE streaming + SQLite
         ├── Ollama     (:11434) — local LLM inference
         ├── Codebox    (:8585)  — sandboxed code execution (LXC)
         │     └── OpenHands Worker (:8586) — agentic code generation
         ├── SearXNG    (:8888)  — private web search
         └── ChromaDB              — vector storage for RAG
```

### Key Backend Modules
| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI routes, SSE endpoints, model/workflow management |
| `backend/agents/chat.py` | Multi-round streaming chat agent with tool calling |
| `backend/agents/personas.py` | Seed bot definitions |
| `backend/tools.py` | Tool execution engine (code, research, OpenHands) |
| `backend/research.py` | Deep research engine |
| `backend/council.py` | Council debate, voting, and synthesis |
| `backend/events.py` | Async SSE EventBus (pub/sub with `asyncio.Lock`) |
| `backend/rag.py` | RAG pipeline (chunking, embedding, retrieval) |
| `backend/workflows.py` | Workflow executor and cron scheduler |
| `backend/hf.py` | HuggingFace model browser and download |
| `backend/database.py` | SQLite schema, migrations, and queries |
| `backend/config.py` | Configuration and environment variables |
| `frontend/dist/index.html` | Entire frontend — React SPA with inline Babel |

---

## 🔧 Configuration

Edit `backend/config.py` or set environment variables:

```python
OLLAMA_URL          = "http://<OLLAMA_IP>:11434"
CODEBOX_URL         = "http://<CODEBOX_IP>:8585"
OPENHANDS_URL       = "http://<CODEBOX_IP>:8586"
SEARXNG_URL         = "http://<SEARXNG_IP>:8888"
DEFAULT_MODEL       = "qwen3.5:27b"
CODER_MODEL         = ""              # empty = use chat model
WORKSPACE_MODEL     = "qwen3.5:4b"   # used for auto-title and topic analysis
OPENHANDS_ENABLED   = True
OPENHANDS_MAX_ROUNDS = 20
OPENHANDS_NUM_CTX   = 16384
MAX_AGENT_ROUNDS    = 12
```

Ollama URL, coder model, planning model, and OpenHands settings can also be changed at runtime from the Settings panel.

---

## 🚀 Deployment

### Requirements
- Python 3.11+
- Ollama instance with at least one model pulled
- Codebox server (for code execution — optional)
- SearXNG instance (for web search — optional)

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

### Updating

```bash
scp backend/*.py root@<SERVER_IP>:/opt/hyprchat/backend/
scp frontend/dist/index.html root@<SERVER_IP>:/opt/hyprchat/frontend/dist/
ssh root@<SERVER_IP> "systemctl restart hyprchat"
```

---

## 📋 Logs & Management

```bash
journalctl -u hyprchat -f        # live logs
systemctl restart hyprchat       # restart
systemctl status hyprchat        # status
```

---

## 🧪 Testing

101 tests covering all major features, running against a live server instance.

```bash
cd backend
pip install pytest httpx
python -m pytest tests/ -v

# Run specific categories
python -m pytest tests/ -v -k "chat"          # SSE streaming
python -m pytest tests/ -v -k "tool"          # tools & execution
python -m pytest tests/ -v -k "council"       # councils & debates
python -m pytest tests/ -v -k "workflow"      # workflow automation
python -m pytest tests/ -v -k "integration"   # end-to-end flows
```

| Category | Tests | Coverage |
|----------|-------|----------|
| Health & Settings | 10 | Health check, settings CRUD, changelog, analytics |
| Models | 7 | Listing, details, info, builtin tools |
| Conversations | 10 | CRUD, messages, search, forking |
| Chat / SSE | 3 | Streaming, token events, error handling |
| Knowledge Bases | 7 | KB CRUD, file upload, reindexing |
| Tools & Execution | 9 | Python/shell exec, fetch_url, web search |
| Personas | 9 | CRUD, seed bots |
| Workspaces | 7 | CRUD, conversation management |
| Councils | 11 | CRUD, members, presets, analytics |
| Workflows | 11 | CRUD, execution, webhooks, schedules |
| HuggingFace | 5 | GGUF search, model info |
| Integration | 5 | Full lifecycle flows |

---

## 🧰 Stack

| Layer | Tech |
|-------|------|
| **Backend** | Python 3.11+, FastAPI, httpx, SQLite (aiosqlite), ChromaDB |
| **Frontend** | React 18 (Babel in-browser), zero build step |
| **LLM** | Ollama (native tool calling + text-based fallback) |
| **Search** | SearXNG (private, self-hosted) |
| **Sandbox** | Codebox API (LXC container) |
| **Agentic Coding** | OpenHands SDK (runs inside Codebox) |
| **Embeddings** | Ollama (`nomic-embed-text`) via ChromaDB |
