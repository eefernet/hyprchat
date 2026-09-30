"""The bounded evidence packet a repair round receives, lowest failing layer first.

Before this, the builder's repair prompt was KEY ERROR LINES plus up to eight "id: reason" lines. Two live
costs of that (2026-09-24): a Java builder rewrote correct Kelvin code eight times to satisfy its OWN wrong
test, because nothing said the test was generated and what the request actually asked; and a Rust repair
ran `rm -rf base64tool && cargo new base64tool` on a checkpoint with 14 passing tests, because the round
read like a fresh build. Every entry here carries the request excerpt it comes from, the exact command or
browser action, the input, expected/actual where observed, the decisive trace lines, the owning files and
the failed revision. Entries are ordered by layer (foundation, core, integration, delivery): a later item
often disappears once an earlier one is fixed. Budgets and routing are untouched; this is rendering.
"""
from __future__ import annotations

from coder_policy7_phases import LAYERS, ORIGIN_RANK, layer

LIMIT = 8


ECOSYSTEM_MANIFESTS = {'npm': ('package.json',), 'node': ('package.json',), 'npx': ('package.json',), 'vitest': ('package.json',),
    'jest': ('package.json',), 'python': ('requirements.txt', 'pyproject.toml'), 'python3': ('requirements.txt', 'pyproject.toml'),
    'pytest': ('requirements.txt', 'pyproject.toml'), 'pip': ('requirements.txt', 'pyproject.toml'), 'mvn': ('pom.xml',),
    'gradle': ('build.gradle', 'build.gradle.kts'), 'gradlew': ('build.gradle', 'build.gradle.kts'), 'cargo': ('Cargo.toml',),
    'go': ('go.mod',), 'cmake': ('CMakeLists.txt',), 'ctest': ('CMakeLists.txt',), 'make': ('Makefile', 'CMakeLists.txt'), 'dotnet': ()}


GUARD_PREFIXES = ('workflow:', 'form:', 'page:', 'frontend:', 'interface:labels')
FRONTEND_DIRS = ('public/', 'frontend/src/', 'static/', 'templates/')
SERVER_ENTRIES = ('server.js', 'app.py', 'main.py', 'index.js', 'app.js')
FRONTEND_SUFFIXES = {'.html', '.js', '.jsx', '.ts', '.tsx', '.vue', '.py', '.mjs', '.cjs'}


def frontend_sources(known, prefix='', limit=8):
    """Delivered non-test sources a page-level failure implicates: front-end folders first, then the server entry."""
    from pathlib import Path
    is_test = lambda p: any('test' in part.lower() or 'spec' in part.lower() for part in Path(p).parts)
    local = sorted(p for p in known if p.startswith(prefix) and Path(p).suffix in FRONTEND_SUFFIXES and not is_test(p))
    front = [p for p in local if p[len(prefix):].startswith(FRONTEND_DIRS)]
    servers = [prefix + name for name in SERVER_ENTRIES if prefix + name in known]
    return list(dict.fromkeys(front + servers))[:limit]


def repair_targets(job, defects):
    """Files a repair may edit, most implicated first: paths the failing log names, the failing
    command's own file arguments, traced bindings, then the owning ecosystem's manifest only."""
    import re, shlex
    from pathlib import Path
    known = set(job.get('last_patch', {}).get('source_hashes', {}))
    paths = []
    for defect in defects:
        first = len(paths)
        package = defect.get('cwd', '.') or '.'
        prefix = '' if package == '.' else package.rstrip('/') + '/'
        log = defect.get('log_tail') or ''
        # Tracebacks and compiler errors name the file; execution copies live under .../project/.
        for match in re.findall(r'[\w./@+-]+\.[A-Za-z0-9]{1,6}', log):
            candidate = match.split('/project/', 1)[-1].lstrip('./')
            for name in (candidate, prefix + candidate):
                if name in known:
                    paths.append(name)
        try:
            words = shlex.split(defect.get('command') or '')
        except ValueError:
            words = []
        paths.extend(prefix + w for w in words if prefix + w in known)
        paths.extend(b['path'] for b in defect.get('test_files', []) + defect.get('source_bindings', []))
        if defect.get('id') == 'immutable-source':
            # The files a check rewrote, and the delivered tests that most likely did it.
            paths.extend(p for p in defect.get('paths', []) if p in known)
            paths.extend(b['path'] for c in job.get('checks', []) if c.get('is_test') and c.get('origin') == 'project'
                         for b in c.get('test_files', []))
            continue
        if str(defect.get('id', '')).startswith('skeleton:'):
            paths.extend(p for p in defect.get('paths', []) if p in known)
            continue
        if str(defect.get('id', '')).startswith('execution-con'):
            paths.extend(name for name in ('.daedalus-run.json', '.daedalus.json') if name in known)
        runner = Path(words[0]).name.strip('"$') if words else ''
        manifests = list(ECOSYSTEM_MANIFESTS.get(runner, ()))
        if runner == 'dotnet':
            manifests = [name[len(prefix):] for name in known if name.startswith(prefix) and name.endswith(('.csproj', '.sln')) and '/' not in name[len(prefix):]]
        paths.extend(prefix + name for name in manifests if prefix + name in known)
        if prefix + '.daedalus.json' in known and ('DAEDALUS_AUDIT_PYTHON' in (defect.get('command') or '') or not words):
            paths.append(prefix + '.daedalus.json')
        if str(defect.get('id', '')).startswith(GUARD_PREFIXES):
            # A browser-guard row has no command file, no bindings and no exit code (sweep3 Kanban: three
            # failing workflow:app:* rows added nothing, and the union collapsed to the skeleton's test file).
            paths.extend(frontend_sources(known, prefix))
        # Resolve each defect separately. Another failure's test path must not
        # suppress the owning application files for a missing flag or native crash.
        local = paths[first:]
        owns_app = any(Path(p).suffix in {'.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.rs', '.java', '.cs', '.c', '.cpp'}
                       and not any('test' in part.lower() or 'spec' in part.lower() for part in Path(p).parts) for p in local)
        if not owns_app and (str(defect.get('id', '')).startswith('interface:') or defect.get('exit_code') not in (None, 0)
                             or defect.get('classification') == 'missing_provenance'):
            paths.extend(p for p in sorted(known) if p.startswith(prefix) and Path(p).suffix in {
                '.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.rs', '.java', '.cs', '.c', '.cpp', '.html'}
                and not any('test' in part.lower() or 'spec' in part.lower() for part in Path(p).parts))
    # Missing deliverables have no bindings yet. Add only the planned files of that kind,
    # so one documentation gap does not queue every source file for a rewrite.
    planned = [p for batch in job['brief']['batches'] for p in batch.get('files', [])]
    is_test = lambda p: any('test' in part.lower() or 'spec' in part.lower() for part in p.split('/'))
    is_doc = lambda p: p.lower().endswith(('.md', '.rst', '.txt'))
    for defect in defects:
        kind = str(defect.get('id', '')).rsplit(':', 1)[-1] if defect.get('outcome') else ''
        if kind == 'documentation':
            paths.extend([p for p in planned if is_doc(p)] or ['README.md'])
        elif kind == 'tests':
            paths.extend(p for p in planned if is_test(p) or p.endswith('.daedalus.json'))
        elif kind == 'review':
            paths.extend(p for p in planned if not is_test(p))
    if not paths:
        paths.extend(planned)
    return list(dict.fromkeys(paths))

RENDER_BYTES = 2600


def _expected_actual(defect):
    ident = str(defect.get('id') or '')
    if defect.get('expected') or defect.get('actual'):
        return str(defect.get('expected') or '')[:160], str(defect.get('actual') or '')[:160]
    if ident.startswith('probe:command:'):
        return 'exit 0', f"exit {defect.get('exit_code')}"
    if ident.startswith('workflow:'):
        return 'the requested workflow completes', (defect.get('log_tail') or '')[:200]
    if ident.startswith('launch:'):
        return 'the service answers its readiness probe', 'it did not (see trace)'
    if ident.startswith('page:'):
        return 'the page loads without uncaught errors', ' | '.join(defect.get('page_errors', [])[:2])[:200]
    for failure in defect.get('assertion_failures') or []:
        if not isinstance(failure, dict):
            continue
        expected, actual = failure.get('expected'), failure.get('actual')
        if expected is not None or actual is not None:
            return str(expected)[:160], str(actual)[:160]
        values = failure.get('values')
        if isinstance(values, dict) and values:
            return '', 'observed ' + ', '.join(f'{k}={v!r}' for k, v in list(values.items())[:4])[:200]
    if defect.get('exit_code') not in (None, 0):
        return 'exit 0', f"exit {defect.get('exit_code')}"
    return '', ''


def _files(defect, job):
    names = [b['path'] for b in defect.get('test_files', []) + defect.get('source_bindings', []) if isinstance(b, dict) and b.get('path')]
    names += [p for p in defect.get('paths', []) if isinstance(p, str)]
    targets = job.get('repair_targets') or []
    log = defect.get('log_tail') or ''
    names += [t for t in targets if t in log or t.rsplit('/', 1)[-1] in log]
    return list(dict.fromkeys(names)) or list(targets[:6])


def packet(job, defects, targets=None, limit=LIMIT):
    """Ordered, bounded entries; every foundation entry is kept even beyond the limit."""
    outcomes = {o.get('id'): o for o in (job.get('brief') or {}).get('outcomes', []) if isinstance(o, dict)}
    from coder_policy7_builder import key_error_lines
    entries = []
    for defect in defects or []:
        ident = str(defect.get('id') or '')
        excerpt = defect.get('request_excerpt') or ' / '.join(
            outcomes[i].get('text', '') for i in defect.get('outcomes', []) if i in outcomes)
        # Missing-deliverable defects carry the outcome OBJECT under 'outcome'; others carry its id.
        reference = defect.get('outcome')
        if not excerpt and isinstance(reference, dict):
            excerpt = reference.get('text', '')
        elif not excerpt and isinstance(reference, str) and reference in outcomes:
            excerpt = outcomes[reference].get('text', '')
        expected, actual = _expected_actual(defect)
        owning = (targets(defect) if targets else _files(defect, job)) or []
        entries.append({
            'layer': layer(defect), 'layer_label': LAYERS[layer(defect)], 'id': ident,
            'origin': defect.get('origin') or '', 'test_origin': defect.get('test_origin') or '',
            'request_excerpt': str(excerpt)[:300], 'command': str(defect.get('command') or '')[:300],
            'cwd': defect.get('cwd') or '.', 'input': str(defect.get('probe_input') or '')[:360],
            'expected': expected, 'actual': actual, 'trace': key_error_lines([defect], limit=4),
            'reason': str(defect.get('reason') or '')[:400], 'owning_files': list(owning)[:6],
            'failed_revision': defect.get('revision_id') or job.get('revision_id') or '', 'priority': bool(defect.get('priority'))})
    entries = list({e['id']: e for e in reversed(entries)}.values())   # duplicated check rows (probe copy + audit run) once each
    # A `priority` entry (a retained test the previous repair broke) comes before every layer.
    entries.sort(key=lambda e: (not e['priority'], e['layer'], ORIGIN_RANK.get(e['origin'], 1), e['id']))
    foundation = [e for e in entries if e['layer'] == 1]
    return entries[:max(limit, len(foundation))]


def render(entries):
    """Prompt text for the packet, bounded to about RENDER_BYTES."""
    if not entries:
        return ''
    lines = ['REPAIR PACKET (fix in this order; a later item often disappears once an earlier one is fixed):']
    for n, e in enumerate(entries, 1):
        parts = [f"{n}. [{'FIRST - ' if e.get('priority') else ''}{e['layer_label']}] {e['id']}"]
        if e['request_excerpt']:
            parts.append('request: ' + e['request_excerpt'][:160])
        if e['command']:
            parts.append(('action: ' if e['id'].startswith('workflow:') else 'ran: ') + e['command'][:160] + (f" (cwd {e['cwd']})" if e['cwd'] not in ('.', '') else ''))
        if e['input']:
            parts.append('input: ' + e['input'][:360 if e['id'].startswith('probe:') else 120])
        if e['expected'] or e['actual']:
            parts.append('expected: ' + (e['expected'] or '?') + ' / actual: ' + (e['actual'] or '?'))
        if e['test_origin'] == 'current_builder':
            parts.append('this test was written in this job, not by the user: when it contradicts the request, fix the test')
        if e['trace']:
            parts.append('trace: ' + ' | '.join(e['trace'][:2])[:240])
        elif e['reason']:
            parts.append('why: ' + e['reason'][:200])
        if e['owning_files']:
            parts.append('files: ' + ', '.join(e['owning_files'][:5]))
        lines.append(' — '.join(parts))
    text = '\n'.join(lines)
    return text[:RENDER_BYTES] + ('\n[packet truncated]' if len(text) > RENDER_BYTES else '')
