# Policy 7 integration and qualification

Policy 7 now has a production-controller implementation in
`coder_policy7_controller.py`, dispatched through the existing durable worker
operations. `coder_policy7_evidence.py` and `coder_policy7_ops.py` share decisions
between that controller and the historical experiment adapter. The web server
does not run `Experiment` or a standalone experimental process.

**New product jobs select policy 6 by default.** The separate
`daedalus_policy7_edits` and `daedalus_policy7_builds` Settings flags opt new jobs
into policy 7; existing jobs retain their policies. V3 remains opt-in. Default
promotion requires the frozen proof below; deterministic tests and saved-response
journeys do not qualify it.

**Evidence version 3 withholds behavior without trusted, criterion-specific proof.** Passing model-generated audits cannot authorize acceptance. See the version 3 safeguards below; historical acceptance counts describe earlier evidence rules.

The implementation separates behavior, documentation, delivered executable tests
and protected-file evidence. Independent audits cannot replace delivered tests.
Evidence records the immutable revision, execution, source/test hashes, observed
assertions and managed-service requests. Audit files and runners are validated
before execution; Node applications remain Node processes even when their HTTP
tests use Python. Managed URLs are supplied through environment variables.

Application defects and audit faults are diagnosed together. Confirmed application
defects remain repairable alongside audit faults. Two application repairs and two
audit corrections survive restart and Continue. Invalid audits cannot receive free
corrections after application edits. Truncated edits split into explicit file
targets without raising Settings limits; repeated focused failure saves a candidate.

## Running an isolated request

### Review safeguards

Compiled subprocess audits require controller-owned `strace` observations outside the audit sandbox. A source-code mention of a subprocess is insufficient: a successful application launch, matching artifact hashes, current revision, and observed assertions are required. JVM and .NET traces must also load an application artifact. Install `strace` and `bubblewrap` on Codebox; missing tracing fails closed.

Lint/typecheck failures remain advisory throughout scheduling, review and repair decisions. They cannot suppress executable tests or spend application repair rounds; setup/build/launch failures still block dependent execution.

Follow-up plans assign every inherited outcome a disposition: retain, replace, remove or clarify. Replacements/removals require exact request quotes and a separate scope review. Ambiguity pauses before editing; Continue accepts a clarification and preserves the requirement history and repair counters. Protected-file constraints remain enforced. Old unfinished jobs re-plan unresolved inherited scope and rerun verification before acceptance; completed deliveries are preserved.

Follow-up structured output constrains parent IDs and request quotes to controller-supplied values. The scope review sees each parent together with its proposed action and resulting requirements; it judges whether that action is authorized. Both stages have bounded format recovery and text fallback for unsupported runtimes. A rejected reconciliation gets one corrective re-plan; unresolved disagreement still pauses before editing. Repeated copies of an identical retained outcome share one active outcome while preserving their history links.

Optional visual review captures current-revision screenshots on managed services and uses an installed vision model. Measured layout defects join ordinary checks. An unavailable optional review records a skip; explicitly requested visual verification pauses for input.

On Codebox, use the worker test interpreter and a dedicated disk-backed directory:

```sh
python backend/evals/run_policy7_proof.py --root /disk/proof/example --request request.json
```

The request is a JSON object with `task`, `model`, `ollama_url` and the current
`settings`. Optional fields are `files` (relative names to initial text),
`explicit` (the existing execution-command shape), `protected`, `project_id`
and `inherited` (unresolved obligations from a draft). Select an installed local
model; the evaluator does not download models or change Settings.

An existing experiment requires explicit `--resume`. This preserves application
repair, audit correction and no-progress recovery counters. An acknowledged
cancelled operation resumes with a new operation identity. Another active writer
for the same experimental project is rejected.

The editor requires the existing dedicated **Aider 0.86.2** CLI. Its text requests
pass through the shared local inference boundary, including internal retries.
Automatic commits, test/lint loops, shell suggestions and URL fetching are off.
Audit editing and execution require Linux bubblewrap; unavailable isolation is
an error, not a fallback to writable project source.

## Execution contract

Existing `.daedalus.json` command overrides and manifest adapters remain usable.
An optional `.daedalus-run.json` describes how packages and services fit together:

```json
{
  "dependencies": {".": ["frontend"]},
  "services": [{
    "id": "app",
    "cwd": ".",
    "command": "python3 -m uvicorn app:app --host 127.0.0.1 --port {port}",
    "ready_path": "/"
  }]
}
```

Package paths must be discovered or explicitly configured. Cycles and unknown
dependencies are rejected. All builds precede service startup. Commands receive
`DAEDALUS_PROJECT_ROOT`, `DAEDALUS_AUDIT_DIR`, `APP_DB_PATH` and
`DAEDALUS_<SERVICE_ID>_URL`. Ports and disposable database paths belong to the
controller. Service process groups are stopped when checks end.

Dependency caches are scoped to project identity, manifest contents and tool
versions. Executable checks use fresh source copies. Existing source changes,
including extensionless executables, invalidate verification.

## Review and evidence

The reviewer writes native test files in its audit directory. `audit.json`
contains command/outcome metadata, not embedded executable source. `{audit}` and
`{project}` in commands resolve to controller-owned paths. Read-only mounts
prevent audit code from editing the application or shared dependencies.

Review reconciles outcomes with the original request. Each failure receives its
own diagnosis. Audit path/import failures, absent provenance and zero collected
tests cannot authorize an application repair. Source-bound assertions and
requests to managed services provide execution evidence. A model verdict alone
cannot establish acceptance.

The experiment retains its journal, worker operations, full inference requests
and responses, editor logs, immutable archives and revision-specific checks.
`Experiment.fork` selects an exact archive revision/hash and carries unresolved
requirements into a fresh request; it does not read a mutable parent workspace.
This is evaluator functionality, not a new public API or UI action.

## Release proof

`backend/evals/run_policy7_release.py` uses the actual persistent controller,
an isolated worker, a dedicated database and frozen source/model/settings.
Run two repetitions of small web creation, React/FastAPI medium creation,
uploaded Python repair, uploaded static-web repair and uploaded
Node/Express/SQLite medium repair. Independently validate the healthy medium
fixture before introducing its documented filter/summary faults.

The proof permits at most 16 jobs: ten base jobs, two sequential edits for each
successful small web build, and one feature follow-up for each successful medium
repair. Follow-ups require both internal and independent acceptance of their parent.
Require independent application checks and Daedalus acceptance to agree, zero
false acceptance, protected-file preservation, successful build/edit and
repair/feature journeys, explicit draft continuation and Stop/Resume.

Retain failures separately and withhold deployment if this proof fails. After a
passing proof, deploy with rollback copies and run six jobs through the live UI
and APIs: the five base scenarios and the medium-repair feature follow-up.
The larger existing qualification gate
for making v3 the default remains separate and unchanged.

Generated campaign reports belong under ignored `agent-output/`, not in Git.

## Persistent-controller proof, 2026-09-18: not qualified

The frozen controller/worker campaign completed both repetitions of the five
base scenarios with `qwen3-coder:30b` and unchanged source and Settings. It had
zero false acceptances, zero internally accepted deliveries, and six independent
passes (both repetitions of all three uploaded-project repairs). The four new-app
builds failed independent checks. Since no parent met both acceptance gates, the
six dependent edit/feature jobs were not run. This is a failed ten-job proof,
not a passing sixteen-job qualification.

Observed blockers included unapplied editor responses, zero-assertion delivered
tests, incorrect test interpreters, invalid independent audits and review
confusion between baseline and current evidence. Later local corrections passed
deterministic regressions but have no completed frozen model qualification.
Their results must not be combined with this frozen campaign. Policy 7 was not
promoted, nothing was deployed, and the six post-deployment live jobs were not run.

The report, both source identities, screenshots, raw responses, immutable
artifacts and hash-verified evidence archive are retained separately under
`agent-output/daedalus-repair-20260917/`. Production health was checked read-only
and its existing Settings were unchanged. Earlier interrupted evidence remains
separate from this campaign.

## Historical standalone proof: no-go

The frozen 2026-09-17 proof completed eight cases with the installed
`qwen3-coder:30b`: two independent passes, zero correctly accepted deliveries,
and one false acceptance. Both independent passes were uploaded Python repairs
that the internal audit nevertheless withheld. The accepted small web app
worked, but its requested delivered browser tests crashed; one passing external
audit had incorrectly been counted as evidence for all outcomes.

That frozen revision was **not qualified for deployment**. Changing the editor did not resolve the
verification failures. In particular, acceptance still needs separate evidence
for mixed behavior/documentation/delivered-test requirements, and audit creation
failures must not prevent diagnosis and repair of already observed project
failures. Keep these failures visible; do not treat the deterministic regression
suite as evidence that generated projects pass.

The full report, source/model/settings identities, immutable app archives,
raw inference records, independent checks and Stop/Resume evidence are retained
under `agent-output/daedalus-policy7-proof/`. The two earlier interrupted
rehearsals have their own identities and are excluded from the eight-case score.
Those results belong only to that older frozen source. New results must not be
combined with them or with interrupted rehearsals. The current release evidence
is retained separately under `agent-output/daedalus-repair-20260917/`.

## Status addendum — 2026-09-19 repair and deployment

The failed proofs above were dominated by controller logic, not model capability: no job approached its
call/time allowance; every stop was a policy limit. The decisive defects and their fixes:

- **Acceptance could never succeed.** The review prompt's `"missing_outcomes":["omitted requirement"]` was filled
  with every outcome ID in 10/10 jobs while `acceptance()` required it empty. The field is gone from the gate; the
  reviewer reply is schema-constrained, normalized (`normalize_verdict`), retried on format errors and re-asked once
  when it fails an outcome whose executed evidence is complete.
- **Audit authoring was a hard gate made of brittle LLM output.** Recurring slips are now repaired mechanically
  (empty `audit.json`, `__file__` paths, wrong evidence types, wrong interpreter/runtime, `dotnet run`), visible
  faults get one in-operation nudge, and corrections are told which outcome IDs lack coverage.
- **The Aider editor wrote blind and its recovery could not converge.** No-op edits, skipped files, salvaged
  truncated replies and un-stuck `editor_stopped` fixed uploaded repairs; **new projects** use a hybrid builder
  (`coder_policy7_builder.py`: the agent loop with a terminal, driven until finish) behind the same gate.
- **Compiled languages were unobservable.** Maven/Gradle, CTest, .NET, Go and Cargo runs now count through
  `COMPILED_RUNNERS` (runner-reported executed tests + package-source provenance); zero tests still fails closed.
- **One false acceptance** (the model substituted `#add-expense` for the requested `#save-expense`; its own test,
  audit and review all agreed) produced the controller-owned `requested_interfaces` guard.

Measured with `qwen3-coder:30b` under the unchanged Settings limits, in isolated evaluator runs:

| Scenario set | Accepted | Externally working | False acceptances |
|---|---|---|---|
| Uploaded repairs + follow-up edits (2 variance runs) | 8 / 14 | 12 / 14 | 0 |
| New CLI builds, 8 languages × 2, hybrid builder | 9 / 16 | 13 / 16 | 0 |
| Same 8 builds through live production policy 6 | 0 / 8 | 3 / 8 | 0 |

These are development smokes, **not** a frozen ≤16-job qualifying proof, and results from different source
snapshots are not combined. Policy 7 is deployed behind two Settings flags that default to off —
`daedalus_policy7_edits` and `daedalus_policy7_builds` — and `coder_loop.POLICY_VERSION` remains 6. Known
limits: roughly half of new builds still end as "Ready for review"; most remaining wrong-withholds come from the
model-written audit; Rust and sometimes Go logic is simply wrong; run-to-run variance is high. Run summaries are
retained under `agent-output/daedalus-repair-20260918/runs/`; each live finding has a regression test in
`backend/tests/test_daedalus_policy7_repair.py`.

The local evidence folders for the older campaigns (`agent-output/daedalus-policy7-proof/` and the policy 4–6
folders) were pruned on 2026-09-19 as superseded, together with their copies on Codebox; the 2026-09-17 proof
evidence remained under `agent-output/daedalus-repair-20260917/` (`proof-r2-evidence.tar.gz`, hash in its report).

On 2026-09-30, ahead of the 18.1 release, the remaining local evidence was pruned as well: the run summaries under
`agent-output/daedalus-repair-20260918/runs/`, the live-run folders, the 2026-09-17 proof folder and the frozen
evidence archives, together with the finished evaluation run directories on Codebox. Paths to those folders in this
document are historical. The evaluation and deploy tooling and `REPORT-newbuilds-20260925.md` are retained; each
live finding keeps its regression test.

### Verification evidence version 3

Policy 7 now keeps behavior without trusted, criterion-specific proof **Ready for review**, even when every generated audit and review passes. Generated tests remain useful for finding defects and guiding repairs. They cannot independently establish that the builder interpreted the request correctly. There is currently no registered controller-maintained behavioral fixture library, so behavioral builds remain candidates. An unchanged user test without an explicit requirement mapping is also insufficient. Candidates do not replace the accepted revision; completed earlier artifacts remain immutable.

The summary includes request excerpts, criterion status, trust origin, assertion failures, and evidence version. Older unfinished policy-7 work must acknowledge its active operation and recheck the exact checkpoint before publication. Policy 6 remains the default.

Project execution uses a Linux bubblewrap child with private process/network namespaces, read-only toolchains, and scoped writable project/runtime directories. SDK and Aider inference passes through a supervisor-owned bridge; child diagnostics cannot change the model-call ledger. Public dependency traffic uses a restricted proxy. Missing isolation is an environment failure. Builder setup and revision checks share manifest-keyed dependency environments. Controller form probes use disposable service/data copies so their writes do not contaminate tests.

Failed setup/build checks return to bounded prerequisite recovery within the current coding round, before audit authoring. A builder finish event cannot bypass them. Python audit diagnostics retain bounded subprocess arguments, working directory, stdout and stderr. An observed temporary fixture passed by a missing relative path is routed to audit correction; these diagnostics do not confer behavioral trust.

Repairs retain failed revisions. If a previously passing project test disappears or regresses, the controller restores the earlier checkpoint for review. Generated audits run in a later stage; their absence during project checks does not trigger rollback. Coding rounds persist a verification reserve based on remaining calls; Continue preserves durable repair limits. Review input packing reserves space for corrections and reports input-budget failures separately from malformed responses.

These safeguards do not constitute release qualification. A fresh frozen server campaign and the documented promotion gates remain required; candidate-only results cannot pass the automatic-acceptance release gate.

## Four undelivered stacks — 2026-09-26 investigation and fixes

At the 64K window the sweep left Rust `base64tool`, Node `mdtoc`, the Express/SQLite Kanban board and the
React/Vite + FastAPI board at 0/2 externally working. Thirty archived jobs (`agent-output/daedalus-repair-20260918/runs/*`)
showed three mechanisms rather than one:

- **Untested inputs.** No model test or audit ever used the inputs the verifier fails on (`====`/`a===`, a fenced
  `# heading`, a real drag/edit/delete, `get_by_label`), and the controller's `probe:command:n` passed on exit 0 alone —
  Node printed nothing and passed; it listed the fixture's fenced heading and passed. Medium's `workflow:*` rows fell to
  advisory because the guard could not find the form fields the request names by label.
- **Mis-routed repairs.** A `workflow:*` defect contributed no repair target, so the union collapsed to the skeleton's
  `tests/api.test.js`; `documentation_files(root, '.')` missed `base64tool/README.md` and a no-op repair became a durable
  `no_progress`; failed `interface:*` rows (`unverified_interface`) reached no `decide()` branch.
- **Rollbacks that discarded progress.** `repair_regressions` compared test rows by id only, so replacing the skeleton
  placeholder test with real failing CRUD tests restored the checkpoint (twice discarding a revision where `form:app` and
  persistence already worked) and parked the job with rounds left. A `server.log` counted as a source change.

Fixes (all rows diagnostic — `evidence_types: []`, `outcomes: []` — and `acceptance()` untouched): wording-derived negative
probes (`probe:invalid:<n>:<k>`, `probe:missing:<n>`, exact exit code + empty stdout + stderr message), fixture-aware output
rules on the positive probe, `interface:labels:<service>` plus label-driven item creation in the workflow guard, front-end and
server files as repair targets for guard defects, same-suite regressions (`retained_suite_regression`) with continue-after-
rollback while rounds remain, `documented()` over package cwds, runtime logs exempt, `unverified_interface` routed to repair,
a second in-operation audit nudge for mechanical faults, request clauses handed to the audit author, and `ContextWindow`
calibration allowed above 1.0 (`RATIO_CEILING` 1.6) with the last reported prompt as a floor so compaction runs before the
window fills. Eval side, per the user's "fix both sides" decision: the Node, Rust and medium requests now state the fenced-code,
padding and auto-selected-project expectations the verifier relied on; the Kanban/medium verifiers locate columns by heading
text, wait for an asynchronous edit form to populate, drop onto the innermost list (real mouse drag first), accept dialogs
and 200/204, and scope CRUD assertions to the created project. The 2026-09-20 false-acceptance Kanban archive still fails
under the corrected verifier (creation does not persist, page error on load). Measured results: see the round report,
`agent-output/daedalus-repair-20260918/REPORT-newbuilds-20260925.md` ("Round 3").
