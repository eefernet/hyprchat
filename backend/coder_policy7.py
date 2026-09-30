"""Shared policy-7 brief/audit validation and the historical experiment adapter.

Production uses coder_policy7_controller and durable worker operations. The
Experiment journal is retained for replay; the web server never runs it.
"""
from __future__ import annotations

import ast
import copy
import re
import shlex
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from coder_inference import local_chat
from coder_patch_runtime import edit, tree_hashes, select_files
from coder_project_runtime import contract, run_revision
from coder_profiles import python_test_kind, validate_command
from coder_policy7_evidence import outcome, acceptance, decide, is_documentation
from coder_repository import Repository, safe_relative, file_hash
from coder_worker_runtime import WorkerStore
from context_policy import DEFAULTS, resolve, estimate_tokens

POLICY_VERSION = 7
MAX_REPAIRS = 2
MAX_AUDIT_CORRECTIONS = 2
MAX_BATCH_FILES = 3

EXECUTION_HELP = '''The controller runs ordinary setup, build, lint and tests discovered from manifests.
You can specify explicit commands in .daedalus.json: {"packages":{".":{"test":"python3 tests/check_app.py"}}}.
ONLY for web/fullstack apps that serve HTTP (never for a command-line program) write .daedalus-run.json with {"dependencies":{},"services":[{"id":"app","cwd":".",
"command":"python3 -m uvicorn app:app --host 127.0.0.1 --port {port}","ready_path":"/"}]}.
Dependencies map a package directory to directories it requires; all builds finish before services start.
The controller exports DAEDALUS_APP_URL for service app (and DAEDALUS_<ID>_URL for other services),
DAEDALUS_PROJECT_ROOT, DAEDALUS_AUDIT_DIR and APP_DB_PATH. Tests should use these URLs when present.
Use actual application imports/CLI/HTTP/browser behavior in tests; never reimplement application logic.
Browser tests must launch a real browser, not execute DOM scripts directly with Node.
Python Playwright is installed in the controller's DAEDALUS_AUDIT_PYTHON interpreter.
For portable browser checks, deliver tests/test_browser.py using sync_playwright(), chromium.launch(headless=True),
os.environ['DAEDALUS_APP_URL'], and plain assert statements on actual page results. Call the test from __main__.
Never wrap the Playwright import in try/except or skip when it is missing: a test that skips, collects nothing,
or prints success without executing assert statements is recorded as a FAILED delivered test.
Register the command in .daedalus.json as {"packages":{".":{"test":"\\\"$DAEDALUS_AUDIT_PYTHON\\\" tests/test_browser.py"}}}.
The app itself must use its own runtime (Node for JavaScript, Python for Python); tests may use a different HTTP client runtime.
Keep explicit user interfaces, file names, commands and protected files.
Non-Python/Node projects use their standard layout and NATIVE test framework; the controller discovers and runs them:
Java: Maven pom.xml + JUnit 5 under src/test/java (mvn test). C#: a .sln with an app project and an xUnit test project (dotnet test).
C/C++: CMakeLists.txt with enable_testing() and add_test() for assert-based test executables (ctest) - a bare Makefile is not enough.
Go: go.mod + *_test.go (go test). Rust: Cargo.toml with integration tests under tests/ (cargo test).
Tests must execute real assertions against the application code; a runner that reports zero executed tests is a FAILED delivery.'''


def json_object(text):
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('Expected one object')
    return value


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def make_brief(answer, original, inherited=()):
    outcomes = answer.get('outcomes')
    batches = answer.get('batches')
    if not isinstance(outcomes, list) or not outcomes or any(not isinstance(x, (str, dict)) for x in outcomes):
        raise ValueError('Brief needs requested outcomes as text')
    if not isinstance(batches, list) or not 1 <= len(batches) <= 4:
        raise ValueError('Brief needs one to four coherent implementation batches')
    for batch in batches:
        if not isinstance(batch, dict) or not isinstance(batch.get('task'), str) or not batch['task'].strip():
            raise ValueError('Each batch needs an implementation task')
        targets = batch.get('files', [])
        if not isinstance(targets, list) or any(not isinstance(p, str) or not p or p == '.' or Path(p).is_absolute() or '..' in Path(p).parts for p in targets):
            raise ValueError('Batch file targets must be relative file paths')
    # Globs, directories and build output are not editable source; planners sometimes list them.
    seen = set()
    for batch in batches:
        kept = []
        for target in batch.get('files', []):
            parts = Path(target).parts
            if re.search(r'[*?\[\]]', target) or target.endswith('/') or {'target', 'build', 'dist', 'node_modules',
                    '.venv', '__pycache__'} & set(parts[:-1]) or target in seen:
                continue
            seen.add(target); kept.append(target)
        batch['files'] = kept
    # One reply must fit the Settings output allowance: new files arrive as whole-file
    # blocks, so oversized batches are split here instead of truncating in the editor.
    split = []
    for batch in batches:
        targets = batch.get('files', [])
        groups = [targets[i:i + MAX_BATCH_FILES] for i in range(0, len(targets), MAX_BATCH_FILES)] or [targets]
        for index, group in enumerate(groups):
            note = '' if len(groups) == 1 else f' (step {index + 1} of {len(groups)}: only {", ".join(group)})'
            split.append({**batch, 'task': batch['task'] + note, 'files': group})
    batches = split
    from coder_scope import reconcile, VERSION as SCOPE_VERSION
    retained, history = reconcile(answer, original, inherited)
    texts, positions = [], {}
    for item in [*retained, *outcomes]:
        identity = json.dumps(outcome(item, 0), sort_keys=True)
        if identity not in positions:
            positions[identity] = len(texts)
            texts.append(item)
    for row in history:
        items = [row['parent_outcome']] if row['action'] == 'retain' else [outcomes[i - 1] for i in row['replacement_outcomes']]
        row['active_outcome_ids'] = [f'o{positions[json.dumps(outcome(item, 0), sort_keys=True)]+1}' for item in items]
    required = outcome(original, 0)
    # The request decides which deliverables are gated: a planner that adds "tests" to an app whose
    # user asked for none would make acceptance demand files nobody requested.
    unrequested = sorted({'tests', 'documentation'} - set(required['evidence_types']))
    def waive(item):
        # Only a box the planner ticked with no wording behind it: an outcome that itself names tests keeps its gate.
        named = set(outcome(item if isinstance(item, str) else {'text': item.get('text', '')}, 0)['evidence_types'])
        dropped = [kind for kind in unrequested if kind not in named]
        return item if item in retained or isinstance(item, str) or not dropped else {**item, 'waived': dropped}
    texts = [waive(item) for item in texts]
    represented = {kind for item in texts for kind in outcome(item, 0)['evidence_types']}
    for kind in set(required['evidence_types']) & {'documentation', 'tests'} - represented:
        texts.append({'text': 'Deliver the requested ' + kind, 'evidence_types': [kind],
                      'test_interfaces': required.get('test_interfaces', []) if kind == 'tests' else []})
    represented_interfaces = {name for item in texts for name in outcome(item, 0)['test_interfaces']}
    missing_interfaces = set(required['test_interfaces']) - represented_interfaces
    if missing_interfaces:
        texts.append({'text': 'Deliver executable regression tests for the requested interfaces',
                      'evidence_types': ['tests'], 'test_interfaces': sorted(missing_interfaces)})
    return {'project_name': str(answer.get('project_name') or 'Daedalus project')[:100],
            'outcomes': [outcome(item, i) for i, item in enumerate(texts)],
            'batches': batches, 'original_request': original,
            'scope_version': SCOPE_VERSION, 'requirement_history': history}


AUDIT_KEYS = ('command', 'cwd', 'outcomes', 'evidence_types', 'kind', 'path', 'assertions', 'name', 'description')


def known_keys(row):
    """A model-written audit row is data the controller reads, never fields it trusts: `classification`,
    `passed`, `file_bindings` or `repair_demonstrated` in audit.json must not reach the check pipeline."""
    return {key: row[key] for key in AUDIT_KEYS if key in row}


def audit_checks(directory, outcomes, project=None):
    """Validate executable targets before registering controller IDs and hashes."""
    metadata = json_object((directory / 'audit.json').read_text())
    rows = metadata.get('checks')
    known = {o['id'] for o in outcomes}
    if not isinstance(rows, list) or not rows:
        raise ValueError('Audit needs native check commands')
    result = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get('outcomes'), list) or not row['outcomes'] or any(not isinstance(v, str) for v in row['outcomes']) or set(row['outcomes']) - known:
            raise ValueError('Audit check must identify requested outcomes')
        kinds = row.get('evidence_types', ['behavior'] if row.get('behavior', True) else ['documentation'])
        if not isinstance(kinds, list) or not kinds or any(not isinstance(v, str) for v in kinds) or set(kinds) - {'behavior', 'documentation'}:
            raise ValueError('Independent audit cannot stand in for delivered tests or protected-file checks')
        cwd = row.get('cwd', '.')
        if not isinstance(cwd, str) or Path(cwd).is_absolute() or '..' in Path(cwd).parts:
            raise ValueError('Audit working directory must be project-relative')
        if project and not safe_relative(project, cwd).is_dir():
            raise ValueError('Audit working directory does not exist')
        if row.get('kind') == 'file':
            if kinds != ['documentation'] or not isinstance(row.get('path'), str) or not row['path'] or not isinstance(row.get('assertions'), list) or not row['assertions']:
                raise ValueError('File checks require documentation paths and assertions')
            if not is_documentation(row['path']):
                raise ValueError('File checks are documentation evidence and must name a documentation file: ' + row['path'])
            if project:
                safe_relative(project, row['path'])
            if any(not isinstance(a, dict) or a.get('kind') not in {'exists', 'nonempty', 'contains'} or
                   a.get('kind') == 'contains' and not isinstance(a.get('value'), str) for a in row['assertions']):
                raise ValueError('Unknown documentation assertion')
            result.append({**known_keys(row), 'id': f'audit-{i+1}', 'origin': 'independent', 'phase': 'test', 'evidence_types': kinds})
            continue
        command = row.get('command', '')
        if not isinstance(command, str) or not command or '{audit}' not in command:
            raise ValueError('Audit commands must execute files under {audit}')
        validate_command(command)
        words = shlex.split(command)
        if re.search(r'[;|&<>`\n]|\$\(', command):
            raise ValueError('Audit commands must run one native test runner')
        runner = Path(words[0]).name
        if runner not in {'python', 'python3', 'node', 'pytest', '$DAEDALUS_AUDIT_PYTHON'}:
            raise ValueError('Unsupported audit runner')
        if any(word in {'-c', '-e', '--eval', '--print', '-p'} for word in words[1:]):
            raise ValueError('Audit commands must execute the registered test file, not inline code')
        targets = [w[len('{audit}/'):] for w in words if w.startswith('{audit}/')]
        if not targets:
            raise ValueError('Audit command needs an explicit executable test file')
        hashes = {}
        for name in targets:
            path = safe_relative(directory, name)
            if not path.is_file() or not path.stat().st_size:
                raise ValueError('Audit executable is missing: ' + name)
            if runner != 'node' and path.suffix != '.py' or runner == 'node' and path.suffix not in {'.js', '.mjs', '.cjs'}:
                raise ValueError('Audit runner does not match its test file language')
            if path.suffix == '.py':
                ast.parse(path.read_text())
            hashes[name] = file_hash(path)
        if project:
            sources = tree_hashes(project, ('.git', '.venv', 'node_modules', '__pycache__'))
            sources = {name: value for name, value in sources.items()
                       if Path(name).suffix in {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'}
                       and not any('test' in part.lower() or 'spec' in part.lower() for part in Path(name).parts)}
            for name, value in tree_hashes(directory).items():
                if name == 'audit.json':
                    continue
                if name in sources or value in sources.values():
                    raise ValueError('Audit contains a copied or shadow application file: ' + name)
        result.append({**known_keys(row), 'id': f'audit-{i+1}', 'origin': 'independent', 'phase': 'test',
                       'is_test': True, 'evidence_types': kinds, 'audit_hashes': hashes})
    return result


def documentation_files(project, component='.'):
    """Nonempty human documentation delivered inside a component."""
    project = Path(project).resolve()   # safe_relative resolves: an unresolved root never equals a parent and the walk reached /
    base = safe_relative(project, component)
    found = []
    # The component's own documentation first, then each enclosing package up to the root.
    for folder in [base, *base.parents]:
        if folder.is_dir():
            found.extend(p for pattern in ('README*', '*.md', 'docs/*.md') for p in sorted(folder.glob(pattern))
                         if p.is_file() and p.read_text(errors='replace').strip())
        if folder == Path(project):
            break
    if not found and component == '.':
        # A wrapper directory may enclose the one actual package. Do not borrow a sibling package README.
        children = [p for p in project.iterdir() if p.is_dir() and not p.name.startswith('.') and p.name not in {'node_modules', 'target', 'build', 'dist', '__pycache__'}]
        if len(children) == 1:
            found.extend(p for p in children[0].glob('README*') if p.is_file() and p.read_text(errors='replace').strip())
    return list(dict.fromkeys(str(p.relative_to(project)) for p in found))


def documented(project, component='.', packages=()):
    """Documentation for a component; the root component also accepts a README inside a discovered package.

    sweep3-c Rust p1 (2026-09-26): the Cargo package lived in base64tool/ beside a scripts/ directory, so the
    lone-child fallback above found nothing, the outcome was reported as undocumented although base64tool/README.md
    existed, the repair round was a no-op and the job parked with a durable no_progress. Package cwds come from the
    execution profile; a sibling non-package directory never lends its README.
    """
    found = documentation_files(project, component)
    if found or component != '.':
        return found
    for cwd in packages:
        if not cwd or cwd == '.':
            continue
        try:
            found = documentation_files(project, cwd)
        except (ValueError, OSError):
            continue
        if found:
            return found
    return []


FILE_ROOT = re.compile(
    r'(?:pathlib\.)?Path\(\s*__file__\s*\)(?:\s*\.\s*(?:resolve|absolute)\(\))?(?:\s*\.\s*(?:parent\b|parents\[\d+\]))+'
    r'|os\.path\.dirname\(\s*os\.path\.(?:abspath|realpath)\(\s*__file__\s*\)\s*\)')


def project_root_paths(path):
    """Audit files sit outside the application, so a root derived from __file__ is
    always wrong. Point it at the controller's project root; keep the file if unsure."""
    original = path.read_text(errors='replace')
    if '__file__' not in original:
        return False
    text = FILE_ROOT.sub(lambda m: ('__import__("pathlib").Path(__import__("os").environ["DAEDALUS_PROJECT_ROOT"])'
        if 'Path' in m.group(0) else '__import__("os").environ["DAEDALUS_PROJECT_ROOT"]'), original)
    if text == original:
        return False
    try:
        ast.parse(text)
    except SyntaxError:
        return False
    path.write_text(text)
    return True


REBUILD = re.compile(r"""["'](?:mvn|mvnw|\./mvnw|gradle|\./gradlew|cmake|make|ninja|cargo|msbuild)["']"""
                     r"""|["']dotnet["']\s*,\s*["'](?:build|test|restore|publish)["']|["']dotnet["']\s*,\s*["']run["'](?!\s*,\s*["']--no-build["'])"""
                     r"""|["']go["']\s*,\s*["'](?:build|test|install)["']|["']npm["']\s*,\s*["'](?:install|ci|run)["']""")


def project_runtime(project):
    """How an audit must launch this application: (label, argv prefix) or None for Python/unknown."""
    if not project:
        return None
    root = Path(project)
    has = lambda *names: any((root / name).exists() for name in names)
    if has('go.mod'):
        return 'Go', '"go", "run", "."'
    if has('package.json') and not has('requirements.txt', 'pyproject.toml', 'setup.py'):
        return 'Node', '"node"'
    return None


def strip_quoted_list_arguments(text):
    """`subprocess.run([binary, '"3 4 +"'])`: the prompt showed `rpncalc "3 4 +"`, and the model kept the
    shell quotes inside a LIST argument, so the program received literal quote characters. A list element
    is already one argument; a `shell=True` command string keeps its quoting."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    spans = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {'run', 'Popen', 'check_output', 'check_call', 'call'}):
            continue
        if any(k.arg == 'shell' and isinstance(k.value, ast.Constant) and k.value.value for k in node.keywords):
            continue
        for argument in node.args[:1]:
            if isinstance(argument, (ast.List, ast.Tuple)):
                for element in argument.elts:
                    if isinstance(element, ast.Constant) and isinstance(element.value, str) and len(element.value) > 2 \
                            and element.value[0] == element.value[-1] and element.value[0] in '"\'':
                        spans.append((element.lineno, element.col_offset, element.end_lineno, element.end_col_offset, element.value[1:-1]))
    if not spans:
        return text
    lines = text.splitlines(keepends=True)
    for lineno, start, end_lineno, end, inner in sorted(spans, reverse=True):
        if lineno != end_lineno:
            continue
        line = lines[lineno - 1]
        lines[lineno - 1] = line[:start] + repr(inner) + line[end:]
    return ''.join(lines)


def mechanical_audit_rewrites(directory, project=None):
    from coder_audit_hygiene import show_failing_line
    """Forms a local model keeps choosing that work once adjusted: `dotnet run` launches the
    already-built app in the read-only sandbox when it is told not to rebuild."""
    notes = []
    for path in sorted(directory.rglob('*.py')):
        text = path.read_text(errors='replace')
        fixed = re.sub(r"""(["']dotnet["']\s*,\s*["']run["'])(?!\s*,\s*["']--no-build["'])""", r'\1, "--no-build"', text)
        # Local models launch everything with the Python interpreter. A JavaScript file runs under node,
        # and `python -m <name>` of a Go module is `go run .`.
        fixed = re.sub(r"""sys\.executable(\s*,\s*["'][^"']+\.(?:js|mjs|cjs)["'])""", r'"node"\1', fixed)
        runtime = project_runtime(project)
        if runtime and runtime[0] == 'Go':
            fixed = re.sub(r"""sys\.executable\s*,\s*["']-m["']\s*,\s*["'][\w.-]+["']""", runtime[1], fixed)
        fixed = strip_quoted_list_arguments(fixed)
        fixed = show_failing_line(fixed)
        if fixed != text:
            try:
                ast.parse(fixed)
            except SyntaxError:
                continue
            path.write_text(fixed)
            notes.append(('Shell quotes removed from subprocess list arguments: ' if strip_quoted_list_arguments(text) != text
                          else 'Audit launcher corrected for this application runtime: ') + path.name)
    return notes


def project_writes(tree, text):
    """Source of each file-write target that lands in the read-only project: `open(..., 'w')`, `.write_text(...)` or
    `.write_bytes(...)` on a path that names the project root or is a bare relative literal (the audit's cwd is the
    project). fix4 Node p1 (2026-09-26) spent both audit corrections on `open(<project>/test_input.md, 'w')`."""
    found, tainted = [], set()
    for _ in range(2):   # names assigned from the project root, and names assigned from those
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                source = ast.get_source_segment(text, node.value) or ''
                if 'PROJECT_ROOT' in source.upper() or any(re.search(rf'\b{re.escape(name)}\b', source) for name in tainted):
                    tainted.add(node.targets[0].id)
    def names_project(source):
        return 'PROJECT_ROOT' in source.upper() or any(re.search(rf'\b{re.escape(name)}\b', source) for name in tainted)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = None
        if isinstance(node.func, ast.Name) and node.func.id == 'open' and node.args:
            mode = ''
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for kw in node.keywords:
                if kw.arg == 'mode' and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if any(ch in mode for ch in 'wax'):
                target = node.args[0]
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {'write_text', 'write_bytes'}:
            target = node.func.value
        if target is None:
            continue
        source = ast.get_source_segment(text, target) or ''
        literal = isinstance(target, ast.Constant) and isinstance(target.value, str)
        if names_project(source) or (literal and not target.value.startswith(('/', '~'))):
            found.append(source[:80])
    return found


def static_audit_faults(directory, project=None, request=''):
    """Defects visible without running the audit. Each costs nothing to point out precisely."""
    from coder_audit_hygiene import web_audit_faults
    faults = []
    runtime = project_runtime(project)
    for path in sorted(directory.rglob('*.py')):
        text = path.read_text(errors='replace')
        try:
            tree = ast.parse(text)
        except SyntaxError as error:
            faults.append(f'{path.name} has a syntax error at line {error.lineno}: {error.msg}')
            continue
        asserts = sum(isinstance(node, ast.Assert) for node in ast.walk(tree)) + sum(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr.startswith('assert')
            for node in ast.walk(tree))
        if not asserts:
            faults.append(f'{path.name} contains no assert statement. Only executed plain `assert` statements count as '
                          'evidence; replace print/sys.exit style checks with `assert actual == expected, message`.')
        faults.extend(web_audit_faults(path.name, text, request))
        if runtime and 'sys.executable' in text:
            faults.append(f'{path.name} launches the application with the Python interpreter (sys.executable), but this is a '
                          f'{runtime[0]} program. Launch it with its own runtime: subprocess.run([{runtime[1]}, ...]).')
        for target in project_writes(tree, text):
            faults.append(f'{path.name} writes a file into the project directory ({target}). The project is READ-ONLY during the '
                          "audit; create fixtures under os.environ['DAEDALUS_AUDIT_DIR'] (or tempfile.mkdtemp()) and pass that path "
                          'to the program.')
        if REBUILD.search(text):
            faults.append(f'{path.name} builds, tests or installs the project. The controller has ALREADY built it and the '
                          'audit filesystem is read-only. Remove that step and launch the existing built artifact directly '
                          '(for example java -cp target/classes <Main>, build/<exe>, target/debug/<bin>, '
                          'dotnet <App>/bin/Debug/net8.0/<App>.dll, go run .).')
    return faults


def normalize_audit(directory, outcomes, project=None, packages=()):
    """Repair mechanical metadata slips before validation; they are not audit judgement.

    The model's tests and outcome mapping are kept. Only fields the controller
    already knows are defaulted, so a one-word slip does not cost a correction round.
    """
    notes, path = [], directory / 'audit.json'
    known = [o['id'] for o in outcomes]
    wanting = lambda kind: [o['id'] for o in outcomes if kind in o.get('evidence_types', [])]
    if project:
        sources = tree_hashes(project, ('.git', '.venv', 'node_modules', '__pycache__'))
        for name, value in tree_hashes(directory).items():
            if name not in {'audit.json', 'test_behavior.py'} and (name in sources or value in sources.values()):
                (directory / name).unlink()
                notes.append('Removed copied application file from audit: ' + name)
    for script in sorted(directory.rglob('*.py')):
        if project_root_paths(script):
            notes.append('Audit project paths now use DAEDALUS_PROJECT_ROOT instead of __file__: ' + script.name)
    try:
        rows = json_object(path.read_text()).get('checks')
    except (OSError, ValueError):
        rows = None
    script = directory / 'test_behavior.py'
    if not isinstance(rows, list) or not rows:
        rows = []
        if script.is_file() and script.read_text().strip() and wanting('behavior'):
            rows.append({'command': '"$DAEDALUS_AUDIT_PYTHON" {audit}/test_behavior.py', 'outcomes': wanting('behavior'),
                         'evidence_types': ['behavior'], 'cwd': '.'})
            notes.append('audit.json was absent or unreadable; registered test_behavior.py for the behavior outcomes')
    fixed, dropped = [], False
    for row in rows:
        if not isinstance(row, dict):
            continue
        row = dict(row)
        if row.get('kind') == 'file' and not is_documentation(row.get('path')):
            # The controller registers the delivered README below; absent documentation is an application defect.
            notes.append('Dropped a file check whose target is not documentation: ' + str(row.get('path')))
            dropped = True
            continue
        default = 'documentation' if row.get('kind') == 'file' else 'behavior'
        kinds = [k for k in row.get('evidence_types', []) if k in {'behavior', 'documentation'}] if isinstance(row.get('evidence_types'), list) else []
        if kinds != row.get('evidence_types'):
            notes.append('Audit evidence types limited to behavior/documentation')
        # Only a file check produces documentation bindings; a command cannot claim it.
        kinds = ['documentation'] if row.get('kind') == 'file' else [k for k in kinds if k == 'behavior']
        row['evidence_types'] = kinds or [default]
        ids = [v for v in row.get('outcomes', []) if v in known] if isinstance(row.get('outcomes'), list) else []
        row['outcomes'] = ids or wanting(row['evidence_types'][0])
        command = row.get('command')
        if isinstance(command, str) and command.strip():
            try:
                words = shlex.split(command)
            except ValueError:
                words = []
            targets = [w[len('{audit}/'):] for w in words if w.startswith('{audit}/')]
            text = ''.join((directory / t).read_text(errors='replace') for t in targets if (directory / t).is_file())
            # Only the controller interpreter has requests and Playwright.
            if words and Path(words[0]).name in {'python', 'python3'} and re.search(r'\b(playwright|requests)\b', text):
                row['command'] = '"$DAEDALUS_AUDIT_PYTHON" ' + ' '.join(shlex.quote(w) if not w.startswith('{audit}') else w for w in words[1:])
                notes.append('Audit runner switched to the controller interpreter for HTTP/browser clients')
        if row['outcomes']:
            fixed.append(row)
    if project:
        covered = {v for row in fixed if row.get('kind') == 'file' for v in row['outcomes']}
        for requirement in outcomes:
            if 'documentation' in requirement.get('evidence_types', []) and requirement['id'] not in covered:
                files = documented(project, requirement.get('component', '.'), packages)
                if files:
                    fixed.append({'kind': 'file', 'path': files[0], 'outcomes': [requirement['id']],
                                  'evidence_types': ['documentation'], 'assertions': [{'kind': 'nonempty'}], 'cwd': '.'})
                    notes.append('Registered delivered documentation check: ' + files[0])
    if fixed or dropped:
        # A dropped claim must not survive in the file that audit_checks validates next.
        atomic_json(path, {'checks': fixed})
    return notes


def audit_commands(directory):
    try:
        return [(c.get('command'), c.get('cwd', '.')) for c in json.loads((directory / 'audit.json').read_text()).get('checks', [])]
    except (OSError, ValueError, TypeError):
        return []




from coder_policy7_ops import Operations


class Experiment(Operations):
    def __init__(self, root, task=None, *, model=None, ollama_url=None, settings=None, files=None,
                 project_id=None, explicit=None, protected=(), inherited=(), editor=edit, chat=local_chat):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'job.json'
        self.editor, self.chat = editor, chat
        self.store = WorkerStore(self.root.parent / 'worker')
        if self.path.exists():
            self.job = json.loads(self.path.read_text())
        else:
            if not task or not model or not ollama_url:
                raise ValueError('New experiments need a task and explicit local model/runtime')
            workspace = self.root / 'workspace'; workspace.mkdir()
            for name, content in (files or {}).items():
                path = safe_relative(workspace, name)
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)
            self.job = {'id': 'p7-' + hashlib.sha256(str(self.root).encode()).hexdigest()[:20],
                'task': task, 'model': model, 'ollama_url': ollama_url, 'settings': {**DEFAULTS, **(settings or {})},
                'state': 'inspect', 'sequence': 0, 'calls': 0, 'seconds': 0, 'repair_round': 0,
                'audit_corrections': 0, 'recovery_used': False, 'batch': 0, 'revision_id': '',
                'project_id': project_id or self.root.name, 'explicit': explicit or {},
                'protected': list(protected), 'inherited': list(inherited), 'operation': '', 'history': []}
            self.save()
        # Experiments resolve inherited compaction exactly like the production controller.
        import context_policy
        context_policy.apply_settings(self.job['settings'])
        self.save(settings=context_policy.runtime_settings())
        self.repo = Repository(self.root / 'workspace', self.root / 'repository', self.job['settings']['daedalus_exclude_dirs'])

    def save(self, **changes):
        self.job.update(changes)
        atomic_json(self.path, self.job)

    def operation(self, kind, run):
        job = self.job
        identity = job.get('operation')
        if not identity:
            identity = f'{job["id"]}-{job["sequence"] + 1}'
            self.save(operation=identity, sequence=job['sequence'] + 1)
        old = self.store.get(identity)
        if old and old['status'] == 'succeeded':
            return old['result']
        if old and old['status'] in {'cancelled', 'interrupted', 'failed'}:
            raise InterruptedError('Resume the interrupted operation explicitly')
        payload = {'policy_version': 7, 'model': job['model'], 'ollama_url': job['ollama_url'],
            'settings': job['settings'], 'revision_id': job['revision_id'], 'baseline': job.get('baseline', []),
            'seconds_remaining': job['settings']['daedalus_job_seconds'] - job['seconds'],
            'calls_remaining': min(job['settings']['daedalus_model_calls'] - job['calls'], job['settings']['daedalus_attempt_turns']),
            'request_key': identity}
        self.store.create(identity, job['id'], kind, payload)
        if old and old['status'] == 'running':
            # Do not silently replay a patch/check after an interrupted process.
            raise InterruptedError('Operation interrupted; inspect and resume its checkpoint')
        import psutil
        self.store.update(identity, status='running', started=time.time(), pid=os.getpid(), process_started=psutil.Process().create_time())
        try:
            result = run(identity)
            self.store.update(identity, status='succeeded', result=result, ended=time.time())
            return result
        except BaseException as error:
            self.store.update(identity, status='interrupted' if isinstance(error, (InterruptedError, KeyboardInterrupt)) else 'failed',
                              result={'error': f'{type(error).__name__}: {error}'}, ended=time.time())
            raise

    def transition(self, state, **changes):
        identity = self.job.get('operation')
        op = self.store.get(identity) if identity else None
        elapsed = max(0, (op.get('ended') or time.time()) - op['started']) if op and op.get('started') else 0
        self.save(state=state, calls=self.job['calls'] + (op['calls'] if op else 0),
            seconds=self.job['seconds'] + elapsed, operation='',
            history=[*self.job['history'], {'state': self.job['state'], 'operation': identity}], **changes)

    def candidate(self, reason):
        self.transition('candidate', reason=reason)

    def resume(self, clarification=None):
        old = self.store.get(self.job['operation']) if self.job.get('operation') else None
        stopped = old and old['status'] in {'cancelled', 'interrupted', 'failed', 'running'}
        if self.job['state'] not in {'interrupted', 'candidate', 'waiting_for_input'} and not stopped:
            raise ValueError('Only interrupted work or a candidate can resume')
        if self.job.get('stop_limit'):
            raise ValueError(self.job.get('reason') or 'Retained limits prevent continuation')
        if self.job.get('scope_question'):
            if not isinstance(clarification, str) or not clarification.strip():
                raise ValueError('Answer the scope question before continuing')
            from coder_scope import effective_task
            self.save(user_task=self.job.get('user_task', self.job['task']),
                      scope_clarifications=[*self.job.get('scope_clarifications', []), {'text': clarification.strip()}],
                      scope_question='', resume_state='planning')
            self.save(task=effective_task(self.job))
        if self.job.get('operation'):
            if old and old['status'] == 'running':
                from coder_worker_runtime import _alive
                if _alive(old):
                    raise ValueError('Previous worker must acknowledge Stop before resume')
            self.transition(self.job.get('resume_state', self.job['state']))
        else:
            self.save(state=self.job.get('resume_state', 'checking'))
        snapshot = self.repo.snapshot('Resumed checkpoint', parent=self.job['revision_id'])
        self.save(revision_id=snapshot['revision'])

    @classmethod
    def fork(cls, parent, root, task, *, revision_id, artifact_sha256, **adapters):
        """Evaluator-only explicit draft continuation from an immutable archive."""
        import tarfile
        job = parent.job
        artifact = job.get('artifact') or {}
        if revision_id != artifact.get('revision_id') or artifact_sha256 != artifact.get('sha256'):
            raise ValueError('Exact source revision and artifact hash are required')
        if file_hash(Path(artifact['path'])) != artifact_sha256:
            raise ValueError('Source artifact hash changed')
        if Path(root).exists():
            raise ValueError('A continuation needs a fresh job directory')
        unresolved = [o['text'] for o in job.get('verification_summary', {}).get('outcomes', []) if o.get('status') != 'passed']
        if job['state'] != 'accepted' and not unresolved:
            unresolved = [o['text'] for o in job.get('brief', {}).get('outcomes', [])] or [job['task']]
        request = task
        if unresolved:
            request += '\nThis request explicitly continues a draft. Retain these unresolved obligations:\n' + '\n'.join(unresolved)
        child = cls(root, request, model=job['model'], ollama_url=job['ollama_url'], settings=job['settings'],
                    project_id=job['project_id'], protected=job['protected'], explicit=job['explicit'], inherited=unresolved, **adapters)
        with tarfile.open(artifact['path']) as bundle:
            bundle.extractall(child.repo.root, filter='data')
        child.save(source_revision=revision_id, source_artifact_sha256=artifact_sha256,
                   expected_accepted_revision=revision_id if job['state'] == 'accepted' else job.get('expected_accepted_revision', ''))
        return child

    def run(self):
        import fcntl
        locks = self.store.root / 'locks'; locks.mkdir(exist_ok=True)
        handle = (locks / hashlib.sha256(self.job['project_id'].encode()).hexdigest()).open('a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise ValueError('Another experimental writer owns this project') from None
        try:
            while self.job['state'] not in {'candidate', 'accepted', 'interrupted', 'waiting_for_input'}:
                self.step()
        except (Exception, KeyboardInterrupt) as error:
            interrupted = isinstance(error, (InterruptedError, KeyboardInterrupt))
            checkpoint = self.repo.snapshot('Interrupted checkpoint', parent=self.job['revision_id'])
            self.save(revision_id=checkpoint['revision'], resume_state=self.job['state'])
            self.transition('interrupted' if interrupted else 'candidate', reason=f'{type(error).__name__}: {error}')
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        if self.job['state'] != 'waiting_for_input':
            self.package()
        return self.job

    def step(self):
        job = self.job; state = job['state']
        if state == 'inspect':
            snapshot = self.repo.snapshot('Initial source')
            self.save(revision_id=snapshot['revision'], baseline_revision=snapshot['revision'],
                      original_hashes=tree_hashes(self.repo.root, job['settings']['daedalus_exclude_dirs']))
            self.transition('baseline')
        elif state in {'baseline', 'checking', 'auditing'}:
            profile = contract(self.repo, job['explicit'])
            try:
                checks = audit_checks(Path(job['audit_dir']), job['brief']['outcomes'], self.repo.root) if state == 'auditing' else None
            except (ValueError, OSError, SyntaxError) as error:
                self.transition('reviewing', checks=[*job.get('project_checks', []),
                    {'id': 'audit-metadata', 'origin': 'independent', 'passed': False,
                     'classification': 'audit_defect', 'log_tail': str(error)}])
                return
            result = self.operation('check', lambda op: run_revision(self.store, op, self.repo, profile,
                project_id=job['project_id'], audit_source=Path(job['audit_dir']) if checks else None, checks=checks))
            if state == 'baseline':
                self.transition('planning', baseline=result['checks'])
            elif state == 'checking':
                protected = [p for p in job['protected'] if job['original_hashes'].get(p) != (file_hash(safe_relative(self.repo.root, p)) if safe_relative(self.repo.root, p).is_file() else None)]
                if protected:
                    result['checks'].append({'id': 'protected-files', 'passed': False, 'classification': 'application_defect', 'paths': protected})
                self.transition('authoring', project_checks=result['checks'], checks=result['checks'], execution_profile=profile)
            else:
                self.transition('reviewing', checks=[*job['project_checks'], *result['checks']])
        elif state == 'planning':
            result = self.operation('plan', self.plan)
            self.transition('waiting_for_input' if result.get('scope_question') else 'editing',
                            **result, **({'resume_state': 'planning'} if result.get('scope_question') else {}))
        elif state == 'editing':
            from coder_policy7_ops import implementation_task, edit_transition
            task, targets = implementation_task(job)
            result = self.operation('code', lambda op: self.editor(self.store, op, self.repo.root, task, preferred=targets))
            snapshot = self.repo.snapshot('Patch operation', parent=job['revision_id'])
            changes = edit_transition(job, result)
            next_state = changes.pop('state')
            self.transition(next_state, revision_id=snapshot['revision'], last_patch=result,
                unchanged_source=snapshot['revision'] == job['revision_id'], **changes)
        elif state == 'authoring':
            result = self.operation('verify', self.author)
            if result.get('audit_error'):
                self.transition('reviewing', **result, checks=[*job.get('project_checks', []),
                    {'id': 'audit-metadata', 'origin': 'independent', 'passed': False,
                     'classification': 'audit_defect', 'log_tail': result['audit_error']}])
            else:
                self.transition('auditing', **result, audit_correction_pending=False)
        elif state == 'reviewing':
            verdict = self.operation('accept', self.review)
            summary = acceptance(job['brief']['outcomes'], job['checks'], verdict, job['revision_id'])
            self.save(verification_summary=summary, review=verdict)
            if summary['accepted']:
                self.transition('accepted')
                return
            decision = decide(job, summary, verdict)
            if decision['action'] == 'repair':
                self.transition('editing', repair_round=job['repair_round'] + 1, batch=0,
                    repair_feedback=decision['feedback'], failure_signature=decision['signature'])
            elif decision['action'] == 'audit':
                self.transition('authoring', audit_corrections=job['audit_corrections'] + 1, audit_correction_pending=True)
            elif decision['action'] == 'execute':
                self.transition('checking')
            else:
                self.save(stop_limit=decision.get('limit', ''))
                self.candidate(decision['reason'])
        else:
            raise ValueError('Unknown experimental state ' + state)

    def package(self):
        # Content-addressed artifacts cannot replace an older report/archive.
        summary = self.job.get('verification_summary')
        if not summary or summary.get('revision_id') != self.job['revision_id']:
            summary = {'accepted': False, 'revision_id': self.job['revision_id'],
            'outcomes': [{**o, 'status': 'unverified', 'executions': []} for o in self.job.get('brief', {}).get('outcomes', [])],
            'passed': [], 'failed': [], 'unverified': [o['id'] for o in self.job.get('brief', {}).get('outcomes', [])]}
        identity = hashlib.sha256(json.dumps({'revision': self.job['revision_id'], 'summary': summary,
                                            'reason': self.job.get('reason', '')}, sort_keys=True).encode()).hexdigest()
        artifact = self.root / (identity + '.tar.gz')
        if not artifact.exists():
            self.repo.archive(self.job['revision_id'], artifact)
        self.save(verification_summary=summary,
                  artifact={'path': str(artifact), 'sha256': file_hash(artifact), 'revision_id': self.job['revision_id']},
                  runnable=any(c.get('execution_succeeded') and c.get('passed') for c in self.job.get('checks', [])))
