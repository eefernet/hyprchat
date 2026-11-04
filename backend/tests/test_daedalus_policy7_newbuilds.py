"""New-build delivery: request probes, workflow guard, repair packet, foundation gate, skeletons.

One test per recorded finding of the 2026-09-24 campaigns (frozen-6/7/8, reliability sweeps); each names
the run that found it. Pure Python, no inference, no server. The Codebox-only skeleton verification lives at
the bottom and skips without the toolchains and bwrap.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from coder_request_probes import requested_commands, launcher, render, command_rows, solution_test_rows, solution_projects
from coder_workflow_guard import workflow_steps, workflow_rows


class FakeStore:
    def __init__(self, root):
        self.root = Path(root); self.events = []
    def get(self, op_id):
        return {'payload': {'settings': {'daedalus_command_seconds': 60}}}
    def event(self, op_id, kind, **data):
        self.events.append((kind, data))


# --- request probes -------------------------------------------------------------------------


def test_unknown_placeholders_and_unlocated_programs_are_skipped_not_failed(tmp_path):
    task = 'Run `unitconv <value> <from> <to>` and `python3 tool.py <file>`; also `npm install` and `min X`.'
    specs = requested_commands(task)
    assert {s['excerpt'] for s in specs} == {'unitconv <value> <from> <to>', 'python3 tool.py <file>', 'min X'}
    assert launcher({'profiles': []}, tmp_path, specs[0]) is None          # no built binary named unitconv
    assert launcher({'profiles': []}, tmp_path, [s for s in specs if s['excerpt'] == 'min X'][0]) is None
    rendered, note = render(['unitconv', '<value>', '<from>', '<to>'], tmp_path / 'fx')
    assert rendered is None and 'placeholder' in note
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, {'profiles': []}, task, 'r1', project_id='p',
                        runner=lambda *a, **k: (0, ''))
    assert [r['command'] for r in rows] == [c for c in (r['command'] for r in rows) if c.startswith('python3 tool.py')]


def test_placeholder_first_invocations_run_the_built_dotnet_application(tmp_path):
    # csharp-csvstats: "Running the app with `<file.csv> --column <name>`" names no program at all.
    (tmp_path / 'CsvStats').mkdir(); (tmp_path / 'CsvStats' / 'CsvStats.csproj').write_text('<Project Sdk="Microsoft.NET.Sdk"><OutputType>Exe</OutputType></Project>')
    (tmp_path / 'CsvStats.Tests').mkdir(); (tmp_path / 'CsvStats.Tests' / 'CsvStats.Tests.csproj').write_text('<Project><PackageReference Include="xunit" /></Project>')
    spec = requested_commands('Running the app with `<file.csv> --column <name>` prints min, max and mean.')[0]
    assert spec['application']
    argv = launcher({'profiles': [{'toolchains': ['dotnet']}]}, tmp_path, spec)
    assert argv[:5] == ['dotnet', 'run', '--no-build', '--project', 'CsvStats/CsvStats.csproj'] and argv[5] == '--'
    rendered, note = render(argv, tmp_path / 'fx')
    assert rendered[-2:] == ['--column', 'value'] and Path(rendered[-3]).read_text().startswith('name,value')


# --- browser workflows ----------------------------------------------------------------------


def test_pilotA_workflow_guard_is_a_registered_isolated_probe():
    # Pilot A kanban (2026-09-25): every web case crashed with "Unsupported isolated probe" because the
    # sandboxed call layer allowlists probe functions and the new guard was not on it.
    source = (Path(__file__).resolve().parents[1] / 'coder_sandbox_call.py').read_text()
    assert "('coder_workflow_guard', 'run_workflows')" in source
    import coder_sandbox_call, coder_workflow_guard
    assert callable(getattr(coder_workflow_guard, 'run_workflows'))


def test_pilotA2_workflow_guard_has_no_unresolved_names():
    # Pilot A kanban (2026-09-25): `_fill` was imported inside run_workflows but used by _Session methods as a
    # global, so every workflow step faulted with NameError inside the sandbox where no unit test executes it.
    import ast, builtins
    source = (Path(__file__).resolve().parents[1] / 'coder_workflow_guard.py').read_text()
    tree = ast.parse(source)
    module_names = set(dir(builtins))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module_names.update((alias.asname or alias.name).split('.')[0] for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, ast.Assign):
            module_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    unresolved = set()
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        local = {a.arg for a in func.args.args + func.args.kwonlyargs} | ({func.args.vararg.arg} if func.args.vararg else set())
        for node in ast.walk(func):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                local.update((alias.asname or alias.name).split('.')[0] for alias in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Lambda)) and node is not func:
                local.update(getattr(node, 'name', '') and [node.name] or [])
                if isinstance(node, ast.Lambda):
                    local.update(a.arg for a in node.args.args)
            elif isinstance(node, ast.comprehension):
                local.update(n.id for n in ast.walk(node.target) if isinstance(n, ast.Name))
        for node in ast.walk(func):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in local and node.id not in module_names:
                unresolved.add(node.id)
    assert unresolved == set(), unresolved


def test_pilotA3_silent_drag_that_moves_nothing_and_sends_nothing_fails():
    # Pilot A kanban (2026-09-25): persist/edit/delete passed, drag "completed" but the card never moved and no
    # request was sent; the guard said advisory and the independent browser journey failed on drag persistence.
    source = (Path(__file__).resolve().parents[1] / 'coder_workflow_guard.py').read_text()
    assert 'changed nothing: no request was sent' in source
    task = 'Move tasks between columns via drag-and-drop.'
    rows = workflow_rows({'id': 'app'}, 'http://x', {}, task, prober=lambda *a, **k: {'fault': '', 'writes': [], 'errors': [],
        'steps': {'drag': {'status': 'failed', 'detail': 'dropping the card onto another column changed nothing: no request was sent and the card stayed where it was.'}}})
    assert [r['id'] for r in rows] == ['workflow:app:drag'] and not rows[0]['passed'] and rows[0]['classification'] == 'application_defect'


def test_frozen8_literal_text_rendering_is_probed_only_when_requested():
    assert 'literal' in workflow_steps('Render entered text literally. Preserve expenses across reloads with localStorage.')
    assert workflow_steps('Build a CLI that prints word counts') == []
    task = 'Render entered text literally.'
    rows = workflow_rows({'id': 'app'}, 'http://x', {}, task, prober=lambda *a, **k: {'fault': '', 'writes': [], 'errors': [],
        'steps': {'literal': {'status': 'failed', 'detail': 'entered text is rendered as HTML: <b>daedalus-literal</b> became a bold element'}}})
    assert [r['id'] for r in rows] == ['workflow:app:literal'] and not rows[0]['passed'] and 'bold element' in rows[0]['reason']


# --- repair packet ----------------------------------------------------------------------------


# --- foundation gate ----------------------------------------------------------------------------


# --- skeletons ------------------------------------------------------------------------------------


def test_live7_apply_settings_works_inside_the_worker_bundle_without_backend_config(monkeypatch):
    # Production 2026-09-25: every policy-7 build failed with "IsolationUnavailable ... No module named 'config'"
    # because the sandboxed SDK child calls context_policy.apply_settings and config.py is not in the worker bundle.
    import sys
    import context_policy
    monkeypatch.setitem(sys.modules, 'config', None)   # `import config` now raises ImportError, as on the worker
    monkeypatch.setattr(context_policy, '_FALLBACK_STORE', None)
    context_policy.apply_settings({**context_policy.DEFAULTS, 'context_compaction': 'on', 'openhands_num_ctx': 65536})
    values = context_policy.runtime_settings()
    assert values['openhands_num_ctx'] == 65536 and values['context_compaction'] == 'on'


def test_worker_bundle_modules_never_import_backend_only_modules_unguarded():
    # A backend module the worker imports but does not receive fails at operation time, not at startup.
    import ast, sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    deploy_monitor = pytest.importorskip('deploy_monitor')   # repo root only; the Codebox dev base ships backend/ alone
    bundle = {Path(p).name for p in deploy_monitor.WORKER_FILES | deploy_monitor.WORKER_SHARED if p.endswith('.py')}
    backend_only = {'config', 'database', 'main', 'tools', 'events', 'rag', 'coder_jobs', 'coder_policy7_controller', 'coder_presentation'} - {n[:-3] for n in bundle}
    offenders = []
    for name in sorted(bundle):
        tree = ast.parse((Path(__file__).resolve().parents[1] / name).read_text())
        guarded = {id(n) for t in ast.walk(tree) if isinstance(t, ast.Try) for n in ast.walk(t) if n is not t}
        for node in tree.body:
            targets = [a.name.split('.')[0] for a in node.names] if isinstance(node, ast.Import) else [(node.module or '').split('.')[0]] if isinstance(node, ast.ImportFrom) else []
            if any(t in backend_only for t in targets) and id(node) not in guarded:
                offenders.append(f'{name}:{node.lineno} imports {targets}')
    assert offenders == [], offenders


# --- Codebox only: each skeleton installs, builds, launches and runs its placeholder tests -----------------


# --- sweep3: negative and output-aware probes, browser-guard repair targets ----------------------

RUST_TASK = ('Build a Rust command-line tool with Cargo, package name base64tool, implementing standard Base64 without external '
             'crates. `base64tool encode <text>` prints the Base64 encoding and `base64tool decode <base64>` prints the decoded text. '
             'Invalid Base64 input prints an error to stderr and exits with code 2. Put the codec in src/lib.rs, add integration '
             'tests under tests/ run by `cargo test`, and include a README.')
NODE_TASK = ('Build a Node.js command-line tool named mdtoc with no runtime dependencies. Running `node mdtoc.js <file.md>` prints a '
             'table of contents for the Markdown headings: one line per heading as two spaces of indentation per level below 1, then '
             '`- [Title](#slug)`, where the slug is the lowercased title with spaces replaced by hyphens and characters other than '
             'letters, digits and hyphens removed. Support `--max-depth N` to ignore deeper headings. A missing file prints an error to '
             'stderr and exits with code 2. Include tests run by `npm test` using node:test, and a README.')
GOOD_TOC = '- [Title](#title)\n  - [Section](#section)'


def _cargo_project(root):
    (root / 'Cargo.toml').write_text('[package]\nname = "base64tool"\nversion = "0.1.0"\n')
    (root / 'target' / 'debug').mkdir(parents=True)
    (root / 'target' / 'debug' / 'base64tool').write_text('')
    (root / 'target' / 'debug' / 'base64tool').chmod(0o755)   # launcher() locates executables only


def test_sweep3_node_missing_file_wording_becomes_a_missing_probe(tmp_path):
    (tmp_path / 'mdtoc.js').write_text('')
    mode, calls = {'strict': False}, []
    def runner(store, op_id, root, argv, *, project_id, log, stderr):
        calls.append(argv)
        if Path(argv[2]).is_file():
            return 0, GOOD_TOC, ''
        return (2, '', 'Error: ENOENT: no such file') if mode['strict'] else (0, '', '')
    profile = {'profiles': [{'toolchains': ['node']}]}
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, NODE_TASK, 'r1', project_id='p', runner=runner)
    by_id = {r['id']: r for r in rows}
    assert list(by_id) == ['probe:command:1', 'probe:command:1:flag:max-depth', 'probe:missing:1']
    assert by_id['probe:command:1']['passed'] and by_id['probe:command:1:flag:max-depth']['passed']
    assert calls[1][-2:] == ['--max-depth', '2']
    missing = by_id['probe:missing:1']
    assert not Path(calls[-1][2]).exists() and 'missing-input.md' in calls[-1][2] and 'does not exist' in missing['probe_input']
    assert not missing['passed'] and missing['classification'] == 'application_defect' and missing['evidence_types'] == []
    assert missing['request_excerpt'] == 'A missing file prints an error to stderr and exits with code 2.'
    assert missing['expected'] == 'exit 2, empty stdout, a message on stderr' and missing['actual'] == 'exit 0, empty stdout, stderr empty'
    mode['strict'] = True
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, NODE_TASK, 'r1', project_id='p', runner=runner)
    assert all(r['passed'] for r in rows) and len(rows) == 3


def test_sweep3_node_probe_fails_on_empty_output_and_on_a_fenced_heading(tmp_path):
    # sweep3-c p1 printed nothing and passed; p2 listed `- [not a heading](#not-a-heading)` from inside the fixture's
    # fenced block and passed, because the probe judged exit 0 alone.
    from coder_request_probes import output_faults
    (tmp_path / 'mdtoc.js').write_text('')
    profile = {'profiles': [{'toolchains': ['node']}]}
    def rows_for(output):
        return command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, NODE_TASK, 'r1', project_id='p',
                            runner=lambda store, op, root, argv, *, project_id, log: (0, output))
    silent = rows_for('')[0]
    assert not silent['passed'] and silent['classification'] == 'application_defect' and silent['exit_code'] == 0
    assert silent['expected'] == 'output on stdout' and silent['actual'].startswith('it printed nothing') and 'exited 0 but' in silent['reason']
    fenced = rows_for(GOOD_TOC + '\n- [not a heading](#not-a-heading)')[0]
    assert not fenced['passed'] and 'not a heading' in fenced['expected'] and 'fenced code block' in fenced['expected'] and fenced['actual'].startswith('it is listed')
    assert rows_for(GOOD_TOC)[0]['passed']
    missing_section = rows_for('- [Title](#title)')[0]
    assert not missing_section['passed'] and "'Section'" in missing_section['expected']
    # The rule is tied to the request's purpose: a converter that prints fenced code is not judged by it.
    converter = 'Build `md2html <file.md>` which prints the document as HTML.'
    assert output_faults('`md2html <file.md>` prints the document as HTML.', converter, '<pre># not a heading</pre>', ['md']) == []
    assert output_faults('`md2html <file.md>` prints the document as HTML.', converter, '', ['md']) == [('output on stdout', 'it printed nothing')]


def test_probe_runner_protocol_reads_stderr_from_the_side_file(tmp_path):
    from coder_request_probes import _run
    import inspect
    from coder_project_runtime import run_probe
    assert 'stderr' in inspect.signature(run_probe).parameters   # the real runner splits the streams on request
    def splitting(store, op_id, root, argv, *, project_id, log, stderr):
        Path(stderr).write_text('boom'); return 2, 'out'
    def legacy(store, op_id, root, argv, *, project_id, log):
        return 2, 'merged'
    assert _run(splitting, None, 'op', tmp_path, ['x'], 'p', tmp_path / 'l', tmp_path / 'fx' / 'e') == (2, 'out', 'boom')
    assert _run(legacy, None, 'op', tmp_path, ['x'], 'p', tmp_path / 'l', tmp_path / 'fx' / 'e2') == (2, 'merged', '')
    # `go run` wraps the child's exit code (always 1): the program's own code is what a negative probe judges.
    wrapped = lambda *a, **k: (1, '', 'open nope.log: no such file or directory\nexit status 2\n')
    assert _run(wrapped, None, 'op', tmp_path, ['go', 'run', '.', 'nope.log'], 'p', tmp_path / 'l', tmp_path / 'fx' / 'e3')[0] == 2
    assert _run(wrapped, None, 'op', tmp_path, ['./logsummary', 'nope.log'], 'p', tmp_path / 'l', tmp_path / 'fx' / 'e4')[0] == 1


def test_sweep3_medium_server_log_is_runtime_state_not_source(tmp_path):
    from coder_project_runtime import runtime_data_path
    (tmp_path / 'server.log').write_text('listening'); (tmp_path / 'server.js').write_text('x')
    (tmp_path / 'store.bin').write_bytes(b'SQLite format 3\x00' + b'\0' * 32)
    assert runtime_data_path(tmp_path, 'server.log') and runtime_data_path(tmp_path, 'data/tasks.db') and runtime_data_path(tmp_path, 'store.bin')
    assert not runtime_data_path(tmp_path, 'server.js') and not runtime_data_path(tmp_path, 'public/app.js') and not runtime_data_path(tmp_path, 'nope.txt')


def test_fix4_rust_probes_resolve_the_program_inside_the_cargo_package_directory(tmp_path):
    # fix4-c pass1 (2026-09-26): the Cargo package lived in base64tool/, Cargo.toml was looked up at the root only
    # and every probe was silently skipped; the model's padding tests failed while no probe ever ran.
    from coder_request_probes import launcher, requested_commands, package_roots
    pkg = tmp_path / 'base64tool'
    (pkg / 'src').mkdir(parents=True); (pkg / 'target' / 'debug').mkdir(parents=True)
    (pkg / 'Cargo.toml').write_text('[package]\nname = "base64tool"\nversion = "0.1.0"\n')
    binary = pkg / 'target' / 'debug' / 'base64tool'; binary.write_text('#!/bin/sh\n'); binary.chmod(0o755)
    profile = {'profiles': [{'cwd': 'base64tool', 'toolchains': ['cargo']}]}
    assert [b.name for b in package_roots(profile, tmp_path)] == [tmp_path.name, 'base64tool']
    seen = []
    def runner(store, op_id, root, argv, *, project_id, log, stderr, cwd='.'):
        seen.append((cwd, argv[-1]))
        if 'encode' in argv or argv[-1] == 'aGVsbG8gd29ybGQ=':
            return 0, 'aGVsbG8gd29ybGQ=' if 'encode' in argv else 'hello world', ''
        return 2, '', 'error: invalid base64 input'
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, RUST_TASK, 'r1', project_id='p', runner=runner)
    ids = [r['id'] for r in rows]
    assert 'probe:command:1' in ids and 'probe:command:2' in ids and any(i.startswith('probe:invalid:') for i in ids)
    assert all(r['cwd'] == 'base64tool' and r['passed'] for r in rows), [(r['id'], r['cwd'], r['passed']) for r in rows]
    assert all(cwd == 'base64tool' for cwd, _ in seen) and all(r['command'].startswith(str(binary)) for r in rows)
    # A Node script in a package directory runs there; a script that exists nowhere keeps the root invocation
    # so the probe fails honestly (frozen-7: `python3 wordfreq.py` delivered as a package).
    (tmp_path / 'pkg').mkdir(); (tmp_path / 'pkg' / 'mdtoc.js').write_text('')
    node_profile = {'profiles': [{'cwd': 'pkg', 'toolchains': ['node']}]}
    spec = requested_commands('Run `node mdtoc.js <file.md>` to print the TOC.')[0]
    assert launcher(node_profile, tmp_path, spec) == ['node', 'mdtoc.js', '<file.md>'] and spec['cwd'] == 'pkg'
    spec = requested_commands('Run `python3 wordfreq.py <file>` to count words.')[0]
    assert launcher(node_profile, tmp_path, spec) == ['python3', 'wordfreq.py', '<file>'] and spec['cwd'] == '.'


def test_fix4_node_markdown_fixture_puts_an_info_string_fence_before_a_heading():
    # fix4-c pass1-node-mdtoc: `\`\`\`text` was never closed and `## Next` vanished; the old fixture ended with its fence.
    from coder_request_probes import FIXTURES, output_faults
    text = FIXTURES['md']['text']
    assert text.index('```text') < text.index('## Section') and text.count('```') == 2
    task = 'Print a table of contents for the Markdown headings.'
    assert output_faults('`node mdtoc.js <file.md>` prints the TOC.', task, '- [Title](#title)\n', ['md']) == \
        [("the output mentions 'Section' (a heading of the input file)", 'it does not')]
    assert output_faults('`node mdtoc.js <file.md>` prints the TOC.', task, '- [Title](#title)\n  - [Section](#section)\n', ['md']) == []


def test_fix4_node_requested_output_shape_is_checked_on_the_positive_probe():
    # fix4-c pass2-node-mdtoc (2026-09-26): `# Intro\n  ## Getting Started!` was printed instead of `- [Title](#slug)`
    # lines; the model's five tests and the audit agreed with it and the probe passed on fixture content alone.
    from coder_request_probes import output_templates, output_faults
    node = ('Running `node mdtoc.js <file.md>` prints a table of contents for the Markdown headings: one line per heading as '
            'two spaces of indentation per level below 1, then `- [Title](#slug)`, where the slug is the lowercased title.')
    [(template, shape)] = output_templates(node)
    assert template == '- [Title](#slug)' and shape.match('  - [Getting Started!](#getting-started)') and not shape.match('# Intro')
    bad = '# Intro\n  ## Getting Started!\n    ### Deep Dive\n'
    faults = output_faults(node, node, bad, [])
    assert faults == [('every output line shaped like `- [Title](#slug)` (the request states that format)', "a line is '# Intro'")]
    assert output_faults(node, node, '- [Intro](#intro)\n  - [Getting Started!](#getting-started)\n', []) == []
    go = 'Running `go run . <logfile>` reads a log file and prints one line per level as `LEVEL count`, sorted by level.'
    [(template, shape)] = output_templates(go)
    assert template == 'LEVEL count' and output_faults(go, go, 'ERROR 1\nINFO 3\nWARN 1\n', []) == []
    assert output_faults(go, go, 'ERROR: 1 line\n', []) != []
    # flags, long prose spans and sentences without "line" wording are never templates
    assert output_templates('Support `--top N` (default 10) to limit the number of lines and `--json` to print JSON.') == []
    assert output_templates('`matrixtool transpose "1,2;3,4"` prints the transposed matrix in the same `a,b;c,d` format.') == []


def test_fix5_rust_encode_of_the_controller_input_must_be_the_standard_base64(tmp_path):
    # fix5-c pass2-rust-base64tool (2026-09-27): encode printed `aGVsbG8gd29ybGQA` (an `A` instead of `=` padding) and the
    # positive probe passed on exit 0; decode printed `hello world\x00`. The controller knows its own input's encoding.
    from coder_request_probes import known_transform
    assert known_transform(RUST_TASK, ['x', 'encode', 'hello world']) == 'aGVsbG8gd29ybGQ='
    assert known_transform(RUST_TASK, ['x', 'decode', 'aGVsbG8gd29ybGQ=']) == 'hello world'
    assert known_transform('Build a wordfreq tool.', ['x', 'encode', 'hello world']) is None
    _cargo_project(tmp_path)
    def runner(store, op_id, root, argv, *, project_id, log, stderr):
        if 'encode' in argv:
            return 0, 'aGVsbG8gd29ybGQA\n', ''
        if argv[-1] == 'aGVsbG8gd29ybGQ=':
            return 0, 'hello world\x00\n', ''
        return 2, '', 'error'
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, {'profiles': [{'toolchains': ['cargo']}]}, RUST_TASK, 'r1', project_id='p', runner=runner)
    positives = [r for r in rows if r['id'] in ('probe:command:1', 'probe:command:2')]
    assert [r['passed'] for r in positives] == [False, False]
    assert "stdout exactly 'aGVsbG8gd29ybGQ='" in positives[0]['expected'] and "it printed 'aGVsbG8gd29ybGQA'" in positives[0]['actual']
    assert "stdout exactly 'hello world'" in positives[1]['expected']


def test_frozen7_empty_dotnet_solution_is_named_not_just_missing_coverage(tmp_path):
    # frozen-7 C#: `dotnet test` exited 0 with no project to restore; the gate only said "zero tests".
    (tmp_path / 'CsvStats.sln').write_text('Microsoft Visual Studio Solution File, Format Version 12.00\nGlobal\nEndGlobal\n')
    row = solution_test_rows(tmp_path, 'r1', 'op')[0]
    assert not row['passed'] and 'lists no projects' in row['reason'] and layer(row) == 1
    full = tmp_path / 'full'; full.mkdir()
    (full / 'CsvStats.sln').write_text('Project("{x}") = "CsvStats", "CsvStats/CsvStats.csproj", "a"\nEndProject\nProject("{x}") = "CsvStats.Tests", "CsvStats.Tests/CsvStats.Tests.csproj", "b"\nEndProject\n')
    (full / 'CsvStats').mkdir(); (full / 'CsvStats.Tests').mkdir()
    (full / 'CsvStats/CsvStats.csproj').write_text('<Project/>')
    (full / 'CsvStats.Tests/CsvStats.Tests.csproj').write_text('<Project><ItemGroup><PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.11.1" /></ItemGroup></Project>')
    (full / 'CsvStats.Tests/AppTests.cs').write_text('[Fact] public void Adds() { Assert.Equal(2, 1 + 1); }')
    assert solution_projects((full / 'CsvStats.sln').read_text()) == ['CsvStats/CsvStats.csproj', 'CsvStats.Tests/CsvStats.Tests.csproj']
    assert solution_test_rows(full, 'r1', 'op')[0]['passed']
    (full / 'CsvStats.Tests' / 'AppTests.cs').write_text('public class AppTests {}')
    assert 'no [Fact]' in solution_test_rows(full, 'r1', 'op')[0]['reason']


def test_frozen6_kanban_edit_and_drag_workflows_are_probed():
    # frozen-6 Kanban: Edit called a missing GET /api/tasks/:id (404); drag sent a status-only PUT (400).
    task = ('Kanban board: create, edit and delete tasks; move tasks between columns via drag-and-drop; '
            'data persistence in SQLite.')
    assert workflow_steps(task) == ['persist', 'edit', 'drag', 'delete']
    def prober(url, steps, **kw):
        return {'fault': '', 'writes': [], 'errors': [], 'steps': {
            'persist': {'status': 'passed', 'detail': 'still shown after reload'},
            'edit': {'status': 'failed', 'detail': 'the edit workflow sent a request the server rejected: GET /api/tasks/3 -> 404'},
            'drag': {'status': 'failed', 'detail': 'the drag workflow sent a request the server rejected: PUT /api/tasks/3 -> 400'},
            'delete': {'status': 'advisory', 'detail': 'no visible Delete control was found'}}}
    rows = workflow_rows({'id': 'app', 'cwd': '.'}, 'http://127.0.0.1:1', {}, task, prober=prober)
    by_id = {r['id']: r for r in rows}
    assert by_id['workflow:app:persist']['passed'] and by_id['workflow:app:delete']['passed']   # advisory never fails
    assert not by_id['workflow:app:edit']['passed'] and by_id['workflow:app:edit']['classification'] == 'application_defect'
    assert not by_id['workflow:app:drag']['passed'] and all(r['evidence_types'] == [] and r['outcomes'] == [] for r in rows)
    assert layer(by_id['workflow:app:drag']) == 3
    down = workflow_rows({'id': 'app'}, 'http://x', {}, task, prober=lambda *a, **k: {'fault': 'Error: browser missing', 'steps': {}, 'writes': [], 'errors': []})
    assert all(r['environment_fault'] and r['classification'] == 'environment' and not r['passed'] for r in down)


def test_sweep3_rust_invalid_input_wording_becomes_negative_probes(tmp_path):
    # sweep3-c: the decoder accepted `====` (printed a NUL byte, exit 0) and no test, audit or probe ever asked.
    from coder_request_probes import wording_rules
    assert wording_rules(RUST_TASK) == [{'kind': 'invalid', 'what': 'base64', 'code': 2, 'stderr_required': True,
                                         'sentence': 'Invalid Base64 input prints an error to stderr and exits with code 2.'}]
    _cargo_project(tmp_path)
    mode, calls = {'strict': False, 'quiet': False}, []
    def runner(store, op_id, root, argv, *, project_id, log, stderr):
        calls.append(argv)
        if 'encode' in argv:
            return 0, 'aGVsbG8gd29ybGQ=', ''
        if argv[-1] == 'aGVsbG8gd29ybGQ=':
            return 0, 'hello world', ''
        if mode['strict']:
            return 2, '', '' if mode['quiet'] else 'error: invalid base64 input'
        return 0, 'h', ''   # lenient decoder: garbage in, a byte out
    profile = {'profiles': [{'toolchains': ['cargo']}]}
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, RUST_TASK, 'r1', project_id='p', runner=runner)
    positives = [r for r in rows if r['id'].startswith('probe:command:')]
    negatives = [r for r in rows if r['id'].startswith('probe:invalid:')]
    assert [r['passed'] for r in positives] == [True, True] and not [r for r in rows if r['id'].startswith('probe:missing')]
    decode = positives[1]['id'].rsplit(':', 1)[-1]   # the decode invocation's own index
    assert [r['id'] for r in negatives] == [f'probe:invalid:{decode}:{k}' for k in (1, 2, 3, 4)]
    assert [c[-1] for c in calls[-4:]] == ['%%%', '====', 'a===', 'A']
    for row in negatives:
        assert not row['passed'] and row['classification'] == 'application_defect' and row['origin'] == 'controller'
        assert row['evidence_types'] == [] and row['outcomes'] == [] and layer(row) == 2
        assert row['request_excerpt'] == 'Invalid Base64 input prints an error to stderr and exits with code 2.'
        assert row['expected'] == 'exit 2, empty stdout, a message on stderr' and row['actual'].startswith("exit 0, stdout 'h'")
        assert row['reason'].startswith('Diagnostic probe (never acceptance evidence)') and '<base64>=' in row['probe_input']
    mode['strict'] = True
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, RUST_TASK, 'r1', project_id='p', runner=runner)
    assert all(r['passed'] for r in rows) and len([r for r in rows if r['id'].startswith('probe:invalid:')]) == 4
    mode['quiet'] = True   # exits 2 silently: the request asked for a message on stderr
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, profile, RUST_TASK, 'r1', project_id='p', runner=runner)
    assert all(not r['passed'] and r['actual'].endswith('stderr empty') for r in rows if r['id'].startswith('probe:invalid:'))


def test_negative_probes_are_skipped_without_a_binding_or_a_startable_program(tmp_path):
    import os
    # (a) "invalid expressions" names a class the controller has no invalid value for: no row, never a guess.
    task = 'Build `rpncalc <expression>` which prints the result. Invalid expressions print an error to stderr and exit with code 2.'
    (tmp_path / 'build').mkdir(); binary = tmp_path / 'build' / 'rpncalc'; binary.write_text(''); os.chmod(binary, 0o755)
    rows = command_rows(FakeStore(tmp_path / 's'), 'op', tmp_path, {'profiles': [{'toolchains': ['cmake']}]}, task, 'r1',
                        project_id='p', runner=lambda *a, **k: (0, '7', ''))
    assert [r['id'] for r in rows] == ['probe:command:1'] and rows[0]['passed']
    # (b) the program cannot start: the positive row is the foundation defect; bad-input rows would only repeat it.
    other = tmp_path / 'other'; other.mkdir(); _cargo_project(other)
    rows = command_rows(FakeStore(other / 's'), 'op', other, {'profiles': [{'toolchains': ['cargo']}]}, RUST_TASK, 'r1', project_id='p',
                        runner=lambda *a, **k: (127, 'bash: target/debug/base64tool: No such file or directory', ''))
    assert [r['id'] for r in rows] == ['probe:command:1', 'probe:command:2'] and all(layer(r) == 1 for r in rows)
    # (c) nothing to launch: nothing at all.
    empty = tmp_path / 'empty'; empty.mkdir()
    assert command_rows(FakeStore(empty / 's'), 'op', empty, {'profiles': [{'toolchains': ['cargo']}]}, RUST_TASK, 'r1', project_id='p',
                        runner=lambda *a, **k: (0, '', '')) == []

from coder_check_layers import layer
