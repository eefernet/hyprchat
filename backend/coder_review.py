"""Compact work briefs and post-implementation independent native review."""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import re

from coder_profiles import validate_command
from coder_repository import safe_relative
from coder_verification import digest

EVIDENCE_TYPES = {'behavior', 'documentation', 'tests', 'preservation', 'visual'}
BRIEF = '''Inspect the project and write a compact work brief for the complete original request.
Return {"outcomes":[{"text":"requested outcome", "evidence_types":["behavior"]}],
"constraints":["explicit constraints"],"components":["affected components"],"approach":["short implementation steps"],"validation":["suggested validation"]}.
Only include evidence types actually needed for each outcome; several can apply to the same outcome.
Allowed types are behavior, documentation, tests, preservation and visual; this is not a checklist to copy.
README plus executable tests requires both documentation and tests. API compatibility is behavior, not byte preservation.
Do not write executable probes, commands, IDs, hashes, or repeat expected_behavior. Steps are guidance for one complete Builder operation.
Visual means an explicitly requested AI visual inspection, not ordinary browser behavior testing.
Responsive UI and data persistence are behavior. Byte preservation applies only to explicitly protected files.
If the request specifies an EXACT command, include execution_commands:{packages:{".":{test:"verbatim command"}}};
commands must appear verbatim in the original request. Optional proposed_commands use the same shape for otherwise
undiscoverable commands and must reference existing repository files. If the user explicitly protects entire files
from modification, include protected_files:["exact/path"]. Do not infer file protection from API compatibility.'''

AUTHOR = '''Independently review the implemented project against the ORIGINAL REQUEST and work brief.
Inspect its actual interfaces, changed source, baseline failures and project execution evidence.
Write ordinary native tests in a SEPARATE audit directory; never modify project source.
Return {"checks":[{"outcomes":["o1"],"evidence_types":["behavior"],"cwd":".",
"files":{"test_behavior.py":"ordinary executable test code"},"command":"python3 \\"$DAEDALUS_AUDIT_DIR/test_behavior.py\\""}],"notes":["uncertainties"]}.
Use $DAEDALUS_PROJECT_ROOT for real project imports/files and $DAEDALUS_AUDIT_DIR for audit files.
The controller writes files under the audit directory, supplies IDs, hashes and evidence references.
Python and Node imports are observed. Import constants and compare their identity or value; do not call constants.
Use throwing assertions and real interfaces; no copied implementation, shadowed imports or printed success.
For a native executable use binary:"relative/path" and directly execute it in command; native runners must report a test count.
For other languages, native test runners may declare subjects:["real/source/path"] for the actual project files used by those tests.
The controller hashes these files and labels this binding as declared; independently confirm their real use. Python and Node still require observed imports.
Documentation may use kind:"file",path:"README.md",assertions:[{kind:"nonempty"},{kind:"contains",value:"..."}].
Only explicitly protected files may use kind:"unchanged". API compatibility never freezes source files.
For browser behavior use kind:"browser",cwd:".",path:"/",steps with action click|fill|text|exists|visible|reload,
selector and value as applicable; the controller supplies the discovered server command. Text-only models can test browser behavior.
Requested project tests use the actual project execution evidence, not audit tests as a substitute.
Multiple checks may cover one outcome. Do not invent checks before inspecting the implemented interfaces.'''

AUTHOR += '''
Cover every outcome's required behavior and documentation, including README checks when requested.
Inspect every requested deliverable, even if another component cannot run. Missing frontend files,
dependency declarations, README or project tests are incomplete application work, not host failures.
For a browser-only app prefer the controller's kind:"browser" check: it starts the actual immutable
project on a private port. Never point Selenium/Playwright at an assumed localhost:8000 server.
The browser helper accepts steps such as {"action":"fill","selector":"#name","value":"Ada"},
{"action":"click","selector":"#submit"},{"action":"text","selector":"#result","value":"Ada"}.
Keep API/CLI native tests as ordinary files importing the actual project. An audit that only imports
a browser driver has no observed project-import provenance; use the controller browser helper.
Audit files are outside the project: never derive the project root from the audit file's __file__.
For subprocesses and fixtures use pathlib.Path(os.environ["DAEDALUS_PROJECT_ROOT"]) explicitly.
'''

JUDGE = '''Independently decide which ORIGINAL requested outcomes are supported by this exact revision.
Read source and failure logs where needed. Project tests and audit results are evidence, not authority on the spec.
Return {"outcomes":[{"id":"o1","status":"passed|failed|unverified","reason":"grounded explanation"}],
"disposition":"accepted|application_defect|check_defect|environment|ambiguous", "summary":"concise diagnosis", "corrections":[]}.
Distinguish baseline failures, new regressions, missing toolchains and application defects.
If a check is faulty, use check_defect and corrections:[{check_id:"...",reason:"request/source/failure basis",replacement:{...complete corrected native check...}}].
Correct checks before requesting application repair. Keep their outcomes/evidence types and all unrelated checks.
A changed explanation alone is not a correction. Never weaken requested behavior to pass. Ambiguity remains unverified.
Do not pass behavior using file existence, source text, narration or zero-test results. Do not pass requested tests using only audit tests.
Confirm that tests exercise actual imports, endpoints or binaries and that the entire original request is represented in the outcomes.'''

JUDGE += '''
Check every requested deliverable before choosing the diagnosis. A partial backend without its requested
frontend, dependency manifest, README or tests is an application defect. A missing dependency that the
project never declared is an application defect; a failed installation/network/toolchain check is environment.
An audit using the wrong server/selector/import is a check defect. Correct that check without changing
the application to match its mistaken assumptions. Environment requires recorded environment-fault evidence.
Use the supplied evidence gaps: missing requested project tests require application work, whereas missing
independent assertions require corrected checks. Never declare accepted while required evidence is absent.
'''


def normalize_brief(answer):
    if not isinstance(answer, dict) or not isinstance(answer.get('outcomes'), list) or not answer['outcomes']:
        raise ValueError('A brief needs requested outcomes')
    outcomes = []
    for i, item in enumerate(answer['outcomes']):
        if not isinstance(item, dict) or not isinstance(item.get('text'), str) or not item['text'].strip():
            raise ValueError('An outcome needs text')
        kinds = item.get('evidence_types', ['behavior'])
        if not isinstance(kinds, list) or not kinds or set(kinds) - EVIDENCE_TYPES:
            raise ValueError('Unknown outcome evidence type')
        outcomes.append({'id': f'o{i+1}', 'text': item['text'], 'evidence_types': list(dict.fromkeys(kinds))})
    brief = {'outcomes': outcomes}
    for key in ('constraints', 'components', 'approach', 'validation'):
        values = answer.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError(f'{key} must contain text')
        brief[key] = values
    return brief


def scope_brief(brief, payload):
    """Optional controller policies cannot become model-invented release gates."""
    result = copy.deepcopy(brief)
    for outcome in result.get('outcomes', []):
        kinds = outcome['evidence_types']
        if not payload.get('protected_files'):
            kinds = [kind for kind in kinds if kind != 'preservation']
        if not payload.get('visual_policy', {}).get('enabled'):
            kinds = [kind for kind in kinds if kind != 'visual']
        outcome['evidence_types'] = kinds or ['behavior']
    return result


def plan(store, operation_id, repository, read_only):
    payload = store.get(operation_id)['payload']
    try:
        answer = read_only(store, operation_id, repository, BRIEF, payload.get('evidence', {}), validate=normalize_brief)
        from coder_profiles import validate_commands
        commands = validate_commands(answer.get('execution_commands', {}))
        for phases in commands.get('packages', {}).values():
            if any(command not in payload['original_task'] for command in phases.values()):
                raise ValueError('Explicit command was not quoted from the original request')
        commands = {'packages': {**commands.get('packages', {}), **payload.get('execution_commands', {}).get('packages', {})}}
        proposals = validate_commands(answer.get('proposed_commands', {}))
        protected = set(payload.get('protected_files', []))
        for path in answer.get('protected_files', []):
            safe_relative(repository.root, path)
            if any(path in sentence and re.search(r'byte.for.byte|do not (?:edit|change|modify)|untouched|file.*unchanged', sentence, re.I)
                   for sentence in payload['original_task'].splitlines()):
                protected.add(path)
        result = {'brief': scope_brief(normalize_brief(answer), {**payload, 'protected_files':sorted(protected)}),
                  'planning_fallback': False, 'execution_commands': commands,
                  'proposed_commands': proposals, 'protected_files': sorted(protected)}
        if repository.inventory()['items']:
            from coder_profiles import discover
            result['verification'] = discover(repository, commands, proposals)
        return result
    except ValueError as error:
        # Failed format recovery is retained in the operation ledger. Builder can
        # still implement the request; independent review remains mandatory.
        retained = [e['data'].get('response', e['data'].get('rejected_response')) for e in store.events(operation_id, limit=10000)
                    if e['data'].get('response', e['data'].get('rejected_response')) is not None]
        if 'answer' in locals():
            retained.append(answer)
        brief = normalize_brief({'outcomes': [{'text': payload['original_task'], 'evidence_types': ['behavior']}]})
        return {'brief': brief, 'planning_fallback': True, 'plan_text': retained,
                'planning_note': str(error)}


def normalize_checks(answer, repository, payload):
    if not isinstance(answer, dict) or not isinstance(answer.get('checks'), list):
        raise ValueError('Independent review needs a checks list')
    known = {o['id'] for o in payload['brief']['outcomes']}
    checks = []
    for index, value in enumerate(answer['checks']):
        row = copy.deepcopy(value)
        if not isinstance(row, dict):
            raise ValueError('Each check must be an object')
        outcomes = row.get('outcomes', [])
        types = row.get('evidence_types', [])
        if not isinstance(outcomes, list) or not outcomes or set(outcomes) - known:
            raise ValueError('Check must reference existing outcomes')
        if not isinstance(types, list) or not types or set(types) - EVIDENCE_TYPES:
            raise ValueError('Check needs evidence types')
        cwd = row.get('cwd', '.')
        if not safe_relative(repository.root, cwd).is_dir():
            raise ValueError('Check package does not exist')
        kind = row.get('kind', 'native')
        if kind == 'file':
            if set(types) - {'documentation', 'preservation'}:
                raise ValueError('File assertions cannot establish behavior or tests')
            safe_relative(repository.root, row['path'])
            if not row.get('assertions'):
                raise ValueError('A file check needs assertions')
            for assertion in row['assertions']:
                if assertion.get('kind') not in {'exists', 'nonempty', 'contains', 'unchanged'}:
                    raise ValueError('Unknown file assertion')
                if assertion['kind'] == 'contains' and not isinstance(assertion.get('value'), str):
                    raise ValueError('contains needs literal text')
                if assertion['kind'] == 'unchanged' and row['path'] not in payload.get('protected_files', []):
                    raise ValueError('Only explicitly protected files may be frozen byte for byte')
            if 'preservation' in types and not any(a['kind'] == 'unchanged' for a in row['assertions']):
                raise ValueError('Byte preservation needs an unchanged assertion')
        elif kind == 'browser':
            from coder_browser_schema import validate_flow
            validate_flow(row)
            preview = next((c for c in payload.get('verification', {}).get('checks', []) if c.get('kind') == 'browser' and c.get('cwd', '.') == cwd), None)
            if not preview:
                raise ValueError('No discovered preview server for this package')
            row['server_command'] = preview['server_command']
        elif kind == 'native':
            validate_command(row.get('command'))
            files = row.get('files', {})
            if not isinstance(files, dict):
                raise ValueError('Audit files must map relative names to content')
            for name, content in files.items():
                safe_relative(repository.state / 'audit', name)
                if not isinstance(content, str):
                    raise ValueError('Audit file contents must be text')
                if name.endswith('.py'):
                    ast.parse(content)
                if re.search(r'\b(?:selenium|playwright|puppeteer)\b', content) and re.search(
                        r'https?://(?:localhost|127\.0\.0\.1):\d+', content):
                    raise ValueError('Browser audit assumes a server on a fixed localhost port. Use kind:"browser" '
                                     'with steps so the controller starts the actual project on an isolated port.')
            if not files and not row.get('binary'):
                raise ValueError('A native audit needs test files or a real binary')
            if row.get('binary'):
                safe_relative(safe_relative(repository.root, cwd), row['binary'])
            for subject in row.get('subjects', []):
                if not safe_relative(repository.root, subject).is_file():
                    raise ValueError('Native test subjects must exist in the implemented project')
            row['is_test'] = True
        else:
            raise ValueError('Use native tests, optional file assertions or browser behavior')
        row.update(id=f'audit-{index+1}', origin='independent', cwd=cwd, kind=kind)
        checks.append(row)
    return checks


def executable_identity(check):
    from coder_check_schema import executable_identity as program_identity
    files = {}
    for name, content in check.get('files', {}).items():
        runner = 'python' if name.endswith('.py') else 'node' if Path(name).suffix in {'.js', '.mjs', '.cjs'} else 'shell'
        files[name] = program_identity({'runner': runner, 'program': content, 'bindings': []})
    return digest({'command': check.get('command'), 'files': files, 'steps': check.get('steps'),
                   'assertions': check.get('assertions'), 'path': check.get('path'), 'binary': check.get('binary')})


def author(store, operation_id, repository, read_only):
    payload = store.get(operation_id)['payload']
    def review_payload(answer):
        if payload.get('planning_fallback') and answer.get('brief'):
            return {**payload, 'brief':scope_brief(normalize_brief(answer['brief']), payload)}
        return {**payload, 'brief':scope_brief(payload['brief'], payload)}
    def validate_author(answer):
        resolved = review_payload(answer)
        checks = normalize_checks(answer, repository, resolved)
        missing = []
        for outcome in resolved['brief']['outcomes']:
            for kind in set(outcome['evidence_types']) & {'behavior', 'documentation'}:
                if not any(outcome['id'] in c['outcomes'] and kind in c['evidence_types'] for c in checks):
                    missing.append(outcome['id'] + ':' + kind)
        if missing:
            raise ValueError('Independent checks omit required coverage: ' + ', '.join(missing) +
                             '. Include file assertions for requested documentation even when files are missing.')
    instruction = AUTHOR
    if payload.get('planning_fallback'):
        instruction += '\nPlanning format recovery failed. Independently reconstruct requested outcomes from the original request and include brief:{outcomes:[{text,evidence_types}],constraints:[],components:[],approach:[],validation:[]} alongside checks. The controller assigns o1, o2, ... in order.'
    answer = read_only(store, operation_id, repository, instruction, payload.get('evidence', {}),
                       validate=validate_author)
    resolved = review_payload(answer)
    return {'checks': normalize_checks(answer, repository, resolved), 'notes': answer.get('notes', []), 'brief':resolved['brief']}


def judge(store, operation_id, repository, read_only):
    payload = store.get(operation_id)['payload']
    def validate(answer):
        if answer.get('disposition') not in {'accepted', 'application_defect', 'check_defect', 'environment', 'ambiguous'}:
            raise ValueError('Review needs a diagnosis')
        rows = answer.get('outcomes', [])
        if {r.get('id') for r in rows} != {r['id'] for r in payload['brief']['outcomes']} or len(rows) != len(payload['brief']['outcomes']):
            raise ValueError('Review must cover every requested outcome once')
        if any(r.get('status') not in {'passed', 'failed', 'unverified'} or not r.get('reason') for r in rows):
            raise ValueError('Outcomes need a status and reason')
        checks = payload.get('evidence', {}).get('checks', [])
        broken_audits = [c['id'] for c in checks if audit_program_error(c)]
        if answer['disposition'] == 'application_defect' and broken_audits:
            raise ValueError('Correct the independent audit program before requesting application repair: ' +
                             ', '.join(broken_audits) + '. Its own code raised an error before establishing behavior. '
                             'Use check_defect with executable replacements, or ambiguous if it cannot be resolved.')
        if answer['disposition'] == 'environment' and not any(c.get('environment_fault') for c in checks):
            raise ValueError('No failing environment check supports this diagnosis. Missing project deliverables or '
                             'undeclared dependencies are application defects; incorrect audit targets are check defects. '
                             'Use ambiguous only if the available source and evidence cannot resolve the cause.')
        if answer['disposition'] == 'accepted':
            summary = verification_summary({**payload, 'checks':checks}, answer)
            if not summary['accepted']:
                raise ValueError('Acceptance lacks passing evidence: ' +
                                 str([{k:r[k] for k in ('id','status','missing_evidence')} for r in summary['outcomes']]) +
                                 '. Diagnose incomplete application work or faulty checks instead.')
    evidence = payload.get('evidence', {})
    gaps = verification_summary({**payload, 'checks':evidence.get('checks', [])}, {})
    answer = read_only(store, operation_id, repository, JUDGE,
                       {**evidence, 'evidence_gaps':[{k:r[k] for k in ('id','missing_evidence')} for r in gaps['outcomes']]},
                       validate=validate)
    answer['revision_id'] = payload['revision_id']
    if answer['disposition'] == 'check_defect':
        try:
            answer['replacement_checks'] = corrected_checks(answer, payload.get('audit_plan', []), repository, payload)
        except (ValueError, KeyError, TypeError) as error:
            answer.update(disposition='ambiguous', correction_error=str(error))
    return answer


def audit_program_error(check):
    """A traceback ending in audit code is not an application failure.

    Assertion failures remain for review: their cause may be the application or
    the expectation. Errors whose final frame is project code also stay there.
    """
    if check.get('passed') or check.get('origin') != 'independent' or not check.get('audit_dir'):
        return False
    log = check.get('log_tail', '')
    frames = re.findall(r'^\s*File "([^"]+)", line \d+', log, re.M)
    if not frames or not re.search(r'^(?:NameError|UnboundLocalError|SyntaxError|IndentationError):', log, re.M):
        return False
    return Path(frames[-1]).is_relative_to(Path(check['audit_dir']))


def corrected_checks(review, previous, repository, payload):
    changes = review.get('corrections', [])
    if not changes:
        raise ValueError('Check defect needs an executable replacement')
    replacements = {r['id']: r for r in previous}
    seen = set()
    for correction in changes:
        identity = correction['check_id']
        if identity in seen or identity not in replacements or not correction.get('reason'):
            raise ValueError('Correction must identify a check and its defect')
        seen.add(identity)
        old = replacements[identity]
        new = normalize_checks({'checks': [correction['replacement']]}, repository, payload)[0]
        if set(new['outcomes']) != set(old['outcomes']) or set(new['evidence_types']) != set(old['evidence_types']):
            raise ValueError('Correction changed outcome coverage')
        if executable_identity(old) == executable_identity(new):
            raise ValueError('Explanation-only change is not an executable correction')
        new['id'] = identity
        replacements[identity] = new
    return [replacements[c['id']] for c in previous]


def verification_summary(job, review):
    """Controller-owned registry: model verdicts cannot invent execution evidence."""
    checks = job.get('checks', [])
    rows = {r['id']: r for r in review.get('outcomes', [])}
    outcomes = []
    for outcome in job['brief']['outcomes']:
        evidence, missing = [], []
        for kind in outcome['evidence_types']:
            matches = [c for c in checks if c.get('passed') and c.get('revision_id') == job['revision_id'] and (
                kind == 'tests' and c.get('origin') == 'project' and c.get('is_test') and c.get('coverage_observed') or
                kind != 'tests' and (c.get('origin') == 'independent' or kind == 'preservation' and c.get('origin') == 'controller') and outcome['id'] in c.get('outcomes', []) and
                kind in c.get('evidence_types', []) and c.get('coverage_observed'))]
            if kind == 'visual' and job.get('visual_review', {}).get('status') == 'passed':
                evidence.append('visual:' + job['revision_id'])
            elif matches:
                evidence.extend(c['execution_id'] for c in matches)
            else:
                missing.append(kind)
        verdict = rows.get(outcome['id'], {'status': 'unverified', 'reason': 'Independent review incomplete'})
        outcomes.append({**outcome, **verdict, 'status': 'unverified' if missing and verdict['status'] == 'passed' else verdict['status'],
                         'missing_evidence': missing, 'executions': sorted(set(evidence))})
    failures = [c for c in checks if not c.get('passed')]
    accepted = review.get('disposition') == 'accepted' and bool(outcomes) and all(o['status'] == 'passed' for o in outcomes) and not failures
    return {'revision_id': job['revision_id'], 'accepted': accepted, 'outcomes': outcomes,
            'passed': [o['id'] for o in outcomes if o['status'] == 'passed'],
            'failed': [o['id'] for o in outcomes if o['status'] == 'failed'],
            'unverified': [o['id'] for o in outcomes if o['status'] == 'unverified'],
            'executions': checks, 'summary': review.get('summary', 'Independent review is incomplete')}
