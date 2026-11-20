<details open>
<summary>Alpha v18.3.1 — Nov 19, 2026</summary>

> Chat and workspace polish, clearer model controls, refreshed themes and backgrounds, and security and reliability fixes.

## Bug fixes

- **Welcome messages** — cached per user/time of day, serialized and time-limited; cancelled requests stop generation instead of queuing unlimited calls.
- **Excel** — accepts blank (`[]`) and uneven rows, including headings, spacers and totals; invalid-row errors report actual and allowed lengths.
- **Delivered files** — removed duplicate document cards and cards created from a reply's own download links.
- **Web search** — excludes message times, skill hints and kept tool results from queries and Sources; restores recognition of “thanks”, “continue” and `/online`.
- **Deep Research** — clearer over-target source counts, accurate card counts and summaries, warnings specific to search failures or truncated writing, and an unclipped workspace action.
- **KB page titles** — decode HTML entities in imported filenames.
- **Workspace document stores** — hidden from Knowledge Bases and all KB pickers.
- **Library Search** — one result per chat, showing its best match and additional-match count.
- **Tables** — keep short dates/times together; Assistant briefs use HH:MM.
- **Tip callouts** — display an icon instead of “tip”.
- **HyprFit** — defaults to the curated catalog; remembers the Hugging Face trending switch.
- **Daedalus** — GPU-memory load failures explain that Continue can retry after another model unloads.
- **Calendar** — recurring-event deletion distinguishes one occurrence from the series, including synced calendars; Outlook invitations and unreadable events no longer break the calendar or others' reminders.
- **Scheduled tasks** — run with tabs open; Assistant saves preserve due briefs; weekly/monthly schedules avoid duplicate DST-fallback runs; events/webhooks arriving during execution get another run; obsolete webhook addresses stop working.
- **Reminders** — re-arm when note due times or remote calendar times change, not when only a note's title changes.
- **Email** — new accounts don't trigger automations or urgent alerts for old mail; dates use local time and sort correctly; Trash requires both account deletion permission and the master email-action switch.
- **Knowledge bases** — same-name uploads replace existing files; deletes require confirmation and report errors; failed creation leaves no phantom KB. Workspace deletion removes its uploaded documents.
- **Research** — interrupted reports stop appearing active; completed reports count as workspace-ready; opening reports preserves order; research and councils respect KB ownership.
- **Hugging Face downloads** — restored exact-file pulls and prevented “ready” when model-name creation fails.
- **Canvas** — AI edits locate the current target text or refuse changed text instead of overwriting stale positions.
- **Chat** — Delete all stops replies/councils and clears history recall; council-chat deletion stops its council; councils run singly; project uploads block sending; history/memory retain complete tool-round replies; unsaved chats hide Continue and Pin.
- **Themes** — readable diff lines, warnings and model badges across themes; removed duplicate diff signs and fixed nested lists.
- **Backgrounds** — reduced motion shows a still image; minor layout changes don't restart effects; display changes retain sharpness.
- **Page previews** — navigation links inside a previewed page stay in the page instead of loading HyprChat in the preview; links to other pages are stopped with a notice, and a web address is offered as “Open in new tab”. A page that navigates away by script is put back once, then stopped.
- **Preview pane resize** — the edge follows the mouse while dragging, including over the previewed page (it used to move only on release); wide screens allow more than 900px, and the handle works with the arrow keys.
- **Other fixes** — concurrent settings saves, brief timezones, non-Latin draft-size counts, atomic skill renames and non-blocking large-bundle creation.

## Interface refresh

- **Fonts** — Figtree is the default for interface/chat text unless customized; code stays monospace. Appearance → Match code font restores the previous style.
- **Chat list** — tighter rows with relative activity times, hover actions, combined Search/New chat controls, and a bottom profile/service-status row opening the monitor.
- **Chat header** — one row with title and a menu for system prompt, memory, export/import; phones also place workspace and theme controls there.
- **Ghost mode** — always accessible in the header, with pink active styling and enter/leave animation.
- **Memory** — its own header button beside Ghost instead of an item under Chat options; accent-coloured and named while on, with a short animation when switched (sparks drop into the brain; turning it off, it shakes them away). Unavailable in Ghost mode.
- **Theme switch** — animated sunrise/moonrise transition.
- **Reading width** — 800px by default, preserving custom choices; code, tables, charts and diagrams can extend 96px per side.
- **Composer** — text above attach, Tools, effort and Send controls.
- **Sources** — compact count/site row with Show all; opening it reveals the query.
- **Settings** — Model & Generation and Appearance use labeled rows with explanations and switches.
- **Theme gallery** — Dark/Light preview cards apply immediately, with Undo.
- **Background gallery** — Calm/Technical/Space groups and a Subtle–Strong slider; Normal strengthens Dot Grid, while Subtle retains its old intensity.
- **Northern Lights** — green, teal and violet animated curtains retain natural colors across themes.
- **Christmas backgrounds** — Snowfall (layered flakes/snowbank), Christmas Lights (twinkling/chasing bulbs), Silent Night (pines/star/snow) and Holiday Glow (red/gold/green discs), grouped under Christmas.
- **Pixel Rain** — three-depth columns with glowing heads, fading trails and splashes.
- **Dot Grid** — moving light bands, pointer highlights and occasional ripples; static under reduced motion.
- **Other effects** — falling Blackbox Terminal glyphs, Synapse Rain trails, Cosmic Microwave drift/twinkle and Quantum Foam impact flashes.
- **Effect repairs** — Blueprint Ghosts now animates; Monastery Candlelight uses steady glow/embers; Quantum Foam timing no longer depends on refresh rate.
- **Service Monitor** — dedicated panel from Connections or sidebar status, side-by-side services, Back/Close controls, fresh data on open and explicit history-load errors.
- **Shared headers** — consistent title bars with at most one primary action across Research, Council, Agents, Images, Artifacts, Memory, KB, Tools, Models and Jobs.
- **Tools** — Your tools, Connectors and Built-in tabs; connector forms open through Add connector.

## Workspaces in the chat list

- **Workspace groups** — remembered expansion, five recent chats, Show more and New chat controls; placed between Pinned and date groups, without duplicate workspace chats. Pins remain in Pinned.
- **Move chats** — check/uncheck workspaces from each chat's menu.
- **Older chats** — pagination removes the previous 50-chat limit.
- **Live updates** — chat/workspace creation, removal, renaming and deletion update immediately; new messages move chats to the top of their group.
- **Activity order** — pinning/renaming no longer changes recency; forks retain workspace membership.
- **Naming** — the nav rail now says Workspaces instead of Projects.

## Models page

- **Tabs** — Installed, Recommended for this server and Browse Hugging Face replace Ollama/Hugging Face/HyprFit labels.
- **Unified entries** — installed Hugging Face models appear once, with their source and chosen name.
- **Capabilities** — tool use, thinking and vision come from Ollama's model metadata; the model picker shares the same results.
- **Plain labels** — parameter/disk sizes, quantization explanations, actual context limits and a glossary; Response style and Advanced contain less common controls.
- **Recommendations** — readable memory/speed/context estimates, usage bars and direct Install; hardware-estimate inputs move to Advanced with explanations.
- **Hugging Face** — explicit search errors, resilient file lists, visible compatibility notes, immediate compatibility-filter searches and working download retries.
- **Tool-calling fix** — one confirmed rewrite action, offered only when Ollama reports no tool support.
- **Defaults** — creativity/word-choice settings sync with profiles; deleting a model also removes its overrides.
- **Fonts** — added Figtree, Hanken Grotesk, Instrument Sans and DM Sans.

## Security and profiles

- **Cross-site protection** — reject state-changing requests from other websites, including passwordless Main sessions; destructive user deletion/Fresh install phrases are checked server-side.
- **Image safety** — proxied images and profile pictures cannot execute script as the user when opened directly.
- **Connector permissions** — server commands, server secrets and private-address access require Main; other profiles retain ordinary web connectors with their own keys.
- **Profile deletion** — removes tasks, mail/calendar credentials, API keys, notes, notifications, calendars and files; Fresh install clears the same remnants.
- **Profile switching** — flushes pending preferences, clears unsaved API keys and notification/statistics state; creating a profile in Settings keeps Main signed in.
- **Notification tokens** — no longer exposed to the browser; removal supported.

## More fixes

- **Image Studio** — forgotten ComfyUI jobs fail explicitly; bulk deletion reports individual failures.
- **Settings** — context edits save on blur and allow clearing; embedding-model changes explain Reindex All; admin-only controls and restore-disabled features are labeled.
- **Statistics** — Today no longer includes yesterday.
- **Councils, agents and connectors** — confirm deletions, report failed saves/deletes, save keyboard-adjusted debate rounds and retain pictures after failed uploads.
- **Memory** — reject empty entries and report failed saves accurately.
- **Voice** — Stop remains available during generation; opening Settings preserves dictated text.
- **Shortcuts** — inactive behind dialogs or sign-in.

## Polish follow-up — Nov 19

- **Statistics** — validate responses, show errors/Retry, retain prior data on failure and reject stale requests after query/profile changes.
- **Backgrounds** — respond immediately to reduced-motion changes; hidden tabs and unmount stop animation work.
- **Profiles and controls** — agent/persona cards, editors and model pickers fit narrow/large-text layouts; segmented filters scroll without shrinking and support Arrow/Home/End keys.
- **Artifact details** — mobile dialogs contain focus, make covered content inert, restore focus on close and switch correctly to desktop panes.
- **Coverage** — updated Daedalus, qualification and custom-tool tests; included background-effect tests in the default suite and added Analytics, lifecycle and browser regressions. Immediate-startup WebKit reload diagnostics remain documented; physical-device and true 200% zoom checks remain unverified.

## Housekeeping

- Removed the retired sandbox cleanup route, original Coder Bot placeholder, three orphaned evaluation scripts and a renamed script.

</details>

<details>
<summary>Alpha v18.3 — Nov 18, 2026</summary>

> Learned skills, steadier chat/tool rendering, context controls and live document previews.

## Skills

- **Learning** — multi-step tasks, fixes and corrections can become reusable procedures. A background reviewer selects skills; chats see a short index and load matches. Corrections update skills, with version comparison/restore.
- **Approval** — skills from scheduled runs, email/connector chats or safety-flagged work need review; Always ask me first applies to all skills.
- **Skills panel** — create, edit, pin, archive/delete, review diffs, attach sandbox scripts and import/export `SKILL.md`. Unused learned skills are flagged, then archived, never auto-deleted.
- **Scope** — ordinary chat, Master Developer, Assistant and scheduled tasks; excludes Daedalus, Ghost and roleplay.

## Chat

- **Scrolling** — follows replies only at the bottom; scrolling up releases follow mode, without jumps or composer overlap.
- **Tool output** — inline collapsed output/files, auto-expanded failures and grouped consecutive runs with summaries, action labels, the newest five and all failures. Continue retains earlier output; Activity counts calls correctly.
- **Tool memory** — trimmed results support follow-ups/Continue. Excludes email, connectors, history recall, Ghost, coding jobs and scheduled runs. Disable through Model & Generation → Keep Tool Results.
- **Tool rounds** — default raised from 12 to 40, configurable per reply; the limit still allows a final answer/Continue, with one recovery prompt for models silent after tools. Bare “continue” no longer searches the web.
- **Queued messages** — Enter queues during generation; edit/remove before sending. Stop, errors, limits, coding jobs or completion in another chat pause the queue with Send now. Reload restores queued text as drafts.
- **Per-chat drafts** — text and attachments stay with their chat; text survives reload except in Ghost mode.
- **Message actions** — Rename/Generate title, Retry when no answer was written, and Copy for user messages.
- **Previews** — edge-to-edge HTML with one header; streamed documents with Read/Source, SVG/CSV previews and intact nested code fences. Delivered files, including Office documents, open automatically; Save keeps previews in that chat's Artifact Studio.
- **Sidebar states** — replying, unread, draft and queue indicators; confirmed deletion stops replies; chats, tags and search results support Tab/Enter.
- **Stable rendering** — message actions stay visible while other chats reply, completed replies don't shift, and activity/footer wording is clearer.

## Chat fixes

- **Recovery** — duplicate user text stays distinct. Stop, disconnects, restarts and output limits retain periodically saved partial replies with status/Continue; stopping also cancels tools.
- **Conversation ownership** — streams, Stop/composer actions, tool cards, edits, menus and loading files stay with their chat. Leaving Ghost stops its reply; mid-reply attachments and renames survive; deleting the open chat selects the next correctly.
- **Regenerate/Edit/Delete** — confirm removal of later messages, exclude greetings/job starters and block duplicate dispatch. Correct saved-message matching, durable deletion, non-duplicating Undo, retained edited PDFs and explicit save errors.
- **Tool parsing** — example code is not executed; text-format calls preserve prior reply text.
- **Errors** — shown once with the server's message, without raw JSON or stale replies.
- **Models** — supports strict single-system-message templates, including two Qwen3.8 builds; replies identify their actual model.
- **Daedalus** — preserves ordinary answers, explains failed job starts, shows one card per job, resets project/version choices after sending and accepts ZIP projects in new chats. Clearer Answering/deleted-job states, persistent question replies and no false historical Planning cards.
- **Other fixes** — PDF reloads, research composer unlock, IME-safe Enter, title retries, latest-query search, ordered saves, close-tab preference flush, unique sources, Escape dismissal, non-text/empty-edit rejection, stale search-card removal and cross-chat Continue refusal.

## Context

- **Accurate meter** — model-reported counts and actual runtime window, stable after completion/Stop/errors and immediate model/context updates. Includes live tool usage, tokens/sec, compaction marker and lighter thinking/tool segments.
- **Breakdown** — instructions, tools, memory, skills, summary, messages, excerpts, current tool calls, free space and compaction point; no double-counted excerpts. Warns below HyprChat's roughly 3,500-token overhead.
- **Compaction** — chat-model summaries preserve older content and recent turns verbatim; visible summary boundary, long-tool-result reduction, guaranteed summary replay and no browser-side trimming or negligible-benefit compaction.
- **Controls** — thresholds of 85/90/95/98/99% (default 85), always reserving a reply; coding jobs keep separate policy. Added Compact now, Read the summary and Forget kept tool results.
- **Model limits** — plan/compact against the model's real window when smaller than Settings; Model Manager explains the limit.
- **Oversized messages** — draft warnings and explicit needed-size notices when Ollama truncates PDFs/pastes.
- **Loaded models** — Ollama default or 1–8; unload least-recently-used chat models, excluding helpers. Can lower, not raise, Ollama's limit. Shows loaded sizes, contexts, expiry and partial GPU offload.
- **Memory caching** — stable memory ordering avoids rereading context; measured 9,000-token chat overhead fell from 1.1–1.4s to 0.12s.
- **Budget/time notes** — warn the model near context exhaustion or with ≤3 rounds left; messages include timestamps/elapsed time. Message Times disables timestamps; Ghost/coding chats omit them.
- **Cloud models** — local context settings no longer judge, trim or summarize Claude/GPT chats.
- **Other fixes** — no Council context pill, current summary markers and no answer request without its question.

</details>

<details>
<summary>Alpha v18.2.1 — Nov 12, 2026</summary>

> Clearer coding evidence, safer follow-ups, persistent applications and live previews.

## Daedalus

- **Build results** — distinguish passed checks, review needs and unverified requirements.
- **Acceptance** — accept a specific revision while keeping later edits separate; accepted downloads remain available.
- **Follow-ups** — edit the latest build before acceptance or start from the accepted version.
- **Verification** — protect passing tests, verify requested test additions and prevent empty recovered projects from being offered for acceptance.
- **Settings** — one Hephaestus coding engine handles planning/building/repair; separate chat/coding models, with historical jobs still readable.
- **Reliability** — improved Java packaging, coding performance, Stop/Continue recovery and job-status clarity.

## Hephaestus & Application Previews

- **Hephaestus replaces Codebox** — isolated Docker execution for code, agents, documents and application previews.
- **Applications** — keep supported previews running, stop/restart and inspect logs; saved data survives restarts, temporary previews expire after 15 minutes.
- **Service labels** — Hephaestus naming in Monitor/project actions preserves uptime history.

## Interface

- **Visual refresh** — refined sidebar, headings, theme previews and shared surfaces; familiar composer/backgrounds retained.
- **Code blocks** — filenames, line numbers, remembered wrap, full-source copying and clearer diffs; long reopened blocks collapse, newly generated code stays expanded.
- **Activity wording** — optional rotating generic status words; precise actions/errors retained. Disable in Appearance.
- **Visual fixes** — composer focus, Send/Stop transitions, interface-font persistence and consistent update notices.
- **Compact job cards** — one live action, timed stage bar, three recent actions and folded plan/checks/files; finished jobs show a verdict and failing check.
- **Job ownership** — the composer job strip clears immediately on conversation switch.
- **HTML replies/context limits** — ordinary chat produces previewable HTML fences rather than unsupported file-tool calls; full context and truncated tool calls explain their token limits and retain reasons after reload.
- **Workbench** — resizable Activity/Plan/Checks/Files/Preview pane replaces the drawer; web jobs auto-open it unless disabled in Previews settings.
- **HTML preview** — streaming sandboxed side pane, automatic full-page opening, fill/tablet/phone widths, CDN controls and downloads.

## Housekeeping

- **Output storage** — unique delivery addresses under `outputs/<month>/<chat>/` prevent same-name files from replacing earlier downloads.
- **Chat work folders** — scripts/shells start in `/root/work`, away from interpreters and uploads.
- **Storage & Cleanup** — correct nested-output counts, month/source breakdowns and simpler temporary-file/VM cleanup controls.
- **Dead code** — removed the unused project indexer, `coder_fixtures`, `frozen_environment`, `inference_meter`, `preview_qa`, tokenizer-preparation script and two frontend harnesses; installer retains tokenizer ownership.
- **Documentation** — removed the superseded reliability-upgrade record; staged releases are documented in the Daedalus guide.
- **Environment example** — removed retired OpenHands/Aider/Fixer/Architect variables, corrected worker port to `8587`, and documented `DAEDALUS_ENGINE_URL`, `HEPHAESTUS_*`, `HYPRCHAT_TRUSTED_WEB_PROXY`, `QUICK_SEARCH_TIMEOUT`/`RANKING`, `RAG_RETRIEVAL_DEADLINE_SECONDS` and `HYPRCHAT_DEPLOY_QUALIFICATION`.

</details>

<details>
<summary>Alpha v18.2 — Oct 26, 2026</summary>

> Workspace hubs, model-aware effort, safer answer reviews and interface polish.

## Workspaces

- **Project chats** — launch from headers/cards; New chat inherits the workspace, with New general chat and an in-chat workspace shortcut.
- **Defaults** — workspace instructions plus optional model/persona/tool choices; instructions work independently of memory.
- **Sources** — cited retrieval from linked/uploaded content, pin/exclude controls, indexing status/retries and retained knowledge snapshots.
- **Overview** — pinned content, recent chats, todos, jobs and review items; searchable linking without moving existing content.
- **Navigation** — Overview, Chats, Sources, Notes, Activity and Settings; mobile section picker, consistent controls and one Add existing dialog.
- **Search** — returning from results preserves query, filters and scroll position; section changes preserve drafts.
- **Nav rail/layout** — Projects beside Chat, configurable in Appearance; removed the sidebar shortcut. Aligned content widths, default/topic chips, compact status rows, switches, pagination and actionable empty states.

## Chat & Reasoning

- **Activity pill** — expandable thinking/tool/review phases with emojis, live thinking preview and responsive details.
- **Effort** — Blurt/Ponder/Forge/Galaxy Brain map to supported native reasoning levels and show model defaults; boolean-only, unsupported or unknown models retain 0–3 review passes.
- **Self-review** — completed answers remain until a replacement succeeds; failed/interrupted/empty/truncated reviews preserve them and use only read-only verification tools.
- **Response Context** — inspect/restore completed review versions and view effective effort and per-call token usage.
- **Delivery** — save before completion, retain review metadata and preserve cloud reasoning state between tool rounds.

## Interface

- **Motion** — anchored menu/dialog/drawer/toast transitions, crossfading panes and animated new messages; loaded history stays still and Reduce Motion is respected.
- **Surfaces** — theme-aware elevation/shadows, consistent radii and lighter type across Settings/navigation/composer/tools/model picker; light scrims for light themes.
- **Activity styling** — crisp icons, progress line, phase/color transitions, animated details and completion chips.
- **Messages** — canvas-based assistant replies, right-aligned tinted user bubbles and tooltip-equipped action icons.
- **Fonts** — optional Inter, Geist or System interface font; code retains monospace.
- **Sticky headers** — Settings, Monitor, Add existing, Response Context and Document revisions keep titles/Close visible while scrolling.
- **Icons** — SVGs replace text/emoji controls across Settings, theme, pins, workspaces and common actions; distinct Workspace/Artifact/Email icons.
- **Sidebar** — date groups, generating dots, workspace markers and accent-colored New Chat.
- **Empty/loading states** — consistent actionable placeholders, dots/spinners and workspace skeletons.
- **Details** — starter chips, chat-title/activity browser tabs, Send/Stop morphing, long-wait quotes, livelier streaming backgrounds and `?` shortcut help.
- **Hugging Face** — exact-filename pulls distinguish fused-MTP/noMTP/draft quants; progress includes bundled projectors. Compatibility checks selected files against installed architectures; draft heads require Download anyway.
- **Startup** — library-search backfill skips deleted-profile notes instead of failing foreign-key checks and crash-looping.
- **Readability** — stronger surfaces, consistent controls/larger labels and selected monospace font support across interface/chat/code.
- **Chat controls** — restored separate memory/Ghost/theme/export/import controls and subtle outer composer focus.
- **Workspace width** — conversation lists collapse outside Chat and restore on return; Research uses responsive report/evidence drawers and labeled secondary-action menus.
- **Mobile/keyboard** — responsive toolbars, touch targets, visible focus, improved dismissal/focus restoration and calendar proposals below the header.

## Settings & Reliability

- **Profiles** — explicit agent/persona CRUD failures; profile-synced effort/thinking preferences.
- **Cleanup preview** — distinguish reclaimable/retained files; protect saved references, active/interrupted staging, changed files and unresolved legacy outputs.
- **Logging** — removed raw chat/reasoning/tool-argument debug logs and image-deletion journal wiping; retired the journal-scrub helper.
- **Development** — Python 3.12 and the pinned test lock align local backend testing with production.

</details>

<details>
<summary>Alpha v18.1.1 — Oct 15, 2026</summary>

> Office document tools, a Jobs panel, searchable saved work, and more reliable chat, research and scheduling.

## Chat & Search

- **Composer tools** — manage tools from the wrench menu beside **+**. Multiple tool calls in one reply now run correctly.
- **Library search** — find chats, notes, research and artifacts with type, workspace and date filters.
- **Response context** — inspect the model, settings, memories, sources and tools used for a reply.
- **Councils & retrieval** — councils support selected local and cloud models with cloud spending caps. Search and knowledge-base retrieval handle slow services better and show clearer source locations.

## Documents & Research

- **Office tools** — read, create, edit and export Word, PowerPoint and Excel files from chat, with previews beside the conversation and PDF export.
- **Document revisions** — compare versions, restore earlier content as a new revision, and export files in batches while preserving originals.
- **Research recovery** — deeper reports use saved evidence and clearer citations. Resume interrupted work, compare drafts and ask follow-up questions using saved sources.

## Jobs, Calendar & Email

- **Jobs panel** — track background work, progress, downloads and available cancel, retry or resume actions across refreshes and devices.
- **Scheduling & notifications** — queued tasks survive restarts, recurring runs avoid overlap, and failed or uncertain deliveries remain visible.
- **Calendar & email** — improved recurring events, reminders and sync-conflict handling. Appointments extracted from email need confirmation before entering your calendar.
- **Image Studio & connectors** — retry failed image downloads, cancel individual queued jobs, and use more reliable MCP connections.

## Reliability & Voice

- **Saved files & backups** — durable artifact storage and fuller backups protect documents and downloads; interrupted restores can recover database and files together.
- **Profiles & mobile** — stronger access controls, fewer stale updates after switching profiles, clearer errors and refreshed app caching. Main gets an operations overview.
- **Voice** — navigation stops recording and playback; failed transcriptions retain the recording for an explicit retry.

## Daedalus & Models

- Fixed repair file listings, thinking controls and model-catalog matching.
- Updated Ollama to **0.35.0** with restored generation throughput. Planning uses **qwen3.8:27b**; coding remains on **qwen3-coder:30b**.

</details>

<details>
<summary>Alpha v18.1 — Sept 30, 2026</summary>

> More reliable Daedalus verification, research, calendar sync and mobile layouts.

## Daedalus

- **Persistent jobs** — opt-in checkpoints, restart recovery, Stop/Continue, repository inspection and revision downloads. Experimental workflows remain off by default; legacy paths remain available.
- **Policy 7** — separate new-build/uploaded-edit toggles; executed tests, independent audit and review produce Ready for review or Build incomplete. **No evidence-gated job is Complete**: trusted per-requirement checks are not yet registered, and generated tests cannot replace them.
- **Isolation** — persistent builders/audits/commands and legacy Builder/Aider use bubblewrap with private process/network namespaces, read-only toolchains, restricted dependency proxy and supervised model bridge. Missing `bubblewrap` blocks execution; compiled-project audits require `strace`.
- **Build/repair** — compile/run during work, target failing files, reduce repeated reads/compaction, reserve audit/review calls and return candidates at budget exhaustion.
- **Verification** — stricter protected-file/interface/docs/test checks reject deleted assertions and printed success. Browser checks catch startup errors, rejected submissions and throwing handlers; silent non-submission is advisory, so manual review remains necessary.
- **Languages** — Java, C/C++, C#, Go and Rust runners join Python/Node; README-only uvicorn/node launch commands are recognized.
- **Behavior probes** — requested invalid-input/missing-file exit codes, command output against independent fixtures and named accessible page controls.
- **Repairs** — browser failures target scripts/server; scaffold-test replacement avoids false regressions; rolled-back repairs may retry within budget; nested package docs recognized.
- **Context calibration** — counts dense code correctly so compaction precedes exhaustion.
- **New-build foundations** — verified static-web, Express/SQLite, React/Vite + FastAPI/SQLite and .NET/xUnit skeletons. Probe literal commands, .NET tests and requested edit/drag/delete/persistence/text flows before audit; ordered repair evidence, bounded retry prompts and visible verification phases.
- **Recovery** — fixed stalled tests, truncated/large edits, malformed audits and toolchain errors. Follow-ups retain uploaded test commands; Stop preserves candidates; call-limit/download failures resume; oversized prompts explain Continue, and small budgets reserve affordable verification calls.
- **Follow-up scope** — accepted projects reconcile changed/conflicting requirements, list differences and request clarification through Continue. Ready for review candidates cannot yet take new follow-ups; continue the existing job.
- **Visual review** — optional AI review of running-app screenshots at configured viewports; enabling it at Continue reruns checks.
- **Progress/context settings** — persistent cards show outcomes, files, failures, calls and review reasons; shared policy supports stage/helper limits and compaction overrides.
- **Settings layout** — grouped switches/descriptions, effective input-budget readout, stage-override table and sticky unsaved-changes bar.
- **Default context** — increased 32,768 → 65,536 tokens to leave repair room after builder overhead.

## Coding & Search

- **Master Developer/Coder Docs** — refreshed SwiftUI references; incremental updates preserve working references, indentation and version notes.
- **Code answers** — Swift highlighting, clearer labels/colors and better search detection of code, API names and follow-ups.
- **Shared web search** — ranked results/excerpts, bounded timeouts, partial results and clearer errors across chat/research/councils; SearXNG recovery retains VPN egress protection.

## Research

- **Investigative Dossier** — guarded direct/search access to leaks, FOIA, government/court sources, including DOJ, CourtListener, Archive.org and WikiLeaks.
- **Reliability** — complete citation-ordered source indexes, bounded concurrency/fallbacks, depth-aware deadlines and cancellation cleanup.

## Calendar & Assistant

- **CalDAV** — recurrence round trips, normalized sync markers, fewer redundant pulls/false conflicts and explicit invalid-timestamp logs.
- **Feedback** — task/note/calendar/email failures surface; failed autonomous-email toggles revert; loading placeholders avoid empty-state flashes.

## Mobile & Interface

- **iPhone PWA** — removed bottom white strip and keyboard layout shifts; bottom-anchored new-chat composer, compact greeting and corrected safe areas.
- **Responsive UI** — landscape/large text/Assistant layouts, mobile Calendar day view, narrow menus/Settings/sign-in/date/Analytics/artifact controls and scrolling attachment chips.
- **Model picker** — keyboard support, viewport bounds, upward opening near the bottom and scroll/resize dismissal.
- **Reliability** — local timestamps, stopped polling for deleted runs, retained image jobs during network errors and one-click outdated-tab reload.

</details>

<details>
<summary>Alpha v18.0 — Sept 15, 2026</summary>

> Personal Assistant, scheduling, event automation, notifications, calendar/email integration and mobile navigation.

## Assistant & Automation

- **Assistant** — per-user persona/pinned chat with scheduled briefs combining tasks, notifications, calendar, notes and email.
- **Tasks** — once/daily/weekly/monthly/cron schedules plus events/webhooks; normal chat tools and chat/notification delivery. Create through natural-language drafting in Tasks or chat.
- **Notifications** — in-app bell/unseen count, browser toasts and optional ntfy/self-email delivery.
- **Dashboard** — latest brief/read-aloud, per-check-in Run Now, weekly/monthly/cron cadence, focus prompts and run status.
- **Weather** — configured-location conditions and today/tomorrow forecasts through keyless Open-Meteo; `get_weather` joins CodeAgent tools.
- **Event automation** — Tasks and `manage_tasks` can react to email/research/artifacts, including post-triage `urgent_email_received`.
- **Quiet hours** — timezone-aware overnight suppression of ntfy/email; optional urgent-email bypass, with in-app notifications retained.
- **Email master switch** — server-side Assistant permission combines with account send permission; disabled sending produces drafts.

## Calendar, Notes & Email

- **Calendar** — month/week/day views, reminders and two-way CalDAV (including Nextcloud/iCloud); remote conflict resolution and timezone-aware all-day/floating events.
- **Notes/todos** — reminders plus chat tools for notes, events and tasks.
- **Email** — IMAP/SMTP, local-model background triage, urgency/summaries, tentative invitation events and urgent alerts. Stores headers/snippets, never bodies.
- **HTML reader** — sanitized sandboxed HTML, embedded inline images, new-tab links and plain-text toggle; scripts, event handlers and same-origin access blocked.
- **Permissions** — list/read/reply/archive/delete tools; send/delete default off per account. Threaded replies, Trash-only deletion (refused without Trash), and cache purge on mailbox reset prevent stale-ID actions.

## Mobile

- Phone drawers for navigation/sidebar, single-column Settings/Research/Models/Artifacts and fixes for iOS input zoom and keyboard viewports.

## Interface

- **Navigation** — profile-synced ordering/visibility through drag or arrows in Appearance; main rail, More or hidden placement, with unique panel icons.
- Two-pane desktop email; Analytics token bars/cost line/model-share chart; loading skeletons; grouped chat avatars/hover actions/timestamps; side-by-side Canvas AI-edit diffs.
- **Empty chat** — gradient wordmark, orbital logo, staggered reduced-motion-aware entrance and personalized time-of-day greeting; more playful local-model taglines cached by user/time of day.

## Reliability

- Pinned Assistant chats inherit profile tools even when seeded with empty tool lists.
- Roughly 35-finding audit: bounded IMAP/scheduler operations, generation cancellation on stop/timeout, re-armed moved-event reminders, fenced/rate-limited webhooks and daily notification/run retention.
- Seventeen follow-up fixes include task → conversation → persona tool resolution and self-healing check-ins after pinned-chat deletion: clear stale references, reconnect seeded chats and resolve the live chat at dispatch.
- Brief failures report errors; notifications contain actual briefs without feeding themselves back. Explicit cloud models are honored with spend warnings, inherited fallbacks stay local. Fixed brief/profile timezones, relative email dates, local next-run display, accidental task retyping, startup gatherers, date-only reminders, retained persona tools and missing research/artifact events.
- **63 offline tests** cover scheduling, email, cancellation, headless tools/models, accurate failures, timezones, re-seeding, quiet hours and weather.

</details>

<details>
<summary>Alpha v17.4.0 — July 9, 2026</summary>

> Cloud tools/cost tracking, richer chat, artifact editing, smarter retrieval, backups and installable PWA support.

## Cloud Models

- **Custom provider** — any OpenAI-compatible `/chat/completions` endpoint, including OpenRouter/Groq/Mistral/vLLM/llama.cpp/LiteLLM; optional key/name, `custom:<model>` IDs and streaming/thinking/vision/usage support.
- **Native tools** — OpenAI Responses function calls and Anthropic `tool_use`; rejecting models fall back to text tools mid-stream. Custom providers retain text fallback.
- **Cost estimates** — per-model and today/30-day/all-time cloud spend from built-in prices; local models excluded.

## Chat

- Live/persisted tokens-per-second from Ollama evaluation timings.
- Continue resumes output-limited replies in the same message/database row without repetition.
- Persistent thumbs-up/down ratings and Statistics totals.
- `/` searches Prompt Library; inline forms fill `{{variables}}` across composer, Use and quick-menu flows.
- **Auto-compaction** — opt-in rolling Workspace Model summaries near context limits, preserving original messages.
- **Smart routing** — opt-in Auto model; local chat/code/reasoning classification plus deterministic long-context checks, category-specific models and actual-model footer.

## Memory & Artifacts

- Memory-enabled chats index into Chroma for `search_history`; semantic/keyword history search in Memory, with edit reindexing and deletion cleanup.
- **Canvas** — lazy CodeMirror editor for text/Markdown/code/JSON/HTML; selected-text AI edits show accept/reject diffs, and saves create new revisions without overwriting originals.

## Knowledge Bases

- **Add URL** — SSRF-safe web/PDF ingestion, trafilatura extraction, Markdown storage/provenance and indexing; repeated URLs update in place.
- **Reranking** — opt-in Workspace Analysis Model relevance pass, roughly 1–2s overhead; errors retain normal ranking.
- **OCR** — CPU RapidOCR for scanned KB/chat PDFs, up to 50 pages; requires `rapidocr-onnxruntime pypdfium2`, otherwise skipped.

## Backup & Access

- **Backup/Restore** — consistent SQLite plus uploads/KB/settings, scrubbing provider keys/connector secrets and excluding derived Chroma. Restore stages for the next restart and keeps a `.pre-restore` database.
- **PWA** — manifest/icons and network-first app-shell service worker, excluding API traffic; install requires HTTPS.
- **Tailscale HTTPS** — Serve proxies `https://hyprchat.<tailnet>.ts.net` with automatic certificates, enabling microphone/PWA use; one-time tailnet admin enablement required.

</details>

<details>
<summary>Alpha v17.3.2 — July 5, 2026</summary>

> Persona/image, search, Settings, background and export polish.

## Persona Photos & Images

- Scene-aware model photo wording is validated, with deterministic fallback; Keep your look reuses the previous seed.
- Adult routing ignores appearance-only wording; selfie rescue requires an explicit photo-of-you request.
- Chat quality/negative defaults and optional LLM enhancement for non-persona prompts.
- Studio adds LoRAs, seed lock/randomize, more SDXL ratios, steps/CFG sliders and backend sampler lists.
- Sparse API calls inherit checkpoint presets; restarted jobs avoid duplicate artifacts; chat images open in preview.

## Search & Research

- Balanced/quality Quick Search adds a small-model planner alongside deterministic search; speed remains deterministic.
- Better ambiguous-category, sports, follow-up and freshness handling; weak results trigger raw-query/refinement recovery.
- Embedding rerank defaults on; completed page fetches survive another source timing out.
- Full-page research composer with topic and report/model/context/source cards; existing reports open by default and Start selects the running report. Export/rerun actions appear only in report view.

## Settings & Interface

- Consistent titled cards/spacing across Settings; reorganized Daedalus, generation and connection controls using shared model/toggle/slider/chip helpers.
- Unified Daedalus header/phase-stepper/detail card with summarized output.
- Uniform sidebar row/avatar sizing, persona/council icons and accent bars; only the active chat is highlighted.

## Backgrounds

- Canvas glow sprites, depth and subtle pointer parallax.
- Neural Constellation signal pulses, Star Drift depth/shooting stars, Aurora gradient curtains, Soft Flow comet trails and Circuit Drift data packets.
- Near-60fps cap, hidden-tab pause, refresh-independent speed and static reduced-motion rendering.

## Fixes & Cleanup

- Research PDF/Print keeps headings/sources together and clamps long snippets to one page.
- Shared research HTML-cleaning/batched-search helpers remove duplication without behavior changes.

</details>

---

<details>
<summary>Alpha v17.3.1 — July 3, 2026</summary>

> Clearer search, Daedalus and council interfaces, plus repair and refactor hardening.

## Search & Daedalus UI

- Calmer source cards, clearer titles, source numbers below favicons and less duplicate metadata/citation listing.
- Ambiguous queries use chat/memory context, avoid needless news filters and prioritize matching sources.
- Summarized workflow cards with plan/build/review/fix/accept/package states; artifacts, downloads, details and raw events remain accessible.

## Builder & Repairs

- Shared OpenHands setup, bounded reasoning and fewer redundant thinking loops.
- Live multiline-argument probes recover lost file content and fall back to text tools.
- Architect carries advisory interfaces/tests/README/`.gitignore` without new hard gates.
- File-scaled rounds/continuations require a real finish signal; incomplete builds stay out of review/delivery.
- Streaming reasoning effort, bounded completions and consistent samplers.
- Aider-first repairs for existing/uploaded/generated projects; Fixer fallback and no-op repair-loop prevention.
- Expanded research-gated retry, nested-project, C#/.NET, Makefile-C and Node/Rust smoke coverage; cleaner archives.

## Councils & Release Notes

- Refined presets, host/member controls and analytics.
- Members link through `model_config_id` to current persona models/prompts/names.
- Address panels, round sections, ballots, moderator verdicts and richer metadata.
- Collapsible changelog release cards, newest expanded.

## Fixes

- KB name/description autosaves survive restart.
- Fenced `generate_image` invokes the tool; incidental local actor names no longer trigger search.
- Restored two-turn Architect → Builder handoff; removed stale single-turn cues and retained incomplete status for truncation.
- Fresh builds use task-specific workspaces unless explicitly resuming.
- Blocked states offer valid actions and research-backed retries before manual release.
- Fixer uses actual class-method references and discards unresolved symbol edits.
- More workflow-output shapes retain run IDs and cards for non-build actions.
- Unreachable live endpoints skip cleanly; connector tests mock asynchronous URL safety.
- Node/Rust and advisory checks avoid false review failures.
- Aider source/test/manifest edits require fresh review; docs-only repairs may return to Acceptance. Persona guidance matches Aider-first routing.

## Refactor

- Extracted backend routes, artifacts, schema, research config, gates, parsers, models and Codebox helpers without API changes.
- Extracted frontend picker, Markdown/artifact/widgets, Daedalus timeline, theme/session/settings and panels while retaining React/Vite architecture.
- Deploy monitor and README include the extracted modules.

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

> Interface and persona refresh; design remains iterative, with Deep Research fixes planned next. Report bugs through [GitHub Issues](https://github.com/eefernet/hyprchat/issues).

## App Shell

- Inspired by PewDiePie's [Odysseus](https://pewdiepie-archdaemon.github.io/odysseus/), with a flatter Hyprland direction retaining HyprChat's terminal/cyberpunk identity.
- HyprFlat polish, tighter glass/borders, flatter controls and refreshed chat/Settings surfaces; HyprChat logo favicon.
- Selectable Pixel Rain, Soft Flow, Aurora Lines, Star Drift, Circuit Drift and Odysseus-style backgrounds; full-viewport effects with clearer visibility and restrained rain/bars/glitches.

## Documentation

- README uses a collapsible gallery plus contextual screenshots for Agents/Personas, KB, Models, Activity, Council/settings, general Settings and chat.
- Added math/Mermaid/chart/callout examples and updated profile documentation for card imports, KBs, ratings, thinking overrides and greetings.

## Startup & Empty Chat

- Loading uses the selected theme/background; empty chat adds the logo, centered greeting, generated daily copy and a taller composer that settles after sending.
- Loads/refreshes and New Chat open an unsaved empty surface; the first message creates the conversation.
- Smoother composer positioning/floating and delayed `preventScroll` focus avoid browser jumps.
- Playful daily-greeting prompts/fallbacks and a cache-version refresh replace stale generic copy.

## Chat & Search

- Composer-edge streaming trace replaces the page-top sweep; controls/menus stack above charts/chat.
- One `+` menu groups attachments, Prompt Library and Daedalus; effort sits beside Send. Pending effort/Daedalus choices apply to the first message.
- PDF extraction failures offer Retry/Remove; search failures show a warning instead of silent clearing or endless loading.
- Quick Search grows from six to **up to 35 sources**, using larger candidate pools for broad/current/news/comparison queries, domain diversity, embedding dedupe and backfill. Context keeps compact metadata/snippets and fetches only top-page excerpts.
- One rail Search action combines title/message search; export menu offers Markdown/JSON with corrected import/export icons and visible failure feedback.

## Settings

- Centered modal with left navigation/right scrolling editor; Changelog is a bottom-pinned section with Refresh.
- Connections stacks status chips and visible Ollama/Codebox/N8N/SearXNG endpoints, persisted through `/api/settings`.
- Consistent save feedback for connections, RAG, quotes, appearance and model parameters.
- Loading Quotes supports full text/attribution, edit/save/cancel/remove, restore and add; 30 sourced defaults replace old placeholder quotes.

## Agents & Personas

- Renamed Coder Bot v2 to **Daedalus**, with updated model/context labels and a built-in avatar across cards, headers, sidebar and replies.
- Added persona descriptions/editor fields and themed restore-default confirmations/results.
- Split task/workflow **Agents** from character/voice/roleplay **Personas**, with explanatory guidance and separate create/edit flows. `parameters.profile_type` retains the existing `model_configs` table/API compatibility.
- Added fully populated **Kayla — Gen Z Bestie** and **Tyler — Based Gamer Bro** (replacing Based Bot), demonstrating description, personality, scenario, greeting, dialogue, lore, tags, ratings and sampling/advanced prompts; Tyler is 18.
- Rating dropdown: G, PG, PG-13, R, NC-17 and Unrated, with emojis/descriptions and model-facing boundaries.
- Per-persona Auto/On/Off thinking overrides; KB attachments, badges and chat retrieval.
- Leaving a profile in empty chat detaches it; fresh persona chats include `first_message` visibly and in context.
- Chub/SillyTavern/TavernAI imports support PNG `chara`, JSON, packed definitions, tags, alternate greetings, character-book lore and first-message mapping.

## Activity & Feedback

- Downloads becomes **Activity** for model downloads, KB upload/indexing, project archives, RAG reindex/purge, cleanup and template patching; KB uploads also show inline progress.
- Themed confirmations/toasts replace browser dialogs for model deletion/tool support, RAG, destructive actions, defaults, import/export and cleanup.
- Shared success/error/warning/info toasts support actions; user-triggered failures appear in UI while retaining diagnostic console logs.

## Fixes

- Acceptance no longer mistakes clipped long-file excerpts for broken source: larger reads, explicit excerpt-truncation markers and authoritative clean Reviewer build/lint syntax evidence.

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

## Coder Bot v2 Hardening

- **Fixer gate** — requires the latest Reviewer to report issues/errors; fabricated IDs cannot waste repair cycles. “Nothing to fix” is skipped and does not count toward the three-cycle cap.
- **No tests** — pytest exit 5 is treated as benign for scaffolds, avoiding missing-test configuration loops.
- **Budget exhaustion** — releases download/read/list tools and recommends packaging when the fix cap is reached.
- **Anti-rebuild** — blocks repeated same-project `generate_code` in one turn; edited uploads without Builder history use the feature profile. SQLite/ISO timestamp normalization makes turn matching reliable.
- **Project paths** — explicitly distinguishes on-disk directories from display names.
- **Small/runtime edits** — use read/write for small changes or compile-clean runtime bugs; reserve Builder for genuinely new ≥3-file work or major refactors. Persona guidance matches these rules.
- **Re-seeding** — updates Coder Bot v2 in place, preserving profile IDs and conversation links.
- **Settings** — empty Planning/Coder overrides survive restart and retain inheritance semantics.
- **Uploads** — limit increases 50 → 250MB, configurable by environment and shared by PDF/frontend guards.

## OpenHands Reasoning

- Low/Medium/High effort selector; Medium becomes the local Ollama default instead of SDK High.
- Lower effort reduces expensive reasoning rounds and VRAM-swapping delays; tested CRUD/UI quality showed no regression.
- `OPENHANDS_REASONING_EFFORT` flows through settings, payload and worker to the SDK; older SDKs warn and ignore unsupported effort arguments.

## Quick Search

Search improvements draw on Perplexica, Khoj and Open WebUI.

- **Standalone queries** — context-independent triage wording drives ranking, embeddings and carousel labels.
- **Embedding rank/dedupe** — batched `nomic-embed-text`; drop relevance below 0.45 and duplicate similarity above 0.85, with heuristic fallback on errors.
- **Extraction** — trafilatura on top pages, with same-HTML fallback and no duplicate fetch; added `trafilatura>=1.10.0`.
- **Conversation dedupe** — skip previously searched subqueries while retaining at least one; follow-up relevance includes prior-turn tokens so refinement can trigger.
- **Freshness** — news with recency cues uses `time_range=month`; cache keys include the range.
- **Categories** — triage drives domain bias, with regex classification only on failure.
- **Safe/bounded fetching** — DNS checks reject private/loopback/link-local targets; 512-entry LRU and six concurrent page/OG fetches bound resource use.
- **Failure guidance** — tell the model search is unavailable and not to guess current facts.
- **News recovery** — thumbnails no longer classify ordinary articles as images; image detection uses URL extensions, restoring results previously filtered out.
- **Cleanup** — removed obsolete page-fetch import and query-rewrite references.

</details>

---

<details>
<summary>Alpha v17 — May 7, 2026</summary>

> Coder Bot v2 introduces a deterministic multi-agent pipeline for new projects, uploaded-project work and code generation. Deprecated v1 remains available while v2 is tested.

## Coder Bot v2

Structured, stateless agent runs replace prompt-only orchestration. SQLite `runs` records retain result envelopes through disconnects; `/api/runs/{id}` restores the UI timeline.

| Role | Responsibility | Model |
|---|---|---|
| Architect | JSON plan, file manifest, commands, dependencies and success criteria | `PLANNING_MODEL` |
| Builder | OpenHands on Codebox; scaffold/continue/feature profiles | `CODER_MODEL` |
| Reviewer | Read-only build/test/lint with scoped file:line issues | `PLANNING_MODEL` |
| Fixer | Minimal edits from Reviewer evidence | `CODER_MODEL` |
| ProjectQA | Read-only, cited project answers | Chat/persona model |
| Indexer | Upload-time build detection and Chroma code indexing | `nomic-embed-text` |

### Reviewer

- Detects markers, executes real Codebox build/test/lint commands and asks the planning model for severity, file:line and suggested fix scope.
- Checks build markers in order: Maven (`pom.xml`), Gradle, Cargo, Go, npm, Python (`pyproject.toml`/`requirements.txt`), CMake, Make; uses each system's install/compile/build and test commands.
- Without manifests, detects dominant Java/Python/Rust/Go/JS/TS/C/C++ sources; Java compilation excludes test directories/classes lacking JUnit setup.
- Unidentifiable/missing projects return errors, never clean results. `run_review` returns the run ID used by Fixer.

### Fixer

- Reads only issue-scoped files and applies bounded coder-model edits; Reviewer remains responsible for rebuilding/verifying.
- Marker-delimited `### EDIT: <path>` fenced replacements avoid JSON code-escaping failures.
- Normalizes relative/compiler-reported paths under the project root; fabricated reviewer IDs fall back to the latest conversation review.
- Server-enforced three-successful-fix cap blocks a fourth attempt and returns the latest summary/first three issues for the user.

### Architect

- `plan_project` returns structured project/language/build-system metadata, build/test/lint commands, `{path,purpose,estimated_loc}` manifest, required tests, dependencies, risks and success criteria.
- Invalid/missing fields get one parse-error-guided retry, then an explicit failure.
- Successful plans feed Builder directly, including commands, criteria, dependencies and risks.
- Plans render file trees, build-system-specific dependency snippets, criteria checklists and test tables. The restored Architecture Plan panel supports copy and expand/collapse through `detail.plan` events.

### Project Q&A and uploads

- `ask_project` combines tree inspection, keyword grep and source reads into grounded file:line answers using the chat/persona model.
- Fuzzy filename matching handles spaced names and reads targeted files up to 30KB; change-like questions are flagged for coding.
- Invalid model-supplied project paths fall back to the latest Builder path.
- Upload indexing detects build systems and indexes up to 100 source files into Chroma `code_memory`, excluding dependencies/build/cache folders; later QA/build calls can retrieve them.
- Raw multipart `/upload-chunk` replaces base64 shell chunks: 1MB instead of 100KB, avoiding Linux argument limits and false command-denylist matches, with roughly 10× upload throughput.

### Builder profiles

| Profile | Behavior |
|---|---|
| `scaffold` | New project: create all files and initialize Git. |
| `continue` | Partial/stuck run: write missing manifest files without rewriting existing work. |
| `feature` | Successful existing project: make minimal requested edits. |
| `bugfix` | Internal Fixer path, outside OpenHands. |

- Profile selection uses the latest succeeded/partial/stuck Builder envelope, ignoring failed/cancelled/running entries and stale project rows.
- Feature prompts include up to 60 tree paths and 1–3 task-relevant files.
- Cards distinguish scaffold/continue/feature runs.

### Interface

- Role/profile icons and labels distinguish Designing, Building, Resuming, Adding feature, Bug fix, Reviewing, Fixing, Investigating and Indexing; action/phase rows also carry icons.
- Failed runs and issue/error reviews auto-expand; other cards start collapsed.
- QA cards render Markdown/code/citations, examined-file lists and change-request flags.
- Architecture plans survive the 200-event live window by using saved message events. Architect labels replace the old brain/planning wording with the drafting icon.

### Workflow gates and reliability

- **v2-only gates** — detected through persona names containing `v2`; v1 behavior remains unchanged. Unreviewed succeeded/partial/stuck builds or succeeded/failed fixes allow only Review; reviews with issues/errors allow Fix or re-review; completed non-change QA allows follow-up QA; three successful fixes block further Fixer calls. Responses include IDs, paths, scope and next actions.
- **QA delivery** — successful non-change answers stream verbatim without another paraphrasing model round.
- **Stop** — propagates to the worker, pauses/closes the SDK and child shell, and handles repeated/finished cancellation safely.
- **SSE fallback** — non-streaming retry occurs only before the first event; mid-run disconnects cancel rather than duplicate execution.
- **Active-project routing** — questions → QA, changes → Builder, bug reports → Review; no automatic packaging.
- **Repeated tools** — Review/Fix/QA bypass duplicate-call detection because workflow gates govern their valid repetition.
- **Round exhaustion** — emits a final summary of builds, review status, file:line issues and exhausted budget instead of a blank reply.
- **Models** — role routing follows the table; empty primaries use the configured planning/default/coder fallback chain.

### Verification

- Java Pong passed Maven compile/tests; Python Flask passed 4/4 tests in 3.1 minutes without repair; Rust CLI passed build/tests after one repair.
- Verified reload/disconnect recovery, rejection of unrelated historical project context, failed-test repair convergence, per-run token records and cited `Ball.java` Q&A.
- Python → Go port passed 4/4 tests first try in 5.4 minutes without repair; measured 41.94/48GB VRAM with Devstral and Qwen3-Coder loaded.
- Verified empty-setting persona fallback and specialized planning/coding/chat role routing.

### Independent critic

- Optional `CRITIC_ENABLED` second review uses `CRITIC_MODEL` (fallback: planning/default), checking runtime behavior beyond syntax/compilation with a live review indicator.

## Quick Search

- Shared `quick_search.py` serves chat injection and `/api/quick-search`, removing duplicated SearXNG handling.
- Skip only empty/greeting/pure-arithmetic or supplied-text rewrite/translate/summarize requests. Programming, research-oriented math and code-block questions remain searchable.
- Rewrite from the last 2–3 turns using `WORKSPACE_MODEL` (default `qwen3.5:4b`); sanitize thinking/URLs/prefixes, bound to 8 seconds and fall back to raw input.
- Keep video/image results in the carousel but exclude them from model context. Rank web results with trusted-domain bonuses/path penalties, at most two per domain and six overall.
- Fetch the top three short/navigation-like snippets in parallel: four-second total budget, 1,500-character excerpts.
- Include today's date; require citations to used URLs and explicit uncertainty when results lack an answer.
- Explicit `safesearch=0`, ten-minute rewritten-query cache and Google-result fallback when SearXNG fails or returns nothing.

## Inline Search Images

- Offer 2–3 thumbnail/OG images for relevant thing/place/person/product/news answers; exclude code/math/conceptual/numeric answers. Existing inline rendering keeps bounded dimensions, rounded corners and broken-image hiding; source carousel remains separate.
- `/api/img-proxy?u=...` fetches server-side with domain-matched Referer, 5MB cap and 24-hour caching, hiding browser IP/referrer/cookies and avoiding hotlink/mixed-content failures.
- Inline images use `no-referrer` and lazy loading, including non-proxied links.

## Coding Reliability & UI

- **Context** — OpenHands checks loaded `num_ctx`, evicts mismatches and preloads the requested context so cached Ollama options cannot override Settings.
- **Builder routing** — plans with ≥3 source files or complete apps/APIs/CLIs/games/libraries require `generate_code`; unfamiliar APIs prompt research first.
- **Stream persistence** — create the assistant row at stream start, snapshot at round boundaries with run IDs, and PATCH that row on completion to avoid lost or duplicate replies after disconnects.
- **Status pills** — clearer transitions/icons and fewer flicker/disappearance glitches across coding/review/fix/search.
- **Bash rendering** — shell variables, substitutions, backticks, ampersands and heredocs no longer break fences.
- **Fixer validation** — issues without `suggested_fix_scope` are skipped instead of crashing.

</details>

---

<details>
<summary>Alpha v16.2 — April 22, 2026</summary>

## Rich Rendering

- **Callouts** — GitHub-style NOTE/TIP/IMPORTANT/WARNING/CAUTION blocks collect multiline quoted content into colored icon/label boxes; model guidance encourages their use.
- **Keyboard caps** — `<kbd>` renders raised monospace shortcut keys.
- **Color swatches** — clickable chips for hex (including alpha), RGB(A) and HSL(A) mentions without special syntax.
- **Charts** — `chart` JSON fences support bar, line, pie, doughnut, scatter, radar and polarArea; simple labels/data or multiple datasets, theme palettes/axes/tooltips, source/copy controls. Uses Chart.js 4.4.4 from jsDelivr.
- **Tables** — real grouped header/body rows, per-column alignment, wrapping and horizontal scrolling replace clipped cells.
- **Multiline rendering** — shared consumed-line tracking prevents duplicate callout/table rows.
- **ChartBlock** — bordered chart header, source/copy, explicit parse/render errors with raw JSON, rebuild on data/theme changes, per-slice circular colors and translucent axis-chart fills.
- **Model guidance** — adds callouts, keys, colors and charts alongside Mermaid/math; chart fences replace unnecessary saved matplotlib/seaborn/Plotly/pandas images when values are already known.
- **Deep Researcher** — uses charts for quantitative results, appropriate caveat/advice callouts, aligned comparisons and key/code formatting.
- **Preset updates** — existing Deep Researcher seeds receive prompt/tool updates on startup while retaining model, KB and parameter choices.
- **Compute then chart** — clarified that image-saving restrictions do not forbid computation: execute arithmetic/aggregations, print JSON, then render chart fences.
- **Research computation** — adds `execute_code` to new/existing seeds for CAGR/CMGR, weighted averages, variance, shares, conversions and aggregations; routes pure math to code, substantive questions to research and trivial lookups to neither.
- **Research-loop limits** — prompt guidance caps repeated same-topic research at two similar/approximate results; projected/preliminary/YTD figures are synthesized with uncertainty callouts instead of endless searches for unavailable final numbers.

## Effort Levels

- **Self-review** — 0–3 additional answer passes may use tools to verify claims.
- **Levels** — Blurt (0), Ponder (1), Forge (2), Galaxy Brain (3).
- **Controls** — global Settings default and per-chat composer override inherited by new chats.
- **Display** — each refinement replaces/re-streams the answer, shows round progress and leaves a persistent Refined N× badge.
- **Bounds** — `effort_rounds` re-enters the agent loop with a critique prompt; total `MAX_AGENT_ROUNDS` still applies.
- **Events** — `refinement_start {round,total}` and `done.refinements` persist review count in message metadata.
- **Storage** — `hc-effort-level` and `hc-effort-per-chat` localStorage keys; no database migration.

</details>

---

<details>
<summary>Alpha v16.1.1 — April 22, 2026</summary>

## Rich Rendering

- **Mermaid** — inline SVG flowcharts, sequence/class/state/ER/Gantt/mindmap/pie diagrams with 34 theme mappings and live theme changes.
- **KaTeX** — inline/display math using dollar or LaTeX delimiters; code stays literal. Multiline display math remains intact, and inline-code delimiters are masked before parsing.
- **Diagram controls** — Mermaid header, source/copy buttons and readable errors with raw source.
- **MDWrap** — applies math rendering to chat, councils, Hugging Face README and changelog after mount; skips streaming to avoid partial-token flicker.
- **Model guidance** — diagrams/math render inline without file/code-generation tools; no LaTeX dollar expressions inside Mermaid labels.
- **Task lists** — read-only unchecked/checked boxes with completed-item strikethrough.
- **Collapsibles** — interactive HTML `<details>`/`<summary>` with chevrons.
- **Diff fences** — theme colors for added/removed/hunk/metadata lines.
- **Syntax highlighting** — Prism CDN autoloader and CodeBlock, retaining language labels/copy.
- **Footnotes** — numbered superscripts, smooth scrolling, appended definitions and unique per-render IDs.

## Message Actions

- **Timestamps** — set on send/regenerate/edit/council creation and retained with message IDs through streaming/reload.
- **Regenerate with…** — one-shot model, temperature or persona overrides.
- **Delete/Undo** — immediate removal with five-second snackbar undo; message-delete API keeps FTS synchronized.
- **Python output** — collapsible per-output cells, initially expanded, with in-memory state.

## Themes

- Rebalanced Terminal, Cyberpunk, Solarized Dark, Gruvbox, Dracula, Rosé Pine and Midnight for text ≥7:1, dim ≥4.5:1 and muted ≥3:1 contrast. Preserved theme identities and left Nord, Catppuccin, Tokyo Night, One Light, Ayu Dark, Material Ocean and Solarized Light unchanged.

## Persistence & Streaming

- **User messages** — server-side persistence at stream start replaces racing frontend saves and duplicate writes.
- **Ordering** — timestamp plus ID ordering keeps same-second messages stable.
- **Conversation switching** — reloading the active stream cannot overwrite user messages with an incomplete server snapshot.
- **Activity persistence** — event accumulation survives UI clears on chat switches, retaining completed-message pills.

## Cleanup & Fixes

- Replaced null-byte Markdown sentinels with Unicode private-use characters so source remains searchable; footnotes use separate markers.
- Fixed code-execution error-hint operator precedence and shell no-output fallback.
- Malformed workspace-analysis JSON safely falls back to an empty topic list.
- Fixed wildcard CORS by disabling credential mode so preflights succeed.
- Escaped full-text search snippets to prevent XSS while retaining match highlights.
- Model-pull HTTP failures now emit explicit SSE errors.
- Evict completed/failed indexing-status entries instead of leaking them.
- Replaced deprecated event-loop lookup with the running-loop API.
- Hoisted parallel-safe/tool-icon constants out of chat rounds and simplified repeated avatar-filename evaluation.

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
