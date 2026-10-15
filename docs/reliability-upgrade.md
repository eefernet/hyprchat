# Reliability upgrade operations

This guide covers durable storage, backup recovery, access controls, background
jobs, integrations, and deployment. Preserve the configured model/runtime
selections and Daedalus acceptance policies when maintaining these features.

Deployment helpers retain the previous installation and record activation state
and rollback snapshot paths in `release.json`. Recovery preserves file ownership
and checks readiness before clearing its journal. Review available disk space
before staging dependencies, and retain snapshots until recovery is no longer
needed.

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
another release. Qualify worker and document-worker environments before backend
activation when the change depends on their updated dependencies.

`staged_worker_release.py` prepares separate Python 3.13 environments and retains
the accepted environment. Document activation switches an atomic `current`
symlink; backend commands fall back to the legacy installation when absent.
Coding-worker activation uses a journaled systemd override and requires an
acceptance preflight confirming no active operations. Both roll back failed
acceptance. Include representative DOCX/PPTX/XLSX operations and PDF previews
in document-worker acceptance checks.

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
