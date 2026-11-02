"""coder_stop_hook: in-session verification decisions (offline; runs real project commands, no bwrap)."""
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import coder_stop_hook as hook


def project(tmp_path, test_body):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'app.py').write_text('def add(a, b):\n    return a + b\n')
    (root / 'test_app.py').write_text('import unittest\nfrom app import add\n\nclass T(unittest.TestCase):\n'
                                      f'    def test_add(self):\n        {test_body}\n')
    # The discovered test command runs through the project venv, as on the worker.
    subprocess.run([sys.executable, '-m', 'venv', str(root / '.venv')], check=True, capture_output=True)
    return root


def status(tmp_path, root, **changes):
    state = {'root': str(root), 'inventory': str(tmp_path / 'inventory'), 'excludes': ['.git', '.venv', '__pycache__'],
             'turns': 3, 'turn_limit': 20, 'finish_seen': True, 'command_seconds': 60, 'hook_seconds': 120,
             'telemetry': str(tmp_path / 'telemetry.jsonl'), **changes}
    path = tmp_path / 'stop-hook.json'
    path.write_text(json.dumps(state))
    return path


def answer(path, capsys):
    assert hook.main(['coder_stop_hook.py', str(path)]) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_a_finish_with_failing_tests_is_denied_with_the_failure(tmp_path, capsys):
    root = project(tmp_path, 'self.assertEqual(add(2, 3), 6)')
    path = status(tmp_path, root)
    decision = answer(path, capsys)
    assert decision['decision'] == 'deny'
    assert 'unittest' in decision['reason'] and 'AssertionError' in decision['reason']
    saved = json.loads(path.read_text())
    assert saved['denials'] == 1 and saved['finish_seen'] is False   # the next stop needs a new finish
    events = [json.loads(line) for line in (tmp_path / 'telemetry.jsonl').read_text().splitlines()]
    assert events[-1]['type'] == 'stop_hook' and events[-1]['data']['decision'] == 'deny'


def test_a_finish_with_passing_tests_is_allowed(tmp_path, capsys):
    root = project(tmp_path, 'self.assertEqual(add(2, 3), 5)')
    assert answer(status(tmp_path, root), capsys)['decision'] == 'allow'


def test_budgets_and_turn_reserve_always_let_the_agent_stop(tmp_path, capsys):
    root = project(tmp_path, 'self.assertEqual(add(2, 3), 6)')
    assert answer(status(tmp_path, root, denials=3), capsys)['decision'] == 'allow'
    assert answer(status(tmp_path, root, turns=18), capsys)['decision'] == 'allow'


def test_a_prose_stop_gets_a_bounded_nudge(tmp_path, capsys):
    root = project(tmp_path, 'self.assertEqual(add(2, 3), 5)')
    path = status(tmp_path, root, finish_seen=False)
    first = answer(path, capsys)
    assert first['decision'] == 'deny' and 'without a tool call' in first['reason']
    assert json.loads(path.read_text())['nudges'] == 1
    assert answer(status(tmp_path, root, finish_seen=False, nudges=2), capsys)['decision'] == 'allow'


def test_environment_failures_never_block(tmp_path):
    assert hook.environment_failure(127, 'bash: mvn: command not found')
    assert hook.environment_failure(1, "OSError: [Errno 30] Read-only file system: '.venv/lib'")
    assert hook.environment_failure(1, 'Could not transfer artifact org.junit:junit from/to central')
    assert not hook.environment_failure(1, 'AssertionError: 5 != 6')


def test_a_broken_hook_state_lets_the_agent_stop(tmp_path, capsys):
    path = tmp_path / 'stop-hook.json'
    path.write_text(json.dumps({'root': str(tmp_path / 'missing'), 'finish_seen': True, 'turns': 1, 'turn_limit': 20}))
    assert answer(path, capsys)['decision'] == 'allow'


def test_dns_codes_do_not_match_inside_python_and_java_not_found_errors():
    # 'eNotFound' inside these used to make an own-module import error or a missing data file an environment fault.
    assert not hook.environment_failure(1, "ModuleNotFoundError: No module named 'wordfreq'")
    assert not hook.environment_failure(1, 'FAILED tests/test_app.py::test_load - FileNotFoundError: data.txt')
    assert not hook.environment_failure(1, 'java.io.FileNotFoundException: input.csv')
    assert hook.environment_failure(1, 'Error: getaddrinfo ENOTFOUND registry.npmjs.org')
    assert hook.environment_failure(1, "npm ERR! code: 'ENOTFOUND'")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_command_is_judged_by_its_own_exit_not_by_processes_it_leaves_behind(tmp_path):
    started = time.monotonic()
    result = hook.run('sleep 30 & echo $! > leftover.pid; echo tests-passed', tmp_path, 10)
    assert result == (0, 'tests-passed', False) and time.monotonic() - started < 5
    leftover, deadline = int((tmp_path / 'leftover.pid').read_text()), time.monotonic() + 3
    while alive(leftover) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(leftover)   # killed with the command's process group
    # A process in its own session escapes the group, but it no longer holds the hook open.
    escape = shlex.quote(sys.executable) + ' -c "import subprocess; print(subprocess.Popen([\'sleep\', \'30\'], start_new_session=True).pid)"'
    started = time.monotonic()
    code, tail, timed_out = hook.run(escape, tmp_path, 10)
    os.kill(int(tail.split()[-1]), signal.SIGKILL)
    assert (code, timed_out) == (0, False) and time.monotonic() - started < 5
    assert hook.run('echo started; sleep 30', tmp_path, 1) == (124, 'started', True)


def test_the_turn_reserve_leaves_room_to_fix_and_finish(tmp_path):
    root = project(tmp_path, 'self.assertEqual(add(2, 3), 6)')
    state = json.loads(status(tmp_path, root).read_text())
    assert hook.decide({**state, 'turns': 15}, 60)[0] == 'deny'    # five turns left: enough to fix and finish
    assert hook.decide({**state, 'turns': 16}, 60)[0] == 'allow'
    assert hook.decide({**state, 'turns': 31, 'turn_limit': 40}, 60)[0] == 'allow'   # a quarter of a long allowance


def test_a_pass_that_ends_after_a_denied_finish_is_reported_unresolved():
    assert hook.outcome({'denials': 2, 'nudges': 1}, 1, False) == {
        'hook_denials': 2, 'hook_nudges': 1, 'hook_denied_unresolved': True}
    assert not hook.outcome({'denials': 2}, 1, True)['hook_denied_unresolved']    # a later finish was allowed
    assert not hook.outcome({'denials': 1}, 1, False)['hook_denied_unresolved']   # no denial in this pass
    assert hook.outcome({}, 0, False) == {'hook_denials': 0, 'hook_nudges': 0, 'hook_denied_unresolved': False}


def test_a_package_whose_dependency_environment_moved_is_left_to_the_controller(tmp_path, capsys):
    root = project(tmp_path, 'self.assertEqual(add(2, 3), 5)')
    started = hook.environment_keys(json.loads(status(tmp_path, root).read_text()))   # as run_coder records them
    # The agent adds a pytest-style test: the operation's read-only .venv has no pytest and cannot get one.
    (root / 'test_more.py').write_text('from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n')
    unjudged = answer(status(tmp_path, root), capsys)
    assert unjudged['decision'] == 'deny' and 'No module named pytest' in unjudged['reason']
    assert answer(status(tmp_path, root, environment_keys=started), capsys)['decision'] == 'allow'
    events = [json.loads(line) for line in (tmp_path / 'telemetry.jsonl').read_text().splitlines()]
    assert 'dependency environment changed' in events[-1]['data']['reason']
    # A package that appeared during the operation has no prepared environment either.
    assert answer(status(tmp_path, root, environment_keys={}), capsys)['decision'] == 'allow'


def test_an_unchanged_dependency_environment_still_denies_real_failures(tmp_path, capsys):
    root = project(tmp_path, 'self.assertEqual(add(2, 3), 6)')
    started = hook.environment_keys(json.loads(status(tmp_path, root).read_text()))
    assert started and answer(status(tmp_path, root, environment_keys=started), capsys)['decision'] == 'deny'


def test_a_package_without_setup_has_no_environment_to_go_stale(tmp_path, capsys):
    if not shutil.which('make'):
        pytest.skip('make is not installed')
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'Makefile').write_text("all:\n\t@true\n\ntest:\n\t@echo 'expected 5, got 6'; exit 1\n")
    decision = answer(status(tmp_path, root, environment_keys={}), capsys)
    assert decision['decision'] == 'deny' and 'expected 5, got 6' in decision['reason']


def test_timeouts_set_by_the_hooks_own_limits_are_not_project_failures(tmp_path, monkeypatch):
    root = project(tmp_path, 'self.assertEqual(add(2, 3), 5)')
    limits = []

    def timed_out(command, cwd, timeout):
        limits.append(timeout)
        return 124, '', True
    monkeypatch.setattr(hook, 'run', timed_out)
    state = json.loads(status(tmp_path, root, command_seconds=600).read_text())
    decision, reason, _ = hook.decide(state, 870)
    assert decision == 'allow' and limits[0] == 300 and 'stopped at 300 s' in reason   # the hook's cap, not 600 s
    state['command_seconds'] = 60
    assert hook.decide(state, 40)[0] == 'allow'   # cut short by the hook's remaining budget
    decision, reason, _ = hook.decide(state, 120)
    assert decision == 'deny' and 'timed out' in reason   # ran for the full limit the controller applies too


def test_cmake_sets_up_in_the_workspace_so_round_zero_and_manifest_edits_still_deny(tmp_path, capsys):
    if not (shutil.which('cmake') and shutil.which('c++')):
        pytest.skip('cmake or a C++ compiler is not installed')
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'test_add.cpp').write_text('#include <cstdio>\nint add(int a, int b) { return a + b; }\n'
                                       'int main() { if (add(2, 3) != 6) { std::puts("expected 6, got 5"); return 1; } }\n')
    (root / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.10)\nproject(t CXX)\nenable_testing()\n'
                                         'add_executable(t_add test_add.cpp)\nadd_test(NAME add COMMAND t_add)\n')
    # Round 0: the workspace was empty when the operation began (keys {}) and the agent configured build/ itself.
    subprocess.run(['cmake', '-S', '.', '-B', 'build'], cwd=root, check=True, capture_output=True)
    excludes = ['.git', 'build']
    started = hook.environment_keys(json.loads(status(tmp_path, root, excludes=excludes).read_text()))
    assert started == {}   # cmake configures inside the writable workspace: no read-only cache to go stale
    decision = answer(status(tmp_path, root, excludes=excludes, environment_keys={}), capsys)
    assert decision['decision'] == 'deny' and 'expected 6, got 5' in decision['reason']
    (root / 'CMakeLists.txt').write_text((root / 'CMakeLists.txt').read_text() + '# agent edit\n')
    decision = answer(status(tmp_path, root, excludes=excludes, environment_keys=started), capsys)
    assert decision['decision'] == 'deny' and 'expected 6, got 5' in decision['reason']


def node_project(tmp_path, test_source, **manifest):
    if not (shutil.which('node') and shutil.which('npm')):
        pytest.skip('node is not installed')
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'package.json').write_text(json.dumps({'name': 'mdtoc', 'version': '1.0.0', 'scripts': {'test': 'node --test'}, **manifest}))
    (root / 'mdtoc.js').write_text('module.exports = s => s.split("\\n").filter(l => l.startsWith("#"));\n')
    (root / 'mdtoc.test.js').write_text("const test = require('node:test');\nconst assert = require('node:assert');\n" + test_source)
    return root


def test_a_dependency_free_node_package_that_appeared_still_denies_a_failing_test(tmp_path, capsys):
    root = node_project(tmp_path, "const toc = require('./mdtoc.js');\n"
                                  "test('fence', () => assert.deepStrictEqual(toc('```\\n# no\\n```'), []));\n")
    decision = answer(status(tmp_path, root, excludes=['.git', 'node_modules'], environment_keys={}), capsys)
    assert decision['decision'] == 'deny' and 'npm run test' in decision['reason']


def test_only_a_missing_third_party_module_in_a_stale_package_is_left_to_the_controller(tmp_path, capsys):
    root = node_project(tmp_path, "const request = require('supertest');\ntest('api', () => assert.ok(request));\n",
                        devDependencies={'supertest': '^7.0.0'})
    changes = {'excludes': ['.git', 'node_modules'], 'environment_keys': {}}
    assert answer(status(tmp_path, root, **changes), capsys)['decision'] == 'allow'
    events = [json.loads(line) for line in (tmp_path / 'telemetry.jsonl').read_text().splitlines()]
    assert 'npm run test (dependency environment changed)' in events[-1]['data']['reason']
    # The project's own missing file is the program's fault, stale cache or not.
    (root / 'mdtoc.test.js').write_text("const test = require('node:test');\nconst toc = require('./toc.js');\n"
                                        "test('toc', () => toc(''));\n")
    assert answer(status(tmp_path, root, **changes), capsys)['decision'] == 'deny'


def test_what_counts_as_a_stale_cache_and_a_missing_dependency(tmp_path):
    (tmp_path / 'app.py').write_text('')
    (tmp_path / 'web').mkdir()
    assert hook.missing_dependency('/p/.venv/bin/python: No module named pytest', tmp_path)
    assert hook.missing_dependency("ModuleNotFoundError: No module named 'requests'", tmp_path)
    assert not hook.missing_dependency("ModuleNotFoundError: No module named 'app.helpers'", tmp_path)   # own module
    assert hook.missing_dependency("Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'supertest' imported from /p/t.js", tmp_path)
    assert not hook.missing_dependency("Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/p/lib.js' imported from /p/t.js", tmp_path)
    assert not hook.missing_dependency("Error: Cannot find module './lib'", tmp_path)
    assert not hook.missing_dependency('AssertionError: 5 != 6', tmp_path)
    # The hook builds .NET with --no-restore: a project nobody restored is the environment wherever it is.
    assert hook.environment_failure(1, "error NETSDK1004: Assets file '/p/obj/project.assets.json' not found.")
    profiles = {'profiles': [{'cwd': '.', 'commands': {'setup': ['dotnet restore app.sln']}},
                             {'cwd': '.', 'commands': {'setup': ['cmake -S . -B build']}},
                             {'cwd': 'web', 'commands': {'setup': ['npm ci']}}]}
    assert set(hook.environment_keys({'root': str(tmp_path)}, profiles)) == {'web'}


def test_a_configured_setup_in_any_spelling_has_a_cache_that_can_go_stale(tmp_path, capsys):
    # Only workspace setups (cmake, dotnet) are exempt; 'npm i' used to miss the old list of installing spellings.
    setups = {'a': 'npm i', 'b': 'yarn', 'c': 'pnpm i', 'd': 'uv sync', 'e': 'bun install', 'f': 'cmake -S . -B out',
              'g': ' dotnet restore', 'h': 'python3 -m venv .venv'}
    profiles = {'profiles': [{'cwd': cwd, 'commands': {'setup': [setup]}} for cwd, setup in setups.items()]}
    for cwd in setups:
        (tmp_path / cwd).mkdir()
    assert set(hook.environment_keys({'root': str(tmp_path)}, profiles)) == {'a', 'b', 'c', 'd', 'e', 'h'}
    root = node_project(tmp_path, "test('ok', () => assert.ok(1));\n")
    (root / '.daedalus.json').write_text(json.dumps({'packages': {'.': {'setup': 'npm i'}}}))
    changes = {'excludes': ['.git', 'node_modules']}
    started = hook.environment_keys(json.loads(status(tmp_path, root, **changes).read_text()))
    assert set(started) == {'.'}
    # The agent adds supertest during the operation; the read-only node_modules from 'npm i' cannot get it.
    manifest = json.loads((root / 'package.json').read_text())
    (root / 'package.json').write_text(json.dumps({**manifest, 'devDependencies': {'supertest': '^7.0.0'}}))
    (root / 'mdtoc.test.js').write_text("const test = require('node:test');\nconst request = require('supertest');\n"
                                        "test('api', () => request);\n")
    decision, reason, _ = hook.decide(json.loads(status(tmp_path, root, environment_keys=started, **changes).read_text()), 120)
    assert decision == 'allow' and 'npm run test (dependency environment changed)' in reason


VITE_LIKE_BUILD = r'''const fs = require('fs');
for (const [, name] of fs.readFileSync('src/main.js', 'utf8').matchAll(/from "([^.][^"]*)"/g)) {
  try { require.resolve(name, {paths: [__dirname]}); }
  catch { console.error(`[vite]: Rollup failed to resolve import "${name}" from "src/main.js".`); process.exit(1); }
}
'''


def test_a_bundler_naming_a_declared_package_node_modules_lacks_is_left_to_the_controller(tmp_path, capsys):
    root = node_project(tmp_path, '', scripts={'build': 'node build.js'})
    (root / 'build.js').write_text(VITE_LIKE_BUILD)
    (root / 'src').mkdir()
    (root / 'src' / 'main.js').write_text('document.title = "board";\n')
    changes = {'excludes': ['.git', 'node_modules', 'src']}
    started = hook.environment_keys(json.loads(status(tmp_path, root, **changes).read_text()))
    assert answer(status(tmp_path, root, environment_keys=started, **changes), capsys)['decision'] == 'allow'
    # The agent declares and imports axios; Vite reports it in its own wording, not "Cannot find module".
    manifest = json.loads((root / 'package.json').read_text())
    (root / 'package.json').write_text(json.dumps({**manifest, 'dependencies': {'axios': '^1.7.0'}}))
    (root / 'src' / 'main.js').write_text('import axios from "axios";\naxios.get("/api/tasks");\n')
    decision, reason, _ = hook.decide(json.loads(status(tmp_path, root, environment_keys=started, **changes).read_text()), 120)
    assert decision == 'allow' and 'npm run build (dependency environment changed)' in reason
    # A misspelled import is not a package setup would install: the program's fault, stale cache or not.
    (root / 'src' / 'main.js').write_text('import axios from "axois";\naxios.get("/api/tasks");\n')
    decision = answer(status(tmp_path, root, environment_keys=started, **changes), capsys)
    assert decision['decision'] == 'deny' and 'resolve import "axois"' in decision['reason']


# Vite 6.4.3's real esbuild output for a syntax error one line below the new import: its frame quotes "axios".
ESBUILD_FRAME_BUILD = r'''console.error(`error during build:
[vite:esbuild] Transform failed with 1 error:
/p/src/App.jsx:3:12: ERROR: Unexpected ";"

Unexpected ";"
1  |  import { useEffect, useState } from "react";
2  |  import axios from "axios";
3  |  const API = ;
   |              ^`);
process.exit(1);
'''


def test_a_syntax_error_whose_source_excerpt_quotes_a_new_package_still_denies(tmp_path, capsys):
    root = node_project(tmp_path, "test('ok', () => assert.ok(1));\n", scripts={'test': 'node --test', 'build': 'node build.js'})
    (root / 'build.js').write_text('process.exit(0);\n')
    changes = {'excludes': ['.git', 'node_modules']}
    started = hook.environment_keys(json.loads(status(tmp_path, root, **changes).read_text()))
    # The agent declares packages the read-only node_modules lacks, then breaks the code right at their imports.
    manifest = json.loads((root / 'package.json').read_text())
    (root / 'package.json').write_text(json.dumps({**manifest, 'dependencies': {'axios': '^1.7.0', 'express': '^4.21.0'}}))
    (root / 'build.js').write_text(ESBUILD_FRAME_BUILD)
    decision = answer(status(tmp_path, root, environment_keys=started, **changes), capsys)
    assert decision['decision'] == 'deny' and 'npm run build' in decision['reason'] and 'Unexpected ";"' in decision['reason']
    # Node echoes the failing source line, quoting 'express', above the SyntaxError.
    (root / 'build.js').write_text('process.exit(0);\n')
    (root / 'mdtoc.test.js').write_text("const test = require('node:test');\nconst express = require('express';\n"
                                        "test('api', () => express);\n")
    decision = answer(status(tmp_path, root, environment_keys=started, **changes), capsys)
    assert decision['decision'] == 'deny' and 'npm run test' in decision['reason'] and 'SyntaxError' in decision['reason']
    assert "require('express';" in decision['reason']


def test_which_declared_node_packages_count_as_uninstalled(tmp_path):
    web = tmp_path / 'web'
    (web / 'node_modules' / '@dnd-kit' / 'core').mkdir(parents=True)
    (web / 'node_modules' / '@dnd-kit' / 'core' / 'package.json').write_text('{}')
    (tmp_path / 'node_modules' / 'react').mkdir(parents=True)
    (tmp_path / 'node_modules' / 'react' / 'package.json').write_text('{}')   # hoisted: Node resolves upward
    (tmp_path.parent / 'node_modules' / 'lodash').mkdir(parents=True, exist_ok=True)
    (tmp_path.parent / 'node_modules' / 'lodash' / 'package.json').write_text('{}')   # outside the project root
    (web / 'package.json').write_text(json.dumps({
        'dependencies': {'react': '^18', '@dnd-kit/core': '^6', 'axios': '^1', 'lodash': '^4', '../escape': '1'},
        'devDependencies': {'vitest': '^2', '@types/node': '^20'}, 'peerDependencies': [], 'optionalDependencies': None}))
    assert hook.uninstalled_dependencies(web, tmp_path) == {'axios', 'lodash', 'vitest', '@types/node'}
    for tail in ('[vite]: Rollup failed to resolve import "axios" from "/p/src/main.js".',
                 "Failed to resolve import \"axios/dist/node/axios.cjs\" from \"src/api.test.js\". Does the file exist?",
                 "✘ [ERROR] Could not resolve \"@types/node\"",
                 "Module not found: Error: Can't resolve 'vitest' in '/p/src'"):
        assert hook.names_uninstalled(tail, web, tmp_path), tail
    assert not hook.names_uninstalled('Failed to resolve import "react-router-dom" from "src/App.jsx".', web, tmp_path)
    assert not hook.names_uninstalled('Could not resolve "@dnd-kit/core"', web, tmp_path)   # installed
    assert not hook.names_uninstalled('expected "axios-mock" to equal "x"', web, tmp_path)
    # Only a resolver message counts; a code frame or an echoed source line quoting the import does not.
    assert not hook.names_uninstalled('Unexpected ";"\n2  |  import axios from "axios";\n3  |  const API = ;', web, tmp_path)
    assert not hook.names_uninstalled("const v = require('vitest';\nSyntaxError: missing ) after argument list", web, tmp_path)
    (web / 'package.json').write_text('{not json')
    assert hook.uninstalled_dependencies(web, tmp_path) == set()
