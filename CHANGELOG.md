# HyprChat Changelog

## Alpha v9 — March 2026

### New Features
- **Prompt Library** — Save reusable prompts with titles and categories. Insert any saved prompt into the chat input with one click from the `⚡ Prompts` button in the input bar. Manage prompts from the dedicated panel (⚡ icon in nav).
- **Conversation Tags** — Tag any conversation with custom colored labels. Filter the sidebar by tag. Click the tag icon on any conversation or use the tag panel. Tags stored locally.
- **Per-Model Parameters** — Customize temperature, context window (num_ctx), top_p, top_k, and repeat_penalty for each model individually. Set any parameter to "Default" to use Ollama's built-in defaults. Find it in Settings → Model Parameters.
- **Ollama Server URL** — Change the Ollama server IP/host directly from Settings without editing config files. Saved persistently.
- **Changelog Viewer** — This window! Access from Settings → View Changelog.

### Improvements
- **Live Token Counter** — The context window pill in the top-right now updates in real-time during generation. Shows generation tokens live, and displays total context (prompt + generation) after completion.
- **Workspace Files Preview** — Fixed preview for workspace files. Added error handling when files have been cleaned up. Shows a friendly error with a re-download link.
- **Workspace Enhancement** — Workspace panel now shows stats, has a notes/description editor, and workspace file thumbnails with proper icons. Upload files directly to a workspace.
- **Version bump** → v9

### Bug Fixes
- Token counter no longer shows chunk count instead of real token count
- Workspace file preview now shows proper error when file is unavailable
- Context window progress bar now reflects actual prompt+gen token total

---

## Alpha v8 — March 2026

### New Features
- **Based Bot persona** — `POST /api/seed/based-bot` → `🤖 Based Bot` (temp=1.0, edgy/funny uncensored, Grok-inspired)
- **Persona avatar + name in chat** — Chat messages now show persona avatar image + styled pink name pill
- **UI Font Size slider** — Range 10-16, saved to localStorage

### Improvements
- System prompt textarea: rows=14, minHeight=220
- Conspiracy research: always runs second wave across gov sources, FOIA, CIA reading room
- "🤖 Seed Based Bot" button in personas panel

---

## Alpha v7 — March 2026

### New Features
- **Conspiracy Theory Bot** — `🕵️ Conspiracy Bot` persona with `conspiracy_research` builtin tool
- **6 new themes** — Terminal, Cyberpunk, Solarized Dark, Solarized Light, Material Ocean, Ayu Dark
- **3 new fonts** — Cascadia Code, Space Mono, Geist Mono
- **Improved model list** — Grouped by family, emoji icons, color-coded size tags, per-model "Use" button

### Improvements
- Streaming: removed 0.008s artificial delay, emits 8-char chunks with asyncio.sleep(0)
- SSE frontend reconnects with exponential backoff

---

## Alpha v6 — March 2026

### New Features
- **AI Peer Voting in council** — After all members respond, each votes for the best response
- **Improved markdown renderer** — Bullet/ordered lists, blockquotes, HR, italic, headings

---

## Alpha v5 — March 2026

### New Features
- **Dracula, One Light, Midnight themes**
- **Font size slider** (11-16px), chat width slider (560-1200px)
- **Animated tool pills** — spin, swing, bounce animations per tool type
- **Workspace system** — Group chats, file tracking, topic analysis, create personas from KB

---

## Alpha v4 — March 2026

### New Features
- **Knowledge base injection** — KB files injected into system prompt when persona is active
- **Model config parameters** — temperature, num_ctx, top_p, top_k applied to Ollama
- **Export conversation** — Download chat as Markdown

---

## Alpha v3 — February 2026

### New Features
- **Council of AI** — Debate topics with multiple models simultaneously
- **Deep Research** — Multi-source parallel research with AI synthesis
- **Custom tools** — Upload Python tools, AI can call them

---

## Alpha v2 — February 2026

### New Features
- **Knowledge Bases** — Upload and attach documents to personas
- **Personas** — Custom AI configurations with system prompts
- **SSE Event Bus** — Real-time tool status events

---

## Alpha v1 — January 2026

- Initial release: FastAPI + React SPA
- Ollama streaming chat
- CodeAgent with sandbox code execution
- SearXNG web search integration
