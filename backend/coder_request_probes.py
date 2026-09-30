"""Controller-owned probes derived from the literal request text. Diagnostic only.

frozen-7 (2026-09-24) delivered `wordfreq/__init__.py` and no `wordfreq.py` although the request and the
README both said `python3 wordfreq.py <file>`: every generated check passed and the independent launch
failed. Another build shipped an empty .NET solution: `dotnet test` exited 0 with nothing to run. Both are
visible from the request alone, so the controller runs each requested command once with generated fixture
input on the built execution copy, and reads the solution file. A failing row is an application defect the
repair receives first; a passing row proves nothing about behavior, carries no outcome and is never
acceptance evidence. Anything the probe cannot derive (an unknown placeholder, a program it cannot locate)
is skipped, never failed.

sweep3 (2026-09-26) showed the exit-0 rule was too weak: a Node TOC tool printed NOTHING and passed, another
listed the `# not a heading` that lives inside a fenced code block of the controller's own Markdown fixture
and passed, and a Rust decoder that accepted `====` was never asked about invalid input although the
request said "Invalid Base64 input prints an error to stderr and exits with code 2". So the positive probe
now also checks what the request says it prints (empty output, fixture headings present, fenced-only text
absent), and sentences of the form "invalid/missing <what> ... exits with code N" become negative probes
(`probe:invalid:<n>:<k>`, `probe:missing:<n>`) with controller-owned invalid values. Still diagnostic.
"""
from __future__ import annotations

import inspect
import os
import re
import shlex
from pathlib import Path

RUNNERS = {'python3', 'python', 'node', 'go', 'cargo', 'dotnet', 'java', 'ruby', 'php', 'deno', 'bun'}
EXCLUDED_FIRST = {'npm', 'npx', 'pip', 'pip3', 'uvicorn', 'mvn', 'cmake', 'ctest', 'make', 'git', 'curl', 'cd', 'export',
                  'docker', 'gradle', 'gradlew', 'flask', 'pytest', 'echo', 'cat', 'ls', 'rm', 'mkdir', 'source'}
EXCLUDED_SUB = {('go', 'build'), ('go', 'test'), ('go', 'mod'), ('go', 'vet'), ('go', 'install'), ('cargo', 'build'),
                ('cargo', 'test'), ('cargo', 'new'), ('cargo', 'install'), ('dotnet', 'build'), ('dotnet', 'test'),
                ('dotnet', 'restore'), ('dotnet', 'new'), ('python3', '-m'), ('python', '-m'), ('node', '--test'),
                ('python3', '-c'), ('python', '-c')}
PLACEHOLDER = re.compile(r'<([^<>\n]{1,40})>')
TEST_SDK = re.compile(r'Microsoft\.NET\.Test\.Sdk|xunit|nunit|MSTest', re.I)
TEST_ATTRIBUTE = re.compile(r'\[(?:Fact|Theory|Test|TestMethod|TestCase)\b')
SOURCE_SUFFIXES = {'.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.rs', '.java', '.cs', '.c', '.cpp', '.rb', '.php'}
# Each fixture is a controller-owned document. `mentions` are strings the output of a program that reads the
# document for the purpose in `when` must contain; `absent` are strings it must not (they exist only inside a
# construct the purpose excludes). Both apply only when the request text matches `when`, so a Markdown-to-HTML
# converter that legitimately prints fenced code is never judged by the table-of-contents rule.
FIXTURES = {
    'csv': {'text': 'name,value\nalpha,1\nbeta,2.5\ngamma,3\n', 'description': 'a CSV file with a header row and a numeric "value" column'},
    'md': {'text': '# Title\n\nIntro text.\n\n```text\n# not a heading\n```\n\n## Section\n\nMore text.\n', 'description': 'a small Markdown file',
           # fix4 Node p1 (2026-09-26): a fence opened WITH an info string was never closed and the heading after it vanished; with the
           # fence last in the file that bug was invisible to the probe. Now `Section` follows the fence, so it is caught by `mentions`.
           'when': r'\bheadings?\b|\btable of contents\b|\btoc\b', 'mentions': ['Title', 'Section'], 'absent': ['not a heading'],
           'mentions_why': 'a heading of the input file', 'absent_why': 'it is inside a fenced code block of the input, which is not a Markdown heading'},
    'log': {'text': '2026-01-01T10:00:00 INFO started\n2026-01-01T10:00:01 ERROR failed once\n2026-01-01T10:00:02 WARN slow\n', 'description': 'a small log file'},
    'json': {'text': '{"name": "alpha", "value": 1}\n', 'description': 'a small JSON file'},
    'txt': {'text': 'the cat sat on the mat the cat\n', 'description': 'a small text file'},
}
VALUES = {
    ('text', 'string', 'message', 'input text', 'word', 'words', 'sentence', 'expression', 'expr'): 'hello world',
    ('base64', 'encoded', 'base64 string', 'encoded string'): 'aGVsbG8gd29ybGQ=',
    ('name', 'column', 'column name', 'field', 'col'): 'value',
    ('n', 'number', 'count', 'depth', 'limit', 'max depth', 'max-depth', 'top', 'k', 'lines'): '2',
    ('level', 'log level', 'loglevel'): 'ERROR',
}
# Invalid values per placeholder class, used only when the request says invalid input of that class exits
# with a code. Base64: out-of-alphabet, padding-only, over-padded, length 1 mod 4 - invalid under any RFC 4648
# reading (Python's strict decoder rejects all four). A class without an entry never binds a negative probe.
INVALID_VALUES = {
    ('base64', 'encoded', 'base64 string', 'encoded string'): ['%%%', '====', 'a===', 'A'],
    ('n', 'number', 'count', 'depth', 'limit', 'max depth', 'max-depth', 'top', 'k', 'lines'): ['abc'],
    ('level', 'log level', 'loglevel'): ['BOGUS'],
}
MAX_INVALID = 4
PRINTS = re.compile(r'\bprints?\b|\boutputs?\b|\bwrites? to stdout\b|\bdisplays?\b', re.I)
EXIT = r'\b(?:exits?|returns?|terminates?)\s+(?:with\s+)?(?:(?:an?\s+)?(?:exit\s+)?(?:code|status)\s+(?:of\s+)?)?(?P<code>\d+|non-?zero)\b'
INVALID_RULE = re.compile(r'\b(?:invalid|malformed|bad|unrecognized|unsupported|unknown)\s+(?P<what>[\w -]{1,30}?)\s*'
                          r'(?:input|argument|value|string|data)?s?\b.*?' + EXIT, re.I | re.S)
MISSING_RULE = re.compile(r'\b(?:missing|nonexistent|non-existent|non-existing|unreadable|absent)\s+(?:input\s+)?'
                          r'(?P<what>file|path|input|argument|filename)s?\b.*?' + EXIT, re.I | re.S)
STDERR = re.compile(r'\bstderr\b|\berror\b', re.I)
VALUED_FLAG = re.compile(r'`(--[a-z][a-z0-9-]{1,40})\s+(?:<([^<>\n]{1,40})>|([A-Z]{1,3}|[a-z]))`')


def sentences(task):
    """Whole sentences of the request. Periods inside backticks (`mdtoc.js`, `<file.md>`) never split."""
    protected = re.sub(r'`[^`\n]*`', lambda m: m.group(0).replace('.', '\x00'), task or '')
    parts = re.split(r'(?<=[.!?])\s+|\s*;\s*|\n+', protected)
    return [p.replace('\x00', '.').strip() for p in parts if p.strip()]


def sentence_for(task_sentences, excerpt):
    for sentence in task_sentences:
        if '`' + excerpt + '`' in sentence or excerpt in sentence:
            return sentence
    return ''


def wording_rules(task):
    """Sentences that promise an exit code for invalid or missing input: [{kind, what, code, stderr_required, sentence}]."""
    rules = []
    for sentence in sentences(task):
        for kind, pattern in (('invalid', INVALID_RULE), ('missing', MISSING_RULE)):
            match = pattern.search(sentence)
            if not match:
                continue
            code = match.group('code').lower()
            rules.append({'kind': kind, 'what': _normal(match.group('what')), 'sentence': sentence,
                          'code': None if code.startswith('non') else int(code), 'stderr_required': bool(STDERR.search(sentence))})
    return rules


def _normal(name):
    return re.sub(r'[^a-z0-9.]+', ' ', (name or '').lower()).strip()


def _group_for(key):
    for group in VALUES:
        if key in group or key.rstrip('s') in group:
            return group
    return None


def requested_commands(task):
    """Backticked spans that read as an invocation of the requested program."""
    specs, seen = [], set()
    for span in re.findall(r'`([^`\n]{2,200})`', task or ''):
        text = span.strip()
        if not text or text in seen or '{port}' in text or '://' in text or 'localhost' in text:
            continue
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        if not tokens:
            continue
        first = tokens[0]
        second = tokens[1] if len(tokens) > 1 else ''
        if first in EXCLUDED_FIRST or (first, second) in EXCLUDED_SUB:
            continue
        placeholders = PLACEHOLDER.findall(text)
        starts_placeholder = bool(PLACEHOLDER.fullmatch(first)) or first.startswith('--')
        starts_program = (first in RUNNERS or first.startswith('./')
                          or bool(re.fullmatch(r'[a-z][a-z0-9_-]{1,40}', first)) and len(tokens) > 1)
        if starts_placeholder and not placeholders:
            continue  # a lone flag: the interface:* row covers it
        if starts_program and len(tokens) == 1 and not placeholders:
            continue  # a bare program or file name, not an invocation
        if not (starts_program or starts_placeholder):
            continue
        seen.add(text)
        specs.append({'excerpt': text, 'tokens': tokens, 'application': starts_placeholder})
    return specs


def _fixture_kind(key):
    """The file fixture a placeholder name maps to, or None when it is a value or unknown."""
    if _group_for(key):
        return None
    if key.endswith('.csv') or 'csv' in key:
        return 'csv'
    if key.endswith('.md') or 'markdown' in key:
        return 'md'
    if key.endswith('.log') or key.startswith('log') or key.endswith(' log') or key == 'logfile':
        return 'log'
    if key.endswith('.json'):
        return 'json'
    if key.endswith('.txt') or key in {'file', 'path', 'input', 'input file', 'filename', 'textfile', 'text file',
                                       'source', 'source file', 'file path', 'infile'}:
        return 'txt'
    return None


def _fixture(name, folder):
    """(argument, description) for one <placeholder>, or None when its meaning is unknown."""
    key = _normal(name)
    group = _group_for(key)
    if group:
        return VALUES[group], f'{name}={VALUES[group]!r}'
    kind = _fixture_kind(key)
    if kind is None:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'probe-input.{kind}'
    path.write_text(FIXTURES[kind]['text'])
    # fix5 Node p1 (2026-09-27): the packet said only "a small Markdown file ... the output mentions 'Section': it does
    # not" and the builder, unable to read the execution copy, changed nothing. The content travels with the row.
    return str(path), f"{FIXTURES[kind]['description']} with content {FIXTURES[kind]['text'].strip()!r}"[:320]


def render(argv, folder, overrides=None):
    """Replace every <placeholder> in argv with a fixture; (rendered, description) or (None, reason).
    `overrides` maps a normalized placeholder name to (argument, description) for one negative probe."""
    rendered, notes = [], []
    for token in argv:
        def substitute(match):
            key = _normal(match.group(1))
            found = (overrides or {}).get(key) or _fixture(match.group(1), folder)
            if found is None:
                raise LookupError(match.group(1))
            notes.append(found[1])
            return found[0]
        try:
            rendered.append(PLACEHOLDER.sub(substitute, token))
        except LookupError as unknown:
            return None, f'placeholder <{unknown}> has no fixture'
    return rendered, '; '.join(dict.fromkeys(notes)) or 'no input'


def fixture_kinds(argv):
    """File fixture kinds the placeholders of an invocation map to."""
    kinds = []
    for token in argv:
        for name in PLACEHOLDER.findall(token):
            kind = _fixture_kind(_normal(name))
            if kind and kind not in kinds:
                kinds.append(kind)
    return kinds


def _executables(root, subdirs):
    found = []
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        for path in base.rglob('*'):
            if path.is_file() and os.access(path, os.X_OK) and path.suffix not in {'.a', '.o', '.so', '.d', '.json', '.txt', '.cmake'} \
                    and not set(path.parts) & {'CMakeFiles', 'deps', 'incremental', '.fingerprint', 'build'} - {sub.split('/')[0]}:
                found.append(path)
    return found


def application_launcher(root, toolchains, strict=False):
    """argv prefix that runs THE application of a compiled project, or None when it is ambiguous."""
    root = Path(root)
    if 'dotnet' in toolchains:
        projects = [p for p in root.rglob('*.csproj') if not set(p.parts) & {'bin', 'obj'}]
        apps = [p for p in projects if not TEST_SDK.search(p.read_text(errors='replace'))]
        if len(apps) == 1:
            return ['dotnet', 'run', '--no-build', '--project', apps[0].relative_to(root).as_posix(), '--']
        return None
    if 'cargo' in toolchains:
        manifest = root / 'Cargo.toml'
        name = re.search(r'^\s*name\s*=\s*"([^"]+)"', manifest.read_text(errors='replace'), re.M) if manifest.is_file() else None
        binary = root / 'target' / 'debug' / name.group(1) if name else None
        return [str(binary)] if binary and binary.is_file() else None
    if 'go' in toolchains:
        return ['go', 'run', '.'] if (root / 'go.mod').is_file() or not strict else None
    if {'cmake', 'ctest', 'make'} & set(toolchains):
        candidates = [p for p in _executables(root, ['build', 'bin', 'out'])
                      if 'test' not in p.name.lower() and not p.name.startswith(('cmake', 'CMake'))]
        return [str(candidates[0])] if len(candidates) == 1 else None
    return None


SCRIPT_SUFFIXES = {'.js', '.mjs', '.cjs', '.py', '.rb', '.php', '.ts'}


def package_roots(profile, root):
    """The project root, then each discovered package directory (fix4 Rust p1, 2026-09-26: the Cargo package lived in
    `base64tool/`, `Cargo.toml` was looked up at the root only, and every probe was silently skipped)."""
    root = Path(root)
    bases = [root]
    for entry in (profile or {}).get('profiles', []):
        cwd = str(entry.get('cwd') or '.').strip('/')
        if cwd and cwd != '.' and (root / cwd).is_dir() and (root / cwd) not in bases:
            bases.append(root / cwd)
    return bases


def launcher(profile, root, spec):
    """argv for a requested invocation, or None when the program cannot be located. With several package
    directories the program is resolved where its script or binary exists (and `spec['cwd']` records that
    directory); when it exists nowhere the root invocation is kept so the probe fails honestly, as the
    frozen-7 `wordfreq.py` finding requires."""
    root = Path(root)
    bases = package_roots(profile, root)
    for base in bases:
        argv = _launch_in(profile, base, spec, strict=len(bases) > 1)
        if argv:
            spec['cwd'] = '.' if base == root else base.relative_to(root).as_posix()
            return argv
    if len(bases) > 1:
        argv = _launch_in(profile, root, spec, strict=False)
        if argv:
            spec['cwd'] = '.'
            return argv
    return None


def _launch_in(profile, root, spec, strict=False):
    root = Path(root)
    tokens = list(spec['tokens'])
    first = tokens[0]
    toolchains = {t for p in (profile or {}).get('profiles', []) for t in p.get('toolchains', [])}
    if spec['application']:
        prefix = application_launcher(root, toolchains, strict=strict)
        return prefix + tokens if prefix else None
    if first == 'dotnet' and tokens[1:2] == ['run']:
        prefix = application_launcher(root, {'dotnet'})
        rest = tokens[tokens.index('--') + 1:] if '--' in tokens else [t for t in tokens[2:] if not t.startswith('--')]
        return prefix + rest if prefix else None
    if first == 'cargo' and tokens[1:2] == ['run']:
        prefix = application_launcher(root, {'cargo'})
        rest = tokens[tokens.index('--') + 1:] if '--' in tokens else []
        return prefix + rest if prefix else None
    if first in RUNNERS:
        script = next((t for t in tokens[1:] if not t.startswith('-') and Path(t).suffix.lower() in SCRIPT_SUFFIXES), None)
        if strict and script and not (root / script).is_file():
            return None   # not in this package directory; the caller tries the next one
        return tokens
    if first.startswith('./'):
        return tokens if (root / first[2:]).exists() else None
    binaries = [p for p in _executables(root, ['build', 'target/debug', 'bin', 'out', 'dist']) if p.name == first]
    if binaries:
        return [str(binaries[0]), *tokens[1:]]
    return None


def _run(execute, store, op_id, root, argv, project_id, log, stderr_path, cwd='.'):
    """(exit_code, stdout, stderr). A runner may return (code, merged) or (code, stdout, stderr); the real
    run_probe writes stderr to `stderr_path` when it accepts the keyword."""
    try:
        parameters = inspect.signature(execute).parameters
        accepts = 'stderr' in parameters or any(p.kind == p.VAR_KEYWORD for p in parameters.values())
    except (TypeError, ValueError):
        accepts = False
    kwargs = {'project_id': project_id, 'log': log}
    if accepts:
        Path(stderr_path).parent.mkdir(parents=True, exist_ok=True)
        kwargs['stderr'] = stderr_path
    try:
        takes_cwd = 'cwd' in parameters or any(p.kind == p.VAR_KEYWORD for p in parameters.values())
    except NameError:
        takes_cwd = False
    if takes_cwd and cwd not in ('', '.'):
        kwargs['cwd'] = cwd
    result = execute(store, op_id, root, argv, **kwargs)
    if len(result) == 3:
        code, out, err = result
    else:
        code, out = result
        err = Path(stderr_path).read_text(errors='replace') if accepts and Path(stderr_path).is_file() else ''
    # `go run` always exits 1 for a failing child and reports the child's code on stderr; the program's own
    # code is what the request promises (the verifier checks the built binary for the same reason).
    if list(argv[:2]) == ['go', 'run'] and code == 1:
        reported = re.search(r'^exit status (\d+)\s*$', err or '', re.M)
        if reported:
            code = int(reported.group(1))
    return code, out or '', err or ''


def _tail(stdout, stderr):
    text = (stdout or '')[-1200:]
    if (stderr or '').strip():
        text += '\n[stderr] ' + stderr[-300:]
    return text


LINE_WORDING = re.compile(r'\blines?\b', re.I)


def output_templates(sentence):
    """(template, regex) pairs for backticked output shapes in a sentence that promises lines of output: words become
    wildcards (`.+?` inside brackets/parentheses/braces/angles, `\\S+` bare), punctuation stays literal. Flags, spans of
    more than four words and spans without a word are never templates. fix4 Node p2 (2026-09-26): the program printed
    the raw heading lines instead of `- [Title](#slug)`, its own tests and the audit agreed, and the probe passed."""
    if not sentence or not LINE_WORDING.search(sentence):
        return []
    found = []
    for span in re.findall(r'`([^`\n]{2,200})`', sentence):
        text = span.strip()
        words = re.findall(r'[A-Za-z][A-Za-z0-9_]*', text)
        first = text.split()[0] if text.split() else ''
        invocation = first in RUNNERS or first.startswith('./') or bool(PLACEHOLDER.search(text) and re.fullmatch(r'[a-z][a-z0-9_.-]*', first))
        if not text or re.match(r'-{1,2}[A-Za-z]', text) or not words or len(words) > 4 or invocation:
            continue
        pattern, depth = '', 0
        for match in re.finditer(r'[A-Za-z][A-Za-z0-9_]*|\s+|.', text):
            piece = match.group(0)
            if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', piece):
                pattern += '.+?' if depth else r'\S+'
            elif piece.isspace():
                pattern += r'\s+'
            else:
                if piece in '[({<':
                    depth += 1
                elif piece in '])}>':
                    depth = max(0, depth - 1)
                pattern += re.escape(piece)
        found.append((text, re.compile(r'^\s*' + pattern + r'\s*$')))
    return found


def known_transform(task, argv):
    """Expected stdout for the controller's OWN fixed input when the request names a standard codec and the invocation
    names the direction: `encode <text>` / `decode <base64>` under "standard Base64" (fix5 Rust p2, 2026-09-27: encode
    printed `aGVsbG8gd29ybGQA`, an `A` where the `=` padding belongs, and the positive probe passed on exit 0)."""
    import base64
    if not re.search(r'\bbase ?64\b', task or '', re.I):
        return None
    tokens = [t.lower() for t in argv]
    text_value = VALUES[next(g for g in VALUES if 'text' in g)]
    b64_value = VALUES[next(g for g in VALUES if 'base64' in g)]
    if 'encode' in tokens and text_value in argv:
        return base64.b64encode(text_value.encode()).decode()
    if 'decode' in tokens and b64_value in argv:
        return base64.b64decode(b64_value).decode()
    return None


def output_faults(sentence, task, stdout, kinds, argv=()):
    """(expected, actual) pairs a successful positive probe violates: silence where the request says it prints,
    fixture content the output must mention, fixture content it must not (fenced-only text)."""
    faults = []
    if PRINTS.search(sentence or '') and not stdout.strip():
        return [('output on stdout', 'it printed nothing')]   # everything else follows from the silence
    expected = known_transform(task, list(argv))
    if expected is not None and stdout.strip() != expected:
        faults.append((f'stdout exactly {expected!r} (standard Base64 of the controller input)', f'it printed {stdout.strip()[:60]!r}'))
    for template, shape in output_templates(sentence):
        lines = [line for line in stdout.splitlines() if line.strip()]
        odd = next((line for line in lines if not shape.match(line)), None)
        if odd is not None:
            faults.append((f'every output line shaped like `{template}` (the request states that format)', f'a line is {odd.strip()[:60]!r}'))
    low = stdout.lower()
    for kind in kinds:
        spec = FIXTURES.get(kind, {})
        if spec.get('when') and not re.search(spec['when'], task or '', re.I):
            continue
        for item in spec.get('mentions', []):
            if item.lower() not in low:
                faults.append((f'the output mentions {item!r} ({spec.get("mentions_why", "content of the input")})', 'it does not'))
        for item in spec.get('absent', []):
            if item.lower() in low:
                faults.append((f'{item!r} is not in the output ({spec.get("absent_why", "it is not content the program should report")})', 'it is listed'))
    return faults


def _row(ident, op_id, revision_id, *, passed, command, code, stdout, stderr, excerpt, description, expected, actual, reason, cwd='.'):
    return {'id': ident, 'origin': 'controller', 'passed': passed, 'cwd': cwd or '.', 'command': command, 'exit_code': code,
            'log_tail': _tail(stdout, stderr)[-1500:], 'request_excerpt': excerpt, 'probe_input': description,
            'expected': expected, 'actual': actual, 'evidence_types': [], 'outcomes': [],
            'classification': 'passed' if passed else 'application_defect', 'revision_id': revision_id,
            'execution_id': f'{op_id}:{ident}', 'execution_succeeded': passed, 'reason': '' if passed else reason}


def _negative_rows(execute, store, op_id, root, spec, argv, n, rules, fixtures, folder, revision_id, project_id):
    """probe:invalid:<n>:<k> and probe:missing:<n> for the wording rules that bind to this invocation."""
    rows = []
    cwd = spec.get('cwd', '.')
    names = [_normal(name) for token in argv for name in PLACEHOLDER.findall(token)]
    invalid_done = 0
    for rule in rules:
        if rule['kind'] == 'invalid':
            bound = next((name for name in names if (group := _group_for(name)) and group in INVALID_VALUES
                          and (rule['what'] in group or rule['what'].rstrip('s') in group)), None)
            if not bound or invalid_done:
                continue
            values = INVALID_VALUES[_group_for(bound)][:MAX_INVALID]
            for k, value in enumerate(values, 1):
                rendered, description = render(argv, fixtures, overrides={bound: (value, f'<{bound}>={value!r}')})
                if rendered is None:
                    continue
                ident = f'probe:invalid:{n}:{k}'
                code, out, err = _run(execute, store, op_id, root, rendered, project_id, folder / f'probe-{n}-invalid-{k}.log',
                                      fixtures / f'probe-{n}-invalid-{k}.stderr', cwd=cwd)
                rows.append(_judge(ident, op_id, revision_id, rule, shlex.join(rendered), code, out, err, description, cwd=cwd))
            invalid_done = 1
        elif rule['kind'] == 'missing':
            bound = next((name for name in names if _fixture_kind(name)), None)
            if not bound or any(r['id'] == f'probe:missing:{n}' for r in rows):
                continue
            missing = str(fixtures / f'missing-input.{_fixture_kind(bound)}')   # never created
            rendered, description = render(argv, fixtures, overrides={bound: (missing, f'<{bound}>=a path that does not exist')})
            if rendered is None:
                continue
            code, out, err = _run(execute, store, op_id, root, rendered, project_id, folder / f'probe-{n}-missing.log',
                                  fixtures / f'probe-{n}-missing.stderr', cwd=cwd)
            rows.append(_judge(f'probe:missing:{n}', op_id, revision_id, rule, shlex.join(rendered), code, out, err, description, cwd=cwd))
    return rows


def _judge(ident, op_id, revision_id, rule, command, code, out, err, description, cwd='.'):
    wanted = rule['code']
    right_code = (code != 0) if wanted is None else (code == wanted)
    passed = bool(right_code and not out.strip() and (err.strip() or not rule['stderr_required']))
    expected = (f'exit {"non-zero" if wanted is None else wanted}, empty stdout' + (', a message on stderr' if rule['stderr_required'] else ''))
    actual = (f'exit {code}, stdout {out.strip()[:60]!r}' if out.strip() else f'exit {code}, empty stdout') + \
             (', stderr empty' if not err.strip() else f', stderr {err.strip()[:60]!r}')
    reason = (f'Diagnostic probe (never acceptance evidence): the request says "{rule["sentence"][:200]}". Run as `{command}` '
              f'({description}) the program gave {actual}; expected {expected}. Detect this input class and fail exactly as requested.')
    return _row(ident, op_id, revision_id, passed=passed, command=command, code=code, stdout=out, stderr=err,
                excerpt=rule['sentence'], description=description, expected=expected, actual=actual, reason=reason, cwd=cwd)


def _flag_rows(execute, store, op_id, root, task, argv, n, fixtures, folder, revision_id, project_id, sentence_text, cwd='.'):
    """probe:command:<n>:flag:<flag>: the positive invocation plus each requested valued flag (`--max-depth N`)."""
    rows = []
    for flag, placeholder, letter in VALUED_FLAG.findall(task or ''):
        key = _normal(placeholder or letter)
        group = _group_for(key)
        if not group or any(flag in token for token in argv):
            continue
        rendered, description = render(argv, fixtures)
        if rendered is None:
            continue
        rendered = rendered + [flag, VALUES[group]]
        ident = f'probe:command:{n}:flag:{flag.lstrip("-")}'
        code, out, err = _run(execute, store, op_id, root, rendered, project_id, folder / f'probe-{n}-flag-{flag.lstrip("-")}.log',
                              fixtures / f'probe-{n}-flag-{flag.lstrip("-")}.stderr', cwd=cwd)
        faults = output_faults(sentence_text, task, out, fixture_kinds(argv)) if code == 0 else []
        passed = code == 0 and not faults
        command = shlex.join(rendered)
        expected = faults[0][0] if faults else 'exit 0'
        actual = (faults[0][1] + f'; observed stdout {(out.strip()[:160] or "(nothing)")!r}') if faults else f'exit {code}'
        reason = (f'Diagnostic probe (never acceptance evidence): the request names `{flag}`. Run as `{command}` ({description}; {flag}={VALUES[group]!r}) '
                  + (f'it exited {code}.' if code else f'it exited 0 but expected {expected}; actual: {actual}.'))
        rows.append(_row(ident, op_id, revision_id, passed=passed, command=command, code=code, stdout=out, stderr=err,
                         excerpt=f'{flag} {placeholder and "<" + placeholder + ">" or letter}', description=description + f'; {flag}={VALUES[group]!r}',
                         expected=expected, actual=actual, reason=reason, cwd=cwd))
    return rows


def command_rows(store, op_id, root, profile, task, revision_id, *, project_id, runner=None):
    """probe:command:<n> rows for the requested invocations that could be derived and located, followed by the
    negative rows (probe:invalid:<n>:<k>, probe:missing:<n>) the request wording promises."""
    from coder_project_runtime import run_probe
    from coder_policy7_phases import layer
    rows = []
    folder = store.root / 'execution' / op_id
    fixtures = folder / 'audit' / 'tmp' / 'probes'
    execute = runner or run_probe
    text = sentences(task)
    rules = wording_rules(task)
    for n, spec in enumerate(requested_commands(task), 1):
        argv = launcher(profile, root, spec)
        if not argv:
            continue
        rendered, description = render(argv, fixtures)
        if rendered is None:
            continue
        sentence = sentence_for(text, spec['excerpt'])
        cwd = spec.get('cwd', '.')
        code, out, err = _run(execute, store, op_id, root, rendered, project_id, folder / f'probe-{n}.log', fixtures / f'probe-{n}.stderr', cwd=cwd)
        faults = output_faults(sentence, task, out, fixture_kinds(argv), rendered) if code == 0 else []
        passed = code == 0 and not faults
        command = shlex.join(rendered)
        expected = '; '.join(f[0] for f in faults) if faults else 'exit 0'
        actual = ('; '.join(f[1] for f in faults) + f'; observed stdout {(out.strip()[:160] or "(nothing)")!r}') if faults else f'exit {code}'
        reason = (f'Diagnostic probe (never acceptance evidence): the request says `{spec["excerpt"]}`. Run as `{command}` ({description}) '
                  + (f'it exited {code}. The requested command must run exactly as written, with the requested file names and entry point, '
                     'and exit 0 for valid input.' if code else f'it exited 0 but expected {expected}; actual: {actual}.'))
        row = _row(f'probe:command:{n}', op_id, revision_id, passed=passed, command=command, code=code, stdout=out, stderr=err,
                   excerpt=spec['excerpt'], description=description, expected=expected, actual=actual, reason=reason, cwd=cwd)
        rows.append(row)
        if layer(row) == 1:
            continue   # cannot start: every further probe of this program would repeat the same fault
        rows.extend(_flag_rows(execute, store, op_id, root, task, argv, n, fixtures, folder, revision_id, project_id, sentence, cwd=cwd))
        rows.extend(_negative_rows(execute, store, op_id, root, spec, argv, n, rules, fixtures, folder, revision_id, project_id))
    return rows


def solution_projects(text):
    """Project file paths a .sln lists, with Windows separators normalized."""
    return [p.replace('\\', '/') for p in re.findall(r'Project\("\{[^}]+\}"\)\s*=\s*"[^"]+",\s*"([^"]+)"', text)]


def solution_test_rows(root, revision_id, execution_id):
    """probe:test-project: a .NET solution must contain a test project with test methods (the empty solution)."""
    root = Path(root)
    solutions = sorted(root.glob('*.sln'))
    if not solutions:
        return []
    listed = solution_projects(solutions[0].read_text(errors='replace'))
    projects = [root / p for p in listed if p.lower().endswith(('.csproj', '.fsproj', '.vbproj'))]
    existing = [p for p in projects if p.is_file()]
    tests = [p for p in existing if TEST_SDK.search(p.read_text(errors='replace'))]
    def has_tests(project):
        return any(TEST_ATTRIBUTE.search(f.read_text(errors='replace')) for f in project.parent.rglob('*.cs')
                   if not set(f.parts) & {'bin', 'obj'})
    if not listed:
        reason = f'{solutions[0].name} lists no projects'
    elif not existing:
        reason = 'the solution references project files that do not exist: ' + ', '.join(listed[:4])
    elif not tests:
        reason = f'the solution has {len(existing)} project(s) and none is a test project (xunit, nunit or MSTest)'
    elif not any(has_tests(p) for p in tests):
        reason = 'the test project has no [Fact]/[Theory]/[Test] methods'
    else:
        reason = ''
    return [{'id': 'probe:test-project', 'origin': 'controller', 'passed': not reason, 'cwd': '.', 'evidence_types': [], 'outcomes': [],
             'command': 'read ' + solutions[0].name, 'classification': 'passed' if not reason else 'application_defect',
             'revision_id': revision_id, 'execution_id': execution_id + ':probe:test-project', 'execution_succeeded': not reason,
             'log_tail': reason, 'reason': '' if not reason else ('Diagnostic probe (never acceptance evidence): ' + reason +
                 '. Deliver the application project AND an xUnit test project referenced by the solution, with real [Fact] tests; '
                 '`dotnet test` reporting zero tests is a failed delivery.')}]
