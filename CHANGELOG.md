# HyprChat Changelog

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
