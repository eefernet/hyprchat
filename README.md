# 🧠 HyprChat

**Self-hosted AI chat platform** — tool calling, agentic code generation, deep research, multi-model councils, n8n automation integration, and full model management. All running on your own hardware.

Built with FastAPI + a single-file React SPA. No build step, no cloud dependencies.

> ⚠️ Alpha software — actively developed, expect rough edges. Check [releases](https://github.com/eefernet/hyprchat/releases) for stable builds.

---

## ⚠️ Security Warning

HyprChat can execute code, upload files, call local services, and drive coding agents. **Do not expose it directly to the public internet.** Run it behind Tailscale, a VPN, or a reverse proxy with authentication. The default configuration binds to `127.0.0.1` and assumes a trusted local network.

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

### 🤖 Coder Bot v2 — Hybrid Agent Workflow

A deterministic coding workflow with three specialized paths: OpenHands for greenfield builds, Aider for uploaded-project fixes, and ProjectQA for read-only codebase questions. Workflow correctness is enforced by a server-side gate, not by hoping the model follows a prompt:

| Agent | What it does |
|-------|-------------|
| 📐 **Architect** | Single-shot structured plan — JSON manifest, build/test commands, dependencies, success criteria |
| 🏗 **Builder** | OpenHands SDK on Codebox — greenfield/full-project builds from a contract |
| 🔍 **Reviewer** | Read-only — runs the project's real build/test/lint commands, returns structured issues with `file:line` references |
| ✅ **Acceptance** | Final quality gate after clean review — checks the delivered project against the user request, docs, tests, packaging, and generated artifacts |
| 🛠 **Aider Fixer** | Primary uploaded-project edit path — runs Aider from the project root, captures diff/touched files/test output |
| 🩹 **Fixer** | Fallback scoped editor — marker-format edits when Aider is disabled or unavailable |
| ❓ **ProjectQA** | Read-only Q&A — "walk me through X", "show me Y" — grounded answers with file:line citations, change-request detection |
| 📚 **Indexer** | Runs once at upload time — walks tree, detects build system, indexes into ChromaDB for semantic retrieval |

- **Workflow gate** — server-side state machine enforces *review-after-build*, *acceptance-after-clean-review*, *fix-after-issues*, *answer-after-QA*, and hard caps on review/fix loops. The model can't skip steps or get stuck.
- **Durable workflows + runs** — `coder_workflows` tracks the user-facing workflow, while every agent invocation is a row in `runs`. Browser disconnects can't lose work; the UI rebuilds the timeline on reload.
- **Project uploads** — drop a `.zip`/`.tar.gz`; HyprChat sanitizes it, creates a local git baseline in Codebox, detects build/test commands, and runs the Indexer. Subsequent questions and changes operate on the uploaded code with full project awareness.
- **Architecture Plan panel** — rich markdown rendering of the Architect's plan: file tree, build commands, dependencies as build-system snippets, success criteria as a checklist.
- **Accepted downloads only** — `download_project` is blocked until Acceptance returns `accepted`, then packages the project while excluding common cache/build artifacts.
- **Cross-language support** — verified end-to-end on Java (Maven), Python (Flask + pytest), Rust (Cargo), Go (gorilla/mux). Builder profiles + Reviewer markers cover most ecosystems.
- Sandboxed execution via Codebox (LXC) with 30+ language support, live progress pills, workflow cards, role-specific run cards, and the original v1 Coder Bot still selectable as a persona.

### 🛠️ Tool Suites

**Coder Bot v2 multi-agent tools:**

| Tool | Description |
|------|-------------|
| `start_coder_workflow` | Backend router for `build_from_prompt`, `fix_uploaded_project`, or `ask_uploaded_project` |
| `plan_project` | Routes through the **Architect** for v2 personas — produces structured JSON manifest (file tree, build/test cmds, deps, success criteria); rich markdown plan panel in chat |
| `generate_code` | **Builder** via OpenHands — greenfield/full-project builds from the Architect contract |
| `run_review` | **Reviewer** — runs build/test/lint, returns structured issue list with `suggested_fix_scope` |
| `run_acceptance_review` | **Acceptance** — final static quality gate after clean review; checks request fit, docs, tests, packaging, entrypoints, and generated artifacts |
| `run_aider_fix` | **Aider Fixer** — surgical edits for uploaded projects, then requires Reviewer verification |
| `run_fixer` | **Fixer** — applies scoped edits driven by a Reviewer or Acceptance envelope; marker-format LLM output |
| `ask_project` | **ProjectQA** — grounded Q&A with file:line citations; auto-resolves `project_dir`; flags change requests |

**Other tools:**

| Tool | Description |
|------|-------------|
| `execute_code` | Sandboxed code execution in 30+ languages with package installs |
| `deep_research` | Multi-phase parallel web research with 5 depth levels and cross-referencing |
| `quick_search` | Instant SearXNG search with OG image cards, YouTube previews, and favicon badges |
| `research` | Web search + full page reading for grounded answers |
| `conspiracy_research` | Alt-source deep dive — FOIA vaults, CIA reading room, FBI vault, whistleblower sites |
| `fetch_url` | Fetch and read any URL directly |
| `write_file` / `read_file` | Direct file ops on the sandbox |
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

### ⚡ External Automation
- Automation lives in the external n8n VM. HyprChat keeps the n8n health/proxy integration, including `/api/n8n/execute`, without maintaining an internal automation engine.

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
         │
         │   Coder Bot v2 — hybrid workflow router:
         │   workflows persist in `coder_workflows`; agent calls persist in `runs`
         │     📐 Architect    → JSON plan
         │     🏗 Builder      → OpenHands greenfield builds
         │     🔍 Reviewer     → build/test/lint, structured issues
         │     ✅ Acceptance   → request/docs/tests/packaging quality gate
         │     🛠 Aider Fixer  → uploaded-project surgical edits
         │     🩹 Fixer        → fallback marker-format edits
         │     ❓ ProjectQA    → grounded Q&A with citations
         │     📚 Indexer      → upload-time tree → ChromaDB
         │
         ├── Ollama     (:11434) — local LLM inference
         ├── Codebox    (:8585)  — sandboxed code execution (LXC)
         │     └── OpenHands Worker (:8586) — OpenHands + Aider coding bridge
         ├── SearXNG    (:8888)  — private web search
         └── ChromaDB              — vector storage for RAG (incl. uploaded-project memory)
```

### Key Backend Modules
| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI routes, SSE endpoints, model management, project upload |
| `backend/agents/chat.py` | Multi-round streaming chat agent with tool calling, QA short-circuit, ACTIVE PROJECT injection |
| `backend/agents/personas.py` | Seed bot definitions (Coder Bot v1 + v2, Conspiracy Bot, Based Bot) |
| `backend/agents/architect.py` | **v2** Architect — structured plan as JSON, rich markdown rendering |
| `backend/agents/reviewer.py` | **v2** Reviewer — read-only build/test/lint with marker auto-detection |
| `backend/agents/acceptance.py` | **v2** Acceptance — final static quality gate for request fit, docs, tests, packaging, and artifacts |
| `backend/agents/fixer.py` | **v2** Fixer — scoped edits via marker-delimited LLM output |
| `backend/agents/aider_fixer.py` | **v2** Aider Fixer — uploaded-project patch worker with diff/test capture |
| `backend/agents/language_adapters.py` | **v2** uploaded-project build/test/smoke/lint contract detection |
| `backend/agents/project_qa.py` | **v2** ProjectQA — grounded Q&A with file:line citations |
| `backend/agents/project_indexer.py` | **v2** Indexer — uploaded-project tree walk → ChromaDB |
| `backend/tools.py` | Tool execution engine + v2 workflow router/gate + OpenHands/Aider dispatch |
| `backend/openhands_worker.py` | OpenHands SDK + Aider bridge on Codebox; `/run*`, `/cancel/*`, and `/aider/*` endpoints |
| `backend/research.py` | Deep research engine |
| `backend/council.py` | Council debate, voting, and synthesis |
| `backend/events.py` | Async SSE EventBus (pub/sub with `asyncio.Lock`) |
| `backend/rag.py` | RAG pipeline (chunking, embedding, retrieval) |
| `backend/hf.py` | HuggingFace model browser and download |
| `backend/database.py` | SQLite schema, migrations, and queries — incl. `runs` and `coder_workflows` for v2 |
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

# Models — each v2 agent inherits from these umbrella defaults:
DEFAULT_MODEL       = "qwen3.5:27b"   # chat / persona fallback
PLANNING_MODEL      = "qwen3.5:27b"   # Architect + Reviewer + Acceptance
CODER_MODEL         = "qwen2.5-coder:14b" # Builder + Fixer
WORKSPACE_MODEL     = "qwen3.5:4b"    # auto-title, topic analysis, query rewriting

# Optional per-agent overrides; empty = inherit from the umbrella default
ARCHITECT_MODEL     = ""               # empty = PLANNING_MODEL
REVIEWER_MODEL      = ""               # empty = PLANNING_MODEL
ACCEPTANCE_MODEL    = ""               # empty = PLANNING_MODEL
BUILDER_MODEL       = ""               # empty = CODER_MODEL
FIXER_MODEL         = ""               # empty = CODER_MODEL
QA_MODEL            = ""               # empty = chat / persona model
AIDER_MODEL         = ""               # empty = FIXER_MODEL, then CODER_MODEL

# Resources
OPENHANDS_ENABLED   = True
OPENHANDS_MAX_ROUNDS = 40
OPENHANDS_NUM_CTX   = 16384
AIDER_ENABLED       = True
AIDER_NUM_CTX       = 16384
AIDER_AUTO_TEST     = True
AIDER_WORKER_URL    = OPENHANDS_URL
DEFAULT_NUM_CTX     = 16384            # used by Architect / Reviewer / Acceptance / Fixer / QA
MAX_AGENT_ROUNDS    = 12               # chat-side cap (non-coder personas)
MAX_AGENT_ROUNDS_CODER = 30            # chat-side cap for Coder Bot personas
```

All model, OpenHands, and Aider settings can be changed at runtime from the Settings panel.

**Recommended model setup (dual-3090 / 48 GB VRAM):**
- `PLANNING_MODEL` = `qwen3-coder:30b` (Architect / Reviewer / Acceptance)
- `CODER_MODEL` = `devstral:24b` (Builder / Fixer)
- Default chat = `qwen3-coder:30b` or another general model

Two large models stay hot simultaneously (~42 GB), eviction kicks in if a third is needed. Verified across Java / Python / Rust / Go builds.

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
scp backend/agents/*.py root@<SERVER_IP>:/opt/hyprchat/backend/agents/
scp frontend/dist/index.html root@<SERVER_IP>:/opt/hyprchat/frontend/dist/
ssh root@<SERVER_IP> "systemctl restart hyprchat"

# Codebox/OpenHands worker
scp backend/openhands_worker.py root@<CODEBOX_IP>:/opt/openhands-worker/
ssh root@<CODEBOX_IP> "systemctl restart openhands-worker"
```

Or use the file-watching deployer that auto-pushes changes and restarts the service:

```bash
python3 deploy_monitor.py
```

The monitor reads `.deploy_config.json` for SSH credentials and watches every backend / frontend / agent file, including `aider_fixer.py` and `language_adapters.py`. Backend changes trigger a service restart; frontend changes deploy without restart. Deploying `backend/openhands_worker.py` also pushes the worker to Codebox, checks/installs Aider in `/opt/openhands-worker/aider-venv`, and restarts `openhands-worker`.

---

## 📋 Logs & Management

```bash
journalctl -u hyprchat -f        # live logs
systemctl restart hyprchat       # restart
systemctl status hyprchat        # status
```

---

## 🧪 Testing

Tests cover the major HyprChat features and run against a live server instance.

```bash
cd backend
pip install pytest httpx
python -m pytest tests/ -v

# Run specific categories
python -m pytest tests/ -v -k "chat"          # SSE streaming
python -m pytest tests/ -v -k "tool"          # tools & execution
python -m pytest tests/ -v -k "council"       # councils & debates
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
| Councils | 13 | CRUD, members, presets, analytics |
| HuggingFace | 5 | GGUF search, model info |
| Integration | 4 | Full lifecycle flows |
| Acceptance Agent | 3 | Structured output parsing and error handling |
| Frontend Settings | 3 | Server-setting hydration and guarded persistence |
| Quick Search Agent | 25 | Triage, relevance, recency, refinement, dedupe |
| Workspace Model Caps | 4 | Helper context limits for workspace/title/council calls |

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
