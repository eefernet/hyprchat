# HyprChat Changelog

## Alpha v15 — March 2026

### Bug Fixes
- **Hallucinated tool call guard** — When only `quick_search` is enabled (no codeagent tools), models would generate native tool calls (`run_shell`, `execute_code`) from training weights even though no tools were sent to Ollama. These are now silently dropped with a log message.
- **Per-tool authorization check** — Each tool call is now verified against `available_tool_names` before execution. Blocks unauthorized tools even when some tools ARE enabled (e.g., model tries `run_shell` when only `research` is available). Returns an error message to the model so it can recover.
- **Over-think loop fix** — When hallucinated tool calls are dropped and the model produces only thinking tokens with no content, the nudge message now explicitly tells the model it has no tools and must answer from the search results already in context. Previously the generic nudge caused infinite think-loops.

### Improvements
- **Stronger search context instruction** — Quick search result injection now tells the model to treat search results as its real-time data and not disclaim about lacking internet access. Models now summarize search snippets directly instead of deflecting.

---

## Alpha v14 — March 2026

### Coder Bot Deep Improvement

#### System Prompt Overhaul
- **Streamlined Coder Bot persona** — Replaced ~95-line system prompt with a focused ~30-line version. Shorter prompts reduce competing instruction noise with local models. Keeps PRIME DIRECTIVE, workflow, and hard rules.
- **Simplified CODING AGENT PROTOCOL injection** — Reduced from ~33 lines to ~15 lines in the chat agent loop. Removes redundancy with the persona prompt.

#### Agent Loop Improvements
- **Smarter code-block rescue** — When the model dumps code in chat instead of using tools, rescued code now routes through `write_file` + `run_shell` instead of `execute_code`, avoiding stdin/sys.argv issues. Limited to 1 rescue per session with feedback message to teach the model.
- **Error-specific recovery hints** — First-occurrence guidance for `ConnectionRefusedError`, `FileNotFoundError`, `SyntaxError`, and `PermissionError` errors, in addition to existing repeated-error handling.
- **Configurable MAX_ROUNDS** — Agent loop rounds now configurable via `MAX_AGENT_ROUNDS` env var (default 12).

#### Infrastructure & Configuration
- **`OPENHANDS_URL` config variable** — Eliminates fragile `CODEBOX_URL.rsplit(":", 1)[0] + ":8586"` URL derivation pattern. Now a proper config with env var override.
- **Health check retry** — OpenHands health check now retries 3 times with 1s delay between attempts before failing.
- **Increased default `OPENHANDS_NUM_CTX`** — Default bumped from 8192 to 16384 for better context handling in coding models.
- **Improved `generate_code` tool description** — Task parameter description now guides models toward thorough specifications.

#### OpenHands Worker Improvements
- **Persistent tool support cache** — Model tool-calling capability checks are now cached to disk (`/opt/openhands-worker/.tool_cache.json`), surviving worker restarts.
- **Project continuity** — New `project_id` field allows reusing an existing workspace directory for iterative work on the same project.
- **Auto-cleanup stale projects** — Projects older than 24 hours are automatically cleaned up every 10th request. New `/clean-stale` endpoint for manual cleanup.
- **Stuck detection logging** — Silent `pass` in stuck detection `except` block replaced with actual logging.

#### Frontend
- **Agent Timeline** — Enhanced `generate_code` step display with timeline dots, step count header, and scrollable container.
- **Coder Bot quick-activate button** — `</>` button in the input bar next to prompt library. One click to apply Coder Bot persona. Glows green when active.

#### Conspiracy Bot Overhaul
- **Flexible output format** — Replaced rigid 9-section report template with adaptive output style. Simple questions get direct answers, deep investigations get structured reports, person/org inquiries get connection maps.
- **Streamlined persona prompt** — 95 lines → 35 lines. Clearer investigative philosophy, same uncensored ATLAS identity.
- **PRIME DIRECTIVE pattern** — Always calls `conspiracy_research` first before answering, matching the Coder Bot's "act don't talk" approach.
- **Document drill-down** — Added `fetch_url` to the bot's tool list so it can read full documents from its research results instead of just summarizing search snippets.
- **Better tool description** — Broader framing ("any topic where official narratives may be incomplete") with clearer parameter descriptions.

#### Deploy Monitor
- **Smart routing** — `openhands_worker.py` now deploys to the Codebox server instead of the HyprChat server, with automatic `systemctl restart openhands-worker`.
- **Server labels** — Deploy results show which server each file was sent to.
- **Configurable SSH timeout** — Service restart timeout increased to 90s to accommodate slow restarts.
- **Watches CHANGELOG.md and README.md** — Now auto-deploys docs to `/opt/hyprchat/` when changed.

---

## Alpha v13 — March 2026

### New Features
- **RAG Pipeline** — Full semantic retrieval-augmented generation replacing raw file injection:
  - **Chunking & Embedding** — KB files are parsed, chunked with sentence-aware splitting (code-aware for Python/JS/TS), and embedded via Ollama (`nomic-embed-text`)
  - **ChromaDB Vector Storage** — Persistent per-KB semantic indices with cosine similarity search
  - **Query-Time Retrieval** — Only the most relevant chunks are injected into the system prompt (top-k filtered by similarity to the user's question), instead of dumping entire files
  - **Research Memory** — Tool results from `research`, `fetch_url`, `deep_research`, and `conspiracy_research` are automatically indexed into per-persona research memory collections, queryable in future chats
  - **Configurable** — Chunk size, overlap, top_k, max context chars, and embed model adjustable via Settings → RAG Pipeline
  - **PDF support** — Extracts and indexes text from uploaded PDFs via pypdf
  - **Live indexing status** — SSE-based progress tracking when indexing KB files
  - Fallback to raw file injection if RAG query fails
- **Smart Research Tool** — The `research` tool now fetches and reads full page content from the top 3 search results (in parallel), giving the AI actual web content to work with instead of just search snippets. Responses are now grounded in real, current information.
- **OG Image Thumbnails** — Quick Search and research result cards now display actual article images (extracted from `og:image` / `twitter:image` meta tags) instead of just site favicons. Images are fetched in parallel with a fast 4s timeout.
- **Redesigned Search Result Cards** — New card layout across all search panels:
  - Thumbnail/article image fills the top area of each card
  - Small favicon badge in a frosted-glass pill, top-right corner
  - Gradient background fallback when no image is available
  - Hover highlight effect on all cards

### Improvements
- **Non-blocking Quick Search** — Quick Search now fires in parallel with the chat request instead of blocking before it. Messages send instantly while search results load in the background.
- **Search results positioned correctly** — Quick Search result cards now render directly above the AI response instead of above the user's message at the top of the chat area. Applies to both regular chat and council views.
- **Reduced page fetch count** — Research tool fetches 3 pages instead of 5 for better latency balance.

---

## Alpha v12 — March 2026

### New Features
- **Council Preset Templates** — One-click preset councils with curated members and detailed persona prompts:
  - **Philosophers** — Socrates, Aristotle, Nietzsche, Confucius, Simone de Beauvoir
  - **Visionaries** — Leonardo da Vinci, Nikola Tesla, Marie Curie, Steve Jobs, Sun Tzu
  - **Scientists** — Einstein, Darwin, Ada Lovelace, Feynman, Carl Sagan
  - **Debaters** — The Pragmatist, Devil's Advocate, Futurist, Ethicist, Historian
- **Debate Rounds** — Configurable rebuttal rounds (0-5) where council members read each other's responses and argue back. Each round streams in parallel with labeled headers.
- **Council Performance Analytics** — "Analyze Performance" button per council generates a report with:
  - Debate count, session count
  - Member rankings with win rate bars, vote counts, avg response length
  - "Voted by" breakdown showing which members voted for whom
  - Auto-generated recommendations (strongest/weakest performer, model diversity, response length disparity)
- **Expandable Debate Rounds in Chat** — Historical council responses grouped by round with collapsible sections. Latest round expanded by default, earlier rounds collapsed with response counts.
- **Delete All Chats** — Danger zone button in Settings to wipe all conversations
- **Purge All RAG Collections** — Button in RAG Pipeline settings to clear all ChromaDB indices

### Improvements
- **Council English enforcement** — All council members and host moderator now always respond in English (fixes Qwen models defaulting to Chinese)
- **New Chat carries context** — Clicking "+ New Chat" while in a council or persona chat creates a new chat with the same council/persona applied
- **Leave Persona / Leave Council** — Dedicated exit buttons in the top bar header. Removed old inline ✕ and sidebar persona tag.
- **Sidebar chat labels** — Council chats show pink left border + council icon; persona chats show warm border + avatar/bot icon
- **Auto-scroll council responses** — Individual member cards and host synthesis auto-scroll during streaming
- **Input bar polish** — Centered alignment, larger padding, "What's on your mind?" placeholder
- **Alpha badge** — Version label replaced with styled "ALPHA" badge next to HyprChat logo

---

## Alpha v11 — March 2026

### New Features
- **OpenHands SDK Integration** — The `generate_code` tool now uses an OpenHands agentic coding agent that writes, tests, and fixes code automatically before returning results. Runs inside the CodeBox LXC sandbox for full isolation.
  - Agent has `terminal` and `file_editor` tools — writes code, runs it, reads errors, fixes, and retries
  - Uses the configured coder model via Ollama as the LLM backend
  - Falls back to the legacy single-shot code generator if the OpenHands worker is unavailable
- **`generate_code` Sub-Agent Tool** — Orchestrator models can delegate code writing to a specialized coder model. Configurable via the Coder Model dropdown in Model Manager settings.
- **OpenHands Worker Service** — New FastAPI microservice (`openhands_worker.py`) running on CodeBox LXC port 8586. Receives coding tasks, runs a full OpenHands agent loop (plan → write → test → fix → iterate), and returns tested code.

### Improvements
- **Coder Model selector** — Dropdown in Model Manager Global Defaults to pick which model handles code generation (or "Same as chat model")
- **OpenHands settings** — `openhands_enabled` toggle and `openhands_max_rounds` configurable via Settings API
- **Tool pill updates** — generate_code pill now shows "Agent Coding" during execution, "Code Ready" when done
- **Code-block rescue hardening** — Minimum 30 chars + code keyword check to prevent ASCII art from being mistaken for code
- **Repetition detector fix** — Skip whitespace-only patterns to avoid killing ASCII art output
- **Coder Bot English-only rule** — Added rule 11 to Coder Bot persona ensuring all output is in English

---

## Alpha v10 — March 2026

### New Features
- **Model Manager** — Dedicated panel (nav tab) for managing all models in one place
  - **Ollama tab** — Installed models grouped by family with emoji icons, Use/Remove buttons, pull new model by name
  - **HuggingFace tab** — Search HuggingFace for GGUF models (GGUF-only filter toggle), card grid results, model detail view with file selector, streaming download directly to Ollama
  - **Multi-part GGUF support** — Detects split GGUF files (e.g. `-00001-of-00004.gguf`), groups all parts, downloads and registers them as a single Ollama model
- **Downloads Bar** — Collapsible pill in the top-right header showing all active and queued model downloads. Displays live progress bar, download speed (MB/s or KB/s), estimated time remaining, and downloaded/total size per download. Auto-expands when a download starts. "Clear done" removes completed entries.
- **Inline Search Result Cards** — When the AI uses the `research` or `deep_research` tool, a horizontally scrollable row of source cards appears directly below the response. Cards show thumbnail previews (or favicons), title, snippet, YouTube play button overlay for video results, and link to the source. Images returned in markdown (`![alt](url)`) now render inline.
- **Tool Response Images** — The markdown renderer now handles `![alt](url)` syntax, rendering images inline with rounded corners and error fallback.

### Improvements
- **Empty response recovery** — When the model returns an empty response, the retry nudge now explicitly instructs it to use its available tools. A second fallback strips tools entirely and retries for a plain text response before giving up.
- **Model dropdown z-index fix** — The model selector dropdown in the chat header now renders above all content via React portal (fixes rendering behind the chat area due to `backdropFilter` stacking context).
- **SearXNG results enriched** — Search results now include thumbnail, type (web/youtube/image), and YouTube video ID thumbnail extraction throughout the research pipeline.
- **Settings cleanup** — Ollama Models section in Settings replaced with "Open Model Manager →" button.

---

## Alpha v9 — March 2026

### New Features
- **Prompt Library** — Save reusable prompts with titles and categories. Insert with one click from the `⚡` button in the input bar. Manage from the dedicated panel.
- **Conversation Tags** — Tag any conversation with custom labels. Filter the sidebar by tag.
- **Per-Model Parameters** — Customize temperature, num_ctx, top_p, top_k, repeat_penalty per model in Settings.
- **Ollama Server URL** — Change the Ollama server address from Settings without editing config files.
- **Changelog Viewer** — This window. Access from Settings → View Changelog.

### Improvements
- Live token counter updates in real-time during generation
- Workspace file preview error handling with re-download fallback
- Version bump → v9

---

## Alpha v8 — March 2026

### New Features
- **Based Bot persona** — Edgy/uncensored Grok-inspired persona, seed via Settings
- **Persona avatar + name in chat** — Messages show persona avatar and styled name pill
- **UI Font Size slider** — Range 10-16, saved across sessions

### Improvements
- System prompt textarea enlarged (rows=14)
- Conspiracy research always runs second wave across gov sources, FOIA, CIA reading room

---

## Alpha v7 — March 2026

### New Features
- **Conspiracy Theory Bot** — `🕵️ Conspiracy Bot` persona with `conspiracy_research` builtin tool. Searches whistleblower sites, FOIA vaults, leaked doc archives, CIA reading room, FBI vault.
- **6 new themes** — Terminal, Cyberpunk, Solarized Dark, Solarized Light, Material Ocean, Ayu Dark (14 themes total)
- **3 new fonts** — Cascadia Code, Space Mono, Geist Mono (9 fonts total)
- **Improved model list** — Grouped by family, emoji icons, color-coded size tags, per-model Use button

### Improvements
- Streaming: removed artificial delay, emits 8-char chunks
- SSE reconnects with exponential backoff on disconnect

---

## Alpha v6 — March 2026

### New Features
- **AI Peer Voting in council** — After all members respond, each model votes for the best answer from the others. Votes are shown on response cards. Host synthesis includes vote summary.
- **Improved markdown renderer** — Bullet/ordered lists, blockquotes, HR, italic, headings

---

## Alpha v5 — March 2026

### New Features
- **Dracula, One Light, Midnight themes**
- **Font size slider** (11-16px), chat width slider (560-1200px), workspace model selector
- **Animated tool pills** — spin, swing, bounce per tool type
- **Workspace system** — Group chats, file tracking, topic analysis, create personas from KB

---

## Alpha v4 — March 2026

### New Features
- **Knowledge base injection** — KB files injected into system prompt when persona is active
- **Model config parameters** — temperature, num_ctx, top_p, top_k applied to Ollama payload
- **Export conversation** — Download chat as Markdown

---

## Alpha v3 — February 2026

### New Features
- **Council of AI** — Debate topics with multiple models simultaneously
- **Deep Research** — Multi-source parallel research engine with AI synthesis
- **Custom tools** — Upload Python tools the AI can call

---

## Alpha v2 — February 2026

### New Features
- **Knowledge Bases** — Upload and attach documents to personas
- **Personas** — Custom AI configurations with system prompts, model config, avatars
- **SSE Event Bus** — Real-time tool status events with asyncio pub/sub

---

## Alpha v1 — January 2026

- Initial release: FastAPI + single-file React SPA
- Ollama streaming chat with tool calling
- CodeAgent with sandboxed code execution (Codebox)
- SearXNG web search integration
