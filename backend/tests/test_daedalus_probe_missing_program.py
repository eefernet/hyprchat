"""Review M1a: a requested command whose program the build did not deliver under that name produced no probe row,
so the project's own tests alone gave "Checks passed". It is now a failing foundation probe; output literals,
ambiguous launchers and unknown placeholders stay skipped."""
from pathlib import Path

from coder_policy7_evidence import acceptance, blocking
from coder_policy7_phases import layer, progress
from coder_request_probes import command_rows, invokes_program, launcher, requested_commands

CARGO = {'profiles': [{'toolchains': ['cargo']}]}
CMAKE = {'profiles': [{'toolchains': ['cmake']}]}
B64_TASK = 'Write a Rust CLI. `base64tool encode hello` prints aGVsbG8=.'


class FakeStore:
    def __init__(self, root):
        self.root = Path(root); self.events = []
    def get(self, op_id):
        return {'payload': {'settings': {'daedalus_command_seconds': 60}}}
    def event(self, op_id, kind, **data):
        self.events.append((kind, data))


def _executable(path):
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text('#!/bin/sh\n'); path.chmod(0o755)
    return path


def _crate(root, name):
    (root / 'Cargo.toml').write_text(f'[package]\nname = "{name}"\nversion = "0.1.0"\n')
    return _executable(root / 'target' / 'debug' / name)


def _rows(root, profile, task, runner):
    return command_rows(FakeStore(root / 's'), 'op', root, profile, task, 'r1', project_id='p', runner=runner)


def _recording(calls, result=(0, 'aGVsbG8=', '')):
    def runner(store, op_id, root, argv, *, project_id, log, stderr):
        calls.append(argv)
        return result
    return runner


def test_crate_named_differently_from_the_requested_program_is_a_failing_foundation_row(tmp_path):
    _crate(tmp_path, 'b64')
    [spec] = requested_commands(B64_TASK)
    assert launcher(CARGO, tmp_path, spec) is None and invokes_program(B64_TASK, spec)
    calls = []
    rows = _rows(tmp_path, CARGO, B64_TASK, _recording(calls))
    assert calls == [] and [r['id'] for r in rows] == ['probe:command:1']   # nothing ran: there is nothing to run
    row = rows[0]
    assert not row['passed'] and row['classification'] == 'application_defect' and row['origin'] == 'controller'
    assert row['evidence_types'] == [] and row['outcomes'] == [] and row['cwd'] == '.' and row['exit_code'] is None
    assert row['command'] == 'base64tool encode hello' and row['request_excerpt'] == 'base64tool encode hello'
    assert row['reason'].startswith('Diagnostic probe (never acceptance evidence)') and '`base64tool`' in row['reason']
    assert 'exactly that name/path' in row['reason'] and 'target/debug/b64' in row['reason']
    assert layer(row) == 1 and blocking(row) and progress(rows)['blocked_by'] == ['probe:command:1']
    # The finding itself: a passing build and test suite are no longer "Checks passed" without the requested program.
    build = {'id': '.:build:0', 'origin': 'project', 'phase': 'build', 'passed': True, 'revision_id': 'r1', 'execution_id': 'b'}
    tests = {'id': '.:test:0', 'origin': 'project', 'phase': 'test', 'is_test': True, 'test_count': 3, 'passed': True,
             'revision_id': 'r1', 'execution_id': 't'}
    assert acceptance([], [build, tests], {}, 'r1')['checks_passed']
    assert not acceptance([], [build, tests, row], {}, 'r1')['checks_passed']


def test_output_literals_after_prints_never_become_rows(tmp_path):
    _crate(tmp_path, 'b64')   # something was built, so only the wording keeps these spans out
    task = ('`b64 decode aGVsbG8gd29ybGQ=` prints `hello world`. With a file it prints `words 3` on one line. '
            'The output is `total 12`.')
    specs = requested_commands(task)
    literals = [s for s in specs if s['excerpt'] in {'hello world', 'words 3', 'total 12'}]
    assert len(literals) == 3 and not any(invokes_program(task, s) for s in literals)
    calls = []
    rows = _rows(tmp_path, CARGO, task, _recording(calls, (0, 'hello world', '')))
    assert [r['id'] for r in rows] == ['probe:command:1'] and rows[0]['passed'] and len(calls) == 1


def test_dot_slash_program_missing_from_the_project_root_fails_even_with_nothing_built(tmp_path):
    task = 'Running `./wordcount input.txt` prints the number of words.'
    calls = []
    rows = _rows(tmp_path, CMAKE, task, _recording(calls))
    assert calls == [] and [r['id'] for r in rows] == ['probe:command:1']
    row = rows[0]
    assert not row['passed'] and row['classification'] == 'application_defect' and layer(row) == 1
    assert '`./wordcount`' in row['reason'] and 'at that path from the project root' in row['reason']
    # Built elsewhere: still the requested path is missing, and the packet names what the build produced.
    other = tmp_path / 'other'; other.mkdir(); _executable(other / 'build' / 'wordcount')
    [row] = _rows(other, CMAKE, task, _recording(calls))
    assert calls == [] and not row['passed'] and 'build/wordcount' in row['reason'] and layer(row) == 1


def test_found_programs_keep_the_existing_probe(tmp_path):
    binary = _crate(tmp_path, 'base64tool')
    calls = []
    [row] = _rows(tmp_path, CARGO, B64_TASK, _recording(calls))
    assert calls == [[str(binary), 'encode', 'hello']]
    assert row['passed'] and row['classification'] == 'passed' and row['exit_code'] == 0 and row['reason'] == ''
    assert row['command'] == f'{binary} encode hello' and row['expected'] == 'exit 0'
    script = tmp_path / 'wc'; script.mkdir(); _executable(script / 'wordcount')
    calls = []
    [row] = _rows(script, CMAKE, 'Running `./wordcount input.txt` prints 3.', _recording(calls, (0, '3', '')))
    assert calls == [['./wordcount', 'input.txt']] and row['passed']


def test_ambiguous_launchers_unknown_placeholders_tools_and_unbuilt_projects_stay_skipped(tmp_path):
    never = lambda *a, **k: (_ for _ in ()).throw(AssertionError('no probe may run'))
    # Nothing built at all: the setup/build rows own that (an empty Cargo project, the policy-7 newbuilds case).
    assert _rows(tmp_path, CARGO, B64_TASK, never) == []
    # Several application projects: the launcher cannot pick the requested program.
    apps = tmp_path / 'apps'
    for name in ('One', 'Two'):
        (apps / name).mkdir(parents=True); (apps / name / f'{name}.csproj').write_text('<Project Sdk="Microsoft.NET.Sdk" />')
    assert _rows(apps, {'profiles': [{'toolchains': ['dotnet']}]},
                 'Running the app with `<file.csv> --column <name>` prints min, max and mean.', never) == []
    built = tmp_path / 'built'; built.mkdir(); _crate(built, 'b64')
    for task in ('Run `base64tool <value> <unit>` to convert.',     # unknown placeholder: render() has no fixture
                 'Run `sh setup.sh` first.',                        # an installed tool, not the deliverable
                 'Run `./gradlew test` to verify.',                 # a build-tool wrapper
                 'It handles input; also `min X` and `max X`.'):   # no invocation wording
        assert _rows(built, CARGO, task, never) == [], task


NODE = {'profiles': [{'toolchains': ['node'], 'cwd': '.'}]}
MDTOC_TASK = 'Build a Node CLI. Running `mdtoc <file.md>` prints a table of contents, one line per heading.'


def _node_cli(root, manifest, script='bin/mdtoc.js', mode=0o755):
    import json
    root.mkdir(parents=True, exist_ok=True)
    (root / 'package.json').write_text(json.dumps(manifest))
    path = root / script
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text('#!/usr/bin/env node\n'); path.chmod(mode)


def test_a_package_json_bin_mapping_runs_the_mapped_script_whatever_its_mode(tmp_path):
    """Review finding 6: `mdtoc` packaged as {"bin": {"mdtoc": "bin/mdtoc.js"}} failed as a missing program, and only
    when bin/mdtoc.js happened to be executable."""
    for mode in (0o755, 0o644):
        root = tmp_path / oct(mode)
        _node_cli(root, {'name': 'mdtoc', 'bin': {'mdtoc': 'bin/mdtoc.js'}}, mode=mode)
        calls = []
        [row] = _rows(root, NODE, MDTOC_TASK, _recording(calls, (0, '- [Title](#title)', '')))
        assert calls[0][:2] == ['node', 'bin/mdtoc.js'] and row['id'] == 'probe:command:1' and row['exit_code'] == 0
        assert row['command'].startswith('node bin/mdtoc.js ') and 'was not found' not in row['reason']
    # A string "bin" installs the package name without its scope; a package directory records its cwd.
    scoped = tmp_path / 'scoped'
    _node_cli(scoped / 'cli', {'name': '@me/mdtoc', 'bin': './index.js'}, script='index.js')
    [spec] = requested_commands(MDTOC_TASK)
    assert launcher({'profiles': [{'toolchains': ['node'], 'cwd': 'cli'}]}, scoped, spec)[:2] == ['node', 'index.js']
    assert spec['cwd'] == 'cli'


def test_a_source_script_under_bin_is_not_build_output(tmp_path):
    # No mapping names the program: an executable bin/mdtoc.js is a source script, so nothing was built and the
    # setup/build rows own it, exactly as for the same file without the execute bit.
    _node_cli(tmp_path, {'name': 'tools'})
    never = lambda *a, **k: (_ for _ in ()).throw(AssertionError('no probe may run'))
    assert _rows(tmp_path, NODE, MDTOC_TASK, never) == []
    # A compiled output beside it still makes the missing requested program a failing foundation row.
    _executable(tmp_path / 'build' / 'mdtoc-bin')
    [row] = _rows(tmp_path, NODE, MDTOC_TASK, never)
    assert not row['passed'] and layer(row) == 1 and 'build/mdtoc-bin' in row['reason'] and 'mdtoc.js' not in row['reason']
