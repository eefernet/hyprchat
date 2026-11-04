# Shared runtime preparation repair

This focused repair keeps OpenHands SDK/tools 1.59.0, Qwen3.6, the 65,536-token
context, 16,384-token output allowance, temperature 0.2, job budgets, isolation,
and explicit revision acceptance. It is not a new qualification campaign.

## Runtime behavior

- One mount policy validates preparation, coding, verification, and preview
  grants. An exact private dependency directory is writable; published shared
  dependencies stay read-only. A read-only child under a required writable
  directory is rejected before launch. Preparation probes writes inside Docker.
- Python virtual environments are created at their final operation-owned path.
  Package downloads are reusable; installed environments are not relocated.
  Editable installs therefore bind to the current execution copy.
- Source-independent Node dependencies can be published once and cloned into
  private writable trees. Copies use reflinks when available, never writable
  hard links. Workspace links and Composer autoloaders remain source-bound.
  Other supported package managers receive explicit private cache paths.
- Readiness is recorded only after installation and matching Playwright browser
  preparation. Cache identities include dependency inputs and the runtime image.
  XFS quota boundaries use the validated copy/atomic-publication fallback.
- Project tests use their declared Playwright browser cache. The controller's
  interaction checks and previews use its separately pinned browser cache.
  A missing controller browser is an environment failure, not a code repair.
- Infrastructure preparation failures have a durable maximum of two attempts per
  unchanged fingerprint, within the preparation allowance. Interrupted attempts
  remain charged. They do not spend either application-repair cycle.
- Dependency classification is shared by inventories, activity, snapshots,
  verification copies, and source archives. Corroborated virtual environments
  and build outputs are excluded; conflicts with authored/protected files fail
  explicitly.
- Literal deliverable paths are frozen from the user's request and inherited by
  follow-ups. `todo_app/ package with cli.py` means `todo_app/cli.py`. Ambiguous
  locations remain unverified. The noun “package” does not invent a root file.
- A source-backed Node `start` script using `listen` and `process.env.PORT` is
  launched as a managed service even without a web framework dependency.
  Ordinary Node CLI scripts retain finite-command behavior.

Python's location-dependent environments and Docker's obscuring mount behavior
are documented in the [Python venv documentation](https://docs.python.org/3/library/venv.html)
and [Docker bind-mount documentation](https://docs.docker.com/engine/storage/bind-mounts/).

## Verification and presentation

The Check stage uses evidence for the current revision. Failed, environment-
blocked, and unverified states do not become passed when a candidate is delivered
or explicitly accepted. Deliver records availability separately. Plans remain
agent-reported, and dependency installation is separate from source-file activity.

Changing an existing test file still requires review, even when every executed
check passes. The focused Python follow-up added four tests without changing the
26 original tests. Independent replay of those originals against the edited
revision passed; explicit acceptance retained the controller's unverified status.

## Evidence and release

Evidence is retained in the ignored directory
`agent-output/daedalus-shared-runtime-20261103/`. The original failed Python
project, Pocket Ledger evidence, previous cohorts, and accepted user revisions
are outside this disposable validation cohort.

At the source-freeze gate, record the final workflow, language compatibility,
preview, deployment, and rollback results in `release-results.json` there.
Keep initial failures and staging interruptions alongside successful reruns.
Do not present these focused results as arbitrary natural-language verification
or as a replacement for the separate full qualification gate.

### November 3 validation result

Candidate `1907a928b272` is frozen separately and running on the isolated server
at port 8002. Production remains on `progress-e8eaf4e8176d`; activation was not
performed because the fresh web workflow failed its release gate.

The final focused suites passed 107 backend tests (one skipped), 28 UI-contract
tests, and 169 frontend tests. Live checks passed all 16 runtime fixtures, 14
containment fixtures, eight publication fixtures, and 29 canonical language
smoke cases. The controller/project Playwright version mismatch was reproduced
before the fix and passed afterward, including failed-launch recovery.

The Python build and follow-up passed independent behavior checks and exact
acceptance tests on disposable projects. The follow-up retained its explicit
test-change review requirement. The fresh web candidate passed 18 of 19
controller checks, but its generated test suite still visited hardcoded port
3001 while skipping its fallback server when `DAEDALUS_APP_URL` was present.
All four project browser assertions failed after both allowed repairs. Separate
browser checks proved add, blank-input rejection, persistence, and Clear against
the downloaded revision; they did not replace the recorded failed checks.

The web follow-up and production cutover are pending. Its candidate remains
downloadable and previewable, with Check shown as failed and Deliver shown as
delivered. The HTTP validation preview displays noVNC's secure-context warning;
the production HTTPS preview was not changed. Existing acceptance pointers for
171 projects, 69 artifacts, and original failed-job records were checked and
preserved. Full evidence, interrupted staging attempts, and the deployment gate
decision are in `release-results.json` and the adjacent reports.

### Web endpoint repair follow-up

Modern verification now returns a specific failure packet when a failed test
reports a refused loopback URL outside the running controller service endpoints.
It identifies the available `DAEDALUS_*_URL` variables and explains that the test
must use one resolved base URL for every navigation and request, as well as its
fallback-server decision. The controller does not rewrite tests, extend repair
allowances, or treat diagnostic log text as passing evidence. Normal assertion
failures and unavailable controller services retain their original classification.

The unchanged failed web archive reproduced the connection failure in real
Docker execution and received the new feedback. Changing only base-URL selection
in a disposable copy passed the same four browser tests with controller-observed
service traffic. Eleven focused regression cases also passed. Original evidence
and the failed release record are retained under `web-endpoint-before/`.

The next web build passed 18 controller checks and independent behavior checks.
Its follow-up exposed an unrelated target-resolution defect: an input caption
was counted alongside an explicitly identified result display. Result matching
now prefers an observed result/output identity before falling back to generic
labels, excluding editable and activatable controls. Multiple result identities
remain ambiguous, and expected text is never used to choose a target. The first
follow-up's unverified evidence remains under `web-result-before/`; the final
release gate uses a separate fresh web cohort after this correction.

### Final web verification and production deployment

The fresh web cohort also exposed a setup-replay boundary bug: a CSS-selected
Clear action was replayed as setup, leaving the result already correct before
the controller tested the transition. Setup now stops before an activation
whose selector matches the required action observed in the current session.
Duplicate action targets remain ambiguous. A no-op starting from the expected
value still cannot pass. The original unverified follow-up and frozen source
remain under `web-replay-before/`.

The fresh web build passed all 18 controller checks and independent add,
validation, persistence, and Clear checks. Its follow-up implemented the requested
“Ready” result. After the replay correction, a separate disposable workflow
verified that unchanged implementation: 14 controller checks passed, and the
contract observed an empty result become `Ready` after one Clear activation.
The build used 41 model calls and two repairs; the final follow-up recheck used
eight calls and no repairs. Independent checks and exact-revision acceptance
passed for both. Review confirmed that all earlier assertions and UI steps were
retained. Project Playwright 1.48.2 used its matching Chromium revision 1140,
separate from the controller browser.

The final backend suites passed 163 tests with one skip, including 56 UI-contract
and endpoint regressions. Live fault fixtures returned passed for the correct
result, failed for `0.00`, and unverified for duplicate targets and an already-
correct no-op: zero false passes. The earlier successful frontend, runtime,
containment, publication, language, and Python checks remain part of this focused
release record; earlier failed and unverified scores were not rewritten.

Release `b6406d76a001` was deployed on November 3 after the focused gate and drain
checks passed: first the Hephaestus worker, then the matching HyprChat backend and
frontend. The worker is at
`/srv/hephaestus/releases/shared-runtime-b6406d76a001`; the server is at
`/opt/hyprchat/.releases/20261103-shared-runtime-b6406d76a001`.
All three worker endpoints and server health passed. The served frontend matched
the tested hashes. Scoped Python, JavaScript, and Java commands ran successfully;
31 language API keys were available and the data disk had 31.46 GiB free.

A new production modern-engine job passed four checks using 16 calls and no
repairs. Through the authenticated HTTPS preview, clicking Increment changed
Count from 0 to 1. Browser warnings/errors and service journal errors were zero;
the temporary preview was closed afterward. Its candidate remains available for
owner review. Qwen3.6, its digest, inference settings, and job allowances are
unchanged. Existing acceptance pointers, artifacts, and failed-job evidence were
preserved; no original failed user job was resumed.

Rollback records are retained at
`/srv/hephaestus/control/shared-runtime-rollbacks/1793745193280481917` and
`/opt/hyprchat/.release-rollbacks/shared-runtime-1793745195537746877`.
The final `release-results.json`, activation reports, production checks, and
`production-preview.jpg` are in the evidence directory above. This deployment
closes the focused repair gate; it does not constitute full engine qualification.
