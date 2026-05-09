## Alpha v17.0.1 — May 8, 2026

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

### Quick Search — multi-round agent

Replaced the single-shot rewrite-and-search pipeline with `backend/search_agent.py`. Same call site, smarter inside.

- **Triage stage** — one Ollama call (`format=json`, `think=false`) returns `{needs_search, queries[1-3], category, reason}`. Replaces the old skip-classify / rewrite / pronoun-validate chain. Falls back to searching the raw user message on JSON-parse failure.
- **Multi-query fanout** — compound questions get parallel SearXNG calls, results merged.
- **Relevance check + optional refine** — if top-3 result tokens overlap < 30% with the user's message, refine the query and search once more (max 2 rounds).
- **Domain bias** retained — news queries demote SO/GitHub, code queries demote news/recipe, etc.
- **Anti-leak prompt** — triage examples now use disjoint domains (sourdough/Rust/Brazil/greeting/attached) with explicit "don't copy these entities" instruction; small models like dolphin-phi:2.7b were regurgitating the old UK-politics example verbatim.
- **VRAM fix** — triage call hard-caps `num_ctx=4096`. Without it, qwen3.5:4b's 256K default allocated ~19GB of KV cache and evicted the chat model. Saved 12.5GB.
- **Workspace model wired end-to-end** — the UI's "Workspace Analysis Model" dropdown now PATCHes `workspace_model` to `/api/settings`, persisted to `settings.json`, loaded into `config.WORKSPACE_MODEL` on boot. Was localStorage-only; backend never saw the user's choice. Triage priority: `QUICK_SEARCH_TRIAGE_MODEL` → workspace → chat model → default.
- **Carousel reuses agent results** — agent emits `search_results` SSE event tagged `source: "quick_search"`; frontend renders that into the QUICK SEARCH RESULTS carousel directly. Removed the parallel `/api/quick-search` POST in the chat send path. One SearXNG round-trip per turn instead of two; carousel and chat answer can no longer disagree.
- **Removed** — `_rewrite_query`, `_try_rewrite_call`, `_topic_phrase` legacy chain. `_should_skip` / `_content_tokens` / `_needs_context` / `_classify_query` / domain-bias / page-fetch / OG enrichment helpers stay (reused by the agent).
- **Tests** — `backend/tests/test_search_agent.py`, 24 unit tests covering triage validation, relevance scoring, single/refine paths, max-rounds cap, skip-gate.
- **Deploy monitor** — `backend/search_agent.py` added to `WATCHED` in `deploy_monitor.py`.


## Alpha v17 — May 7, 2026

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


## Alpha v16.2 — April 22, 2026

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


## Alpha v16.1.1 — April 22, 2026

### Rich Rendering
- **Mermaid.js diagrams** — ` ```mermaid ` code fences render inline as live SVG: flowcharts, sequence, class, state, ER, gantt, mindmap, pie. Theme-synced (34 mapped variables) and re-render when the user switches themes mid-conversation.
- **KaTeX math** — Inline `$...$`, display `$$...$$`, and LaTeX `\(...\)` / `\[...\]` delimiters all render as typeset math. Code blocks are ignored so `$` in source stays literal.
- **`<MermaidBlock>` component** — Header with `◈ mermaid` label, source toggle, and copy button matching existing code-block styling. Broken diagrams show a red error banner plus the raw source instead of breaking the message.
- **`<MDWrap>` wrapper** — Wraps 8 render surfaces (chat, council cards, HF README, changelog modal) and invokes KaTeX auto-render after mount. Streaming messages skip wrapping so partial tokens don't flicker.
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
- Fixed workspace file preview panel not showing when Workspace tab is active — preview panel moved outside panel ternary so it renders alongside any active panel


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
