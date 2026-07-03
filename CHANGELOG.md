<details open>
<summary>Alpha v17.3.1 — July 3, 2026</summary>

> UI readability and reliability polish for Quick Search, Daedalus, Council
> sessions, and release notes.

## Quick Search
- Search results now scan cleaner, with calmer cards, clearer titles, and less repeated metadata.
- Ambiguous searches use recent chat and memory context, avoid unnecessary news recency, and rank context-matching sources higher.

## Daedalus UI Rendering
- Workflow output now renders as summarized project status cards instead of raw tool-heavy transcripts.
- State labels and phase chips make plan, build, review, fix, acceptance, and package progress easier to follow.
- Artifacts, downloads, build details, and raw events remain available without crowding the main message.

## Daedalus Builder, Model and Repair Hardening
- Thinking-capable coder models now build with bounded reasoning, avoid extra think-tool loops, and share one OpenHands agent setup across build paths.
- Tool-calling probes now verify multiline arguments live, recover from dropped file content, and fall back to prompt-based tools when needed.
- Architect manifests now carry advisory interfaces, required tests, README, and `.gitignore` into Builder without adding hard gates.
- Builder rounds and continue passes scale with planned files, require a real finish signal, and keep incomplete builds out of review/delivery.
- OpenHands builds now honor streaming `reasoning_effort`, cap runaway completions, and apply sampler options consistently.
- Aider is the default repair editor for existing, uploaded, and Builder-created projects; Fixer remains the fallback, and no-op repairs no longer force review loops.
- Repair and review coverage now includes research-gated retry caps, nested projects, C#/.NET, Makefile-only C, cleaner Node/Rust smoke checks, and tidier delivered archives.

## Council of AI
- Council setup now has cleaner presets, host settings, member cards, and analytics presentation.
- Council members can link to personas through `model_config_id`, using the latest persona model, prompt, and name at runtime.
- Council chats now show address panels, round sections, peer ballots, moderator verdicts, and richer round metadata.

## UI
- Changelog releases now render as collapsible version cards, with the latest release expanded.
- Quick Search source cards now show source numbers under favicons and avoid duplicate citation lists.

## Bug fixes
- Knowledge-base name and description edits now autosave and persist across server restarts.
- `generate_image` fenced blocks now become tool calls, and local actor names are no longer searched when they are only part of the conversation wording.
- Restored Daedalus to the intended two-turn Architect → Builder flow, removed stale single-turn handoff cues, and kept truncated builds incomplete.
- Fresh OpenHands builds now use task-derived workspaces unless an existing project directory is truly being resumed.
- Blocked Daedalus summaries and repair gates now recommend valid next actions and support research-backed retries before hand-fix release.
- Fixer symbol-mismatch handling now uses real class method references and drops unresolved edits instead of rewriting call sites incorrectly.
- Daedalus message rendering now detects more output shapes, preserves run ids, and shows workflow/run cards for non-full-build actions.
- Live endpoint tests now skip cleanly when no HyprChat server is reachable, and connector tests mock the async URL-safety checks introduced by the refactor.
- Node/Rust smoke checks and non-required advisory verification now avoid false review failures.
- Acceptance now requires fresh review after source/test/manifest edits from `aider.fix`; docs-only fixes may return directly to Acceptance.
- Daedalus persona guidance now matches Aider-first repair routing, with Fixer described as fallback only.

## Refactor follow-up
- Backend route, artifact, database schema, research-config, workflow-gate, parser, model-management, and Codebox tool helpers were split into focused modules while keeping the public API behavior stable.
- Frontend model picker, markdown blocks, artifact widgets, HyprChat widgets, Daedalus timeline helpers, theme/session/settings helpers, and panels were extracted from `main.jsx` without changing the Vite app architecture.
- Deploy monitor and README deploy notes now include the extracted backend and frontend modules so homelab deploys ship the refactored source tree.

</details>

---

<details>
<summary>Alpha v17.3.0 — June 22, 2026</summary>

> Adds the local media stack, persona-aware image generation, voice input/output,
> hybrid RAG citations, storage diagnostics, and token analytics.
> Adds SSH-based remote Ollama HyprFit hardware scanning and clearer rescan status.

## Highlights
- Local image generation is now available from chat and the new Image Studio.
- Voice STT/TTS is proxied through HyprChat, keeping browser traffic pointed at the main server.
- Knowledge-base answers now use hybrid retrieval and render clickable inline citations.
- Persona chats can generate rating-aware character photos from appearance/context.
- Settings now exposes token analytics, health history, cleanup, media controls, and service connection status.
- Chat list polish improves the sidebar styling and adds marquee hover behavior for long chat titles.

## Image Generation
- New `generate_image` chat tool renders ComfyUI images inline with seed, size, steps, and artifact metadata.
- Image Studio adds prompt controls, model/VAE/workflow selectors, queue progress, Stop, gallery, reuse, delete, and full purge tools.
- Settings → Model & Generation now includes chat image defaults: checkpoint, saved workflow, resolution, VAE, prompt defaults, negative prompt, and compose model.
- Prompt enhancement is available through `/api/images/enhance-prompt`, using the configured local compose/workspace model and rejecting unusable empty JSON output.
- Saved workflows can be uploaded from API JSON or workflow-bearing PNGs; KSampler and Flux-family graphs are patched by node class.
- Per-checkpoint presets now include sampler, scheduler, CFG, steps, model type, and prompt prefixes.
- Generated images are tracked as `kind=image` artifacts and appear in Artifact Studio.
- Image cleanup is trace-aware: completed jobs forget ComfyUI history/output copies when the optional control node is installed, and Delete all purges HyprChat artifacts, chat image references, ComfyUI traces, and logs where available.
- New unload/restart/memory controls and optional ComfyUI custom routes: `/hyprchat/free`, `/hyprchat/memory`, `/hyprchat/restart`, `/hyprchat/cleanup`, plus idle model unload.
- Hardening pass: in-flight job guards, prompt redaction, safer tool-call parsing, purge pagination fixes, chat attachment cleanup, and stricter persona/photo prompt validation.

## Persona Photos
- Personas now have an Appearance field used for selfie and character-photo requests.
- Photo prompts are composed from appearance, current scene, user request, prior images, and optional per-persona image profiles.
- Persona image profiles live in `persona_image_profiles.json` and can route intents to profile-specific workflow/default settings.
- Persona content ratings gate image prompts: PG/PG-13 stays conservative, while adult-rated personas can use the configured compose model for allowed requests.
- Selfie rescue forces `generate_image` when a persona describes a requested photo in text instead of calling the tool.

## Voice
- Composer mic button records audio and transcribes it through an OpenAI-compatible STT service such as Speaches.
- Assistant reply Speak button and optional autoplay synthesize audio through an OpenAI-compatible TTS service such as Kokoro.
- Deferred speech playback is available through `/api/audio/speech/request` and `/api/audio/speech/{request_id}` so the UI can create expiring TTS URLs.
- Markdown, code fences, links, and citations are stripped before TTS.
- Settings → Connections adds Voice STT/TTS URLs, health status, and a voice picker.
- Plain HTTP installs still need a browser secure-context exception for microphone capture.

## HyprFit
- Rescan Hardware can now scan remote Ollama hosts over SSH with `nvidia-smi`, RAM, OS, and architecture probes.
- Remote scans persist detected accelerator profiles only when SSH hardware detection succeeds.
- Remote Ollama hosts without SSH scan settings now show setup-required status instead of treating saved profiles as scanned hardware.
- Remote SSH/auth/command failures keep the saved profile for recommendations and surface a scan-failed status.
- Local Ollama hosts continue to support automatic accelerator detection.

## UI
- Settings → Connections now includes local-only Ollama hardware scan SSH settings with write-only password storage.
- New HyprFit rescan toasts distinguish local detection, remote scanned, setup-required, scan-failed, and CPU fallback states.
- New hardware profile chips show rescan mode, detected backend, Ollama reachability, and the sanitized Ollama origin.

## RAG + Citations
- KB search now fuses Chroma vector results with SQLite FTS5 keyword results using Reciprocal Rank Fusion.
- Exact tokens such as part numbers, IDs, and error strings rank more reliably.
- Retrieval falls back to keyword-only if embeddings are unavailable.
- Answers cite numbered excerpts with `[n]`; citations render as chips and persist in saved events.
- Existing KBs backfill into FTS automatically; Reindex rebuilds both stores.
- Added `POST /api/knowledge-bases/query` for retrieval testing.

## Analytics, Storage, and Cleanup
- Token usage is recorded per conversation/model/persona and exposed through `/api/analytics/tokens` and `/api/analytics/tokens/summary`.
- Runtime storage diagnostics check SQLite and Chroma writeability so readonly data-dir failures surface as actionable errors instead of opaque RAG crashes.
- Settings cleanup tools cover local sandbox outputs and Codebox project cleanup through `/api/settings/cleanup-now` and `/api/settings/cleanup-codebox`.
- Danger Zone cleanup now covers chats, memories, artifacts, statistics, local Ollama models, other users, and a full fresh-install reset.
- Health history persists dependency checks in `service_health_log` and is shown in Settings.

## Bug fixes
- Fixed `conspiracy_research` depth parsing so malformed values fall back safely instead of crashing the tool call.
- Fixed Image Studio artifact hashing to use a closed, non-blocking metadata path during job polling.
- Fixed ComfyUI v-prediction/flow sampling injection so unrelated sampling nodes no longer suppress the required mode-specific node.
- Fixed ComfyUI cleanup races by tracking pending prompt submissions before ComfyUI returns a prompt id.
- Fixed empty SSE subscriber lists lingering after the last client unsubscribes.
- Long chat titles now fade at the edge and marquee on hover instead of ending with hard ellipses.
- Removed the remaining runtime v1 CodeAgent planning path; `plan_project` now always routes through the structured Architect path.
- Fixed KB reindex failures from readonly RAG/Chroma SQLite storage with clearer diagnostics and deploy-time data-directory permission repair.
- Hardened `/api/img-proxy` so proxied chat images use the same safe-fetch path as URL previewing, blocking loopback/private targets and unsafe redirects before any private URL is fetched.
- Kept image proxy behavior intact while enforcing the existing 5 MB cap, image-only content check, cache header, and domain-matched Referer.
- Fixed dependency-light backend test collection by replacing fragile optional-dependency stubs with proper module stubs and centralized `aiosqlite`/`chromadb` availability checks.

## Setup, Ops, and Tests
- `.env.example`, deploy scripts, `create-lxc.sh`, and `deploy_monitor.py` were updated for the media services, connector secrets, sandbox paths, Quick Search, and Aider/OpenHands settings used by this release.
- `create-lxc.sh` can optionally hand off to the ComfyUI/Voice LXC installer.
- `create-comfyui-lxc.sh` documents the HyprChat ComfyUI control node, idle unload env vars, restart route, and verification commands.
- `deploy_monitor.py` now watches the Vite source/package files plus `image_prompt_enhancer.py`, `persona_images.py`, and `storage_diagnostics.py`.
- New and expanded tests cover workflow patching, persona image prompts, prompt enhancement normalization, ComfyUI control endpoints, hybrid RAG, audio routes, storage diagnostics, and token analytics.
- Live media tests skip cleanly when the configured services are unavailable.

## Deployment
- `deploy_monitor.py` now includes the HyprFit backend module in watched deploy files.

</details>

---

<details>
<summary>Alpha v17.2.1 - June 14, 2026</summary>

> This update focuses on the first series to migrate a single file react **no build step** app into a built app versus having all built at runtime on the users browser. Load times should be faster and it should be easier to maintain the code base in the future.

## Vite Migration
- **The frontend now has a build step.** Source moved to `frontend/src/main.jsx` (same single component file); `frontend/dist/` is Vite build output and is **no longer committed** — fresh clones must run `cd frontend && npm install && npm run build` before the backend can serve the UI.
- **No more in-browser Babel** — JSX is pre-compiled, so pages load faster and a bad edit fails loudly at `npm run build` instead of white-screening the app.
- Third-party libs (React, Prism, KaTeX, Mermaid, Chart.js, html2pdf, svg-pan-zoom) are npm-bundled locally — no runtime CDN. Mermaid/Chart/html2pdf lazy-load on demand.
- **Deploy-safe caching:** hashed `assets/` are served immutable (1-year cache); `index.html` is always `no-cache`, so a deploy can never leave browsers requesting deleted chunks.
- **Atomic frontend deploys:** `deploy_monitor.py` watches `frontend/src/**`, builds, uploads to a staging dir, and swaps it in — a failed upload no longer leaves the server with no frontend. The server stays Node-free.
- Backend prints clear startup instructions if `frontend/dist/` is missing instead of serving nothing silently.

## Ollama 0.30 Compatibility
- **Leaked-reasoning guard:** some models (e.g. gemma4 on Ollama 0.30.x) emit chain-of-thought as plain content after tool rounds; the chat stream now detects this and routes it to the thinking pane instead of the reply.
- The same strip (`strip_leaked_cot`) protects non-streamed agent calls, council member responses, and research synthesis, so leaked CoT never enters debate context, votes, or the database.

## Artifact Delivery & Versioning
- **Auto-redelivery:** a feature-addition turn on an already-delivered project repackages it automatically and posts a fresh download pill (skipped if the reviewer flagged issues).
- **Per-version downloads:** new `/api/artifacts/{id}/download` endpoint serves each artifact's exact bytes — old pills always download what they originally packaged.
- **`latest` / `⚠ stale` badges** on artifacts and version lists when the project changed after packaging, with per-version download links.

## UI Updates
- **Cleaner chat list:** icon-based pin/tag/delete actions that appear on hover, row hover highlight, tighter spacing, and proper title truncation.
- Artifact cards: Add to KB always available, version rows show status badges inline.

## Bug fixes
- **Phantom-completion guard:** a coder persona claiming it built something while calling zero tools is re-prompted to actually do the work (or admit it didn't) — users are never handed a false success.
- Council sends no longer fire a duplicate quick-search request; one SearXNG fetch feeds both the members and the results carousel.
- Connector URL safety checks (`assert_url_allowed`) are now async with DNS resolution off the event loop.

## Cleanup
- Removed the deprecated Quick Search LLM triage path and its tests (deterministic planner is the only path).
- Removed dead code across `database.py`, `cancel_registry.py`, `acceptance.py`, and `connectors.py`.
- `create-lxc.sh` and `deploy.sh` updated for the built-`dist/` layout.

</details>

---

<details>
<summary>Alpha v17.2.0 — June 12, 2026</summary>

> This update focuses on existing system hardening and and improving existing features.

## Cloud Model Support (optional)
- Added API key entry for openAI
- Added API key entry for Anthropic
- Cloud models can be used for regular chat, research and agents.

## Deep Research & SearXNG
- **SSRF-safe page fetchers** with 2 MB body cap and per-hop DNS checks
- **Redirect URLs tracked** for proper attribution (no more "S?" sources)
- **Research tokens via SSE only** (no DB writes per chunk)
- **Cancel can't be resurrected** — pre-cancelled rows honored at start
- **Embeddings batched** (64-text per Ollama request)
- **ChromaDB off event loop** — large upserts don't freeze the server
- **Parallel seed/GitHub fetches** with cancellable loop
- **Context window setting** (`research_num_ctx`, default 40960) prevents prompt truncation
- **SearXNG failures logged**, Google fallback capped at 8/report
- **RAG reindex fixed** — chunk size clamped, upserts batched ≤5000 records
- **SearXNG hourly rotation** (was 10-min) — engines no longer suspended

## Core System
- **Event logs append-only** — O(n²) write amplification eliminated
- **Startup reaper** covers both runs and research reports
- **Memory suggestions run post-turn** in background (no more 90s delays)
- **Cloud models safe for judgment agents** — JSON output enforced
- **Mid-stream failures persist partial messages** with "interrupted" note
- **Council debates can't wedge** — done-sentinel always fires
- **RAG reindex clears orphan chunks**
- **Settings PATCH clamps junk values**
- **Frontend:** bounded message metadata, memoized markdown rendering

## Daedalus (Coder Bot v2)

### Architecture & Workflow
- **SEARCH/REPLACE diffs** instead of whole-file regeneration; `### REWRITE:` escape for full rewrites. Fixes data-loss from truncated prompts.
- **Git commits after every build/fix cycle** — `git log --oneline` is now the authoritative attempt history for easy reverts
- **Real FSM workflow state** with single transition function (`PLAN_DONE`, `BUILD_OK`, `REVIEW_CLEAN/ISSUES`, `FIX_APPLIED`, `ACCEPT_OK/ISSUES`)
- **Auto-verification** after `generate_code`, `run_fixer`, and `run_aider_fix` — Reviewer runs automatically, removing LLM routing rounds
- **Schema-constrained Architect output** (`format=json`) eliminates most plan parse-retry rounds
- **Fixer can delete files** (`# DELETE:` sections) — fixes infinite loops on runtime/state files that should be removed
- **Reviewer smoke phase cleans up** its own artifacts — verification doesn't pollute the tree it grades

### Cancellation & Control
- **Stop frees GPU immediately** — agent LLM calls now stream internally instead of non-streaming, aborting Ollama on cancel
- **Workers self-cancel on disconnect** — OpenHands/Aider SSE consumer vanishes triggers worker cancellation
- **4096 token cap** on structured-output agents (plan/review/acceptance JSON, QA answers)
- **Acceptance progress ticker** — "analyzing… Ns elapsed" during model calls
- **Fix-budget feedback** — "Fix-cycle budget: N/3 used" in every fixer/Aider result
- **Reviewer smokes real CLI** — reads `[project.scripts]` and runs `<script> --help` for pyproject projects

### Agentic Improvements
- **Fix attempts have memory** — Fixer/Aider see compact history of prior changes ("touched app.py — renamed handler")
- **Cloud models opt-in** for Architect/Reviewer/Acceptance via Settings → Coder Bot (never silently inherited)
- **Fix-cycle caps per request** (3 reviewer-driven, 2 acceptance-driven), reset by new user message
- **Uploaded-project indexer** prioritizes entrypoints/larger source files (was 100 smallest), cancellable via Stop
- **Context window respected** — Architect uses configured `research_num_ctx` instead of hardcoded 16384
- **Duplicate-BLOCKED detection** keys on blocking trigger, not tool name
- **Frontend polling** stops on terminal states (`cancelled`, `skipped`, `blocked`)

**Migration:** Run `POST /api/seed/coder-bot-v2` after deploy for updated persona prompts.

</details>

---

<details>
<summary>Alpha v17.1.2 — June 10, 2026</summary>

### HyprChat Memory & Navigation
- Added global HyprChat memory that can persist across chats when memory is enabled for a conversation.
- Added a user profile and memory management panel for profile details, interests, links, manual memories, and reviewed memory suggestions.
- Added a per-chat memory toggle, including support for enabling memory on an empty chat before the first message creates the saved conversation.
- Reorganized the left rail so Search, Chat, Research, Council, Memory, Agents, and Prompt Library are always visible.
- Moved Knowledge Bases, Tools, Model Manager, and Analytics behind an inline expandable More section that opens inside the rail instead of a separate popout.

### Workspace Memory & Ghost Mode
- Added workspace-scoped memory with reviewed suggestions, accepted memories, and manual memory blocks that can be injected into workspace chats.
- Added memory review controls for accepting, rejecting, editing, deleting, and manually creating semantic, episodic, and procedural memories.
- Added Ghost Mode for local-only chats that are not saved, indexed, added to history, or used for workspace memory suggestions.

### Quick Search
- Improved same-day event queries so prompts like "WWDC that just took place today" search cleaner event/year/date variants instead of dragging filler words into SearXNG.
- Added a general-web fallback for strict same-day searches so fresh official pages, live blogs, and tech coverage can surface when the news/day filter misses them.
- Made same-day evidence handling smarter by accepting exact dates, "today", recent-hour snippets, and current-year live-update text before warning that sources are stale.

### Chat Streaming & Context Meter
- Tool-enabled chat rounds now stream assistant text live while it is generated instead of waiting for the full model output to finish.
- If streamed draft text turns into a native or text-parsed tool call, the chat clears that draft before showing the tool execution cards/results.
- Replaced the misleading live "Generating ... chars" status with token-based wording that matches the top-right context meter.
- Clarified the header context meter tooltip as prompt plus generated tokens against the active `num_ctx`.

### Multi-User support (Not for concurrent usage, only for splitting up your chats)
- Now you have the option to have different users for different things. Keep all your chats, workspaces and configurations separate from each other by signing into the appropriate user.
- Each user has the option to have a password or not.
- Manage users in the settings panel. You can set passwords, reset passwords, add users and remove users.


### Bug Fixes
- Fixed a fresh empty-chat race where selecting a model in the top-left picker and immediately sending could create the chat with the selected model but stream the first response through a stale/default model before React state caught up.

</details>

---

<details>
<summary>Alpha v17.1.1 — June 8, 2026</summary>

>Deep Research overhaul update. This update focued on making sure deep research got the love it deserved. Daedalus will still use deep research but for the user front end it has been renamed to Agent Research (the togglable tool above the composer). A backend code rename could break a lot of things and that is something ill do at a later date.

### Removals
- CoderbotV1 has been completely removed and replaced by Daedalus. RIP.
- Removed the legacy Deep Research Agent under personas and removed it from load default seeds.

### Deep Research Panel
- Added the dedicated Deep Research workspace as a first-class panel with report creation, live run timeline, sources, findings, metrics, rerun/cancel/delete actions, Markdown export, and report history.
- Added report inputs for pasted notes, uploaded files/PDF text extraction, knowledge-base selection, report templates, depth, and role-model overrides.
- Deep Research reports now persist `events_log`, findings, source metadata, metrics, and rendered report Markdown so completed runs can be reopened from the report list.
- Added a running-report polling fallback so completed reports load into the main display even if the dedicated SSE stream disconnects or the browser misses the final `research_done` event.
- Reworked research report PDF export to generate a cleaner document-style PDF from semantic report Markdown instead of cloning the app-themed React view.
- Added a separate Print action with cleaner print HTML and a direct Markdown export that uses the active report title for filenames.
- Deep research tool has been renamed to Agent Research on the front end to better clairify what each one is used for. Agents and AI will use Agent Research and users will use the dedicated new deep research panel. I did not renamed the deep research tool on the backend for right now.
- Deep Research tool (now Agent Research) has been updated for better compatability for AI usage (specificially Daedalus) by improving MD prompting and parameter descriptions.

### Quick Search
- Rebuilt Quick Search around deterministic planning instead of default LLM triage, with speed, balanced, and quality modes.
- Added stricter freshness handling for today/latest/current-event searches using SearXNG native filters and same-day source warnings.
- Added provider/scraper/reranker configuration seams, optional SearXNG engine routing, richer source metadata, and clearer search-unavailable guidance. 

### Bug Fixes
- Cleaned up Daedalus agent prompt. 11k token prompt slimed down to only 300ish tokens. Removed descrepencies.
- Daedalus Reviewer now dynamically verifies markerless/static projects instead of failing valid HTML, plain-source, or generic deliverables for missing package/build manifests.
- Empty new-chat screens now show the same model selector as existing conversations, and that pending selection is applied when the first message creates the chat.
- Reviewer now scopes Python dependency install failures to the editable package manifest instead of misrouting external package compiler errors to application source files.
- Hardened external preview and `fetch_url` paths against localhost/LAN/Tailscale/metadata SSRF targets, unsafe redirects, oversized responses, and same-origin external HTML execution.
- Sanitized JSON-created custom tool filenames so they cannot write outside the tools directory.
- Fixed skipped Quick Search turns leaving the composer/search chip stuck in a loading timeout state.
- Fixed Deep Research final synthesis transport failures being saved as completed reports.
- Fixed Deep Research panel cancel/delete/rerun state so active reports do not stay stuck as running or duplicate report rows.
- Added regression coverage for Agent Research cache handoff, Daedalus stuck-fix research gating, and durable Deep Research report persistence.
- Updated backend test defaults to the Tailscale HTTP endpoint the service actually listens on.

</details>

---

<details>
<summary>Alpha v17.1 — June 3, 2026</summary>

## UI Overhaul Update
### Developer Note 
> Complete refresh and update to the overall feel of hyprchat with a new take on personas. Next update will feature hotfixes and changes to deep research. UI changes are subject to undergo more changes till I feel they look right. This is not final for UI but a step into the right direction. For bugs, please raise an issue through the GitHub issue tracker [here](https://github.com/eefernet/hyprchat/issues).
### Visual Direction & App Shell
- This UI refresh was heavily inspired by Pewdiepies [Odysseus](https://pewdiepie-archdaemon.github.io/odysseus/) local AI webview. So I stole his ideas.
- Added a flatter, modern Hyprland-inspired visual direction while preserving the existing HyprChat terminal/cyberpunk feel.
- Added HyprFlat-style theme polish, tighter glass panels, cleaner borders, flatter controls, and refreshed chat/settings surfaces.
- Updated the site favicon to the HyprChat logo mark.
- Added selectable animated background effects in Settings next to the theme selector, including Pixel Rain, Soft Flow, Aurora Lines, Star Drift, Circuit Drift, and Odysseus-style flow variants.
- Tuned the background effects so they fill the whole viewport, are more visible where needed, and avoid dense rain, oversized bars, or overly aggressive glitching.

### Documentation & Screenshots
- Reworked the README screenshots from a bare image dump into a top collapsible gallery plus contextual feature screenshots placed next to the sections they explain.
- Added README coverage for the new Agents and Personas profile manager, Knowledge Bases, Model Manager, Activity Monitor, Council, Council Settings, Settings, and main chat screenshots.
- Added rich-rendering README examples for KaTeX/LaTeX notation, Mermaid diagrams, charts, and callouts.
- Updated README feature copy to match the Agents/Personas split, including Persona card import, per-profile KBs, rating controls, thinking-mode overrides, and first-message behavior.

### Startup & Empty Chat
- Updated the startup/loading screen so it uses the selected theme and selected animated background instead of the old standalone loading look.
- Updated the first-chat empty state with the HyprChat logo, centered product greeting, generated daily message copy, and a taller first-message composer that settles back to the normal bottom composer after sending.
- Page loads and refreshes now open to the new empty chat surface instead of restoring the most recent conversation; the real chat row is created when the first message is sent.
- New Chat now opens the empty chat surface immediately; the composer keeps the smooth float up/down behavior, lands closer to the greeting, and delays textarea focus with `preventScroll` so the browser does not nudge it upward at the end.
- Empty-chat daily greeting messages now use a more playful prompt and stronger fallback lines, with a cache version bump so browsers refresh stale generic copy.

### Chat & Composer
- Moved the streaming/loading sweep from the top of the page to an animated trace around the composer.
- Raised the composer control layer so Prompt Library and effort menus render above chat/chart content, and nudged the empty-chat composer closer to the greeting.
- Reworked the composer controls so Attachments, Prompt Library, and Daedalus activation live under a single `+` quick-actions menu, with the effort selector moved next to Send.
- Empty-chat effort selection and Daedalus activation now work before the first message; pending choices are applied when the real conversation is created.
- Added inline PDF extraction failure feedback with retry/remove actions.
- Quick Search failures now surface as a compact warning chip instead of silently clearing results or spinning indefinitely.
- Expanded chat Quick Search from a 6-source helper to a broader fast search that can return up to 35 source cards when enough useful results are available.
- Increased Quick Search SearXNG retrieval to pull larger candidate pools, with broader/current/news/comparison searches requesting more results before ranking and dedupe.
- Updated Quick Search ranking to run domain diversity and embedding dedupe over the larger pool, then backfill from remaining candidates so dedupe does not unnecessarily shrink the final source list.
- Kept Quick Search context compact by injecting title, URL, domain, and snippets for up to 35 sources while fetching full page excerpts only for the top few results.

### Sidebar, Search & Chat Utilities
- Cleaned up sidebar search: title filtering and full-message search now live behind a single rail search action above Chat.
- Reworked export/import controls into a single export menu with Markdown/JSON choices and corrected import/export icon usage.
- Import/export failures now show visible in-app feedback instead of failing silently or only logging to the console.

### Settings
- Reworked Settings into a centered modal overlay with left-side section navigation and a scrollable right-side editor instead of replacing the chat surface.
- Moved Changelog into Settings as a bottom-pinned sidebar section that renders in the right editor pane with inline refresh.
- Reworked **Connections** into a vertically stacked endpoint editor with service status chips, always-visible Ollama/Codebox/N8N/SearXNG URL fields, and `/api/settings` persistence.
- Added consistent save feedback for Connections, RAG settings, Loading Quotes, appearance preferences, and model parameter settings.
- Rebuilt **Loading Quotes** as a scrollable editor with readable full quote rows, attribution display, inline edit/save/cancel/remove controls, restore defaults, and add-new-quote controls.
- Added 30 default loading quotes from real philosophers, religious texts, mystics, and scientists, with cleanup for previously saved placeholder HyprChat-authored defaults.

### Personas & Daedalus
- Renamed Coder Bot v2 to **Daedalus** throughout the UI and updated related model/context labels.
- Added persona descriptions to the Personas screen and persona editor.
- Restore-defaults actions now use themed in-app confirmation and visible success/error feedback.

### Agents & Personas Split
- Split model configs into **Agents** for task/workflow-driven assistants and **Personas** for roleplay, voice, and character-style profiles.
- Added profile-type metadata via `parameters.profile_type` while keeping the existing `model_configs` table and compatibility API field names.
- Added compact top-of-page guidance explaining the difference between Agents and Personas, plus separate create/edit flows for each profile type.
- Added **Kayla — Gen Z Bestie** as a fully populated example Persona so users can see how description, personality, scenario, first message, example dialogue, lore, tags, rating, and sampling fields are meant to be filled.
- Replaced the old Based Bot seed with **Tyler — Based Gamer Bro**, a fully populated 18-year-old gamer-bro Persona with description, personality, scenario, first message, example dialogue, lore, tags, rating, and advanced prompt filled in.
- Added a Persona age rating to a fixed emoji-labeled dropdown (`G`, `PG`, `PG-13`, `R`, `NC-17`, `Unrated`) with visible descriptors and model-facing content-boundary guidance during chat.
- Added a per-Persona Thinking Mode control (`Auto`, `On`, `Off`) that can override the global thinking setting for Persona chats.
- Enabled Knowledge Base attachment controls for Personas, including Persona card KB badges and chat-time KB retrieval when a Persona is active.
- Fixed empty-chat Persona state handling so leaving a Persona/Agent detaches the active profile instead of spawning a new chat with the same profile.
- Persona `first_message` appears as the first assistant message in fresh Persona chats and is included in the chat context.
- Added Persona import for Chub, SillyTavern, and TavernAI character cards, including PNG `chara` metadata cards, JSON card files, Chub-style packed definition splitting, tags, alternate greetings, character-book lore, and first-message mapping.
- Added a built-in Daedalus avatar so the coding agent has a fitting image in the Agents page, chat header, sidebar, and assistant message bubbles.

### Models, Knowledge Bases & Activity
- Reworked the top-right Downloads tray into a shared **Activity** tray for model pulls/downloads, KB uploads/indexing, project archive uploads, RAG reindex/purge jobs, cleanup jobs, and tool-template patching.
- Model deletion, tool-calling enablement, and tool-template patching now use themed in-app confirmations/toasts instead of browser alerts.
- Knowledge Base uploads now show both inline upload/indexing progress and Activity tray status.
- RAG reindex and purge flows now use themed confirmation dialogs, Activity progress rows, and visible success/error feedback.

### Feedback & Confirmations
- Added shared toast feedback variants for success, error, warning, and info messages, with optional action buttons.
- Replaced normal browser `alert()` / `confirm()` flows with themed in-app dialogs and toasts across destructive actions, restore-defaults actions, model management, RAG maintenance, tool-template patching, import/export, and cleanup workflows.
- User-triggered failures that previously only wrote to the developer console now surface visible toasts or inline card errors while keeping console logging for debugging.

### Bug Fixes
- Fixed Acceptance falsely reporting long source files as truncated or syntactically incomplete when its static source excerpt ended mid-statement. Acceptance now reads much larger source excerpts, marks any remaining excerpt truncation explicitly, and treats the clean Reviewer build/lint result as authoritative for syntax status.

</details>

---

<details>
<summary>Alpha v17.0.2 — June 2, 2026</summary>

### Coder Bot v2 — uploaded-project repair hardening

- Reviewer now deterministically detects stale `/root/projects/...` references in test/source files before LLM analysis, points the issue at the concrete stale-path file, and includes both the stale path and active project root in the repair envelope.
- Reviewer now classifies persistent state/schema failures such as `no such column`, `No item with that key`, shared database/cache/state files, and duplicate/unique constraint errors without relying on LLM guesses.
- Reviewer issue file refs now prefer real files from the project tree and failure output, including mapping guessed storage modules to the actual implementation file when needed.
- Aider uploaded-project prompts now include a `Known Test Root` section with active `project_dir`, test command, stale absolute paths, and the latest failure tail.
- Aider prompts now include `Test State Isolation` guidance so fixes make tests project-root-relative and keep DB/cache/state paths configurable or temp-dir scoped.
- Uploaded-project repair workflows now block initial manual file/shell tools and route first edits through `run_aider_fix` or verification through `run_review`.
- The chat loop now stops after repeated duplicate `BLOCKED` manual-tool responses, marks the uploaded-project workflow blocked, and reports the latest reviewer issue, active project path, and next valid action instead of burning rounds on the same forbidden tool.
- Verified against the TaskForge upload loop: stale test roots were repaired, Aider/Reviewer cycles converged, Reviewer passed, Acceptance accepted, and the artifact was delivered. Also verified a clean greenfield NoteShelf path through Architect → OpenHands → Reviewer (`11 passed`) → Acceptance → download.

### Coder Bot v2 — acceptance gate

- Added `run_acceptance_review` as a required final gate after clean `run_review`.
- Added `backend/agents/acceptance.py` for static spec/docs/tests/packaging checks.
- `run_fixer` now handles acceptance issues, with docs-only fixes allowed to skip build review.
- `download_project` now waits for accepted status and excludes common generated/cache/build artifacts.
- Added Acceptance model override in Settings; empty inherits from Planning Model.
- Acceptance now uses the user-configured default context window instead of Ollama defaults.
- Deploy monitor now pushes the new Acceptance agent file.
- Architect, Reviewer, and Fixer structured model calls now set `think=false`, avoiding thinking-only review rounds and keeping JSON/envelope output in the message content.
- Acceptance structured JSON calls now also set `think=false` and parse JSON from `message.content`, `message.thinking`, or top-level `thinking`.
- Acceptance file scans now prune generated/dependency/cache folders instead of walking them before filtering.

### Coder Bot v2 — hybrid workflow router

- Added workflow-level state via `coder_workflows` and `WorkflowCard`.
- Routed greenfield builds to OpenHands, uploaded-project fixes to Aider, and uploaded-project questions to read-only ProjectQA.
- Added `run_aider_fix` plus `/aider/*` worker endpoints on Codebox.
- Uploads now create a local git baseline and return detected build/test contract metadata.
- ProjectQA now mixes filename targets, grep, code-memory hits, and marker files for better citations.
- Deploy monitor now bootstraps fresh hosts and installs missing Aider worker support.

### Bug fixes

- Settings no longer PATCH stale `localStorage` values over server-persisted v2 model/context settings during initial page load.
- Workspace Model helper calls no longer load small analysis models at their native huge context; workspace topic analysis and council suggestions are capped at 4K, and title generation remains capped at 2K.
- Uploaded-project git baselines now mark Codebox project paths as safe directories before status/init checks.
- Uploaded-project reviewer issues now route to Aider even when a stale bot tries the old Fixer path.
- Aider fixes now use Codebox Python, track active workflow runs, and stop repeated-output loops.

### Removed
- HyprChat already has n8n integration, no need to reinvent the wheel with Workflows feature built in. Also at this point, I rarely use it.
- Removed the legacy internal automation runner, its UI panel, REST/webhook APIs, scheduler, chat slash command, deploy watch entry, and tests. External automation should run through the existing n8n VM and `/api/n8n/execute`.
- Startup DB cleanup now drops the legacy internal automation tables (`workflow_schedules`, `workflow_runs`, `workflows`) on existing installs.

### Tests

- Added focused Acceptance structured-output tests, frontend settings hydration guard tests, Workspace Model context-cap tests, and a Quick Search recency test for `time_range="month"`.

</details>

---

<details>
<summary>Alpha v17.0.1 — May 26, 2026</summary>

### Coder Bot v2 — workflow gate hardening

Ten edge cases in the v2 gate, profile detection, and persona prompt that together swung the same prompt between a clean 16-round bug fix and a 24-round rebuild loop. All fixed.

#### Gate / Reviewer / Fixer
- **Phantom `run_fixer` blocked** — `tools.py` gate refuses `run_fixer` unless the latest run is a reviewer with `status="issues"`/`"error"`. Stops cycle-cap burn from hallucinated `reviewer_run_id`s.
- **"Nothing to fix" is `skipped`, not `succeeded`** — `agents/fixer.py:249`. The 3-cycle cap counts only real attempts now.
- **`pytest` exit 5 ("no tests collected") treated as benign** — `agents/reviewer.py:43,47` append `|| echo '(no tests)'`. Greenfield scaffolds stop infinite-looping on a missing `pytest.ini`.
- **Cycle cap delivers gracefully** — PENDING_FIX gate releases `download_project`, `download_file`, `read_file`, `list_files` once the cap fires, with the cap message suggesting `download_project` as the ship path.
- **Anti-rebuild guard** — `tools.py` blocks a second `generate_code` against the same project in the same turn (detected via `runs.started_at >= latest_user_message.created_at`). Pushes the model toward `read_file` + `write_file` for refinement.
- **Profile auto-detect for edited uploads** — `tools.py` fallback sets `_builder_profile = "feature"` when an active project has no prior builder run, so `write_file`-edited uploads aren't clobbered by a scaffold rebuild.
- **`_parse_ts_loose()`** — new helper unifies SQLite `CURRENT_TIMESTAMP` (space, no µs) and Python `isoformat()` (T, with µs) for cross-table "is this run from the current turn?" comparisons.

#### ACTIVE PROJECT block
- **Path-explicit injection** — `agents/chat.py` emits `**ON-DISK PATH (use this for ALL tool calls): /root/projects/{project_id}**` and relabels the human name `display name (NOT a directory)`. Stops `mkdir -p /root/projects/{display-name}` mistakes.
- **`write_file` is the default** — small edits, refactors, 1–5 line pastes all route to `read_file` → `write_file`. `generate_code` reserved for genuinely new 3+ files or major refactors.
- **Runtime-bug branch** — runtime crashes that compile fine (font errors, bad preview state) skip `run_review` and go straight to `read_file` → `write_file`, since the reviewer can't see them. Persona prompt updated to match.

#### Persona + seeding
- **Coder Bot v2 prompt** — runtime-bug branch added; new top-level **ANTI-REBUILD RULE** explaining the gate.
- **`seed_coder_bot_v2()` updates in-place** — `agents/personas.py` no longer deletes-and-recreates on re-seed, preserving `mc_id` so existing conversations keep their persona link.

#### Settings & uploads
- **Empty-string settings respected on restart** — `main.py:183` switched to `"key" in _settings` membership checks. Planning Model = "(use chat model)" now survives restarts instead of reverting to the env default. Same fix for `coder_model`.
- **Upload limit 50 MB → 250 MB** — `config.MAX_UPLOAD_SIZE_MB` (env-overridable). Fits a typical PyQt5 + venv. `main.py:1641` PDF route and `frontend/dist/index.html:2589` attach guard now route through the constant.

### OpenHands — Reasoning Effort
- **New "Reasoning Effort" dropdown** in the OpenHands settings tile: **Low** / **Medium** (new default for local Ollama) / **High** (SDK default).
- **Why it matters** — the SDK defaults `reasoning_effort=high` + `extended_thinking_budget=200000`. On a 30B model with `num_ctx=16384` that's 30+ s per tool round; with chat ↔ builder VRAM thrashing, a 3-file scaffold ran 5 min wall-clock. Medium/Low cuts that with no quality regression on CRUD/UI tasks.
- **Plumbing** — `OPENHANDS_REASONING_EFFORT` in `config.py` → settings handlers in `main.py` → `oh_payload` in `tools.py` → `RunRequest.reasoning_effort` in `openhands_worker.py` → `_LLM(reasoning_effort=…)` with a `TypeError` fallback for older SDKs (logs a warning, no-op).

### Quick Search — best-of synthesis

Pulled the highest-ROI patterns from Perplexica (24k★), Khoj (18k★), and Open WebUI (70k★) into `backend/search_agent.py` + `backend/quick_search.py`.

- **`standalone_query` in triage** (Perplexica) — triage now returns a context-independent rephrasing of the latest message alongside `queries[]`. Used as the canonical query for ranking, embedding, and the carousel — kills pronoun/anaphora bugs at the source.
- **Embedding rerank + dedup** (Perplexica) — batched `nomic-embed-text` call scores snippets vs `standalone_query`; drops sim < 0.45, dedups pairs > 0.85. Catches synonym/paraphrase mismatches and the SearXNG-mirror dup case. Falls back to heuristic order on any embed failure.
- **Trafilatura page extraction** — replaces the regex strip with `trafilatura.extract()` on top-N pages. JS-heavy and paywall-prelude pages now produce usable content. Falls back inline (same fetched HTML, no double round-trip) if trafilatura is unavailable.
- **Per-conversation subquery dedup** (Khoj) — drops queries already searched earlier in the same conversation; preserves at least one query if all are dupes.
- **Follow-up relevance fix** — `relevance_score` now folds prior-turn tokens into the user-token set when the message is a follow-up. Refine actually fires for "any updates?" instead of returning 1.0 every time.
- **News freshness** — when triage's category is `news` and the user message has a recency cue (`today`/`latest`/`current year`), SearXNG gets `time_range=month`. Cache key becomes `(query, time_range)`.
- **Triage category used for ranking** — was being computed and discarded; now drives `_apply_domain_bias` directly. Regex `_classify_query` becomes the triage-failure fallback only.
- **SSRF guard** (Open WebUI) — `_url_safe()` resolves hostnames before fetch; rejects private/loopback/link-local IPs. Defends against malicious search results redirecting to internal services.
- **Bounded LRU cache** — `_CACHE` capped at 512 entries with LRU eviction. Prevents long-running-process leak.
- **Concurrency semaphore** — `_FETCH_SEMA(6)` around page-fetch + OG-image-fetch. Smooths bursts on the SearXNG LXC and ProtonVPN exit.
- **Search-failure note** — when quick search throws, a system note ("web search was unavailable, don't guess") is appended to the user turn so the model doesn't hallucinate current events.
- **Pre-existing `_search_searxng` bug** — any SearXNG result with a thumbnail was being marked `type="image"` and filtered out by the chat ranker. News results almost always carry an `og:image`; this was silently dropping ~60% of news results before they reached embedding rerank. Narrowed to URL-extension-only.
- **Removed** — `_fetch_page` import in `quick_search.py` (replaced by `_fetch_clean_page`); stale `_rewrite_query` references in docstrings.
- **New dep** — `trafilatura>=1.10.0` in `requirements.txt`.

</details>

---

<details>
<summary>Alpha v17 — May 7, 2026</summary>

### Coder Bot v2 — Multi-Agent Rebuild

Introducing a completely rebuilt from the ground up replacement for Coderbot. For right now, coderbot v1 will still be present in builds until v2 is more battle tested. Coderbot v1 is deprecated and will be removed in a later release. Coderbot v2 is faster, more effecient and more accurate. Its more feature packed than coderbot v1 would ever dream to be. Coderbot v2 is intended to be used for full projects or investigating existing user uploaded projects or generating any code.

The v1 Coder Bot was a single chat agent that did everything: planning, building, reviewing, fixing, packaging — all crammed into one round-by-round loop. It worked, but every step depended on the model "remembering" prompt rules, and a single misstep (manual `write_file` instead of `generate_code`, refusing to call `run_review`, ignoring the fix-loop cap) sent the whole thing into a 28-round death spiral. v2 is a **deterministic multi-agent pipeline**: each role is a stateless run with a structured envelope, and a server-side gate enforces the workflow instead of trusting the model to obey.

#### Agent roster

| Role | File | Responsibility | Model |
|---|---|---|---|
| **Architect** | `backend/agents/architect.py` | Single-shot structured plan (JSON manifest, build/test cmds, deps, success criteria) | `PLANNING_MODEL` |
| **Builder** | `backend/openhands_worker.py` | OpenHands SDK on Codebox; profile-aware (scaffold/continue/feature/bugfix) | `CODER_MODEL` |
| **Reviewer** | `backend/agents/reviewer.py` | Read-only — runs build + test + lint, returns structured issues with file:line | `PLANNING_MODEL` |
| **Fixer** | `backend/agents/fixer.py` | Stateless scoped edits driven by a Reviewer envelope; marker-format output | `CODER_MODEL` |
| **ProjectQA** | `backend/agents/project_qa.py` | Read-only Q&A over an existing project; greps + reads + cites file:line | chat/persona model |
| **Indexer** | `backend/agents/project_indexer.py` | One-shot at upload time; walks tree, detects build system, indexes ChromaDB | `nomic-embed-text` |

Every agent invocation is a row in the `runs` table with a structured `result_envelope`, persisted to SQLite and exposed via `/api/runs/{id}`. Browser disconnects can't lose work; the UI rebuilds the timeline on reload.

#### Phase 1 — Reviewer

- **Stateless build/test/lint runner** that replaces the v1 antipattern of "orchestrator reads files and rewrites them across 28 chat rounds." Detects project markers, runs the real build commands inside Codebox, parses output, asks the planning model for a structured issue list with severity / file:line / `suggested_fix_scope`. Read-only by contract.
- **Project marker auto-detection** — checked in this order so monorepos hit the right one:

| Marker | Build system | Build cmd | Test cmd |
|---|---|---|---|
| `pom.xml` | maven | `mvn -q -DskipTests compile` | `mvn -q test` |
| `build.gradle(.kts)` | gradle | `./gradlew build -q -x test` | `./gradlew test -q` |
| `Cargo.toml` | cargo | `cargo build --quiet` | `cargo test --quiet` |
| `go.mod` | go | `go build ./...` | `go test ./...` |
| `package.json` | npm | `npm install && npm run build` | `npm test` |
| `pyproject.toml` / `requirements.txt` | pip | `pip install -e .` | `pytest -q` |
| `CMakeLists.txt` | cmake | `cmake -B build && cmake --build build` | `ctest --output-on-failure` |
| `Makefile` | make | `make` | `make test` |

- **Plain-source fallback** — when no formal build file exists, walks the tree and picks the dominant language (`*.java`, `*.py`, `*.rs`, `*.go`, `*.js`, `*.ts`, `*.c`, `*.cpp`). Java fallback excludes `*Test.java` / `./test` / `./tests` so a missing JUnit classpath doesn't fail the whole compile.
- **Hard fail on `(none)` marker** — if the Reviewer can't identify a project at the given path, returns `status="error"` instead of trivially "clean." A reviewer that silently passes a missing project is worse than no reviewer.
- **`run_review` tool** — the chat agent invokes it after each build/edit. Tool result includes the `reviewer_run_id` so the Fixer can pick up where it left off.

#### Phase 2 — Fixer

- **Stateless minimal-edit applier.** Loads a Reviewer envelope from the run store, reads each issue's fix-scope files, asks the coder LLM for targeted edits, writes via Codebox. Validates that the model only touches files in scope. Does NOT re-run the build — that's the Reviewer's job. Clean separation: Reviewer verifies, Fixer edits, neither does the other's job.
- **Marker-delimited output format** — replaced JSON with `### EDIT: <path>` + ` ```lang ` fenced full-file replacements. JSON-with-code routinely breaks because models don't escape newlines/quotes inside string values; markers don't need escaping. Robust across every language we tested.
- **Path normalization** — relative paths from reviewer output (`./tests/Foo.java`, `com/pong/Bar.java`) get resolved to absolute paths under `project_dir`. Defensive against models that copy paths verbatim from compiler error messages.
- **Validate-and-fallback shim** — if the model passes a fabricated `reviewer_run_id` that doesn't exist in the runs table, the dispatch falls back to the most recent reviewer for the conv instead of erroring out.
- **Hard 3-cycle cap, server-enforced** — `tools.py` exec_tool gate refuses a 4th `run_fixer` call if 3 succeeded fixer runs already exist on the conv. Forces the model to stop and ask the user instead of looping forever. Includes the latest reviewer's summary + first 3 issues in the gate message so the model has specifics to relay.

#### Phase 3 — Architect (structured planning)

- **Replaces v1's prose `plan_project`** with a single LLM call to `PLANNING_MODEL` that produces a JSON object the Builder/Reviewer/Overseer all consume directly:

```
project_id, language, build_system, build_cmd, test_cmd, lint_cmd,
manifest [{path, purpose, estimated_loc}],
tests_required, external_deps, risk_notes, success_criteria
```

- **Validate + retry once on parse failure** — if the model emits invalid JSON or misses required keys, the parse error is fed back into the prompt for one retry. Fail loudly on second miss; no regex hacks.
- **Manifest injection into Builder context** — when `generate_code` runs and a recent successful Architect envelope exists, the manifest + build/test commands + success criteria + deps + risk notes get prepended to the OpenHands task prompt. Builder follows the structured plan instead of re-deriving it from prose.
- **Rich plan visualization** — `format_plan_for_chat()` renders the JSON as styled markdown: file tree drawn with box-drawing characters in a code fence, deps as build-system-specific snippets (XML for Maven, `npm install` for npm, TOML for Cargo, `pip install` for Python, Groovy for Gradle), success criteria as a checkbox list, tests as a markdown table.
- **Architecture Plan panel restored** — Architect's `tool_end` event includes `detail.plan` with the rendered markdown, so the frontend's `PlanPanel` component renders the styled "Architecture Plan" panel above the agent run cards (📐 icon, copy button, expand/collapse).

#### Phase 4 — ProjectQA + Project Indexer

- **`ProjectQA` agent** for "how does X work?", "walk me through Y", "show me Z" questions. Steps: list_files → keyword extraction → grep across the tree → read snippets → ask LLM to compose grounded answer with `file:line` citations.
- **Filename-targeted reads** — extracts file mentions from the question (`ScoreTracker.java`, `Ball.rs`, even `score tracker.java` with a stopword-filtered space split → `scoretracker.java`), fuzzy-matches against the project tree, reads the full file (up to 30KB) so the model can do a line-by-line breakdown.
- **Change-request detection** — flags questions that look like change requests (`add X`, `fix Y`, `refactor Z`) so the chat agent can route to `generate_code` instead of more `ask_project` calls.
- **Path validation shim** — if the model fabricates a `project_dir` that doesn't exist on Codebox, the dispatch falls back to the auto-resolved path from the latest builder run. Same pattern as the `run_fixer` shim.
- **`ask_project` tool** with `❓ Investigating project` icon. Uses chat/persona model so the answer voice matches the assistant the user is actually talking to (was hardcoded to `DEFAULT_MODEL` before; now respects `conv_model`).
- **`Project Indexer` agent** — runs once at upload time. Walks the uploaded project tree, detects build system from markers, reads up to 100 source files (excludes node_modules, .git, target, build, dist, __pycache__, venv), pushes contents into ChromaDB's `code_memory` collection via the existing `rag.index_generated_code` pipeline. Future `ask_project` / `generate_code` calls have semantic retrieval over the uploaded code.
- **`/upload-chunk` endpoint added to codebox-api** — accepts raw bytes via multipart instead of base64-in-shell-command. Bypasses two pre-existing pitfalls of the chunked upload path: (1) Linux `MAX_ARG_STRLEN` (~128KB per argv string) when chunks exceeded ~130KB, and (2) the `mkfs` substring in codebox's deny-list which any random base64 chunk could trigger. Uploads now use 1MB raw chunks instead of 100KB base64 chunks — ~10× faster, no false positives.

#### Phase 5 — Builder profiles

- **Single `generate_code` tool, four profile prompts** based on conversation state:

| Profile | Trigger | Builder behavior |
|---|---|---|
| `scaffold` | No active project (fresh build) | "Create ALL files from scratch" + git init |
| `continue` | Last builder ran `partial`/`stuck` | "Resume — these files in `manifest_missing` aren't written yet, do NOT rewrite existing files" |
| `feature` | Last builder `succeeded`, user asks for change | "EXISTING WORKING project at `/root/projects/X`, here's the tree + relevant files, edit minimally, do NOT regenerate working code" |
| `bugfix` | Internal — used by run_fixer | n/a (Fixer doesn't go through OpenHands) |

- **Profile detection from runs table** (authoritative; bypasses stale `coding_projects` rows) — walks newest-first, finds most recent builder with status in `(succeeded, partial, stuck)`, pulls `project_dir` + `project_id` from its envelope. Skips cancelled/failed/running entries that would otherwise mask the real prior build.
- **Feature-profile context prep** — when the profile is `feature`, the dispatch packages the existing file tree (60 paths) + 1-3 inlined files mentioned in the user's task into the OpenHands `context` field. Builder reads what already exists out of its prompt and edits surgically instead of walking the tree itself.
- **Run cards distinguish profiles** — `Run · builder.scaffold` / `Run · builder.continue` / `Run · builder.feature` show in the UI with role-specific icons. Frontend `RunCard` renders the right label per profile.

#### Phase 6 — Frontend UX polish

- **Role-specific run-card icons + labels** — every agent type is visually distinct in the timeline:

| Role | Icon | Label |
|---|---|---|
| `architect` | 📐 | Designing plan |
| `builder.scaffold` | 🏗 | Building project |
| `builder.continue` | 🔄 | Resuming build |
| `builder.feature` | ✨ | Adding feature |
| `builder.bugfix` | 🩹 | Bug fix build |
| `reviewer` | 🔍 | Reviewing |
| `fixer` | 🛠 | Fixing issues |
| `qa` | ❓ | Investigating |
| `indexer` | 📚 | Indexing project |

- **Auto-expand cards that need attention** — `failed` runs and reviewers with `status=issues`/`error` default to expanded so the user sees the problem immediately. Other cards stay collapsed (default).
- **Step row icons** — OpenHands action names (`file_create`, `file_edit`, `terminal`, `glob`, `finish`, etc.), Reviewer phases (`detect`, `build`, `test`, `lint`, `read_files`, `analyze`), Architect phases (`planning`, `manifest`, `criterion`, `risk`), Fixer phases (`writing`, `calling`), Indexer phases, QA phases — all get small leading emoji so the timeline reads as a build log instead of a wall of words.
- **QA answer rendered inside the run card** — for `qa` role cards, `env.answer` renders as styled markdown (full code blocks + file:line citations) inside the expanded card body, with a "📂 examined: ..." footer listing inspected files and a "⚡ Flagged as change request" pill when the QA detected one.
- **Plan panel survives long runs** — live event stream is a 200-event sliding window, so on long multi-cycle builds the early `tool_end plan_project` event used to roll off and the Architecture Plan panel would disappear. Fixed by passing `savedEvts={msg.metadata?.saved_events}` to `ToolStatus`; the panel now reads from the merged pool, falling back to the persistent message metadata when the live window has aged out.
- **Brain → compass icon swap** — `🧠 Planning architecture` is now `📐 Architect designing plan` everywhere (live pill, run card, PlanPanel header). The `🧠` was carryover from the v1 prose `plan_project`; the `📐` ruler reads as architect/blueprint.

#### Workflow gate — deterministic over persuasion

A four-state gate in `tools.py:exec_tool` enforces the v2 workflow without trusting the model's prompt compliance. Fires only for v2 personas (detected via `model_config.name` containing `"v2"`); v1 personas pass through unchanged.

| State | Trigger | Allowed | Blocked |
|---|---|---|---|
| **PENDING REVIEW** | Last run is `succeeded`/`partial`/`stuck` builder OR `succeeded`/`failed` fixer with no reviewer after | `run_review` only | everything else (incl. read_file, write_file, generate_code) |
| **PENDING FIX** | Last reviewer has `status=issues` or `error` | `run_fixer` (or `run_review` to re-check) | everything else |
| **Q&A TERMINAL** | Last `qa` run succeeded, `looks_like_change_request=False` | `ask_project` (for follow-up questions) | everything else — model must respond with text |
| **CYCLE LIMIT** | ≥3 succeeded `fixer` runs on this conv | nothing for `run_fixer` | `run_fixer` itself; instructs model to summarize and ask user |

Each gate state's tool result tells the model exactly what to call next, with the relevant `run_id` / `project_dir` / fix-scope info embedded so it doesn't have to guess.

#### Other v2 hardening

- **QA short-circuit** — when `ask_project` succeeds with a non-change-request answer, the chat agent bypasses the next LLM round entirely and streams the QA envelope's `answer` field verbatim as the assistant message. Avoids the model paraphrasing the grounded answer and losing code blocks. The QA card and the chat-side message now show identical content.
- **Stop button cancels OpenHands** — frontend abort propagates through chat stream cancellation → `POST /cancel/{run_id}` to OpenHands worker → `_RunCancelled` raised in the event callback → `conversation.pause()` + `close()` to terminate the SDK + child bash session. Worker registers active runs in a thread-safe `_ACTIVE_RUNS` dict keyed by `run_id`. Idempotent — calling cancel on a finished run returns `{"status": "not_found"}` rather than 404.
- **SSE → /run fallback no longer resurrects dead runs** — when the SSE stream from OpenHands drops mid-run, the dispatch used to fall back to the non-streaming `/run` endpoint, which spawned a *second* concurrent run on the same task. Fixed: fallback only fires for connect-time failures (no first event received). Mid-run drops signal cancel to the worker instead.
- **Active project injection v2-aware** — `chat.py`'s `ACTIVE PROJECT` system message used to say "use read_file → write_file/generate_code" (v1 antipattern). For v2 personas it now says "questions → `ask_project`, change requests → `generate_code(project_id=...)`, bug reports → `run_review` first, do NOT auto-package."
- **Loop-tool exemption** — `run_review` / `run_fixer` / `ask_project` are designed to be called multiple times back-to-back with identical args. Chat-loop's near-duplicate detector now treats them as `_LOOP_TOOLS` and exempts them from both exact and near-duplicate checks. Workflow correctness is enforced by the gate, not the dedupe logic.
- **MAX_ROUNDS fallback message** — when the chat agent's round budget runs out without producing final user-facing text, a synthesized markdown summary is streamed: builder runs created, last reviewer status, any flagged issues with file:line refs, and a "ran out of budget" line. Replaces the previous blank-message ghost-out.
- **Model routing verified** — Architect uses `PLANNING_MODEL`, Builder/Fixer use `CODER_MODEL`, Reviewer uses `PLANNING_MODEL`, ProjectQA uses chat/persona model. Each falls back through `PLANNING_MODEL → DEFAULT_MODEL → CODER_MODEL` if its primary is empty.

#### Verification (10/10 passing)

| # | Test | Result |
|---|---|---|
| 1a | Java pong fresh build | ✅ mvn compile + test exit 0 |
| 1b | Python Flask API fresh build | ✅ 3.1m, 4/4 pytest pass, no fixer cycle |
| 1c | Rust CLI fresh build | ✅ cargo build + test exit 0, fixer-loop-once-then-clean |
| 2 | Disconnect resilience | ✅ RunCard rebuilds via `/api/runs/{id}` on reload |
| 3 | Hostile context growth | ✅ "the Java pong from earlier?" → correctly says "not in this code" |
| 4 | Failed-tests recovery | ✅ reviewer→fixer→reviewer cycles converge to clean |
| 5 | VRAM headroom | ✅ 41.94 GB / 48 GB during build (devstral + qwen3-coder both hot) |
| 6 | Token tracking | ✅ `token_usage` table populates per run |
| 7 | Project Q&A with citations | ✅ "walk me through Ball.java" with file:line refs |
| 8 | Cross-language port (Python → Go) | ✅ 5.4m, 4/4 Go tests pass on first try, no fixer cycle |
| 9 | Settings fallback (all empty) | ✅ persona's pinned model used everywhere |
| 10 | Settings specialization | ✅ Architect/Reviewer → planning, Builder/Fixer → coder, QA → chat |

### Coder Bot v2 — Independent Critic Pass
- **Independent critic on `generate_code` output** — When `CRITIC_ENABLED` is on, the post-build flow runs a second reviewer pass using a different model (`CRITIC_MODEL` falling back to `PLANNING_MODEL` / `DEFAULT_MODEL`) so it can't rationalize its own mistakes. Catches runtime bugs that pass syntax / compile checks but break behavior. Live "🔍 Reviewing code with…" pill while it runs.

### Quick Search Overhaul
- **New shared module `backend/quick_search.py`** — Both the chat-injection path (`agents/chat.py`) and the standalone `/api/quick-search` endpoint (`main.py`) now flow through one helper, so quality fixes apply to both. Replaced ~150 lines of duplicated SearXNG-call boilerplate.
- **Skip-gate: only skip when search is unambiguously useless** — Empty queries, pure greetings (`hi`, `thanks`, `ok`), pure arithmetic with no letters (`2+2`, `15 * 200 = ?`), and "rewrite/translate/summarize **this/that/the following/it**" (operate-on-attached-text only) are skipped. **Programming questions, math questions involving research, and queries with code blocks now all go through to search** — the previous gate's `fix|refactor|format` regex would have silently bailed on "fix this Rust borrow error" or "what's the largest prime under 10^18", forcing the model to hallucinate. The asymmetry is on purpose: a useless search costs ~2s, a wrong skip costs a wrong answer.
- **Conversation-aware query rewriting** — Before searching, the latest user message is rewritten by the `WORKSPACE_MODEL` (small/fast — `qwen3.5:4b` by default) using the last 2–3 turns. "what about for v18?" after a React 19 question becomes "React v18", not the literal pronoun-soup. Output is sanitized (strips `<think>` blocks, rejects suspicious prefixes / URLs), 8s timeout, falls back to the raw message on any failure.
- **Result filtering, ranking, and dedup** — YouTube / image results dropped from the *model* context (they pollute fact-check answers) but kept for the frontend carousel. Remaining web results re-ranked via `_rank_urls()` (Wikipedia / arxiv / .gov / .edu / Reuters / BBC bonuses, badpaths penalties), capped to 2 per registrable domain so SEO farms can't crowd out diverse sources, top 6 retained.
- **Selective page fetch** — For the top 3 ranked results, if the snippet is < 120 chars or looks like a navigation menu, the page is fetched in parallel via `_fetch_page` (4-second total budget across all three, 1500 chars per page) and the cleaned excerpt is injected instead of the snippet. Catches the common case where SearXNG returns just the page title and the actual answer lives on the page.
- **Date injection** — Search-context block now opens with `=== WEB SEARCH (today: YYYY-MM-DD) ===` so questions like "is X still CEO?" / "latest version of Y" / "did Z happen yet?" can answer with current grounding instead of "as of my training data."
- **Rewritten injection prompt — no more forced confidence** — The old prompt told the model `"Do NOT say you lack real-time data... summarize as if you know it"`, which actively encouraged confabulation when results didn't contain the answer. Replaced with `"If the results don't contain the answer, say so plainly — don't guess. Cite the URLs you actually used."`
- **Explicit `safesearch=0`** — Both call sites now pass `safesearch="0"` explicitly via a new optional param on `_search_searxng`, so behavior doesn't depend on the SearXNG instance default. Filtering stays at 0 by user direction.
- **10-minute TTL cache** — Module-level `_CACHE` keyed by rewritten query saves SearXNG round-trips and page fetches when the user asks several follow-ups about the same topic.
- **SearXNG fallback already wired** — The shared helper now uses `_search_searxng` from `research.py`, which falls back to a Google-results scrape when SearXNG is down or returns nothing. Quick search no longer goes blank if the SearXNG container hiccups.

### Inline Search Images
- **Hero-image embedding in chat responses** — When Quick Search runs and a result has a thumbnail / OG image, the helper offers the top 2–3 image URLs to the model with explicit permission to embed `![alt](url)` near the top of the answer "for a thing, place, person, product, or news event." Explicitly told to skip for code / math / conceptual / numeric answers. The frontend's existing `inlineParse` (`frontend/dist/index.html:2796`) already rendered `![]()` markdown as `<img>` with max-width 100% / max-height 380px / rounded corners / `onError` hide — no chat-renderer changes were needed.
- **New `/api/img-proxy?u=...` endpoint** — Every image URL the model embeds is wrapped in our backend proxy. The proxy fetches the remote image server-side with a domain-matched `Referer` (bypasses most hotlink blocks on news sites), caps at 5MB, returns with a 24-hour cache header. Three wins at once: (1) **privacy** — the user's IP, referrer, and cookies never reach NYT / Reuters / etc.; (2) **hotlink protection** — domain-matched Referer satisfies most blocks; (3) **mixed-content** — http images on https pages now load without browser warnings.
- **Privacy attrs on the inline image render** — `referrerPolicy="no-referrer"` (belt-and-suspenders for the rare case the model embeds a non-proxied URL) and `loading="lazy"` (off-screen images don't block render) added to the `<img>` element at `frontend/dist/index.html:2796`.
- **The Quick Search Results carousel above each response is unchanged** — that UI is for "here are sources / related links," and the new inline image is for "this is what the thing looks like." Two distinct jobs, both supported.

### Coding Agent Reliability
- **`num_ctx` is now authoritative for OpenHands** — Ollama caches model options on first load and silently ignores subsequent `num_ctx` requests, so even after the user changed their setting, OpenHands kept running at the modfile default. New `_ensure_loaded()` in `openhands_worker.py` checks `/api/ps` for the current loaded context, evicts (best-effort) if it doesn't match, and preloads the model with an empty prompt + the user's `num_ctx` so the cached options bind correctly. Logged as `[OH-Worker] {model} loaded at num_ctx=X, evicting to reload at Y`.
- **`generate_code` mandate, not suggestion** — `personas.py` rewritten: the file-count gate is no longer advisory wording. Plan with **3 or more source files**, OR the task is a complete app / API / CLI / web app / game / library, → the very next tool call MUST be `generate_code`. Repeated `write_file` calls when `generate_code` is available is now explicitly framed as a BUG. Bookkeeping language ("the user is paying for the autonomous agent — use it") removes ambiguity for models that hedged.
- **Stream-disconnect resilience** — The assistant-message stub is created at stream start (Phase 0.6 in `chat.py`) and snapshotted at every agent-round boundary, tagged with any `run_ids` created during the stream. If the client disconnects mid-stream, the most recent snapshot is what the user sees on reload — no lost work, no orphaned runs. The `message_id` is sent down to the frontend so completion `PATCH`es the existing row instead of `POST`ing a duplicate.
- **`research()` tool surface expanded** — Coder personas now reach for `research()` before guessing at unfamiliar APIs (`personas.py` rule 6 made explicit and reordered up the list).

### Front-End Polish
- **Pill rendering improvements** — Real-time status pills for tool execution show clearer state transitions, fewer flicker-then-vanish glitches, and a more consistent icon vocabulary across `generate_code` / `run_review` / `run_fixer` / quick search.
- **Bash code-block rendering fix** — Triple-backtick `bash` fences no longer break out of the code block on shell metacharacters (specifically `$`, backticks inside `$()`, and unescaped `&` in heredocs). Snippets with realistic shell pipelines now render cleanly instead of bleeding into surrounding prose.

### Bug Fixes
- Fixed Quick Search skip-gate over-aggressively bailing on programming and math questions — the original `fix|refactor|format` intent regex and the "code block present → skip" rule have been removed. Programming questions, math questions involving research, and queries with code blocks all go through to search now.
- Fixed missing `safesearch` param on `_search_searxng` — added an optional `safesearch` argument so callers can preserve explicit `safesearch=0` instead of relying on the SearXNG instance default.
- Fixed Reviewer / Fixer scope-validation edge case where issues with no `suggested_fix_scope` field caused the Fixer to error out instead of skipping the issue cleanly.
- Fixed Bash render error where `$VAR` and command-substitution `$(cmd)` inside a `bash` code fence prematurely terminated rendering.
- Fixed `num_ctx` from user settings being silently ignored by OpenHands runs after the first model load — now evict-and-reload guarantees the runtime context matches the requested value.

</details>

---

<details>
<summary>Alpha v16.2 — April 22, 2026</summary>

### Rich Rendering Polish
- **GitHub-style callouts** — `> [!NOTE]`, `> [!TIP]`, `> [!IMPORTANT]`, `> [!WARNING]`, `> [!CAUTION]` render as coloured admonition boxes (blue / green / warm / orange / red) with icon and label header. Subsequent `> ` lines are collected as body. Models pick these up from the updated backend RENDERING system message and use them for caveats, tips, and warnings without prompting.
- **Keyboard key caps** — `<kbd>Ctrl</kbd>+<kbd>K</kbd>` renders as raised key caps with gradient bg, thick bottom border, and monospace face. Makes shortcut-heavy how-to answers legible at a glance.
- **Inline color swatches** — Hex codes (`#rrggbb`, `#rrggbbaa`), `rgb(…)`, `rgba(…)`, `hsl(…)`, `hsla(…)` in chat text auto-render with a small clickable colour chip next to the code. No special syntax — plain mentions get upgraded. Great for design/CSS chats.
- **Inline charts** — New ` ```chart ` code fence parses JSON via Chart.js. Supported types: `bar`, `line`, `pie`, `doughnut`, `scatter`, `radar`, `polarArea`. Simple form: `{"type":"bar","labels":[...],"data":[...]}`; multi-series via `"datasets":[...]`. Dataset colors auto-pick from the active theme palette (`acc`/`ok`/`warm`/`err`/derived tints). Grid, ticks, title, and tooltip all theme-synced. Source toggle + copy button matching `MermaidBlock`. Pairs Chart.js 4.4.4 UMD (~80 KB) loaded from jsDelivr.
- **Table renderer rewrite** — Pipe tables now group contiguous rows into one real `<table>` with proper `<thead>` styling (uppercase, accent-colored, surface background) and `<tbody>` row separators. The divider row (`|:---:|---:|---|`) is parsed for `left` / `center` / `right` alignment per column and applied to both header and body cells. Cells now wrap (`wordBreak: break-word`) instead of the previous `whiteSpace: nowrap` + ellipsis that silently truncated real model output. Horizontal scroll container for tables wider than the bubble.
- **Shared `_consumed` set across line rendering** — Refactored the inner `seg.split("\n").map(...)` in `md()` to pull `_lines` and a `_consumed` Set out into segment scope. Callout and table handlers use this to mark multi-line blocks so subsequent lines skip rendering, cleanly replacing the old per-line attribution lookahead pattern.
- **`ChartBlock` component** — Mirrors `MermaidBlock`: canvas element inside a bordered container with header pill (`◈ chart`), source toggle, copy button, and red error banner plus raw JSON on parse/render failure. Chart instance destroyed + rebuilt on code or theme change (reuses `mermaidEpoch` as the theme-change signal). Circular chart types get per-slice palette colors; axis charts get translucent fills.
- **Backend RENDERING hint extended** — `chat.py` system message now documents callouts, `<kbd>`, colors, and chart fences alongside the existing mermaid/math guidance so models reach for them unprompted.
- **No-more-matplotlib steer** — RENDERING hint's "do not generate with tools" rule now explicitly forbids using `execute_code` / `generate_code` / `write_file` + matplotlib / seaborn / plotly / `pandas.plot` to save a chart image when a ```chart fence would render the same data. Code tools are reserved for *computing* values; visualisation of values already in hand goes through the fence.
- **Deep Researcher persona — Presenting Findings section** — The seed preset (`mc-preset-deepresearch`) gained a dedicated "Presenting Findings — Use Rich Rendering" section that routes quantitative data to ```chart fences, source-conflict caveats to `[!NOTE]`, material-conclusion changes to `[!IMPORTANT]`, deprecations / security issues to `[!WARNING]` / `[!CAUTION]`, actionable advice to `[!TIP]`, multi-attribute comparisons to aligned tables, and keys/commands to `<kbd>`/inline code. Explicit "chart fence > Python matplotlib" rule for research data.
- **Preset seeder now upserts** — `seed_deep_researcher` previously only inserted on first boot, so existing installs never saw prompt updates. It now UPDATEs the `system_prompt` + `tool_ids` + `updated_at` columns when the preset row already exists, preserving the user's model choice / KB assignments / parameters. Version-bump prompt and tool-list updates land automatically on next server start.
- **RENDERING hint restructured — compute-then-chart pattern** — The earlier "do NOT call execute_code to produce a chart" clause was over-broad and risked suppressing `execute_code` for legitimate math. The hint is now split into three explicit sub-sections: **Visualisation** (fence-only, prohibits `write_file`/matplotlib `savefig` but never names `execute_code` in a prohibition), **Computation** (explicitly endorses `execute_code` for arithmetic / aggregation / CAGR / variance / weighted-average work — "the prohibition above is purely about saving image files, not about running code"), and a dedicated **compute-then-chart pattern** section showing the two-step flow: run `execute_code` to compute, print JSON to stdout, then emit a ```chart fence with the computed values. LLM mental math is unreliable past trivial cases; the sandbox is trusted.
- **Deep Researcher gains `execute_code`** — `tool_ids` updated from `["deep_research"]` to `["deep_research", "execute_code"]` on both fresh seeds and existing-install upserts. The persona's prompt added a new `## Computation` section mandating `execute_code` for CAGR/CMGR, weighted averages, variance, percentage shares, date/unit conversion, and any aggregation on research results — with an explicit "a chart with wrong numbers is worse than no chart" rule. Existing `## Skip the Tool When` section replaced with a clearer `## Tool Selection — Which One, When` table routing pure math to `execute_code`, substantive questions to `deep_research`, and trivial lookups to neither.
- **Research-loop guardrails** — Observed in practice: on a test prompt about 2025 EV sales, the model chained 6 `deep_research` calls across 20+ minutes looking for "exact full-year numbers" that weren't yet audited/published, never reaching the `execute_code` + chart step. `DEEP_RESEARCHER_PROMPT` gained an `## Avoiding Research Loops — CRITICAL` section enforcing (1) a hard **2-call same-topic cap** — once two calls on the same underlying topic both return approximate/similar data, synthesis happens, no third call — and (2) a **recency realism** rule treating "projected" / "estimated" / "preliminary" / "YTD" figures as the answer rather than an invitation to re-search. Uncertainty flows into `> [!NOTE]` / `> [!CAUTION]` callouts rather than another tool round. Prevents runaway context growth and minute-per-round wall-clock costs on questions whose "exact" answer doesn't publicly exist yet.

### Effort Level — Iterative Self-Review
- **Scalable response quality** — After the initial answer is finished, the model re-examines its own response and produces a refined version. Runs 0–3 additional review passes. Each pass can call tools (`research`, `fetch_url`, etc.) to verify claims before refining.
- **4 creative levels** — 💭 **Blurt** (raw, no review) / 🧠 **Ponder** (1 pass) / 🔥 **Forge** (2 passes) / 🌌 **Galaxy Brain** (3 passes).
- **Global default + per-chat override** — Settings has a "Default Effort Level" chip row. Each chat gets a compact emoji chip next to the input that lets the user override for just that conversation (new chats inherit the global default).
- **Replace-with-final UX** — During refinement, the streamed answer is wiped and re-streamed each round; only the polished final version stays in the bubble. Live pill shows "✨ Refining answer (1/3)..." during each pass. Finished messages carry an `✨ Refined N×` badge next to the timestamp.
- **Mechanism** — New `effort_rounds` field on `ChatRequest`. `chat_stream_generate` re-enters the main agent loop after the "no more tool calls" exit, appending a critique prompt that asks the model to check for factual errors, logical gaps, missing context, or unclear phrasing. The existing `MAX_AGENT_ROUNDS` cap still governs total rounds so review can't runaway.
- **New SSE event** — `refinement_start` `{round, total}` signals each review pass to the frontend. The `done` payload now carries `refinements: N` which is persisted to the message metadata so the badge survives reload.
- **Storage** — `localStorage["hc-effort-level"]` for the global default; `localStorage["hc-effort-per-chat"] = {convId: level}` for per-chat overrides. No DB migration needed.

</details>

---

<details>
<summary>Alpha v16.1.1 — April 22, 2026</summary>

### Rich Rendering
- **Mermaid.js diagrams** — ` ```mermaid ` code fences render inline as live SVG: flowcharts, sequence, class, state, ER, gantt, mindmap, pie. Theme-synced (34 mapped variables) and re-render when the user switches themes mid-conversation.
- **KaTeX math** — Inline `$...$`, display `$$...$$`, and LaTeX `\(...\)` / `\[...\]` delimiters all render as typeset math. Code blocks are ignored so `$` in source stays literal.
- **`<MermaidBlock>` component** — Header with `◈ mermaid` label, source toggle, and copy button matching existing code-block styling. Broken diagrams show a red error banner plus the raw source instead of breaking the message.
- **`<MDWrap>` wrapper** — Wraps render surfaces including chat, council cards, HF README, and the changelog view, then invokes KaTeX auto-render after mount. Streaming messages skip wrapping so partial tokens don't flicker.
- **Multi-line display math** — `md()` pre-splits non-code segments by `$$...$$` before line-splitting so equations spanning multiple lines render as one KaTeX node instead of fragmenting across `<div>`s.
- **Backend rendering hint** — `chat.py` injects a system message telling the model diagrams/math render inline (not via `write_file` or `generate_code`) and explicitly warns against embedding `$...$` LaTeX inside Mermaid node labels.
- **Display-math escape inside inline code** — `md()` now masks `$$` and `$` inside single-line backtick inline code before the display-math split, so documentation examples that quote a math delimiter render as inline code instead of being yanked out as a math block.
- **GFM task lists** — `- [ ]` / `- [x]` render as real checkboxes with strikethrough on completed items (read-only; reflects the markdown state).
- **Collapsible `<details>`/`<summary>`** — Raw HTML `<details>` blocks render as interactive collapsibles with a chevron. New top-level `Collapsible` component.
- **Diff code blocks** — fences tagged `diff` color `+` / `-` / hunk / metadata lines using the active theme's `ok`/`err`/`acc`/`mut` channels.
- **Syntax highlighting** — Prism.js autoloader loaded via CDN; every code fence gets `language-X` highlighting via a new `CodeBlock` component that runs `Prism.highlightElement` after mount. Copy button and language chip preserved.
- **Footnotes** — `[^label]` / `[^label]: …` pairs render as superscript numeric links with smooth-scroll to an auto-appended footnote block. Unique per-render IDs prevent collisions across messages.

### Message Actions
- **Timestamps fixed** — `created_at` now set at every in-session construction site (send, regenerate, edit, council) and preserved across streaming updates by spreading the prior message object. Fixes HH:MM labels that never appeared until a page reload. Reload path also preserves `id` + `created_at` from the backend.
- **Regenerate with…** — The plain regenerate button is now a split button; the ▾ chevron opens a popover to pick a one-shot model / temperature / persona override for this single retry. `sendMessages` and `regenerate` accept an `overrides` parameter.
- **Delete message with undo** — New trash button per message; removes immediately, shows a 5-second undo snackbar via a new fixed-position `ToastHost`. Backend: `DELETE /api/conversations/{conv_id}/messages/{msg_id}` + `db.delete_message()`. FTS trigger auto-syncs the search index.
- **Collapsible python output** — Each code-output block header is click-to-toggle; ▾ rotates to show collapsed state. In-memory per `{msgIndex, outputIndex}`; default expanded.

### Themes
- **Contrast rebalance** — Seven harshest themes rebalanced to meet readable minimums (text ≥7:1, dim ≥4.5:1, mut ≥3:1) without losing identity: Terminal (tamed neon body text, kept Matrix green accent), Cyberpunk (softened pure-magenta), Solarized Dark (lifted the famously-dim gray body text), Gruvbox (raised the dark-teal `f4`), Dracula (`mut` readable), Rosé Pine (fixed inverted dim/text hierarchy), Midnight (`mut` lifted). Nord, Catppuccin, Tokyo Night, One Light, Ayu Dark, Material Ocean, Solarized Light untouched.

### Persistence & Streaming Robustness
- **User message save moved server-side** — Frontend no longer POSTs the user message separately. `chat_stream_generate` defensively persists the latest user message at stream start if the DB's most-recent user row doesn't match. Eliminates the fire-and-forget race that silently dropped user messages on flaky networks (and the duplicate it caused when both paths fired).
- **Stable message order** — `get_conversation` now orders by `created_at ASC, id ASC`, so same-second user/assistant pairs (typical on fast greetings) can't flip on reload.
- **Stream-clobber fix** — New `streamingCidRef` tracks the conversation being streamed; `loadConversation` skips the messages-array overwrite when loading the streaming conv. Previously, switching away mid-stream and back could fetch a backend snapshot with only the user message; the in-progress `m[m.length-1]` stream update then overwrote the user message with assistant content, making it disappear.
- **Pills race fix** — New `streamSaveEvtsRef` accumulates events independently of the UI `evts` state, so `setEvts([])` on chat switch no longer wipes the metadata buffer that `saved_events` reads from at stream finalization. Pills persist on the completed message.

### Cleanup
- **Sentinel cleanup** — The `$$` backtick-masking in `md()` previously used null-byte sentinels, which made grep treat the frontend file as binary. Swapped for Unicode PUA characters (U+E000 / U+E001). Footnote sentinels use U+E010.

### Bug Fixes
- Fixed operator-precedence bug in `tools.py` execute_code error hinting — `"no such file" in err or "not found" in err and "command" not in err` was bound as `or (... and ...)`, silently skipping the `command` guard on the "no such file" branch. Parens now force the intended grouping.
- Fixed dead-code `or` fallback in `run_shell` result text — `f"exit code: {exit_code}\n{out}{err}" or "(no output)"` is always truthy because the f-string contains literal text, so the no-output fallback never fired. Replaced with an explicit `if (stdout or stderr)` branch.
- Fixed `analyze_workspace` crashing on malformed LLM JSON — the topic parser sliced `raw[start:end+1]` without checking `end > start`. If the response had `[` but no `]`, the empty slice raised inside `json.loads`. Added the `end > start` guard so it falls back to `[]` cleanly.
- Fixed invalid CORS configuration — `allow_origins=["*"]` with `allow_credentials=True` is rejected by browsers per the CORS spec. Switched `allow_credentials` to `False` so preflight requests succeed.
- Fixed XSS in full-text conversation search — SQLite's `snippet()` wraps matches in `<mark>` tags but does NOT HTML-escape surrounding message content, and the frontend rendered it via `dangerouslySetInnerHTML`. A malicious message could inject script/iframe tags that executed when searched. Snippet is now fully HTML-escaped with only `<mark>`/`</mark>` re-enabled.
- Fixed `pull_model` silently returning empty on upstream errors — the streaming generator never checked `response.status_code` before iterating, so non-200 responses from Ollama produced no SSE events. Now yields a clear error event and bails out.
- Fixed unbounded growth of `_indexing_status` dict — every KB file upload left a permanent entry. Terminal `done`/`error` statuses are now evicted on read.
- Fixed deprecated `asyncio.get_event_loop()` calls in `agents/chat.py` — replaced with `asyncio.get_running_loop()` to silence deprecation warnings in Python 3.10+ and avoid the "no running loop" edge case on future versions.
- Reduced chat-loop allocations — the per-round `_PARALLEL_SAFE` set and 22-entry `_TOOL_ICONS` dict are now module-level constants instead of being rebuilt every tool-calling round.
- Minor: avatar upload no longer evaluates `file.filename or ""` three times in one expression.

</details>

---

<details>
<summary>Alpha v16.1 — April 2026</summary>

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
- Fixed workspace file preview panel not showing when Workspace tab is active — preview panel moved outside panel ternary so it renders alongside any active panel

</details>

---

<details>
<summary>Alpha v16 — March 2026</summary>

### New Features
- **Legacy internal automation runner** — Added in this release and removed in Alpha v17.0.2 in favor of external n8n automation.
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
- Added a legacy internal automation module and scheduler, later removed in Alpha v17.0.2.
- New DB tables: `token_usage`; legacy internal automation tables were later removed in Alpha v17.0.2.
- FTS5 virtual table `messages_fts` with INSERT/DELETE/UPDATE triggers
- New columns: `forked_from`, `fork_point_msg_id`, `pinned` on conversations
- 17 new API endpoints; 3 new nav rail icons

</details>

---

<details>
<summary>Alpha v15.1 — March 2026</summary>

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

</details>

---

<details>
<summary>Alpha v15 — March 2026</summary>

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

</details>

---

<details>
<summary>Alpha v14 — March 2026</summary>

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

</details>

---

<details>
<summary>Alpha v13 — March 2026</summary>

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

</details>

---

<details>
<summary>Alpha v12 — March 2026</summary>

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

</details>

---

<details>
<summary>Alpha v11 — March 2026</summary>

### New Features
- **OpenHands Integration** — `generate_code` tool runs a full agentic coding loop (plan -> write -> test -> fix -> iterate) inside CodeBox sandbox
- **OpenHands Worker** — Dedicated FastAPI microservice on port 8586
- **Coder Model Selector** — Pick which model handles code generation

### Improvements
- Code-block rescue hardening (min 30 chars + keyword check)
- Repetition detector skips whitespace-only patterns
- Coder Bot English-only rule

</details>

---

<details>
<summary>Alpha v10 — March 2026</summary>

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

</details>

---

<details>
<summary>Alpha v9 — March 2026</summary>

- **Prompt Library** — Save and quick-insert reusable prompts
- **Conversation Tags** — Custom labels with sidebar filtering
- **Per-Model Parameters** — temperature, num_ctx, top_p, top_k, repeat_penalty per model
- **Ollama Server URL** — Change from Settings at runtime
- **Changelog Viewer** — Access from Settings
- Live token counter during generation

</details>

---

<details>
<summary>Alpha v8 — March 2026</summary>

- **Based Bot** — Edgy/uncensored Grok-inspired persona
- **Persona avatars in chat** — Messages show avatar and styled name pill
- **UI Font Size slider** (10-16px)
- Conspiracy research: always runs second wave across gov sources

</details>

---

<details>
<summary>Alpha v7 — March 2026</summary>

- **Conspiracy Bot** — `conspiracy_research` tool searching FOIA vaults, whistleblower sites, CIA/FBI archives
- **6 new themes** — Terminal, Cyberpunk, Solarized Dark/Light, Material Ocean, Ayu Dark (14 total)
- **3 new fonts** — Cascadia Code, Space Mono, Geist Mono (9 total)
- Model list grouped by family with emoji icons and size tags
- Streaming: removed artificial delay, 8-char chunks

</details>

---

<details>
<summary>Alpha v6 — March 2026</summary>

- **AI Peer Voting** — Council members vote for the best answer; host includes vote summary
- **Improved markdown** — Lists, blockquotes, HR, italic, headings

</details>

---

<details>
<summary>Alpha v5 — March 2026</summary>

- **3 new themes** — Dracula, One Light, Midnight
- **Animated tool pills** — spin, swing, bounce per tool type
- **Workspace system** — Group chats, file tracking, topic analysis, create personas from KB
- Font size slider, chat width slider, workspace model selector

</details>

---

<details>
<summary>Alpha v4 — March 2026</summary>

- **KB injection** — Files injected into system prompt when persona is active
- **Model parameters** — temperature, num_ctx, top_p, top_k applied to Ollama
- **Export conversation** as Markdown

</details>

---

<details>
<summary>Alpha v3 — February 2026</summary>

- **Council of AI** — Multi-model parallel debates
- **Deep Research** — Multi-source parallel research with AI synthesis
- **Custom tools** — Upload Python tools the AI can call

</details>

---

<details>
<summary>Alpha v2 — February 2026</summary>

- **Knowledge Bases** — Upload and attach documents to personas
- **Personas** — Custom AI configs with system prompts, models, avatars
- **SSE Event Bus** — Real-time tool status events

</details>

---

<details>
<summary>Alpha v1 — January 2026</summary>

- Initial release: FastAPI + single-file React SPA
- Ollama streaming chat with tool calling
- CodeAgent with sandboxed code execution (Codebox)
- SearXNG web search integration
</details>
