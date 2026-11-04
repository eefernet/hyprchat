# Daedalus operating guide

Daedalus uses OpenHands SDK/tools **1.59.0** on the Hephaestus Docker VM. One SDK conversation handles inspection, planning, implementation and repairs. HyprChat owns durable jobs, budgets, cancellation, independent verification, immutable revisions, artifacts and explicit owner acceptance. This retirement does not change Qwen3.6 or its inference settings and is not a full model qualification.

## Models and settings

**Daedalus chat model** routes requests and explains status. **Coding model** handles the implementation conversation, including its plan and repairs. Optional Project Q&A and visual-review model overrides remain. Context, output, reasoning, compaction, command and job allowances live in `context_policy.py` and Settings.

On upgrade, settings version 2 preserves the effective Builder override as the Coding model, falling back to the previous Coding model. The Planning model keeps its selected value under the chat label. The original settings are retained in the private `settings.before-daedalus-modern.json` beside `settings.json`. Stale clients cannot enable retired engines or save obsolete role selectors. Reload Settings after upgrading.

## Builds, uploads and questions

Request a new project or upload source and describe an edit. Uploading alone does not authorize editing. Follow-ups use the working candidate by default. Project Q&A runs on the modern worker with read-only repository inspection, source citations, normal budgets and cancellation; it does not dispatch a coding action.

The card shows Plan → Build → Check → Deliver, a structured agent-reported plan, current file activity and expandable history. A completed checklist is not verification. Unfinished items remain “Not updated.” The Files & logs browser shows working files; delivered checks belong to the exact immutable revision.

Older jobs and their recorded evidence remain readable. Retired engines cannot resume or dispatch operations. Download retained source and upload it into a new modern project to continue; old conversations, counters and evidence are never reinterpreted as modern jobs.

## Verification and recovery

Independent checks build and launch a fresh revision copy with declared dependencies. Real browser/desktop interactions, executable assertions, requested deliverables, regression test identities and protected source govern evidence. Project-authored tests and source inspection alone cannot establish arbitrary requested behavior.

UI expectations are frozen from original user wording for both new builds and follow-ups. Parser version 3 applies the same narrow clearing/exact-value rules from the first build; historical evidence remains unchanged. A new follow-up inherits trustworthy original request history, including requirements missed by older initial-build contracts. Supported transitions need observed controls and a meaningful different starting state. Unsupported, ambiguous or unexercised requirements remain unverified. UI manifests cannot redefine these expectations. Automatic UI-flow preflight shares validation/replay with independent verification, but its diagnostic results never add delivered passing checks.

Finish requests verification. The existing conversation receives specific feedback, with at most two application repairs per user request. One bounded pre-dispatch regeneration handles eligible generation failures without replaying dispatched actions. Infrastructure preparation has a separate durable attempt allowance and never spends an application repair. Persistent infrastructure failure parks saved work.

Resume preserves consumed calls, execution time, repair counts and conversation state. Explicit additional allowance grants calls/time only; it cannot reset repairs. A terminal generation failure may deliver a verified checkpoint as an incomplete review candidate. Historical checks do not become current evidence.

Passing check rows, advisory rows, failures, skipped checks and unverified criteria are reported separately. Test-case totals are distinct from check-row counts. A candidate can be delivered despite failed or unverified requirements. **Accept promotes only the exact reviewed revision** and never upgrades verification. HyprChat retains the delivered bundle; Hephaestus retains working source, revisions and application data until explicitly reset.

## Runtime and previews

The trusted broker runs project commands in scoped containers without a Docker socket, privileged mode or host networking. Dependencies use private writable preparation generations, leases and quota-aware publication. Python environments are created at their final path; compatible public downloads and browser binaries can be reused read-only. Matching Playwright binaries are validated against the declared package version. Downloads must use portable setup commands, not Hephaestus executable paths.

Temporary authenticated web/desktop previews expire after 15 minutes. Keep running pins the selected revision, runtime and application data; Stop retains that data. Pinning does not accept a revision or deploy future edits. See [Hephaestus](hephaestus.md) for storage, provisioning and maintenance.

## Development, releases and validation

Use `backend/tests`, `scripts/tests`, and `frontend`'s test/build commands for deterministic validation. Modern evaluators live under `backend/evals/run_engine_*.py`; held-out assertions stay outside builder workspaces. Keep local logs, screenshots, private traces and generated reports under ignored `agent-output/` or `docs/evaluations/`.

Prepare worker releases with `scripts/hephaestus/install.py` from a complete source checkout and application releases with `scripts/staged_release.py`. The worker uses `engine-runtime.lock.txt`; document support uses its separate lock. Do not install dependencies into shared live environments. Drain operations and block dispatch during activation, activate worker then matching application, and only then stop/disable the retired worker. Retain the current release and a verified rollback, including referenced image dependencies.

Check `/api/health`, modern worker health, served asset hashes, settings save/reload, authenticated previews and browser console output. A release must pass disposable build/follow-up, uploaded edit/Q&A, cancellation/recovery and shared runtime checks. Failed release gates retain or restore production and preserve diagnostics. Focused passing workflows do not claim broad qualification of every language or arbitrary natural-language behavior.
