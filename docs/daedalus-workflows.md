# Daedalus operating guide

Daedalus uses OpenHands SDK/tools **1.59.0** on the Hephaestus Docker VM. One SDK conversation handles inspection, planning, implementation and repairs. HyprChat owns durable jobs, budgets, cancellation, independent verification, immutable revisions, artifacts and explicit owner acceptance. This retirement does not change Qwen3.6 or its inference settings and is not a full model qualification.

## Models and settings

**Daedalus chat model** routes requests and explains status. **Coding model** handles the implementation conversation, including its plan and repairs. Optional Project Q&A and visual-review model overrides remain. Context, output, reasoning, compaction, command and job allowances live in `context_policy.py` and Settings.

On upgrade, settings version 2 preserves the effective Builder override as the Coding model, falling back to the previous Coding model. The Planning model keeps its selected value under the chat label. The original settings are retained in the private `settings.before-daedalus-modern.json` beside `settings.json`. Stale clients cannot enable retired engines or save obsolete role selectors. Reload Settings after upgrading.

## Builds, uploads and questions

Request a new project or upload source and describe an edit. Uploading alone does not authorize editing. Follow-ups use the working candidate by default. Project Q&A runs on the modern worker with read-only repository inspection, source citations, normal budgets and cancellation; it does not dispatch a coding action.

Chat reports a coding start only from a durable job receipt for the saved user message and selected project. Unverified progress prose stays buffered. One correction may use the remaining chat-round allowance; its original response and consumed correction are retained across reconnects. A repeated unsupported claim ends with a truthful no-job result. Job creation saves its chat identity atomically, so a lost response reattaches to the original job without dispatching again. Questions, status, Stop and plan-only requests do not authorize corrective editing.

The card shows Plan → Build → Check → Deliver, a structured agent-reported plan, current file activity and expandable history. A completed checklist is not verification. Unfinished items remain “Not updated.” The Files & logs browser shows working files; delivered checks belong to the exact immutable revision.

Older jobs and their recorded evidence remain readable. Retired engines cannot resume or dispatch operations. Download retained source and upload it into a new modern project to continue; old conversations, counters and evidence are never reinterpreted as modern jobs.

## Verification and recovery

Independent checks build and launch a fresh revision copy with declared dependencies. Real browser/desktop interactions, executable assertions, requested deliverables, regression test identities and protected source govern evidence. Project-authored tests and source inspection alone cannot establish arbitrary requested behavior.

New jobs use engine behavior version 2: desktop commands inherit the controller display, and accessible controls must belong to application windows mapped on that display. Preview uses the same display. Existing jobs retain their saved behavior version. Test evidence records the command and resolved manifest runner; unsupported JSDOM inline-document tracing remains unverified, with a diagnostic rather than an inferred source binding.

UI parser version 4 adds literal two-input examples, per-input negative-error probes, and named desktop input/format/error checks. New edits rebuild inherited contracts from original user wording while preserving historical contracts. Unsupported clauses remain visible. In particular, a Qt label exposed only through its accessible name cannot establish that name's equality to independently visible text. Qualification polling retries only bounded read-only transport failures, rechecks identity, and never replays an uncertain composer submission.

UI expectations are frozen from original user wording for both new builds and follow-ups. Parser version 3 applies the same narrow clearing/exact-value rules from the first build; historical evidence remains unchanged. A new follow-up inherits trustworthy original request history, including requirements missed by older initial-build contracts. Supported transitions need observed controls and a meaningful different starting state. Unsupported, ambiguous or unexercised requirements remain unverified. UI manifests cannot redefine these expectations. Automatic UI-flow preflight shares validation/replay with independent verification, but its diagnostic results never add delivered passing checks.

Finish requests verification. The existing conversation receives specific feedback, with at most two application repairs per user request. One bounded pre-dispatch regeneration handles eligible generation failures without replaying dispatched actions. Infrastructure preparation has a separate durable attempt allowance and never spends an application repair. Persistent infrastructure failure parks saved work.

An SDK stuck/error result remains a failed completion check even when the checkpoint's existing tests pass. Recovery uses the same conversation and the shared two-repair ceiling; an unresolved stop delivers an explicitly incomplete candidate. A normal Finish that defers dependency preparation can still pass subsequent independent verification. Edits receive the observed baseline test identities, files, commands and revision before coding; changing a runner so those identities disappear remains a regression.

Project UI scenarios are identified by their launch and ordered interactions, not array position. Appending or reordering flows preserves existing coverage; replacing or deleting a scenario loses a required test. Assertion-value edits retain the scenario identity but remain visible as changed expectations requiring review. Old positional identities remain readable for resumed jobs. Tests must exercise actual application imports or the running interface; copied logic in test helpers is not application evidence.

Resume preserves consumed calls, execution time, repair counts and conversation state. Explicit additional allowance grants calls/time only; it cannot reset repairs. A terminal generation failure may deliver a verified checkpoint as an incomplete review candidate. Historical checks do not become current evidence.

Passing check rows, advisory rows, failures, skipped checks and unverified criteria are reported separately. Test-case totals are distinct from check-row counts. A candidate can be delivered despite failed or unverified requirements. **Accept promotes only the exact reviewed revision** and never upgrades verification. HyprChat retains the delivered bundle; Hephaestus retains working source, revisions and application data until explicitly reset.

## Runtime and previews

The trusted broker runs project commands in scoped containers without a Docker socket, privileged mode or host networking. Dependencies use private writable preparation generations, leases and quota-aware publication. Python environments are created at their final path; compatible public downloads and browser binaries can be reused read-only. Matching Playwright binaries are validated against the declared package version. Downloads must use portable setup commands, not Hephaestus executable paths.

Temporary authenticated web/desktop previews expire after 15 minutes. Keep running pins the selected revision, runtime and application data; Stop retains that data. Pinning does not accept a revision or deploy future edits. See [Hephaestus](hephaestus.md) for storage, provisioning and maintenance.

## Development, releases and validation

Use `backend/tests`, `scripts/tests`, and `frontend`'s test/build commands for deterministic validation. Modern evaluators live under `backend/evals/run_engine_*.py`; held-out assertions stay outside builder workspaces. Keep local logs, screenshots, private traces and generated reports under ignored `agent-output/` or `docs/evaluations/`.

Prepare worker releases with `scripts/hephaestus/install.py` from a complete source checkout and application releases with `scripts/staged_release.py`. The worker uses `engine-runtime.lock.txt`; document support uses its separate lock. Do not install dependencies into shared live environments. Drain operations and block dispatch during activation, activate worker then matching application, and only then stop/disable the retired worker. Retain the current release and a verified rollback, including referenced image dependencies.

Check `/api/health`, modern worker health, served asset hashes, settings save/reload, authenticated previews and browser console output. A release must pass disposable build/follow-up, uploaded edit/Q&A, cancellation/recovery and shared runtime checks. Failed release gates retain or restore production and preserve diagnostics. Focused passing workflows do not claim broad qualification of every language or arbitrary natural-language behavior.

### User-prompt release qualification

Accepted-source transfer has its own durable pre-dispatch ledger: at most three admission rounds within the configured preparation time. Only the explicit maintenance rejection or a transport interruption can retry; each round probes the exact checksum and opens a fresh stream. A lost successful response can be resolved by one final receipt probe. Authorization, archive/size validation and storage failures stop immediately. Resume and new execution allowance preserve this ledger, model usage and application repairs. A terminal source/preparation limit counts as a failed user workflow in qualification.

GUI startup charges broker admission to preparation and starts its 20-second readiness window once the namespace child's control socket exists. Failure to start is environment evidence, not an application interaction failure. Command admission keeps an authoritative 10 GiB reserve check; full-VM discovery and cache reclamation belong to scheduled maintenance, outside individual command launches.

Run the scored workflows from a browser against an isolated application and worker with a disposable profile. The candidate needs its own database, worker ledger, quota-scoped scratch directory and matching controller browser path. Install the built-in Daedalus agent in that profile and preserve both effective model roles. Record release, SDK, image, tokenizer, model and settings identities before sending prompts.

`backend/evals/run_engine_chat.py` chooses Daedalus in the normal composer, sends the frozen messages and follows the delivered project card. It records the saved message, workflow, parent revision, downloaded digest, private observations and exact acceptance. The controller-only runner remains available for diagnosis. Historical scores retain their original grader and prompt set.

The browser runner takes `--environment candidate.json --output <private-run-dir> --cohort development|pocket|classic|everyday|breadth`. Its trusted environment file supplies `isolated_candidate: true`, the candidate `url`, `model`, `model_digest`, `job_seconds` and `judge_command`. For release comparisons include `release_id`, `chat_model`, `chat_digest`, the image `profiles`, frozen `settings`, `judge_sha256`, `judge_bank_sha256`, `sdk_version`, `limits` and `cache_policy`. Keep private run data under ignored `agent-output/`. Give the downloaded program only its declared runtime and project files.

Set `judge_command` to an object with an `argv` list to stream a one-line JSON request followed by the archive to a separate Linux judge. Use the installed evaluation interpreter with `backend/evals/engine_judge.py --store <private-store> --settings <frozen-settings>`. Mark that dedicated store with a `qualification-judge` file and configure the normal Docker broker, scoped scratch, controller browser path and test-store role. A trusted transport wrapper may instead receive a JSON request with a local archive path and return the same verdict. The judge runs independently of the coding conversation. Archive hashes must match before extraction.

Prompt-bank version 2 explicitly states numeric-only output where the private assertions require it. The grader retains v1 wording for historical reports; scores from different bank versions are not interchangeable. Before opening the browser, the runner requests `{"kind":"identity"}` from the private judge and validates its receipt against the frozen environment. Record `prompt_bank_version`, `judge_identity`, `judge_adapter_path` and `judge_adapter_sha256`; derive judge hashes from the receipt, not an older environment file. The receipt covers manifest-verified controller sources, prompt banks, runtime images, SDK and settings. Archive requests name its `judge_identity_sha256`, and returned verdicts must match. Application/worker release identity remains separate from judge and transport identity.

Run the model-free good/broken archive controls first, followed by the 18 fixed-settings protocol probes and eight development workflows. Run the recorded Pocket Tasks prompt and the classic cohort separately. For the everyday comparison, `run_engine_paired.py --previous prior.json --candidate candidate.json --output <paired-dir>` alternates arm order across the 16 cases and three repeats. A completed trial retains its first outcome. An infrastructure interruption preserves the submission journal. Only a development run can record an explicit browser-driver correction via `--resume-driver`.

Grade user runs with `scripts/qualify_engine_report.py report.json --require-composer`. The grader checks the roster, raw observations, artifacts, revisions and user turn relationships, and exits nonzero on a failed gate. `--compare prior-report.json` additionally checks the paired improvement target. A score from deliberately constructed controls or language-runtime smoke tests belongs in its own evidence section.
