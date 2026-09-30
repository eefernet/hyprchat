"""Controller-owned policy-7 evidence and decisions, shared by both dispatchers."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re

KINDS = {'behavior', 'documentation', 'tests', 'preservation'}
MAX_REPAIRS = 2
MAX_AUDIT_CORRECTIONS = 2
# Limits a Continue cannot lift: the repair/correction rounds are spent, or the editor has nothing left
# to change. A spent model-call allowance is NOT one of them: Continue grants a fresh allowance.
DURABLE_LIMITS = {'application_repairs', 'audit_corrections', 'no_progress'}


def runnable(job):
    current = [c for c in job.get('checks', []) if c.get('revision_id') == job.get('revision_id')]
    launches = [c for c in current if c.get('phase') == 'launch']
    if launches:
        return all(c.get('passed') for c in launches)
    if re.search(r'\bweb|browser|frontend|fullstack|react', job.get('user_task', job.get('task', '')), re.I):
        # A static site has no launch command; an observed browser/service check proves it runs.
        return any(c.get('passed') and (c.get('service_bindings') or 'browser' in c.get('test_interfaces', [])) for c in current)
    return any(c.get('execution_succeeded') and c.get('passed') for c in current)


def requested_interfaces(task):
    """Explicit controls the user named: CSS id selectors and long CLI flags.

    Model-written tests, audits and reviews share the model's misreadings, so
    these are checked by the controller straight from the request text.
    """
    text = task or ''
    selectors = list(dict.fromkeys(re.findall(r'(?<![\w&/(\]])#([A-Za-z][\w-]{1,40})(?![\w-])', text)))
    # Hex colours are not controls; three-letter words such as #add are.
    selectors = [v for v in selectors if not re.fullmatch(r'[0-9a-fA-F]{6}|[0-9a-fA-F]{8}|(?=[0-9a-fA-F]{3,4}$)(?=.*\d).*|[fF]{3}|0{3}', v)]
    flags = list(dict.fromkeys(re.findall(r'(?<![\w-])(--[a-z][a-z0-9-]{1,40})(?![\w-])', text)))
    # Launcher/package-manager options in a quoted run command are not application interfaces.
    flags = [v for v in flags if v not in {'--host', '--port', '--reload', '--bind', '--save', '--save-dev', '--prefix',
                                           '--workers', '--app-dir', '--no-audit', '--no-fund', '--package-lock'}]
    labels, buttons, conditional = requested_names(text)
    return {'selectors': selectors, 'flags': flags, 'labels': labels, 'buttons': buttons, 'conditional_buttons': conditional}


_NAME_WORD = r"[A-Za-z0-9][\w'/-]*"
_LEADING_VERB = re.compile(r'^(?:show|display|provide|include|render|offer|add|use|have|expose|give|put)\s+', re.I)
# A Title-Case name of one to four words ("Save task"), and a comma/and list of them ("Edit and Delete").
_NAME = r"[A-Z][\w'/-]*(?:\s+(?!buttons?\b|and\b|or\b)[a-z][\w'/-]*){0,3}"
_NAME_LIST = _NAME + r'(?:\s*,\s*' + _NAME + r')*(?:,?\s+(?:and|or)\s+' + _NAME + r')?'
_CONDITIONAL = re.compile(r'\bfor each\b|\bper (?:item|task|row|entry|record|project)\b|\bwhen editing\b|\bwhile editing\b|\bon each\b', re.I)


def _name_tokens(listing):
    """Title-Case names of one to four words from a comma/and list; quotes and backticks stripped."""
    names = []
    for item in re.split(r'\s*,\s*|\s+and\s+|\s+or\s+|\s*/\s*(?=[A-Z])', listing or ''):
        item = item.strip().strip('`"\'.:;()').strip()
        item = re.sub(r'\s+(?:and|or)$', '', item)
        # "Show Edit" -> "Edit", but "Add task" stays: the verb is only dropped when a Title-Case name remains.
        stripped = _LEADING_VERB.sub('', item) if len(item.split()) > 1 else item
        item = stripped if stripped[:1].isupper() else item
        words = item.split()
        if not 1 <= len(words) <= 4 or not words[0][:1].isupper():
            continue
        if not all(re.fullmatch(_NAME_WORD, w) for w in words):
            continue
        if item not in names:
            names.append(item)
    return names


def requested_names(text):
    """(labels, buttons, conditional_buttons) the request names verbatim.

    "accessible labels A, B, C" and "labels: A, B" name form controls the page must expose through their
    label text; "buttons X and Y" / "X and Y buttons" name visible buttons; a clause with "for each" or
    "when editing" names buttons that only exist once an item does (Edit, Delete, Save task). The medium
    board (2026-09-26) failed every browser verdict at get_by_label('Project name') while its own tests
    and audits never used a label, so the controller reads them from the request itself.
    """
    labels, buttons, conditional = [], [], []
    for sentence in re.split(r'[.;\n]+', text or ''):
        sentence = sentence.strip()
        if not sentence:
            continue
        for match in re.finditer(r'\b(?:accessible\s+labels?|labels?\s*:)\s*(?P<list>.+?)(?=,?\s+and\s+(?:the\s+)?buttons?\b|$)', sentence, re.I):
            labels.extend(n for n in _name_tokens(match.group('list')) if n not in labels)
        for part in re.split(r',\s*and\s+', sentence):
            if not re.search(r'\bbuttons?\b', part, re.I):
                # "..., and Save task when editing": a button named by its editing role alone.
                for match in re.finditer(r"(?P<name>(?<![\w-])[A-Z][\w'/-]*(?:\s+[a-z][\w'/-]*){0,3})\s+(?:when|while)\s+editing\b", part):
                    conditional.extend(n for n in _name_tokens(match.group('name')) if n not in conditional)
                continue
            target = conditional if _CONDITIONAL.search(part) else buttons
            found = []
            for match in re.finditer(r'\bbuttons?\s+(?:named\s+|called\s+|labell?ed\s+)?(?P<list>[A-Z`"\'].+?)(?=\s+(?:for|when|while|that|which|to)\b|$)', part):
                found.extend(_name_tokens(match.group('list')))
            for match in re.finditer(r'(?<![\w-])(?P<list>' + _NAME_LIST + r')\s+buttons?\b', part):
                found.extend(_name_tokens(match.group('list')))
            target.extend(n for n in found if n not in target)
    buttons = [b for b in buttons if b not in conditional]
    return labels, buttons, conditional


_LEAVE = r"(?:do(?:es)?\s+not|don'?t|never|must\s+not|should\s+not)\s+(?:edit|change|modify|touch|alter|rewrite)"
_KEPT = r'(?:byte.for.byte\s+)?(?:unchanged|untouched|identical|as.is|alone|intact)'


def protected_paths(task, paths):
    """Existing files the request says to leave alone, read by the controller straight from the text.

    Only a phrase that governs the file itself counts ("do not modify X", "leave X untouched",
    "X must remain unchanged"). "Do not change the public API of X" constrains X's behaviour and
    must not forbid the edit the request asks for. A bare file name must be unambiguous.
    """
    names, owners = {path: path for path in paths}, {}
    for path in paths:
        owners.setdefault(PurePosixPath(path).name, []).append(path)
    for base, holders in owners.items():
        if base not in names and len(holders) == 1:
            names[base] = holders[0]
    found = []
    for line in (task or '').splitlines():
        for name, path in names.items():
            if name not in line:
                continue
            # Not "utils.py's public API" (a constraint ABOUT the file) and not the longer name app.py.bak.
            target = r'(?:the\s+)?(?:file\s+)?[`\'"]?(?<![\w./-])' + re.escape(name) + r'(?![\w/-]|\.\w|[\'’]s\b)[`\'"]?'
            if re.search(_LEAVE + r'\s+' + target, line, re.I) or re.search(
                    r'\b(?:leave|keep)\s+' + target + r'\s+' + _KEPT, line, re.I) or re.search(
                    target + r'\s+(?:must|should|has\s+to|needs\s+to|is\s+to)?\s*(?:stay|remain|be\s+left|be\s+kept)\s+' + _KEPT, line, re.I):
                found.append(path)
    return list(dict.fromkeys(found))


def is_documentation(path):
    """Human documentation by name. A file check on source code or a data file is not documentation
    evidence; plain .txt counts only as a named document or under a docs folder."""
    if not isinstance(path, str) or not path:
        return False
    parts = PurePosixPath(path)
    name, suffix = parts.name.lower(), parts.suffix.lower()
    named = name.split('.')[0] in {'readme', 'changelog', 'usage', 'install', 'guide', 'manual', 'contributing', 'history'}
    if suffix in {'.md', '.markdown', '.rst', '.adoc'}:
        return True
    if suffix in {'', '.txt'}:
        return named or (suffix == '.txt' and any(part.lower() in {'docs', 'doc', 'documentation'} for part in parts.parts[:-1]))
    return False


def interface_files(kind, token, sources):
    """Delivered source files that define or handle a requested control.

    A CSS id must appear AS an id: quoted (id="total", getElementById('total')), after # (a CSS or
    querySelector selector) or as an unquoted HTML attribute. The bare word also matched a Python
    local `total = 0` or a comment, which defeated "do not substitute another control".
    """
    if kind == 'selector':
        name = re.escape(token)
        pattern = '#' + name + r'(?![\w-])|(["\'`])' + name + r'\1|\bid\s*=\s*' + name + r'(?![\w-])'
    else:
        pattern = r'(?<![\w-])' + re.escape(token) + r'(?![\w-])'
    found = []
    for name, text in sources.items():
        text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
        text = '\n'.join(line for line in text.splitlines() if not re.match(r'\s*(?://|\*)' + (r'|\s*#' if PurePosixPath(name).suffix in {'.py', '.sh'} else ''), line))
        declared = re.search(pattern, text)
        if kind == 'flag' and not declared:
            # Go flag/pflag names omit the leading -- in declarations.
            flag = re.escape(token.removeprefix('--'))
            declared = re.search(r'\b(?:flag|pflag)\.(?:String|Int|Bool|Float64|Duration)(?:Var)?P?\(\s*(?:&?\w+\s*,\s*)?[\"\']' + flag + r'[\"\']', text)
        if declared:
            found.append(name)
    return sorted(found)


def outcome(value, index):
    row = {'text': value} if isinstance(value, str) else dict(value)
    if not isinstance(row.get('text'), str) or not row['text'].strip():
        raise ValueError('Outcome needs requested-result text')
    kinds = set(row.get('evidence_types', []))
    # Kinds read from the wording ADD gates to an outcome that stated none; they never replace the
    # behavior gate ("Manage uploaded documents" once needed only a README).
    implicit = not kinds
    text = row['text'].lower()
    # Older briefs and combined requirements must not lose deliverable gates.
    # Wording the request is likely to use. A miss here WAIVES the gate (make_brief), so err wide:
    # "with docs and automated testing" once matched neither pattern.
    from coder_criteria import deliverables
    requested = deliverables(text)
    if 'documentation' in requested:
        kinds.add('documentation')
    if 'tests' in requested:
        kinds.add('tests')
    # Deliverables the user never requested are not acceptance gates, whatever the planner wrote.
    waived = set(row.get('waived', [])) & {'tests', 'documentation'}
    kinds -= waived
    if implicit or not kinds:
        kinds.add('behavior')
    if kinds - KINDS:
        raise ValueError('Unknown required evidence type')
    component = row.get('component', '.')
    # Evidence binds to package directories; planners sometimes name a file instead.
    if isinstance(component, str) and PurePosixPath(component).suffix:
        component = str(PurePosixPath(component).parent)
    # A test or docs folder is part of its package, which is where checks run.
    while isinstance(component, str) and PurePosixPath(component).name.lower() in {'tests', 'test', '__tests__', 'spec', 'specs', 'docs', 'doc'}:
        component = str(PurePosixPath(component).parent)
    if not isinstance(component, str) or PurePosixPath(component).is_absolute() or '..' in PurePosixPath(component).parts:
        raise ValueError('Component must be a relative project path')
    interfaces = set() if 'tests' in waived else set(row.get('test_interfaces', []))
    if 'tests' in kinds:
        if 'browser' in text:
            interfaces.add('browser')
        # An HTTP API, not a function's "API": observed only as /api/ traffic.
        if re.search(r'/api\b|\b(?:rest|http|json|web)\s+api\b|\bapi\s+(?:tests?|endpoints?|routes?|checks?|regression)\b', text):
            interfaces.add('api')
    return {'id': f'o{index + 1}', 'text': row['text'], 'evidence_types': sorted(kinds),
            'component': component, 'test_interfaces': sorted(interfaces), **({'waived': sorted(waived)} if waived else {})}


def within(path, component):
    return component == '.' or path == component or path.startswith(component.rstrip('/') + '/')


def repair_demonstrated(row, old):
    """A user-owned test that failed at baseline and passes now, with the SAME suite.

    Subset was not enough: deleting the failing test file leaves a byte-identical remainder that
    passes. Every baseline test file must still run unchanged, none may be added, and when both
    runs report a count the repaired run may not execute fewer tests (deselect/-k/addopts).
    """
    if not (old and not old.get('passed') and row.get('passed') and row.get('origin', 'project') == 'project'):
        return False
    files = {(f['path'], f['sha256']) for f in row.get('test_files', [])}
    if not files or files != {(f['path'], f['sha256']) for f in old.get('test_files', [])}:
        return False
    if old.get('command') is not None and row.get('command') is not None and old['command'] != row['command']:
        return False  # a narrowed command (-k, -t, a different script) is not the user's suite
    before, after = old.get('test_count'), row.get('test_count')
    if before is not None and after is not None and after < before:
        return False
    # Jest, vitest and node --test count skipped tests in their totals.
    return _unrun(row) <= _unrun(old)


def _unrun(row):
    return sum(int(n) for n in re.findall(r'(\d+)\s+(?:skipped|deselected|pending|todo|ignored)\b', str(row.get('log_tail') or ''), re.I))


def behavior_peers(requirement, outcomes):
    """Behavior outcomes whose component overlaps this one (either may enclose the other)."""
    component = requirement.get('component', '.')
    return [o for o in outcomes if 'behavior' in o.get('evidence_types', []) and
            (within(o.get('component', '.'), component) or within(component, o.get('component', '.')))]


def needs_behavior_audit(outcomes, checks):
    """The independent behavior audit is skipped only for outcomes a repaired user test can prove:
    a lone behavior outcome in its component. "Fix A and add B" still needs evidence for B."""
    repaired = any(c.get('repair_demonstrated') and c.get('passed') for c in checks)
    behavior = [o for o in outcomes if 'behavior' in o.get('evidence_types', [])]
    return any(not repaired or len(behavior_peers(o, outcomes)) != 1 for o in behavior)


def evidence_matches(check, requirement, kind, revision, solo=True):
    if not (check.get('passed') and check.get('revision_id') == revision and check.get('execution_id')):
        return False
    if check.get('provenance_kind') == 'compiled_subprocess':
        from coder_process_evidence import VERSION
        process = check.get('process_evidence') or {}
        if process.get('version') != VERSION or process.get('revision_id') != revision or not process.get('processes'):
            return False
    component = requirement.get('component', '.')
    if kind == 'tests':
        return (check.get('origin') == 'project' and check.get('is_test') and
                check.get('coverage_observed') and check.get('execution_succeeded') and
                within(check.get('cwd', '.'), component) and bool(check.get('test_files')))
    bound = any(within(b['path'], component) for b in [*check.get('source_bindings', []), *check.get('service_bindings', [])])
    if kind == 'behavior' and solo and check.get('repair_demonstrated') and check.get('is_test') and check.get('origin', 'project') == 'project':
        # Unchanged user test: failed at baseline, passes on this revision with observed assertions.
        # It carries no outcome IDs, so it speaks only for a lone behavior outcome in its component.
        return bool(check.get('coverage_observed') and check.get('execution_succeeded') and bound)
    if requirement['id'] not in check.get('outcomes', []) or kind not in check.get('evidence_types', []):
        return False
    if kind == 'preservation':
        return check.get('origin') == 'controller' and bool(check.get('file_bindings'))
    if kind == 'documentation':
        # A package is documented by its own files or by an enclosing package's README.
        return bool(check.get('file_bindings')) and all(is_documentation(b['path']) for b in check['file_bindings']) and all(within(b['path'], component) or
            within(component, str(PurePosixPath(b['path']).parent)) for b in check['file_bindings'])
    return (check.get('origin') == 'independent' and check.get('coverage_observed') and
            check.get('execution_succeeded') and any(within(b['path'], component)
                for b in [*check.get('source_bindings', []), *check.get('service_bindings', [])]))


STATUSES = {'passed', 'failed', 'unverified'}
DIAGNOSES = {'application_defect', 'audit_defect', 'environment', 'unresolved'}
ADVISORY_PHASES = {'lint', 'typecheck'}


def normalize_verdict(verdict, outcomes):
    """Coerce a reviewer reply into the controller's shape, or raise for a format retry.

    Only per-outcome statuses and per-check diagnoses carry weight. Free-form
    scope fields are not gates: local models fill them with outcome IDs.
    """
    if not isinstance(verdict, dict) or not isinstance(verdict.get('outcomes'), list):
        raise ValueError('Review needs an outcomes list')
    known = [o['id'] for o in outcomes]
    rows = {}
    for row in verdict['outcomes']:
        if isinstance(row, dict) and row.get('id') in known and row['id'] not in rows:
            status = str(row.get('status', '')).strip().lower()
            if status not in STATUSES:
                raise ValueError('Outcome status must be passed, failed or unverified: ' + str(row.get('id')))
            rows[row['id']] = {'id': row['id'], 'status': status, 'reason': str(row.get('reason', ''))[:2000]}
    absent = [name for name in known if name not in rows]
    if absent:
        raise ValueError('Review must give a status for every outcome ID: ' + ', '.join(absent))
    diagnoses = []
    for row in verdict.get('diagnoses') or []:
        if isinstance(row, dict) and isinstance(row.get('check_id'), str):
            kind = str(row.get('kind', '')).strip().lower()
            diagnoses.append({**row, 'kind': kind if kind in DIAGNOSES else 'unresolved',
                              'outcomes': [v for v in row.get('outcomes') or [] if v in known]})
    scope = verdict.get('scope_complete')
    scope = scope.strip().lower() not in {'false', 'no', '0'} if isinstance(scope, str) else scope is not False
    notes = [str(v)[:500] for v in verdict.get('notes') or [] if isinstance(v, (str, int, float))][:10]
    return {'scope_complete': bool(scope), 'outcomes': [rows[name] for name in known], 'diagnoses': diagnoses, 'notes': notes}


def blocking(check):
    """Discovered lint/typecheck phases are reported, not release gates. Setup, build,
    tests, launch, audits, protected files and retained baseline failures still block."""
    if check.get('passed'):
        return False
    if check.get('origin') == 'controller' and check.get('phase') == 'visual' and check.get('skipped') and check.get('optional_visual'):
        return False
    return not (check.get('origin') == 'project' and check.get('phase') in ADVISORY_PHASES and not check.get('is_test'))


def acceptance(outcomes, checks, verdict, revision, request=None, history=()):
    from coder_criteria import EVIDENCE_VERSION, criteria
    obligations = criteria(request, outcomes, history)
    reported = {o.get('id'): o for o in verdict.get('outcomes', [])}
    rows = []
    requirements = [{**outcome(original, index), 'id': original['id']} for index, original in enumerate(outcomes)]
    for requirement in requirements:
        executions, missing = [], []
        solo = len(behavior_peers(requirement, requirements)) == 1
        for kind in requirement['evidence_types']:
            matches = [c for c in checks if evidence_matches(c, requirement, kind, revision, solo)]
            if kind == 'behavior':
                # Generated audits and an unmapped user suite cannot prove a request clause.
                matches = []
            if kind == 'tests':
                interfaces = {value for c in matches for value in c.get('test_interfaces', [])}
                if set(requirement.get('test_interfaces', [])) - interfaces:
                    matches = []
            executions.extend(c['execution_id'] for c in matches)
            if not matches:
                missing.append(kind)
        report = reported.get(requirement['id'], {})
        status = report.get('status', 'unverified')
        if status == 'passed' and missing:
            status = 'unverified'
        rows.append({**requirement, 'status': status, 'missing_evidence': missing,
            'executions': sorted(set(executions)),
            'trust_origin': None if 'behavior' in requirement['evidence_types'] else 'executed_deliverable_check',
            'reason': ('Trusted independent behavioral verification is required; generated audits are diagnostic.'
                       if 'behavior' in missing else report.get('reason', 'Independent review incomplete'))})
    # Scope is established per outcome by executed evidence plus the reviewer's
    # status; an explicit scope_complete=false still withholds acceptance.
    accepted = (not obligations and bool(rows) and verdict.get('scope_complete') is not False and
                all(r['status'] == 'passed' for r in rows) and bool(checks) and
                all(c.get('revision_id') == revision for c in checks) and
                not any(blocking(c) for c in checks))
    return {'accepted': accepted, 'evidence_version': EVIDENCE_VERSION, 'criteria': obligations,
        'revision_id': revision, 'outcomes': rows, 'checks': checks,
        'advisory': [c['id'] for c in checks if not c.get('passed') and not blocking(c)],
        **{name: [r['id'] for r in rows if r['status'] == status]
           for name, status in [('passed', 'passed'), ('failed', 'failed'), ('unverified', 'unverified')]}}


def failure_signature(checks):
    rows = []
    for check in checks:
        text = str(check.get('log_tail') or check.get('reason') or check.get('classification') or '')
        text = re.sub(r'(?:/[^\s\"\']+)/(?:project|workspace|audit)(?=/|\b)', '<execution>', text)
        text = re.sub(r'127\.0\.0\.1:\d+', '127.0.0.1:<port>', text)
        text = re.sub(r'\b\d+(?:\.\d+)?\s*(?:seconds?|ms|s)\b', '<duration>', text)
        text = re.sub(r'0x[0-9a-fA-F]+', '<address>', text)
        rows.append((check['id'], check.get('classification'), text))
    return hashlib.sha256(json.dumps(sorted(rows), sort_keys=True).encode()).hexdigest()


def decide(job, summary, verdict):
    """Diagnose all failures before choosing an action; budgets are durable."""
    checks = job.get('checks', [])
    failed = [c for c in checks if blocking(c)]
    diagnoses = {d.get('check_id'): d for d in verdict.get('diagnoses', [])}
    defects, audit_faults, environments, existing = [], [], [], []
    for check in failed:
        category = check.get('classification')
        diagnosis = diagnoses.get(check['id'], {}).get('kind')
        application_exception = (diagnosis == 'application_defect' and check.get('exit_code') not in (None, 0)
                                 and bool(check.get('source_bindings') or check.get('service_bindings')))
        if category == 'source_mutation' and check.get('origin') != 'independent':
            # No origin, so every branch below missed it and the job ended with a generic candidate.
            defects.append({**check, 'classification': 'application_defect', 'reason':
                'Running the project checks rewrote tracked project files: ' + ', '.join(check.get('paths', [])[:8]) + '. Checks run on an '
                'immutable copy of the source. Tests and the application must write runtime data (JSON stores, databases, caches, '
                'generated output) to a temporary directory or a path taken from an environment variable, never to files in the project.'})
        elif category == 'existing_failure':
            references = set(diagnoses.get(check['id'], {}).get('outcomes', []))
            known = {o['id'] for o in summary['outcomes']}
            if diagnosis == 'application_defect' and references and references <= known:
                defects.append(check)
            else:
                existing.append(check)
        elif category == 'environment':
            environments.append(check)
        elif check.get('origin') == 'independent' and (category in {
                'audit_defect', 'missing_provenance'} or diagnosis == 'audit_defect' or
                category == 'missing_coverage' and not application_exception):
            audit_faults.append(check)
        elif category == 'application_defect' or application_exception or (check.get('origin') == 'project' and category in {'missing_coverage', 'missing_provenance'}) \
                or (check.get('origin') == 'controller' and category == 'unverified_interface') or (category == 'unresolved' and
                diagnosis == 'application_defect' and check.get('coverage_observed') and
                (check.get('source_bindings') or check.get('service_bindings'))):
            if check.get('origin') == 'project' and category == 'missing_coverage':
                check = {**check, 'reason': 'This delivered test exited without executing any assert statement (skipped, '
                    'nothing collected, or success was only printed). Remove skip/try-except guards around its imports '
                    'and run real assertions against the application with the registered interpreter.'}
            elif check.get('origin') == 'project' and check.get('is_test') and not check.get('reason'):
                check = {**check, 'reason': 'A delivered test fails. The defect may be in the application OR in this test '
                    '(wrong selector, leftover state from earlier steps, a count over unrelated elements). Read the traceback, '
                    'inspect both, and fix whichever is wrong without weakening the requested behavior.'}
            defects.append(check)
        elif check.get('origin') == 'independent':
            # A failing audit check nobody confirmed as an application defect is the audit
            # author's to correct; it must never fall through unrouted.
            audit_faults.append(check)
    unexecuted = []
    for row in summary['outcomes']:
        for kind in row['missing_evidence']:
            if kind == 'tests':
                available = [c for c in checks if c.get('origin') == 'project' and c.get('is_test') and
                             within(c.get('cwd', '.'), row.get('component', '.'))]
                discovered = [c for c in job.get('execution_profile', {}).get('checks', []) if c.get('is_test') and
                              within(c.get('cwd', '.'), row.get('component', '.'))]
                if available:
                    # Failed tests already have their own grounded diagnosis.
                    if all(c.get('passed') for c in available):
                        defects.append({'id': row['id'] + ':tests', 'classification': 'application_defect',
                            'reason': 'Delivered tests lack the requested interface coverage or application provenance', 'outcome': row})
                elif discovered:
                    if not environments and not failed:
                        unexecuted.append(row)
                else:
                    defects.append({'id': row['id'] + ':tests', 'classification': 'application_defect',
                                    'reason': 'Requested executable project tests are missing', 'outcome': row})
            elif kind == 'documentation' and row['id'] in job.get('missing_deliverables', []):
                defects.append({'id': row['id'] + ':documentation', 'classification': 'application_defect',
                                'reason': 'Requested documentation is missing', 'outcome': row})
            elif kind not in {'preservation', 'behavior'}:
                audit_faults.append({'id': row['id'] + ':' + kind, 'reason': 'Independent coverage is missing'})
    # The reviewer may fail an outcome whose checks all pass (thin documentation, a gap the
    # checks do not exercise). That is a grounded repair request, bounded by the repair rounds.
    if not failed:
        for row in summary['outcomes']:
            if row.get('status') == 'failed' and not row.get('missing_evidence') and row.get('reason'):
                defects.append({'id': row['id'] + ':review', 'classification': 'application_defect',
                                'reason': row['reason'], 'outcome': row})
    # A faulty audit cannot justify an edit, but it cannot veto other confirmed defects.
    if defects:
        signature = failure_signature(defects)
        patch = job.get('last_patch') or {}
        # The editor's final answer for the round was "the application already does this": it changed nothing,
        # stopped, or finished with a no-op. Against ONLY independent audit checks, the audit is the likelier
        # culprit (wrong fixture reading, URL, selector, expected value) - live: an audit read `## Conclusion`
        # as level 1, the reviewer agreed three times, and a correct program spent both repair rounds. This is
        # judged before the repair cap and without demanding an identical failure signature; both caps still bound it.
        disputed = bool(job.get('unchanged_source') or job.get('editor_stopped') or
                        (patch.get('category') == 'no_op' and patch.get('agent_finished')))
        if disputed and all(d.get('origin') == 'independent' for d in defects) and job.get('audit_corrections', 0) < MAX_AUDIT_CORRECTIONS:
            return {'action': 'audit', 'feedback': [{'id': d['id'], 'reason': 'The application repair found nothing to change for this '
                'failing audit check. Re-read the application source AND the fixture/input this audit wrote, compare with the '
                "application's actual output, and fix the audit itself (expected value, URL path, selector).",
                'log_tail': (d.get('log_tail') or '')[-600:]} for d in defects]}
        history = job.get('failed_result_history', [])
        if len(history) >= 2 and history[-2:] == [signature, signature] and not job.get('no_progress_diagnosed') and job.get('audit_corrections', 0) < MAX_AUDIT_CORRECTIONS:
            return {'action': 'audit', 'no_progress_diagnosis': True, 'feedback': [{'id': d['id'],
                'reason': 'The same failed result recurred three times without reducing failures. Diagnose against the exact authorized request and retained expected/actual values. Do not change application behavior to satisfy a wrong generated expectation or weaken user-owned tests. Preserve both audit versions.',
                'log_tail': (d.get('log_tail') or '')[-1200:]} for d in defects]}
        if job.get('repair_round', 0) >= MAX_REPAIRS:
            return {'action': 'candidate', 'reason': 'Both application repair rounds were used.', 'limit': 'application_repairs'}
        # A repair that ended because the model-call allowance ran out is not "no progress": the model was
        # cut off, not stuck (sweep C++ 2026-09-25 parked a spent allowance as a durable limit, disabling Continue).
        if patch.get('input_budget') and not patch.get('changed'):
            return {'action': 'candidate', 'limit': 'input_budget', 'reason': job.get('editor_stopped') or
                    'The builder prompt for this round exceeds the configured input budget, so the model never ran. Raise the Daedalus context window in Settings, then Continue.'}
        if patch.get('allowance_spent') and job.get('unchanged_source'):
            return {'action': 'candidate', 'limit': 'model_calls',
                    'reason': 'The model-call allowance ended during the repair before it changed any source; the checkpoint was checked. Continue grants a fresh allowance.'}
        # A stalled editor ends the job only once the durable repair rounds have had
        # their grounded attempt at the same defects.
        if signature == job.get('failure_signature') and (job.get('unchanged_source') or job.get('editor_stopped')):
            return {'action': 'candidate', 'reason': job.get('editor_stopped') or 'The focused repair made no source changes; the same defects remain.', 'limit': 'no_progress'}
        return {'action': 'repair', 'feedback': defects, 'signature': signature}
    if unexecuted:
        return {'action': 'execute', 'reason': 'Execute the delivered tests against this revision.'}
    if audit_faults:
        if job.get('audit_corrections', 0) >= MAX_AUDIT_CORRECTIONS:
            return {'action': 'candidate', 'reason': 'Both audit correction attempts were used; independent evidence is incomplete.', 'limit': 'audit_corrections'}
        return {'action': 'audit', 'feedback': audit_faults}
    if environments:
        return {'action': 'candidate', 'reason': 'The execution environment needs attention; application repairs were not exhausted.'}
    if existing:
        return {'action': 'candidate', 'reason': 'Existing baseline failures remain outside the confirmed repair scope.'}
    return {'action': 'candidate', 'reason': 'Ready for review: requested behavior lacks trusted criterion-specific verification. Generated audits cannot authorize acceptance.'}
