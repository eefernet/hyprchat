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

from coder_workflow_guard import workflow_steps, workflow_rows


class FakeStore:
    def __init__(self, root):
        self.root = Path(root); self.events = []
    def get(self, op_id):
        return {'payload': {'settings': {'daedalus_command_seconds': 60}}}
    def event(self, op_id, kind, **data):
        self.events.append((kind, data))


# --- request probes -------------------------------------------------------------------------


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


def test_sweep3_medium_server_log_is_runtime_state_not_source(tmp_path):
    from coder_project_runtime import runtime_data_path
    (tmp_path / 'server.log').write_text('listening'); (tmp_path / 'server.js').write_text('x')
    (tmp_path / 'store.bin').write_bytes(b'SQLite format 3\x00' + b'\0' * 32)
    assert runtime_data_path(tmp_path, 'server.log') and runtime_data_path(tmp_path, 'data/tasks.db') and runtime_data_path(tmp_path, 'store.bin')
    assert not runtime_data_path(tmp_path, 'server.js') and not runtime_data_path(tmp_path, 'public/app.js') and not runtime_data_path(tmp_path, 'nope.txt')


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
    down = workflow_rows({'id': 'app'}, 'http://x', {}, task, prober=lambda *a, **k: {'fault': 'Error: browser missing', 'steps': {}, 'writes': [], 'errors': []})
    assert all(r['environment_fault'] and r['classification'] == 'environment' and not r['passed'] for r in down)


