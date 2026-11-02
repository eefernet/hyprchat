# OpenHands 1.59 engine

This is an isolated engine addition. Existing jobs retain their original engine
and worker. New jobs select `openhands159` explicitly, or inherit it from their
source project. The default remains `legacy` until qualification succeeds.

**October 29, 2026: qualification failed.** The isolated implementation and
development scenarios work, but this engine/model combination is not ready for
rollout. Production remains on its existing OpenHands 1.11.4 worker.

## Boundaries

- HyprChat owns durable job state, allowances, cancellation, immutable revisions,
  verification, artifact delivery and owner acceptance.
- The worker uses OpenHands SDK and tools **1.59.0** through public `Conversation`,
  tool factory, hook and persistence interfaces. Inspection, planning, coding and
  repairs share one persisted SDK conversation within a job.
- A stop hook gives concrete command failures and permits at most two repairs.
  The durable controller shares that repair allowance. Completion always leads
  to fresh verification; SDK events cannot accept a project.
- Working and accepted revisions remain separate. Follow-ups start with the
  working candidate. Accept promotes the exact delivered revision. Changing
  existing tests is visible and requires owner review; losing named passing
  tests remains a regression.
- Exhausted calls can deliver an explicitly unverified checkpoint. Empty tests,
  printed pass messages, stale evidence and compile-only results cannot become
  “Checks passed.” GUI unit tests alone cannot establish interface behavior.
- All generated commands and applications run in the existing Linux process,
  filesystem and network sandbox. Evaluator assertions remain outside it.

## Dependencies and preparation

The tested Linux Python 3.13 environment is pinned in
`backend/engine-requirements.lock.txt`. `backend/engine-requirements.txt` records
the direct requirements. Keep `backend/worker-requirements.txt` and the old
virtual environment for legacy jobs.

Prepare a fresh directory on Codebox from a source bundle:

```sh
python3 scripts/prepare_engine_release.py \
  --source-root /path/to/source-bundle \
  --destination /opt/daedalus-openhands159-RELEASE \
  --python /usr/local/bin/python3.13
```

The script installs the lock, runs `pip check`, imports the worker, preserves
existing Playwright browser versions, records source hashes and writes a
service template. It does not activate a service or change production.

Run the candidate under its systemd unit, including during qualification. The
worker and all its descendants share a 4 GiB memory ceiling, a 3 GiB memory
pressure threshold and a 512-task limit. An OOM kills the candidate unit, which
then restarts; the legacy worker is outside that unit. These limits protect the
6 GiB Codebox container from generated recursive tests. The October qualification
initially used an unbounded detached worker; a recursive Go test exposed that
gap. Its failure is retained, and subsequent trials record the containment change.

Linux GUI requirements include `xvfb`, `x11-utils`, `x11vnc`, `openbox`,
`dbus-x11`, `at-spi2-core`, `python3-pyatspi`, `python3-pyqt6`,
`gir1.2-gtk-4.0`, `libatk-adaptor` and `libatk-wrapper-java`, plus the existing
language toolchains and bubblewrap. Playwright needs its matching Chromium and
system libraries. Java Swing uses the ATK bridge; Qt and GTK use AT-SPI.

Worker preparation records the model digest, template hash, stop parameters,
capabilities, Settings snapshot and GPU residency. Model loading has a separate
bounded preparation allowance. Active execution and compaction calls consume
the job allowance. Context size and output limits come from `context_policy.py`.

The current evaluation model is `qwen3.6-35b-a3b-gguf:latest`, downloaded from
Unsloth Q4_K_M, digest
`793390598026bea9fd308ed376b955c191ff663648a96493c40c6471a52c9d19`.
The downloaded template contains unusual stop strings, including `<think>`.
The model has not been overwritten. Native tool continuation worked with
reasoning enabled; the focused reasoning-disabled probe skipped the required
tool. Evaluation explicitly records reasoning on for builder and compaction.
This does not silently change production Settings.

The SDK's reasoning-model capability fallback can remove sampling parameters.
The local adapter uses the public `supports_sampling_params` capability override
so its configured temperature 0.2 reaches Ollama. Each supervisor model-call
event records the actual forwarded options and reasoning setting. A transport
fixture checks the emitted request, rather than only inspecting the LLM config.

## Verification and qualification

Development cases cover Python CLI, Maven Java, browser interaction and Qt
desktop interaction, each with a follow-up and exact-revision acceptance.
These are separate from the held-out cases in `backend/evals/engine_cases.py`.

Run the campaign on the isolated worker host, using a sanitized Settings export
containing context policy keys and `context_compaction` (no credentials):

```sh
systemd-run --unit=daedalus-evaluation \
  --working-directory=/opt/daedalus-openhands159-RELEASE \
  --property=MemoryHigh=1536M --property=MemoryMax=2G \
  --property=TasksMax=256 --property=OOMPolicy=kill \
  --setenv=DAEDALUS_STATE_DIR=/opt/daedalus-openhands159-RELEASE/state \
  --setenv=DAEDALUS_PROJECTS_DIR=/opt/daedalus-openhands159-RELEASE/projects \
  /opt/daedalus-openhands159-RELEASE/venv/bin/python \
  /opt/daedalus-openhands159-RELEASE/backend/evals/run_engine_qualification.py \
  --output /opt/daedalus-openhands159-RELEASE/qualification-1 \
  --settings /path/to/sanitized-settings.json --repeats 3
```

The evaluator also executes untrusted application code, so its separate unit
limits that process tree to 2 GiB and 256 tasks. These limits complement the
worker's limits. The campaign serializes worker execution and held-out checks.

The runner serializes inference, saves progress, pins source hashes and model
digest, and resumes without replaying finished trials. A changed engine,
evaluator or model requires a new cohort. Failed held-out assertions are never
returned to the coding agent. Each trial includes initial delivery, independent
checks, exact acceptance, follow-up behavior and regression checks.

The gate is at least 20/24 core, 10/12 web and 10/12 desktop trials, plus no false
“Checks passed” results in verification fault tests. Lifecycle tests separately
exercise inference/command cancellation, HTTP worker restart, event replay,
model-call exhaustion and Settings-controlled compaction. Deterministic
transport fixtures in those tests are not evidence of model quality.

For rollout, `scripts/qualify_engine_report.py` also requires all project checks
to pass on both delivered revisions, no exhausted allowance, and confirmed
exact acceptance. A candidate whose independent behavior passes while its own
declared tests fail is useful development evidence, but does not count as a
qualified success. This additional conservative gate leaves the frozen campaign
results and every failure intact.

Useful commands on the isolated host:

```sh
DAEDALUS_LIVE_GUI=1 DAEDALUS_TEST_WORKER=http://127.0.0.1:8587 \
  venv/bin/python -m pytest backend/tests/test_openhands159_sdk.py \
  backend/tests/test_daedalus_gui_live.py backend/tests/test_engine_worker_live.py
```

The live worker tests intentionally restart the candidate HTTP process through
its systemd service when managed. They verify the service's PID, directory and
restart semantics before acting; they must not start a competing detached worker.
Run them before the serialized model campaign. Evidence is retained under ignored
`agent-output/openhands159-upgrade/` locally and the candidate release remotely.

## October qualification results

The final campaign contains 48 trials: eight core, four web and four desktop
cases, repeated three times. A successful trial requires both its initial
request and its follow-up. Every trial made real inference calls; no unstarted
storage failure remains substituted for a model attempt.

| Family | Held-out behavior and acceptance | Full rollout checks | Required |
| --- | --- | --- | --- |
| Core | 20/24 (83.3%) | 20/24 (83.3%) | 20/24 |
| Web | 4/12 (33.3%) | 1/12 (8.3%) | 10/12 |
| Desktop | 8/12 (66.7%) | 6/12 (50.0%) | 10/12 |

The full rollout column additionally requires revision-bound project checks and
no exhausted allowance. A working application with broken or unverified declared
tests is not counted there. The four separate development scenarios—Python CLI,
Maven Java, web and Qt—passed initial behavior, follow-ups, project checks and
exact acceptance on all eight delivered revisions. The development Python
stderr assertion was corrected to require a nonempty error message rather than
one literal word; its original report is retained. Held-out assertions were not
changed during qualification or returned to the coding agent.

The frozen campaign used 65,536 context tokens, 4,096 output tokens, temperature
0.2, reasoning enabled, 120 calls and 3,600 active seconds per job, with SDK
compaction enabled at 75%. The configured command allowance was 600 seconds
for controller checks and stop-hook verification. **SDK terminal actions retain
the SDK's wait/yield semantics under the overall job deadline; the per-command
Settings ceiling is not yet enforced on that tool.** Do not interpret its
timeout argument as a verified hard kill deadline.

Final verification matched all 346 frozen Python source hashes, all 195 locked
dependency versions and the exact model digest above. No job operations remained
active. A subsequent change only made the live restart test service-aware; it
does not alter the evaluated engine implementation. That test passed against the
guarded worker (one command execution across restart and replay).

Observed failures span several layers:

- Generated programs: wrong Rust output layout, a missing requested Java JAR,
  invalid Swing accessibility methods, an invalid GTK enum, and missing or
  mismatched web follow-up behavior.
- Transport: four trials ended on Ollama HTTP 500 responses reporting incomplete
  JSON arguments for `file_editor` or `think`. A separate probe observed an
  analogous failure at the output-token ceiling; that does not establish the
  cause of every HTTP 500.
- Engine context handling: two trials hit the supervisor's byte-based input
  estimate before SDK compaction. In the inspected GTK case, the last actual
  prompt had 37,648 tokens while the SDK compaction threshold was 43,622; the
  exact rejected request's tokenizer count was not recorded.
- Browser preparation and verification: a generated Node project resolved
  Playwright 1.64.1, requiring Chromium revision 1248, while the worker's shared
  cache contained revisions 1208, 1234 and 1243. Some tests also referenced a
  temporary `venv` absent from the delivered archive. File-URL browser tests can
  run without the current provenance instrumentation recognizing their
  application dependency.
- Evaluator limitation: `qt-list-3` had a valid input and a label both named
  `Item`. The frozen name-only fill selector was ambiguous. This is not a
  demonstrated application defect, and its failure was not regraded. Future
  evaluation must select the editable control by role. GTK launch timeouts also
  need to surface the retained application traceback directly.
- Capacity: two todo trials reached storage limits after real inference and
  remain failures. One generated Go test recursively launched itself and
  exhausted Codebox before resource containment was installed.

The Go incident temporarily made Codebox unresponsive. Recovery terminated only
the identified candidate sandbox process namespace through Proxmox; existing
services recovered. The candidate and evaluator subsequently received the
resource limits documented above. Controlled process-limit and OOM checks
passed, as did restart during a live SDK command with exactly one execution.
No candidate OOM kills occurred during the remaining campaign.

Runtime conditions changed after that incident (worker containment after 22
finished trials; evaluator containment at 24). Completed-trial disposable caches
were also reclaimed, and zero-call storage blocks were resumed with consumed
budgets preserved. The user's pause/resume retained its existing SDK conversation
and consumed allowance. All original failures and interventions remain in the
report. These are diagnostic qualification results, not a uniform performance
comparison between models or agent frameworks.

Focused lifecycle tests covered inference/command cancellation, restart/replay,
model-call exhaustion and compaction. Injected printed-success, empty-suite,
mock-only, failing-assertion and build-only checks never produced a false
“Checks passed.” Real GUI tests exercised Qt, GTK, Swing and web controls;
frontend-to-noVNC checks exercised web and Qt interaction and owner/revision
access boundaries. Frontend tests (153) and the production build passed.
The broader Linux regression run had 685 passes, 18 skips and an app-only
ChromaDB import limitation in the worker environment; the corresponding chat
test passed locally. The 29 Linux-dependent local failures reproduced on the
pre-change baseline.

Before another cohort, align SDK and supervisor token accounting, enforce the
terminal command ceiling, provision browsers matching project dependency locks,
improve cache lifecycle and GUI selectors/launch diagnostics, and isolate the
tool-JSON failures with controlled transport probes. Keep these results intact
and use a new cohort for changed code, settings or assertions.

Local evidence includes `qualification-3-final-report.json`,
`qualification-3-final-grade.json`, `qualification-final-verification.json`,
the worker/evaluator journals and the resource-recovery receipts under
`agent-output/openhands159-upgrade/`. The candidate release is retained at
`/opt/daedalus-openhands159-20261028`. No production rollout or obsolete-workflow
removal is justified by these results. The idle qualification worker was stopped
after testing; its release, state and artifacts remain intact. The existing
1.11.4 worker and Codebox returned healthy responses, as did HyprChat.

## Previews

Delivered modern-engine revisions offer an Application preview tab. A session
starts from that exact archive in a private Xvfb/DBus environment and expires
after 15 minutes. Web interaction uses Playwright; desktop interaction uses
AT-SPI. The browser receives noVNC pixels through an owner-checked WebSocket,
not generated HTML on the HyprChat origin. Worker capabilities stay server-side.
Only three sessions may run at once; starting another for a job closes its
previous session. A closed or expired preview can be relaunched.

AT-SPI accepting an activation does not mean the button callback has completed.
The adapter waits for toolkit activation timers and the pressed state before
returning. This prevents repeated actions coalescing in
[Qt animateClick](https://doc.qt.io/qt-6/qabstractbutton.html#animateClick)
and [GTK4 button activation](https://github.com/GNOME/gtk/blob/4.8.3/gtk/gtkbutton.c#L79).
Live regression tests perform five consecutive activations in Qt, GTK, Swing and
the browser without inserting delays or retrying clicks in the test driver.

## Rollout and rollback

1. Keep the old worker release and service running. Install the candidate as a
   separate service and state directory. Qualification binds it to loopback.
2. After all gates pass, bind its distinct port to the trusted worker interface,
   verify `/health`, and deploy the compatible HyprChat routing/UI additions.
   Set `DAEDALUS_ENGINE_URL` on HyprChat to the new worker URL.
3. Enable the new engine only for opted-in jobs. Existing jobs continue using
   their persisted engine. Verify delivered artifacts, follow-ups and previews
   through the production backend before considering a default change.
4. To roll back selection, return `daedalus_engine` to `legacy`. Keep the modern
   service available for existing modern jobs and their snapshots. Do not route
   those jobs into the old SDK or replace either service's virtual environment.
5. Remove obsolete workflows only after replacement coverage and qualification
   are demonstrated. Failed qualification leaves the current deployment intact.

No commits, staging or pushes are part of this upgrade.

## Controller-owned UI follow-ups (2026-10-31, release gates failed)

The next isolated candidate lives at
`/opt/daedalus-openhands159-ui-contract-20261031`. Production and the prior
release, accepted revisions, and 7/8 development result are preserved. The
candidate reuses the previous release's unchanged, lock-matching SDK/tools
1.59.0 virtual environment; source, job state and project directories are
separate. Model digest, tokenizer, 65,536 context, 16,384 output, temperature
0.2 and job/resource allowances remain fixed.

New modern jobs persist `ui_contract_version: 1` and a controller-owned
`ui_contract` in job JSON before dispatch. Old jobs without that version keep
their existing behavior. The narrow grammar supports an explicitly named
button clearing inputs/results, or setting a target to a literal value.
"Reset" alone, negation, unspecified defaults and other unsupported wording
remain unverified. Initial builds retain their existing checks; this added
contract covers follow-up requests. It is not a general English-to-test system.

Expected values come only from the quoted request. Confirmed interaction flows
are inherited; successful controller-observed flows can supply launch/setup
recipes, but their expected values cannot weaken the contract. Source targets
are bound before coding when stable IDs or accessible identities are available.
Each check launches a fresh revision, establishes a different starting state,
activates the named control once, and reads editable values separately from
displayed text. Missing/ambiguous targets and no-op starting states remain
unverified. Desktop node IDs are resolved per session. Explicit changes to the
same action/target supersede an older expectation with retained history.

Concrete failures enter the existing shared two-repair path. Unverified
requirements alone do not trigger repairs. `checks_ok` still describes executed
project checks; `checks_passed` is suppressed for failed/unverified UI criteria.
The job card shows requirement status, excerpt and observed failure reason.
Candidate delivery and exact-revision owner acceptance remain available.

Validation evidence is under
`agent-output/openhands159-ui-contract-20261031/`. Local checks passed 79 tests
(one Linux-only skip); frontend passed 155 tests and its build. Browser UI QA
verified requirement statuses/reasons, mobile layout and exact-revision
acceptance. The first Linux run passed 101 tests and exposed one GTK ambiguity:
button-child labels were considered output candidates, so the conservative
verifier returned unverified. After excluding interactive descendants, the
affected Linux gate passed **88/88**. Real served/file web, Qt, GTK and Swing
fixtures reject wrong results even when project-authored tests pass, and accept
only the observed empty or `Ready` value.

The frozen 16,384-output protocol run passed **18/18 on the first attempt**.
Full GPU residency at 65,536 context and the unchanged digest were confirmed.
The fresh development cohort's first web follow-up demonstrated
the intended repair: the controller observed empty inputs but a result still
equal to `22.00`, issued user-derived feedback, and then observed empty inputs
and result after one repair. Private behavior and exact acceptance passed.
Revision-bound before/after evidence is in `web-reset-repair-evidence.json`.
The first six complete workflows passed strict checks. During the second web
build, a real SDK GUI action exposed Chromium's Unix socket path limit under
the durable operation cache. The campaign paused with 40 calls, 589.86 seconds,
one repair and allowance 1 retained for that job. The modern sandbox now gives
its existing disk-backed temporary cache a short private path; legacy behavior
is unchanged. Two live regressions prove the actual SDK browser interaction and
cleanup, and that the short alias still writes to the owned disk cache.
The affected runtime gate then passed **65/65**. The repeated fixed-configuration
protocol run passed **17/18 on the first attempt, with zero recoveries**. One
native-tool probe returned the numerical answer without the required tool call;
the response terminated normally, so this is a model tool-use failure rather
than a malformed transport response. Both protocol reports are retained; the
earlier 18/18 is not substituted for this failed gate.

The same development cohort resumed solely to validate recovery from the
Chromium infrastructure interruption. Its prior six finished trials and consumed
budgets remained intact. The resumed web build finished with 52 total calls,
one repair and allowance 1; its follow-up also passed. This infrastructure
source change is recorded in the development history.

The completed cohort passed **7/8 strict workflows**:

| Family | Complete workflows passed |
| --- | ---: |
| Python | 2/2 |
| Java | 2/2 |
| Web | 2/2 |
| Qt | 1/2 |

The second Qt initial build had invalid interaction locators (object names and
`lineedit` did not match the accessible names/roles). Controller checks reported
missing targets and withheld desktop verification. During its second and final
repair, generation exhausted 16,384 output tokens and the single permitted
regeneration also exhausted that limit. The job stopped blocked at 46 total
calls, two repairs and allowance 1; it did not deliver a candidate or reach its
follow-up. This was output-generation exhaustion, not exhaustion of the
120-call job allowance. Workspace, conversation and diagnostic evidence remain
available in the isolated candidate release.

All six supported contract criteria in completed UI follow-ups passed with
observed values. Broader preservation wording remains explicitly unverified by
the UI contract, even where the independent development evaluator passed it.
The live served/file web, Qt, GTK and Swing fault fixtures had zero false passes
for the supported wrong-result cases. These results establish only the narrow
contract coverage described above, not arbitrary natural-language verification.

The old 7/8 cohort remains unchanged; this new 7/8 result is a separate report
with a different failure. Both current release gates failed: protocol 17/18 and
development 7/8 instead of 8/8. The prior-failure cohort and fresh 48-run
qualification campaign were not started. No production deployment or default
change was performed. Final inspection found no active evaluation operations,
362 candidate backend source files matching the local source, and the original
production worker healthy with PID 2773749 unchanged.

Final evidence: `final-results.json`, `final-runtime.json`,
`protocol-failure.json`, `qt-generation-diagnostics.json`,
`development-1-report.json`, and `web-reset-repair-evidence.json` under the
evidence directory above. No commits, staging, pushes or legacy-code removal
were performed.

## Reliability repair (2026-10-30, development gate failed)

The repair candidate is isolated at `/opt/daedalus-openhands159-repair-20261030`.
Production remains on the existing worker. The original strict scores above
remain the baseline; none of these new runs replaces an original trial.

The implementation and deterministic gates are complete, but the candidate is
**not qualified for rollout**. The final Linux regression suite passed 71 tests;
frontend tests passed 154 and its build succeeded. At 16,384 output tokens, the
completed development cohort passed seven of eight complete workflows:

| Development scenario | Strict successes |
| --- | --- |
| Python CLI, including follow-up | 2/2 |
| Java packaging, including follow-up | 2/2 |
| Interactive web, including follow-up | 1/2 |
| Qt desktop, including follow-up | 2/2 |

The failed web follow-up was a generated behavior defect: Reset set the result
to `0.00`, although the request required clearing it. Project-authored tests and
controller replay missed that requirement; the private evaluator rejected it.
The follow-up was not accepted, and the prior accepted revision remains intact.
This is separate from the verification fault fixtures, which passed. The
all-eight development gate correctly blocked the previous-failure campaign and
fresh 48-run qualification. Neither campaign was started, no output configuration
is fully qualified, and production remains unchanged. All evaluation operations
are stopped; the isolated candidate service remains available for further work.

Final evidence: `agent-output/openhands159-reliability-20261030/development-6-strict.json`,
`reliability-full-17.log`, and `final-observation.json`. No commits, staging,
pushes, or legacy-code removal were performed.

The candidate uses a shared Qwen template/tokenizer counter at the SDK condenser
and inference boundary. Assets are pinned to tokenizer commit
`995ad96eacd98c81ed38be0c5b274b04031597b0` and the downloaded model digest
`793390598026bea9fd308ed376b955c191ff663648a96493c40c6471a52c9d19`.
Missing or mismatched assets fail explicitly. Settings continue to determine
context, output reservation and compaction; modern operations no longer inherit
the legacy compatibility rule that discards per-stage output settings.

`prepare_environment` refreshes declared dependencies within the existing SDK
conversation. Browser preparation invokes the project's installed Playwright
CLI and mounts its version-specific browser cache. Preparation has a separate
bounded clock and retry count. This follows Playwright's [version-specific binary requirements](https://playwright.dev/docs/browsers).
The stop hook defers changed or unprepared dependencies to that controller
without consuming a code-repair cycle. The terminal adapter retains its command deadline
across yielded results and polling, and kills its terminal process descendants
on expiration. Managed previews use their own lifecycle.

Project environments and operation caches have process-held leases. Collection
reclaims only reconstructible caches, preserving active execution, verification
and previews. The candidate slice caps aggregate memory at 5 GiB; worker
operations have separate 3-GiB / 256-task cgroups. The worker uses
`DelegateSubgroup=control` so it can restart while operations survive, and
`OOMPolicy=continue` so an operation OOM does not kill the HTTP supervisor.
These settings follow systemd's [delegated subgroup requirements](https://www.freedesktop.org/software/systemd/man/257/systemd.resource-control.html).

Verification supports standalone Java compilation, literal packaged deliverables,
and controller-replayed `.daedalus-ui.json` flows, including `file_path` HTML.
UI evidence requires an interaction followed by an observed assertion at the
exact revision. Desktop targeting filters by editable/activation capability and
accessible name or role, rejecting remaining ambiguity. Requested JAR/WAR/ZIP
products carry their verified source revision and content hash into the delivered
archive. Changed products cannot be silently substituted during packaging.

Modern Resume preserves consumed calls, active time, repair counts and the SDK
conversation. The separate **Continue with more allowance** action explicitly
requests `budget_mode: "new_allowance"`; previous usage and resume events remain
recorded. Legacy jobs retain their earlier default behavior. Inference records
request IDs, actual options, token counts, upstream status and termination reason.
Eligible failures get at most one regeneration before dispatch, with both calls
charged; successful tool dispatches are never retried by the transport. A response
terminated by its output limit is recorded as `output_limit`, including available
token usage, and receives the same single-retry bound. It cannot enter an SDK
loop of reasoning-only continuations.

The corrected protocol matrix (`protocol-3`) ran all six probes three times at
4,096, 8,192 and 16,384 output tokens. The 4,096 configuration passed 18/18, with
16 first-attempt passes and two allowed recoveries. Larger allowances showed
required-tool omissions and did not qualify in that matrix. Earlier `protocol-1`
and `protocol-2` remain diagnostic evidence: the first had a probe constructor
error and the second inadvertently used the legacy output fallback. Neither is
valid output-limit comparison evidence.

A supplemental tool/reasoning continuation measured 1,334 estimated versus
1,337 actual prompt tokens, and 46,334 versus 46,337. Full GPU residency was
confirmed at 65,536 context (25,745,854,298 resident bytes). The downloaded
model template and stop configuration remain unchanged. Alternative reasoning
modes were not tested because `/api/show` did not advertise selectable modes,
consistent with Ollama's [model-specific thinking controls](https://docs.ollama.com/capabilities/thinking).

Live bounded process and memory faults were contained; candidate, legacy worker
and Codebox stayed healthy with unchanged worker PIDs. The combined Linux
fault/SDK/GUI suite passed 45 tests after correcting a SQLite read-cursor lock and
persisting active-time checkpoints across lost workers. The evaluator itself
needed a 3-GiB limit; the aggregate 5-GiB candidate cap stayed unchanged. Local
controller/revision/delivery tests passed 92 tests; frontend tests passed 154 and
the production build succeeded. A browser check confirmed separate `preserve`
and `new_allowance` resume requests with no page errors.

The first development cohort passed Python build, acceptance and follow-up,
then exposed a string-versus-Path bug when collecting a requested JAR. The web
trial exposed repeated output-limit reasoning and unclear feedback for comparing
a page snapshot with element text. This cohort was stopped and retained as
diagnostic evidence. Fixes normalize the product path, bound output-limit
recovery, and explain the `text` action for element assertions. Focused local
regressions passed 65 tests (two Linux-only skips). The corrected Linux gate then
passed all 49 tests.

The fresh `protocol-4` matrix made **8,192 output tokens** the smallest
protocol-passing configuration, pending development validation:

| Output limit | Passed | First-attempt passes |
| --- | --- | --- |
| 4,096 | 16/18 | 15/18 |
| 8,192 | 18/18 | 18/18 |
| 16,384 | 18/18 | 18/18 |

The two 4,096 failures were malformed large-file tool calls after the allowed
retry. At 8,192 output tokens, the unchanged Settings headroom policy leaves
54,067 input tokens and compacts at 40,550. Full GPU residency was reconfirmed
at the same model digest and 65,536 context.

The second development cohort passed Python and Java, including both follow-ups,
but exposed insufficient guidance when generated UI steps omitted the required
`action` field. That cohort was stopped and retained. The conversation now gets
the concrete step schema, and invalid steps report their flow/step index and the
allowed actions. Project-authored UI checks also bind to the requested application
entrypoint, so a separate working fixture cannot verify a broken `index.html`.
This binding checks explicit entrypoints; it is not a proof against every possible
mock implementation.

The resulting Linux regression suite passed **52 tests**, including a live
substitute-page verification fault. Held-out evaluations now share private,
leased dependency caches across delivered revisions rather than accumulating a
new environment for each artifact. Assertions remain outside the coding sandbox.
The third development cohort passed Python and Java again. Its web application
passed held-out behavior, but a project test started its own server outside the
controller's observed endpoint and still failed provenance after two repairs.
That cohort was stopped with its results and usage retained. Initial guidance and
repair feedback now name `DAEDALUS_APP_URL` / `DAEDALUS_<SERVICE_ID>_URL` explicitly
and explain that local fallback servers run only when those variables are absent.

A real-browser regression then exposed an additional sandbox mismatch: the
`http://upstream-*.invalid` alias suppressed browser Fetch Metadata. Modern
verification now forwards each granted endpoint onto loopback inside the private
network namespace; other host endpoints remain inaccessible. The legacy alias
behavior remains unchanged. This preserves the browser's trustworthy-origin
semantics required by the [Fetch Metadata specification](https://www.w3.org/TR/fetch-metadata/).
The regression compares identical application assertions through a test-owned
server and the controller endpoint, requiring revision-bound browser evidence
only for the latter. A separate network fixture checks endpoint isolation.

New evaluator stores now initialize their storage checks on the first execution
boundary, so leased cache collection actually runs before subsequent evaluations.
Storage interruptions in the private evaluator pause the campaign rather than
becoming completed application failures. Live test fixtures now use disk-backed
candidate storage with automatic lease-aware dependency collection. Repeated
fixtures on the container's RAM-backed `/tmp` had caused test-only memory pressure;
the unchanged 5-GiB candidate cap contained it. The corrected full Linux gate
passed **62 tests** in 552.64 seconds, including browser endpoint provenance,
endpoint isolation, cache initialization, and all prior fault/SDK/GUI checks.
The fourth development cohort passed the complete Python, Java, and web workflows.
Qt generated `launch` where the UI manifest requires `command`; generic feedback
led to interpreter-path edits rather than correcting that field. Its final repair
then exhausted the 8,192-token output allowance twice and blocked after 25 calls.
The failure and the interrupted second repetition remain recorded. The contract
now includes exact JSON flow shapes, and validation names the missing field and
the fields actually received.

A focused Qt run exposed a separate adapter defect: labels without an AT-SPI
Text interface returned a node object, so correct visible values such as `212.0`
were compared against JSON instead of text. The adapter now returns the accessible
label value as text; unsupported non-label text reads report an explicit error.
The latest focused engine/GUI suite passed **50 tests** in 217.15 seconds, including
real label reads for Qt, GTK and Swing and a Qt controller test that accepts the
correct value and rejects an incorrect one. The fresh `qt-contract2` comparison
produced **1/2 strict successes at 8,192** and **2/2 at 16,384**, including build,
exact-revision acceptance and follow-up for each workflow. The failed 8,192
workflow passed private behavior checks, but its generated UI commands pointed
to an original workspace path rather than the delivered revision's fresh copy.
It exhausted two verification repairs and remains a strict failure.

Both configurations used identical source, dependencies, tokenizer, model digest
and resource limits; only builder/compaction output allowance differed.
The 16,384 configuration leaves 45,875 input tokens and compacts at 34,406;
full GPU residency was confirmed again. It advances to the fresh eight-workflow
`development-6` cohort. These small development samples do not establish a
general reliability advantage for the larger allowance.

Development then exposed disk-pressure recovery defects. The Qt build paused
with four calls consumed and resumed the same conversation, reaching a verified
candidate at nine total calls without resetting its allowance. Cache collection
was slow because it rescanned the entire worker store before every eviction;
it now measures once, accounts for removals incrementally, and retains the final
storage guard. Source uploads also collect inactive modern-engine caches before
returning HTTP 507. Active cache leases and legacy upload behavior are preserved.
The evaluator now classifies HTTP 507 and artifact-storage reserve failures as
infrastructure pauses. Its original misclassified record is saved alongside the
corrected pause, with all job usage retained.

Private evaluator browser caches now have automatic lease-aware collection at
campaign and live-test boundaries. Reports, assertions, sources and accepted
artifacts remain intact. This restored about 6 GB of disk headroom before the
Linux regression rerun. The local recovery gate passed 46 tests with one
Linux-only skip; the complete Linux worker/SDK/GUI/sandbox gate then passed
**71 tests** in 595.48 seconds. Development retains a source-history record for
these storage fixes and resumes the existing jobs with preserved budgets;
fresh qualification will freeze the final source anew.
Only eight strict development passes unlock the previous failures as development
regressions and then the fresh 48-trial qualification. No production promotion is
automatic. These results are not yet a production qualification.

Repair evidence and the backup of preexisting dirty work are under
`agent-output/openhands159-reliability-20261030/`. Campaigns freeze source,
dependency/tokenizer/browser identities, settings and resource configuration.
Infrastructure interruptions leave the trial unfinished and resume the same job
with `budget_mode: "preserve"`. Held-out assertions remain outside agent mounts.

## Desktop and generation recovery (2026-11-02, rollout blocked)

This round preserves the earlier 7/8 development cohorts, the 17/18 protocol
result, and their original evidence. It keeps SDK/tools 1.59.0, the existing Qwen
digest and tokenizer, 65,536 context, 16,384 output, temperature 0.2, reasoning,
and the existing job/resource allowances. Production remains on its existing
engine while the separate candidate release is evaluated.

New modern jobs persist `engine_behavior_version: 1` in their existing JSON.
Missing/version 0 retains the previous behavior. Replaying creation of an older
job returns that original job: the new version field cannot silently upgrade it
or hide a change to another request field. UI contracts remain version 1, with
no new database tables or public endpoints.

Desktop target failures now include the attempted locator, category, actual
accessible names/roles/capabilities, and bounded candidate controls. Verification
feedback includes the failing flow/step and application logs. Matching remains
strict; Qt object names and widget class names are not substituted for observed
accessible identities. Raw node references require the current session identity,
while saved flows use names/roles. Failed launches clean up their application
processes before another launch. Builder guidance distinguishes input `value`
from displayed `text`, recommends stable accessible names, and separates visible
GUI launch commands from offscreen unit tests.

The bridge declares coding responses `action_required`; compaction and explicit
text-response probe phases allow text. Merely supplying tools does not require
Ollama to call them ([chat API](https://docs.ollama.com/api/chat)). Missing calls,
undeclared/malformed calls, and truncated output share one pre-dispatch retry.
The retry contains concise corrective feedback and is recounted against the same
context/output policy. Discarded responses never dispatch actions. Events retain
request identity, upstream versus bridge status, available usage, termination
reason, and separate first-attempt/recovery outcomes. Cancellation is propagated
without becoming a network retry. SDK-persisted message sender receipts prevent
duplicate request insertion; later repair operations append only their feedback.

After a terminal generation failure, the worker stops descendants, restores
protected files, cleans the workspace, and snapshots it. The controller can
check and publish that exact checkpoint without more inference, using remaining
execution time. The result is explicitly incomplete even if its executed checks
pass. Stale checks cannot establish this revision's status; checks that did not
finish remain unverified. Failed verification does not initiate another coding
repair on this path. Publication failure retains the checkpoint and its blocker.
Accepted revisions are never replaced by recovery. Failed SDK operations retain
their consumed repair count, and additional call/time allowance cannot lift the
two-repair limit.

The existing job card exposes the failure, revision, usage, review/download,
acceptance, and permitted resume actions. It suppresses a green overall verdict
for incomplete work. Additional allowance is offered for exhausted job calls or
time; it is not offered as a cure for an individual response output limit.

Evidence is retained under `agent-output/openhands159-recovery-20261102/` and the
isolated release `/opt/daedalus-openhands159-recovery-20261102`. Local checks passed
126 tests with one Linux environment skip; frontend checks passed 156 tests and
the production build. Browser checks covered mobile fit, requirement statuses,
incomplete-candidate controls, exact revision acceptance, and console errors.
The end-to-end forced-generation-failure fixture passed: the real worker and
controller delivered an incomplete candidate while preserving an earlier
accepted project revision.

A Linux validation interruption reached the configured disk reserve. Its log is
preserved, and no Qwen cohort had started. Lease-aware collection reclaimed
7,491,693,462 bytes from inactive candidate dependency/operation caches; sources,
snapshots, artifacts, logs, and SDK conversations were excluded. Validation
resumed with unchanged inference settings and original evidence retained.
No commits, Git staging, pushes, or legacy code removal were performed.

The complete Linux worker/SDK/GUI/sandbox regression gate passed **132 tests**
(581.88 seconds), followed by **48 passing** Linux recovery/job compatibility
checks after the creation-replay fix. Supported deliberately incorrect GUI and
verification fixtures produced zero false passes. This includes a real failed
Qt launch followed by a corrected launch, actionable wrong-name/role feedback,
stale-session rejection, and an observed input transition after correcting the
locator. The pinned-template long-conversation output-limit recovery test passed.

The fresh fixed-configuration protocol gate passed **18/18**: **17 first-attempt
successes and one recovery success**. The recovered native-tool probe received
upstream HTTP 500 for malformed `add` arguments; one corrective regeneration
returned the required call. Omitted required calls were tested separately through
the real SDK/worker fault fixture. Recorded GPU residency was
`size == size_vram == 25,745,854,298` at context 65,536. Input budget remains
45,875 and compaction threshold 34,406.

The first Qt initial build passed real conversion/error interactions and exact
acceptance. Its Reset follow-up received the correct request once, inspected the
project, planned the changes, and ran baseline tests. It then returned no tool
call on both the first response and the single corrective regeneration. Both
responses were HTTP 200 upstream and `missing_required_tool` / HTTP 502 at the
bridge; neither dispatched an action. The recorded request is
`19603bc45bb24d2a98822e3d55101d14`. This is a generation-compliance failure, not
the earlier inaccessible Qt locator failure.

Recovery retained revision `1fd7bc9a7e7fedc824e4f7352720bdd9d0521093`, verified
its existing behavior, and delivered it explicitly incomplete. Reset remained
absent, so its controller-owned clear/Ready requirements remained unverified
and the private follow-up check failed. The previously accepted initial revision
`5f652133d49817dca167ea88f940f9be852da131` was preserved. The failed job consumed
six model calls and 67.72 active seconds, with zero application repairs; extra
job allowance would not address this failure. No manual repair or retry was
used to change the cohort's result.

The second web initial build delivered revision
`27f891110da6058d624848c5af0a5b142f4b69a1` after one provenance-driven repair
(41 calls). Its private check failed before executing a numeric assertion:
the button's visible text was `Calculate`, but its accessible name was
`Calculate total`, so the exact `button` / `Calculate` locator timed out.
This records a strict interface/evaluator naming mismatch, not evidence that the
calculation itself was wrong. Controller-run application checks passed; those
checks do not prove arbitrary wording or the private evaluator's full contract.
The private assertion was not disclosed to the builder, the application was not
manually changed, and the original failure remains in the cohort.

The development transport ledger also records real Qwen output-limit recovery,
in addition to the injected regression fixture. Three responses reached exactly
16,384 generated tokens with `done_reason: length`; each was discarded and its
single regeneration succeeded. The longest recorded request
`8b4413e8197448678b93be28ae5f9929` had 32,104 actual input tokens before retry and
32,148 afterward (counted estimates 32,044 and 32,088), within the unchanged
45,875 input budget. Both attempts were charged. This demonstrates recovery in
a long live conversation without increasing output limits or application-repair
allowances. Request/attempt metadata is preserved in `development-transport.json`
under this round's evidence directory.

The second Qt initial build also passed and was accepted at revision
`b2246acc6ba9aadf64fa86a7ca155f33fec4c536`. Its follow-up cleared the input but
exposed accessible label name `Result` after Reset, while the controller and
private evaluator required `Ready`. The source sets displayed text to `Ready`
and accessible name to `Result`; this is an accessibility/text-binding gap,
not proof that the visible label still displayed `Result`. The original request
required the result's accessible name to track its visible text. The generic
stable-name guidance and the Qt label observation path need reconciliation with
that explicit request before another release attempt.

Two concrete repair packets included observed controls and the failed flow step.
The second repair then ended with another two-attempt `missing_required_tool`
failure, request `0d9d36b251634fdbbec61f9d29405567`. Revision
`a4dfb2122cbe92a063fc120ca570af83ee3c29ce` was retained and delivered incomplete,
with the clear criterion passed, Ready failed, and `application_repairs` exhausted.
The final usage was 28 calls and 662.41 active seconds. The accepted initial
revision remained unchanged; recovery neither hid the failed check nor granted
another repair.

The fresh development gate finished **5/8**, below its required **8/8**:

| Family | Complete workflows | Remaining strict failure |
| --- | ---: | --- |
| Python CLI | 2/2 | None |
| Java packaging | 2/2 | None |
| Web | 1/2 | Exact accessible button-name mismatch before numeric assertion |
| Qt desktop | 0/2 | Both follow-ups ended with missing required tool calls; second also retained the label-binding failure |

Across development, 303 generation requests succeeded on their first attempt,
14 succeeded with the one allowed regeneration, and two exhausted regeneration.
There were 15 missing-tool failure attempts and three output-limit failure
attempts, with 335 charged calls in total. These are request-level counts, distinct
from the 18-case protocol score. No retained incomplete candidate counted as a
successful workflow. Previous 7/8 cohorts and original results were not modified.

**Release decision: retain production.** The pipeline stopped at the failed
development gate. The separate 11-case previous-failure model cohort, fresh
48-case qualification, deployment, post-deploy smoke checks, and default-engine
switch were not run. The focused historical failure fixtures did run as part of
the passing Linux regressions; they do not substitute for those downstream
cohorts. No private assertion was sent back to the coding agent.

Final checks found no active candidate operations and no candidate source-hash
changes. Production health returned HTTP 200; its existing worker remained PID
2773749 at the original release, and the default engine remained legacy. All 111
pre-existing changed files remained present; unrelated contents were preserved.
