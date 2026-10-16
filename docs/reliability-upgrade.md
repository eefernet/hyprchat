# Reliability upgrade operations — October 6, 2026

The approved program covers data protection, durable jobs, integration
correctness, and user interfaces. All four areas were implemented and qualified
together, then deployed as one cumulative application release after the worker
and document-worker environments. This was not four separate production rollouts.
The accepted model/runtime selections and Daedalus acceptance policies remain
unchanged. Changes are uncommitted.

## Deployment and validation record

For the October 6 deployment, the backend was `/opt/hyprchat/.releases/20261006-102219-a5c74667`.
The coding worker is `/opt/openhands-worker/.releases/20261006-030610-0a89b104`;
document-worker `current` points to
`/opt/hyprchat-documents/.releases/20261006-025458-0a850a32`.
Previous installations remain available. Backend `release.json` records the
committed activation and unique rollback database/vector-store snapshot paths.

- Frozen Linux qualification: 1,492 passed, 117 skipped. Final targeted delta:
  34 passed; subsequent root-owned rollback failure-injection checks: 4 passed.
  These are separate runs, not a combined full-suite result.
- Frontend: 62 tests passed and production build passed. Desktop (1440 × 1000)
  and mobile (390 × 844) fixture checks covered rendering, new interfaces,
  profile switching, context inspection, revisions, and explicit voice retry.
  Live HTTPS Jobs reload and Calendar checks passed at both sizes without
  browser or HTTP errors.
- Production-data clone migration and backup/restore verified 60 artifact hashes
  and a 4,428-file archive. Sixteen pre-existing unresolved references remain
  preserved for review. Live activation reverified all 60 migrated hashes.
- Staged SDK/API checks and real sandbox DOCX/PPTX/XLSX creation/PDF conversion
  passed. Paid-provider calls and external email/calendar writes used fixtures.
  Live bounded reachability/search checks passed for the existing local services.
- Frontend and document-worker audits reported no vulnerabilities. Backend and
  coding-worker advisories remain documented below; their audits are not clean.

The first backend acceptance attempt rejected a harness asset-URL assumption.
Rollback then exposed ownership loss in restored SQLite/Chroma copies. The old
service was recovered, ownership-preserving snapshot/restore was implemented
and tested as root, and the corrected activation passed. Recovery now verifies
readiness before clearing its journal; each attempt retains separate snapshots.

These checks do not establish long-duration production stability, arbitrary
document compatibility, or a new model qualification. Voice used a synthetic
recording fixture; physical iPhone/PWA and microphone behavior remain unverified.
About 1.35 GB remained free on the backend disk after activation: review retained
snapshots and originals deliberately before future dependency staging.

## Durable files and cleanup

New artifact publication copies and hashes content into
`data/artifacts/<sha256>/<original-storage-name>`. Database publication follows
verification. Startup backfills existing artifact references; IDs and URLs stay
stable. Original files remain in place. A missing file or recorded hash mismatch
is reported as unresolved; migration does not invent replacement content.

Automatic and manual sandbox cleanup preserve legacy and active outputs until
ownership can be resolved. This trades disk reclamation for avoiding data loss.
Monitor available space before migration, backup or deployment. Retained original
copies and unresolved files require a later reviewed cleanup, not an age cutoff.

## Backup and restore

Version 2 archives contain a consistent SQLite snapshot, a file inventory with
sizes and SHA-256 hashes, uploads, knowledge-base originals, tools, workflow
configuration, durable artifacts (including document previews), and legacy
sandbox outputs. Saved research evidence is in SQLite. Chroma remains derived
state and should be rebuilt after restore. Referenced content in configured
external storage is copied into the archive and its snapshot paths are rebased.
Already missing artifact IDs are reported in the manifest.

Provider/account credentials, sessions, connector headers/environment/arguments,
webhook tokens, and recognized configuration/preference secrets are removed from
the snapshot. Existing local profile passwords survive restore; protected
profiles absent locally require Main to reset their passwords. Restored schedules
are disabled for review before re-enabling external actions. Never distribute an
archive as public content: it still contains private chats and documents.

Restore staging checks archive paths/types, extraction limits, manifest hashes,
SQLite integrity and isolated schema migration before writing the pending marker. Version 1 archives
remain supported with explicit missing-content/hash warnings. Concurrent restore
uploads have separate temporary files; one pending restore must be cancelled
before another can replace it.

Startup applies the pending restore before database initialization. The
`.restore-journal.json` and `restore-rollback-*` trees protect the database and
all replaced file trees. An interrupted prepared activation rolls back on the
next startup; rollback failure stops startup instead of serving mixed data.
Do not remove the journal or rollback tree while recovery is pending. Rollback
copies remain available for operator review.

## Route access and profile changes

`backend/route_policy.json` assigns an exact method and registered route to
`profile`, `admin`, `public` or independently authenticated `webhook`. Unknown
API routes fail closed. New endpoints must update the registry and ownership
checks together. Passwordless profiles remain available; Main retains its admin
role. The public `/api/health/live` endpoint reports only process availability.
Detailed health and profile-owned endpoints require the selected profile's
configured password/session. Shared environment provider keys belong to Main.

Profile changes cancel queued preference saves, requests and voice activity.
Delayed preference writes retain their original profile identity. Save failures
are visible. Failed transcription keeps the recording in memory for an explicit
retry; profile/navigation changes discard it.

Image Studio persists owner and ComfyUI prompt ID. Uncertain submissions are
retained, not automatically resubmitted. Image bytes are validated and atomically
published, with artifact records committed before upstream cleanup. A failed
transfer can be retried. Queue cancellation deletes only its prompt ID; a running
ComfyUI generation can remain cancelling until it settles because the global
interrupt endpoint could interrupt another user's job.

## Dependency locks and release preparation

Four compiled, hashed pip requirements files live beside their input manifests:
backend and test locks target Linux Python 3.12; worker and document-worker locks
target their deployed Linux Python 3.13. The frontend retains package-lock.json.
Regenerate with the command recorded at the top of each lock. The worker SDK
selection remains unchanged. See [uv's locking documentation](https://docs.astral.sh/uv/pip/compile/).

The dependency-changing path in deploy_monitor refuses live pip installation.
Use the staged backend helper on the server with a complete source tree and
built frontend:

```sh
python3 scripts/staged_release.py prepare --source /path/to/candidate
python3 scripts/staged_release.py activate --release /opt/hyprchat/.releases/RELEASE --acceptance /path/to/qualified-acceptance-check
```

Preparation uses the deployed Python interpreter, creates an isolated environment,
checks actual pip exit status and dependency consistency, compiles the candidate,
and tests database initialization against an isolated snapshot without starting
background services. Activation checks source hashes and active domain jobs,
retains the old service configuration, then requires both HTTP health and the
supplied acceptance command. The service is stopped before taking a rollback
database snapshot. Qualification startup suppresses scheduled tasks, email and
calendar polling, coder dispatch, image monitoring, and embedding-model pulls;
write requests return a retryable maintenance error. Failure restores the
previous service configuration, database and vector store, preserving ownership.
Original content files are retained.
After an interrupted deployment, run `staged_release.py recover` before preparing
another release. The worker and document-worker locks were installed and their
staged activation checks passed before this backend activation.

`staged_worker_release.py` prepares separate Python 3.13 environments and retains
the accepted environment. Document activation switches an atomic `current`
symlink; backend commands fall back to the legacy installation when absent.
Coding-worker activation uses a journaled systemd override and requires an
acceptance preflight confirming no active operations. Both roll back failed
acceptance. Synthetic DOCX/PPTX/XLSX creation and PDF previews have passed in
the staged document sandbox; this does not qualify arbitrary uploaded files.

## Durable jobs and external effects

`/api/jobs` and the existing event stream expose owner-scoped projections of
domain records. Domain controllers remain authoritative. Queued scheduled runs
and schedule advancement commit together; once-only runs survive restart and
overlapping recurring runs coalesce. One slot executes scheduled agents.
Research parents retain child identifiers and await terminal child results.
Cancellation remains pending while a child or external worker is unconfirmed.

Index submissions retain source hashes and require explicit retry after restart.
Notification deliveries record attempts; definite transient failures retry at
most four times, while ambiguous sends remain uncertain. Quiet hours are
checked immediately before sending. Review the external destination before
manually repeating an uncertain operation. Reminders, calendar synchronization,
email polling and notification delivery have independent bounded loops.

## Integration changes

Council members and synthesis use the provider adapter without replacing
explicit model choices. Cloud councils require opt-in and a positive spending
cap; unknown pricing blocks cloud dispatch. Fixtures cover paid providers.
Keyword and vector retrieval run together under the Settings-owned deadline
(three seconds by default). Index health records the embedding model and
version; citations include available Office/PDF locations. All search consumers
share two provider request slots; queue time consumes the existing deadline.

Calendar resources retain full iCalendar data, time zones and exceptions.
Occurrence expansion is bounded to 366 days and 2,000 results. Occurrence IDs
anchor reminders and individual edits. Reminder settings belong to the series.
Synchronization conflicts preserve both versions; conditional updates protect
local edits made during network requests. Email appointments require proposal
confirmation, and rejected proposals remain deduplicated. Invalid triage output
is retryable failure, not a normal-priority classification.

MCP uses the official Python SDK with reusable stdio, SSE and streamable HTTP
sessions, discovery pagination, cancellation and explicit reconnect. Destination
checks apply to redirects; cross-origin redirects are refused. Responses and
stdio lines are bounded to 8 MB. A failed tool call is never replayed silently.

## Saved work and interfaces

Jobs shows persisted status, progress, outputs and supported recovery actions.
Library search uses owner-scoped SQLite FTS for chats, notes, research and
artifacts, including type, workspace and date filters. Main can rebuild it.
Per-message context records capture selected settings, memories, retrieval,
search and tools during execution, excluding hidden reasoning and credentials.
Historical responses without capture explicitly show unavailable information.

Research checkpoints retain settings, immutable evidence versions and drafts.
Interrupted work resumes from a compatible collection/writing checkpoint;
older reports without checkpoints require a fresh rerun. Saved-evidence
questions do not refresh sources. Version comparisons retain old drafts.
Office comparisons show bounded text/structure differences and available
previews; restoring creates a new revision and rejects stale heads. Batch
exports produce per-file results and a bundle while preserving child jobs.

The Jobs operations view is Main-only and separates readiness, recent work,
queue age, retry counts, disk pressure and dependency versions. Panel errors
remain visible with a retry action. Profile changes abort requests, preference
saves and voice activity; failed transcriptions require explicit retry.

The current ChromaDB audit reports server authorization/code-injection advisories
without a published fixed version. HyprChat uses embedded `PersistentClient`,
controller-selected collection names and fixed metadata; it does not expose the
Chroma server API or accept embedding-function configuration. Keep the audit
findings visible and recheck applicability/version availability before the next
deployment.

The retained LiteLLM 1.82.0 worker dependency also has outstanding advisories.
The OpenHands/Aider runtime selection was not promoted. Reviewed findings concern
LiteLLM proxy/management/authentication/enterprise-hook surfaces; the worker runs
the SDK and does not serve the LiteLLM proxy. Compatible security remediation
still requires qualification; reproducible locks are not a clean audit. The
document-worker XML parser is updated to lxml 6.1.0.


## Model-aware effort and answer delivery

The composer retains Blurt, Ponder, Forge and Galaxy Brain. For Ollama models
advertising multiple reasoning levels, these select the actual `/api/show`
`thinking.values` in order. Three levels omit Forge; two retain Blurt and Galaxy
Brain; additional levels use their native names. The model's default is labeled
separately from the lowest level. Explicit native effort takes precedence over
persona/global boolean thinking controls and runs no self-review passes.

Models with boolean-only thinking, no thinking, unknown provider capabilities,
or unavailable metadata keep the existing 0/1/2/3 self-review ladder and existing
thinking on/off controls. Cloud capability entries are exact, documented model
families and dated snapshots in `backend/reasoning.py`; unlisted/custom models
remain on self-review. Auto routing resolves capabilities after choosing the
model and uses a native model's default. Legacy API callers without
`effort_mode: "auto"` retain their existing behavior.

Self-review keeps the last completed answer visible while buffering its
replacement. A completed shorter correction is allowed; empty, truncated,
failed, or interrupted reviews retain the preceding answer. Only explicitly
allowlisted read-only tools can execute during review. Context Inspector shows
review history, effective effort, and reported usage for each model call; a
previous answer can be restored after generation finishes.

The server commits delivery state before sending `done`; browser completion
PATCHes merge presentation metadata without replacing the canonical answer.
Disconnect saves partial initial output or the last completed review. Cloud
reasoning/tool continuity is retained in memory using provider-native state,
including OpenAI encrypted reasoning and Anthropic signed thinking blocks.

Sandbox cleanup now previews retained and reclaimable bytes. It reclaims only
explicitly released staging leases whose contents still match and which have no
saved references. Active/interrupted leases, legacy outputs, symlinks and durable
files remain. The initial lease producer is Image Studio's transfer staging;
other output producers remain conservatively retained. The old image-purge
journal helper is retired; see [image setup](image-generation-setup.md).

Local development uses `.python-version` (3.12) and `.venv`; the test runner
refuses a different interpreter and never installs into system Python. Linux
Daedalus execution tests still require the Linux isolation environment; matching
Python alone does not supply bubblewrap or network namespaces on macOS.


Cloud registry references: [OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning),
[OpenAI model guide](https://developers.openai.com/api/docs/guides/latest-model), and
[Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort).
Registry entries are conservative; new model IDs require verification rather than
prefix-based guesses. No paid-provider inference was used to validate this change.
