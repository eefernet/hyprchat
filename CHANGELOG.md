## Alpha v16.1 — April 2026

### New Features
- **PDF Chat Attachments** — Drag-and-drop or paste PDF files into chat; text is extracted server-side via `pypdf` and injected as readable content with page markers. Dedicated PDF chip with page count and loading state.
- **`POST /api/extract-pdf`** — Standalone PDF text extraction endpoint (up to 50MB)

### Coder Bot Overhaul
- **Plan-first architecture** — The bot plans before calling tools. Configurable planning model in Settings (thinking models recommended).
- **Smart OpenHands routing** — Automatically decides whether to use the OpenHands agent based on project complexity (3+ files triggers agent).
- **Overseer verification** — After the agent finishes, the overseer reviews output against user specs and re-prompts if needed.
- **Project-level `generate_code`** — One call builds the entire project (source, configs, manifests) instead of one file at a time.
- **Isolated workspaces** — Each OpenHands run gets `/root/project-{uuid}`, preventing file contamination across tasks.
- **Filesystem snapshot diffing** — Replaces unreliable event parsing; `find -mmin -10` fallback catches every created file.
- **Auto-package on success** — Download link returned in the same tool result, no extra round-trip.
- **Per-language task hints** — Python venv, Vite for React, cargo, go mod, javac, etc. plus "install EVERY dependency" rule.
- **Stuck detector** — Stuck-with-files = success; stuck-without-files = clean error with last 5 agent steps.
- **Live progress pills** — Real-time status icons (wand, package, microscope, eye, archive) from the worker.
- **Higher limits** — `OPENHANDS_MAX_ROUNDS` 6 → 12, HTTP timeout 300s → 600s for larger projects.
- **PROJECT COMPLETE guard** — After success, blocks further tool calls except `download_project`.
- **Rescue loop guard** — After a `generate_code` error, rescue path disabled to prevent infinite code-dump loops.
- **Context pruning** — `MAX_CONTEXT_CHARS=50000` truncates old tool results to prevent context explosion.
- **Near-duplicate detection** — Tracks last 3 tool-call signatures to catch retries across non-adjacent rounds.
- **Dev server detection** — Warns agent instead of hanging on `npm run dev`, `flask run`, `uvicorn`, etc.
- **Repeated-error stop** — Same error 3x in a row breaks the loop and forces a summary.
- **Clean archive names** — `project-abc12345.tar.gz` normalized to `project.tar.gz`.

### UI Improvements
- **ArchiveLink component** — Expandable file tree for `.tar.gz`/`.zip` downloads with preview toggle.
- **Markdown links** — `[text](url)` rendering in chat; archive links auto-upgrade to ArchiveLink.
- **List rendering** — Bullet and numbered lists render as proper HTML lists.
- **PDF badge in chat** — Uploaded PDFs display as a compact `📄 filename.pdf  N pages` badge instead of dumping extracted text into the message bubble. Full text is still sent to the model.
- **New chat remembers model** — New chats default to the last model you used (persisted in localStorage) instead of the first model in the list.
- Drag overlay now mentions PDF support.

### Bug Fixes
- Fixed conversations merging on fresh start due to incorrect database loading order
- Fixed RAG purge only deleting from database, not disk
- Fixed download button disappearing when model made extra tool calls after `generate_code` success
- Fixed `generate_code` reporting 0 files when OpenHands events couldn't be parsed
- Fixed `work_dir` ordering bug where task prompt referenced workspace before creation
- Fixed Coder Bot hanging on dev server commands (`npm run dev`, `npm start`, etc.)
- Fixed workspace analysis not surfacing errors — Ollama failures now return proper HTTP status and error detail
- Fixed workspace analysis timeout (30s → 60s) for slower models
- Fixed OpenHands not receiving uploaded project files — `generate_code` now auto-resolves the active project for the conversation so the agent works inside the user's uploaded project directory
- Fixed quick search results bleeding between conversations — results now clear on conversation switch
- Fixed new chat defaulting to first model in list instead of the last model the user actually used
- Fixed last-used model not persisting — `hc-last-model` now saved on every message send and seeded from most recent conversation on first load
- Fixed stale model references on conversations — `sendMessages` auto-detects deleted models and falls back to first available
- Fixed cryptic Ollama errors — "model not found" and corrupt model errors ("Error in input stream") now show clear, actionable messages instead of raw Ollama output
- Fixed auto-title using wrong model — now uses the user's workspace analysis model from Settings instead of the backend default
- Fixed auto-title not firing reliably — stale React closure caused message count check to miss; now triggers based on conversation title instead


## Alpha v16 — March 2026

### New Features
- **Workflow Automation** — Deterministic tool-chain engine with visual step editor and chat trigger (`/run Name input`)
  - 5 step types: tool, ai_completion, parallel, loop, run_workflow
  - Conditionals, named variables (`{{input}}`, `{{vars.name}}`, `{{steps.N.result}}`, etc.)
  - Per-step retry (0-3) with exponential backoff, per-step error handling (fail/skip/continue)
  - Cron scheduling with enable/disable and run tracking
  - Webhook triggers — each workflow gets a unique URL for external integrations
  - Run history with per-step status, duration, and collapsible results
  - 4 seed presets: Deep Research, System Health Check, Scrape & Analyze, Multi-URL Scraper
- **Full-Text Conversation Search** — SQLite FTS5 search across all messages with highlighted snippets and click-to-navigate
- **Conversation Forking** — Branch from any message to explore alternatives; forked chats link back to the original
- **Token Analytics Dashboard** — Track cumulative usage per model/persona/day with summary cards and bar charts
- **Keyboard Shortcuts** — `Ctrl+K` search, `Ctrl+N` new chat, `Ctrl+/` toggle sidebar, `Escape` close modals
- **Pinned Conversations** — Pin chats to the top of the sidebar
- **System Prompt Templates** — Quick-apply from Prompt Library without creating a persona
- **Auto-Title Generation** — LLM-generated titles after first exchange (toggle in Settings)
- **Streaming Markdown** — `mdStream()` closes unclosed fences/backticks mid-stream
- **Inline Code Output** — Jupyter-style cells with language label, status badge, and execution time
- **Drag-and-Drop Upload** — Drop files onto the chat area to attach
- **Dark/Light Quick Toggle** — Moon/sun icon in header switches themes instantly
- **JSON Export/Import** — Export conversations as JSON for backup and reimport
- **Message Timestamps** — HH:MM display on each message
- **Scroll Buttons** — Floating arrow buttons for long conversations

### Bug Fixes
- Changelog rendering: quoted strings no longer render as italic spans
- SearXNG health: suspended engines no longer trigger false rate-limit warnings
- Agent coding: ANSI escape codes stripped from step output; improved step labels
- Fixed Council AI respones and voting behavior, added a gibberish detector to insure quality responses
- Fixed Countil AI rebuttal rounds, sometimes would go to rebuttal round 1 -> 3, skipping round 2
- Debate context (so gibberish doesn't confuse other members' rebuttals)
- Voting phase (gibberish members excluded from voting entirely) 
- Host synthesis (gibberish filtered from both debate and non-debate modes)
- `member_responses` preserves previous round data when a member produces an empty response, preventing cascade failures where one bad round   wipes all prior context
- Fixes to Coderbot and how it operates with OpenHands.
- Fixes to step output from the coding agent (no more `[]%!` escape characters), steps should be much clearer to understand.
- Fixed status pills from still apearing in saved chats, updated to show completed rather staying in a generating state.


### Technical
- New `backend/workflows.py` with WorkflowExecutor and hand-rolled cron parser
- New DB tables: `token_usage`, `workflows`, `workflow_runs`, `workflow_schedules`
- FTS5 virtual table `messages_fts` with INSERT/DELETE/UPDATE triggers
- New columns: `forked_from`, `fork_point_msg_id`, `pinned` on conversations
- 17 new API endpoints; 3 new nav rail icons

---

## Alpha v15.1 — March 2026

### New Features
- **KB PDF Text Preview** — First 10 pages extracted and displayed; toggle to embedded PDF viewer
- **Thinking Pill Click-to-Expand** — View live reasoning content during streaming
- **KB File Preview** — Preview uploaded files in-browser (first 200 lines)
- **Theme Preview** — Live color swatches and mock chat bubble before applying
- **Font Preview** — Sample text + code snippet preview before applying
- **Nav Rail Labels** — Text labels below icons (toggle in Settings)
- **Settings Tooltips** — Hover info icons for RAG and model parameter explanations

### Improvements
- Personas icon changed to person silhouette
- Model list auto-refreshes on panel switch and dropdown open
- Completed downloads persist until manually cleared
- KB file list redesigned as scrollable vertical list with type-specific icons
- Wider nav rail (60px -> 68px), larger icons
- Tag editor: close button and Escape-to-dismiss

### Bug Fixes
- Prompt library: fixed click-through race condition on quick insert
- Downloads panel: replaced full-screen backdrop with click-away listener
- Settings: fixed React error #310 (too many re-renders)
- Chat state preserved when clicking the same conversation
- Status pills persist across sessions via message metadata
- Council: rounds render live during debate, voting phase shows final round
- Council: fixed duplicate user messages
- Search cards: better OG image fetching, fallback shows larger favicon
- Archive preview: shows file tree instead of infinite spinner
- New chats no longer default to CodeAgent system prompt
- Orphaned tags cleaned up on conversation deletion

---

## Alpha v15 — March 2026

### New Features
- **Quick Search** — Lightweight search injection (no tool calling needed)
- **Thinking Mode Control** — Auto / On / Off setting for thinking tokens
- **Scanline Effect Toggle** — CRT overlay now off by default, toggle in Settings
- **Coder Bot KB Seeder** — 60+ programming reference docs auto-indexed into RAG
- **Source Tier Scoring** — Evidence-first prioritization for research tools

### Improvements
- Smarter text-based tool prompts based on which tools are enabled
- Research tool fetches 5 pages (up from 3), prioritized by source tier
- SearXNG rate-limit retry with 3s backoff
- Conspiracy Bot: prompt reduced 95 -> 35 lines, adaptive output format
- Model pulls use shared downloads UI with progress/speed/ETA
- Post-generate_code verification with execute_code and run_shell
- Blockquote attribution rendering

### Bug Fixes
- Hallucinated tool calls silently dropped when no tools are enabled
- Per-tool authorization check before execution
- Over-think loop fix for models with no tools
- Persona ID properly cleared on Leave Persona
- Page fetch returns None on HTTP 4xx/5xx

---

## Alpha v14 — March 2026

### Coder Bot Overhaul
- Streamlined system prompt (95 -> 30 lines)
- Smarter code-block rescue via write_file + run_shell
- Error-specific recovery hints (ConnectionRefused, FileNotFound, SyntaxError, Permission)
- Configurable `MAX_AGENT_ROUNDS` (default 12)
- `OPENHANDS_URL` config variable, health check retry, increased default context (16384)

### OpenHands Worker
- Persistent tool support cache, project continuity, auto-cleanup stale projects

### Conspiracy Bot Overhaul
- Adaptive output format, streamlined prompt, PRIME DIRECTIVE pattern, document drill-down

### Frontend
- Agent timeline with step dots and scrollable container
- Coder Bot `</>` quick-activate button (glows green when active)

### Deploy Monitor
- Smart routing (worker -> Codebox server, backend -> HyprChat server)
- Watches CHANGELOG.md and README.md

---

## Alpha v13 — March 2026

### New Features
- **RAG Pipeline** — Semantic retrieval replacing raw file injection
  - Sentence-aware chunking (code-aware for Python/JS/TS)
  - ChromaDB vector storage with cosine similarity search
  - Query-time top-k retrieval instead of full file dumps
  - Research memory: tool results auto-indexed for future recall
  - PDF text extraction via pypdf
  - Configurable chunk size, overlap, top_k, embed model
- **Smart Research Tool** — Fetches and reads full page content from top 3 results in parallel
- **OG Image Thumbnails** — Search cards show article images from og:image meta tags
- **Redesigned Search Cards** — Thumbnail fills top, favicon pill, gradient fallback, hover highlight

### Improvements
- Non-blocking Quick Search (fires in parallel with chat request)
- Search results positioned directly above the AI response

---

## Alpha v12 — March 2026

### New Features
- **Council Presets** — Philosophers, Visionaries, Scientists, Debaters (one-click setup)
- **Debate Rounds** — Configurable rebuttal rounds (0-5) with parallel streaming
- **Council Analytics** — Win rates, vote breakdowns, member rankings, recommendations
- **Expandable Debate History** — Collapsible round-by-round sections in chat
- **Delete All Chats** and **Purge All RAG** — Danger zone buttons in Settings

### Improvements
- Council English enforcement for all members
- New Chat carries council/persona context
- Leave Persona / Leave Council buttons in header
- Sidebar labels: pink border for councils, warm border for personas

---

## Alpha v11 — March 2026

### New Features
- **OpenHands Integration** — `generate_code` tool runs a full agentic coding loop (plan -> write -> test -> fix -> iterate) inside CodeBox sandbox
- **OpenHands Worker** — Dedicated FastAPI microservice on port 8586
- **Coder Model Selector** — Pick which model handles code generation

### Improvements
- Code-block rescue hardening (min 30 chars + keyword check)
- Repetition detector skips whitespace-only patterns
- Coder Bot English-only rule

---

## Alpha v10 — March 2026

### New Features
- **Model Manager** — Dedicated panel with Ollama and HuggingFace tabs
  - Ollama: models grouped by family, Use/Remove buttons, pull by name
  - HuggingFace: search GGUF models, file selector, streaming download to Ollama
  - Multi-part GGUF auto-detection and grouped download
- **Downloads Bar** — Live progress, speed, ETA for all active downloads
- **Inline Search Cards** — Scrollable source cards below AI responses with thumbnails
- **Tool Response Images** — Inline image rendering in markdown

### Improvements
- Empty response recovery with tool-use nudge and plain-text fallback
- Model dropdown z-index fix via React portal

---

## Alpha v9 — March 2026
- **Prompt Library** — Save and quick-insert reusable prompts
- **Conversation Tags** — Custom labels with sidebar filtering
- **Per-Model Parameters** — temperature, num_ctx, top_p, top_k, repeat_penalty per model
- **Ollama Server URL** — Change from Settings at runtime
- **Changelog Viewer** — Access from Settings
- Live token counter during generation

---

## Alpha v8 — March 2026
- **Based Bot** — Edgy/uncensored Grok-inspired persona
- **Persona avatars in chat** — Messages show avatar and styled name pill
- **UI Font Size slider** (10-16px)
- Conspiracy research: always runs second wave across gov sources

---

## Alpha v7 — March 2026
- **Conspiracy Bot** — `conspiracy_research` tool searching FOIA vaults, whistleblower sites, CIA/FBI archives
- **6 new themes** — Terminal, Cyberpunk, Solarized Dark/Light, Material Ocean, Ayu Dark (14 total)
- **3 new fonts** — Cascadia Code, Space Mono, Geist Mono (9 total)
- Model list grouped by family with emoji icons and size tags
- Streaming: removed artificial delay, 8-char chunks

---

## Alpha v6 — March 2026
- **AI Peer Voting** — Council members vote for the best answer; host includes vote summary
- **Improved markdown** — Lists, blockquotes, HR, italic, headings

---

## Alpha v5 — March 2026
- **3 new themes** — Dracula, One Light, Midnight
- **Animated tool pills** — spin, swing, bounce per tool type
- **Workspace system** — Group chats, file tracking, topic analysis, create personas from KB
- Font size slider, chat width slider, workspace model selector

---

## Alpha v4 — March 2026
- **KB injection** — Files injected into system prompt when persona is active
- **Model parameters** — temperature, num_ctx, top_p, top_k applied to Ollama
- **Export conversation** as Markdown

---

## Alpha v3 — February 2026
- **Council of AI** — Multi-model parallel debates
- **Deep Research** — Multi-source parallel research with AI synthesis
- **Custom tools** — Upload Python tools the AI can call

---

## Alpha v2 — February 2026
- **Knowledge Bases** — Upload and attach documents to personas
- **Personas** — Custom AI configs with system prompts, models, avatars
- **SSE Event Bus** — Real-time tool status events

---

## Alpha v1 — January 2026
- Initial release: FastAPI + single-file React SPA
- Ollama streaming chat with tool calling
- CodeAgent with sandboxed code execution (Codebox)
- SearXNG web search integration
