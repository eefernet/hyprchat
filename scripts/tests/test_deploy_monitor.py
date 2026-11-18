"""deploy_monitor.py: what is watched, how a batch is split, and how a deploy
is checked and undone. ssh/scp are stubbed; nothing leaves this machine."""
import importlib.util
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('deploy_monitor_under_test', REPO / 'deploy_monitor.py')
dm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dm)

RELEASE_WORKDIR = '/opt/hyprchat/.releases/20261117-test/backend'
RELEASE_ROOT = RELEASE_WORKDIR[:-len('/backend')]


# ── the real repository ──

def _repo_files(pattern):
    return {p.relative_to(REPO).as_posix() for p in REPO.glob(pattern) if p.is_file()}


def test_every_runtime_backend_file_is_watched():
    dm.refresh_watch()
    expected = _repo_files('backend/*.py') | _repo_files('backend/*.json')
    for sub in ('agents', 'routes', 'db', 'tooling', 'seed_kb'):
        expected |= _repo_files(f'backend/{sub}/*.py')
    expected |= {'backend/requirements.txt', 'backend/requirements.lock.txt'}
    missing = expected - set(dm.WATCHED)
    assert not missing, missing
    # the modules the audit found unwatched
    for path in ('backend/image_settings.py', 'backend/reasoning.py', 'backend/workspace_service.py',
                 'backend/routes/workspaces.py', 'backend/routes/coder_preview.py',
                 'backend/agents/coder_dispatch.py'):
        assert dm.WATCHED[path][2] is True, path
    assert dm.WATCHED['backend/seed_kb/seed_coder_kb.py'][2] is False
    assert not [p for p in dm.WATCHED if p.startswith(('backend/tests/', 'backend/evals/'))]
    assert 'backend/hyprchat.service' not in dm.WATCHED


def test_every_frontend_source_is_watched():
    dm.refresh_watch()
    expected = set()
    for base in ('frontend/src', 'frontend/public'):
        expected |= {p for p in _repo_files(base + '/**/*')
                     if '.test.' not in p and '.fixtures.' not in p and not Path(p).name.startswith('.')}
    expected |= {'frontend/index.html', 'frontend/vite.config.js', 'frontend/package.json', 'frontend/package-lock.json'}
    assert expected == dm.FRONTEND_SRC_FILES
    for path in ('frontend/src/storageSummary.js', 'frontend/src/components/SidePane.jsx', 'frontend/src/chatDrafts.js'):
        assert dm.WATCHED[path][0] == 'Frontend (build)'


def test_release_only_files_split_by_backend_imports():
    dm.refresh_watch()
    vm_only, used = dm.release_split()
    assert vm_only | used == dm.release_only_files()
    assert not vm_only & used
    for path in ('backend/context_policy.py', 'backend/document_formats.py',
                 'backend/hephaestus_scope.py', 'backend/coder_naming.py', 'backend/coder_progress.py'):
        assert path in used, path
    for path in ('backend/hephaestus_api.py', 'backend/coder_engine_worker.py', 'backend/coder_preview.py',
                 'backend/document_worker.py', 'backend/engine-runtime.lock.txt'):
        assert path in vm_only, path
    assert dm.WATCHED['backend/hephaestus_api.py'][1] == dm.REMOTE_HEPHAESTUS
    # coder_jobs/coder_presentation are ordinary backend modules
    assert 'backend/coder_jobs.py' not in dm.release_only_files()


def test_import_needs_no_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dm.refresh_watch()
    assert 'backend/main.py' in dm.WATCHED and dm.WORKER_FILES


# ── a small fake repository ──

FILES = {
    'backend/main.py': 'import config\nfrom routes import health\n\ndef later():\n    import hephaestus_scope\n',
    'backend/config.py': 'import context_policy\nimport importlib\nfmt = importlib.import_module("document_formats")\n',
    'backend/context_policy.py': '',
    'backend/document_formats.py': '',
    'backend/hephaestus_scope.py': '',
    'backend/hephaestus_api.py': 'import hephaestus_scope\n',
    'backend/coder_engine_worker.py': '',
    'backend/coder_naming.py': '',
    'backend/coder_jobs.py': '',
    'backend/document_worker.py': '',
    'backend/plain.py': 'X = 1\n',
    'backend/route_policy.json': '{}',
    'backend/requirements.txt': 'fastapi\n',
    'backend/engine-requirements.lock.txt': 'x\n',
    'backend/hyprchat.service': '[Unit]\n',
    'backend/routes/__init__.py': '',
    'backend/routes/health.py': 'from . import helpers\nimport coder_naming\n',
    'backend/routes/helpers.py': '',
    'backend/agents/__init__.py': '',
    'backend/agents/chat.py': '',
    'backend/seed_kb/seeder.py': '',
    'backend/tests/test_x.py': '',
    'backend/evals/run.py': '',
    'frontend/src/main.jsx': 'x\n',
    'frontend/src/a.test.js': '',
    'frontend/src/p.fixtures.json': '{}',
    'frontend/src/components/Pane.jsx': 'x\n',
    'frontend/public/sw.js': '',
    'frontend/index.html': '<html></html>',
    'frontend/package.json': '{}',
    'CHANGELOG.md': '# log\n',
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for rel, text in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.setattr(dm, 'ROOT', tmp_path)
    monkeypatch.setattr('builtins.input', lambda *_a, **_k: '')
    monkeypatch.setattr(dm, 'clear', lambda: None)
    dm.HELD.clear()
    dm._LIVE_ROOT.update(root=dm.HYPRCHAT_ROOT, release=False, checked=False)
    dm.refresh_watch()
    yield tmp_path
    dm.HELD.clear()
    monkeypatch.undo()
    dm.refresh_watch()


def edit(root, rel, text):
    path = root / rel
    path.write_text(text)
    later = os.path.getmtime(path) + 5
    os.utime(path, (later, later))


class Remote:
    """Stands in for ssh/scp. `healthy` is a list of answers to successive health polls."""

    def __init__(self, workdir=RELEASE_WORKDIR, healthy=(True,), scp_ok=True):
        self.workdir, self.healthy, self.scp_ok = workdir, list(healthy), scp_ok
        self.commands, self.copies, self.builds = [], [], 0

    def ssh(self, host, user, password, command, timeout=30, attempts=3):
        self.commands.append(command)
        if 'WorkingDirectory' in command:
            return True, self.workdir, ''
        if dm.HEALTH_PATH in command:
            ok = self.healthy.pop(0) if self.healthy else True
            return ok, 'healthy' if ok else 'no answer', ''
        return True, '', ''

    def scp(self, local, host, remote_path, user, password):
        self.copies.append((local, remote_path))
        return self.scp_ok, '' if self.scp_ok else 'copy failed'

    def build(self, target, root=dm.HYPRCHAT_ROOT):
        self.builds += 1
        return True, ''

    def install(self, monkeypatch):
        monkeypatch.setattr(dm, 'ssh_cmd', self.ssh)
        monkeypatch.setattr(dm, 'scp', self.scp)
        monkeypatch.setattr(dm, '_build_and_deploy_frontend', self.build)
        return self

    def sent(self):
        return {Path(local).relative_to(dm.ROOT).as_posix() for local, _r in self.copies}

    def matching(self, word):
        return [c for c in self.commands if word in c]


CFG = {'hyprchat': {'ip': 'h', 'user': 'root', 'pass': ''}, 'codebox': {'ip': 'c', 'user': 'root', 'pass': ''}}


def batch(*paths):
    return [(p, dm.WATCHED[p]) for p in paths]


def test_fake_repo_watch_rules(repo):
    w = dm.WATCHED
    assert w['backend/plain.py'] == ('Backend', dm.REMOTE_BACKEND, True)
    assert w['backend/routes/helpers.py'][1:] == (dm.REMOTE_BACKEND + 'routes/', True)
    assert w['backend/seed_kb/seeder.py'][2] is False
    assert w['backend/route_policy.json'][2] is True
    assert 'backend/hyprchat.service' not in w
    assert not [p for p in w if '/tests/' in p or '/evals/' in p or '.test.' in p or '.fixtures.' in p]
    assert {'frontend/src/main.jsx', 'frontend/src/components/Pane.jsx', 'frontend/public/sw.js',
            'frontend/index.html', 'frontend/package.json'} <= dm.FRONTEND_SRC_FILES
    vm_only, used = dm.release_split()
    # function-level import, importlib.import_module, and an import from a route
    assert used == {'backend/context_policy.py', 'backend/document_formats.py',
                    'backend/hephaestus_scope.py', 'backend/coder_naming.py'}
    assert vm_only == {'backend/hephaestus_api.py', 'backend/coder_engine_worker.py',
                       'backend/document_worker.py', 'backend/engine-requirements.lock.txt'}


def test_classify_splits_vm_only_and_holds_backend(repo):
    plan = dm.classify_batch(['backend/hephaestus_api.py', 'backend/plain.py', 'frontend/src/main.jsx'])
    assert plan['vm_only'] == ['backend/hephaestus_api.py']
    assert plan['deploy'] == ['backend/plain.py', 'frontend/src/main.jsx']
    assert plan['hold'] == []
    plan = dm.classify_batch(['backend/context_policy.py', 'backend/plain.py', 'frontend/src/main.jsx', 'CHANGELOG.md'])
    assert plan['hold'] == ['backend/context_policy.py', 'backend/plain.py']
    assert plan['deploy'] == ['frontend/src/main.jsx', 'CHANGELOG.md']
    assert dm.classify_batch(['backend/requirements.txt', 'backend/plain.py'])['deploy'] == []
    assert dm.classify_batch(['backend/plain.py'], holding=True)['hold'] == ['backend/plain.py']


def test_vm_only_file_is_left_out_and_the_rest_deploys(repo, monkeypatch):
    remote = Remote().install(monkeypatch)
    pushed = dm.deploy_changes(batch('backend/hephaestus_api.py', 'backend/plain.py'), CFG)
    assert remote.sent() == {'backend/plain.py'}
    assert set(pushed) == {'backend/plain.py'}
    assert remote.copies[0][1] == RELEASE_ROOT + '/backend/plain.py.deploy-tmp'
    assert remote.matching(dm.HEALTH_PATH)


def test_backend_imported_release_file_holds_backend_but_frontend_goes(repo, monkeypatch):
    remote = Remote().install(monkeypatch)
    pending = batch('backend/context_policy.py', 'backend/plain.py', 'frontend/src/main.jsx')
    plan = dm.classify_batch([p for p, _ in pending])
    pushed = dm.deploy_changes(pending, CFG, plan=plan)
    assert remote.copies == [] and remote.builds == 1
    assert set(pushed) == {'frontend/src/main.jsx'}
    assert not remote.matching('systemctl restart')

    prev, base, states = {}, {p: 'old' for p, _ in pending}, {}
    dm.apply_outcome(plan, pushed, prev, base, states)
    assert set(dm.HELD) == {'backend/context_policy.py', 'backend/plain.py'}
    assert base['backend/plain.py'] == 'old' and base['backend/context_policy.py'] == 'old'
    assert states['backend/plain.py'] == 'held' and states['frontend/src/main.jsx'] == 'deployed'


def test_held_files_do_not_prompt_again_and_later_backend_edits_join_the_hold(repo):
    prev = {p: dm._mtime(p) for p in dm.WATCHED}
    base = {p: dm.file_digest(p) for p in dm.WATCHED}
    states = {}
    edit(repo, 'backend/context_policy.py', 'NEW = 1\n')
    pending = []
    dm.scan_for_changes(prev, base, states, pending)
    plan = dm.classify_batch([p for p, _ in pending])
    dm.apply_outcome(plan, {}, prev, base, states)
    assert dm.blocking_holds() == ["backend/context_policy.py"]

    # nothing changed: nothing offered, even with the cheap filter reset
    prev['backend/context_policy.py'] = 0
    pending = []
    assert dm.scan_for_changes(prev, base, states, pending) == [] and pending == []

    # another backend edit joins the hold; a frontend edit is offered
    edit(repo, 'backend/plain.py', 'X = 2\n')
    edit(repo, 'frontend/src/main.jsx', 'y\n')
    pending = []
    dm.scan_for_changes(prev, base, states, pending)
    assert [p for p, _ in pending] == ['frontend/src/main.jsx']
    assert set(dm.HELD) == {'backend/context_policy.py', 'backend/plain.py'}

    # reverting the blocker releases the others as ordinary changes
    edit(repo, 'backend/context_policy.py', '')
    pending = []
    dm.scan_for_changes(prev, base, states, pending)
    assert [p for p, _ in pending] == ['backend/plain.py']
    assert dm.HELD == {}


def test_clear_held_rebaselines(repo):
    base = {'backend/plain.py': 'old'}
    dm.HELD['backend/plain.py'] = 'x'
    assert dm.clear_held({}, base, {}) == 1
    assert base['backend/plain.py'] == dm.file_digest('backend/plain.py') and dm.HELD == {}


def test_new_file_is_picked_up_by_the_reglob(repo):
    prev = {p: dm._mtime(p) for p in dm.WATCHED}
    base = {p: dm.file_digest(p) for p in dm.WATCHED}
    (repo / 'backend/routes/fresh.py').write_text('')
    (repo / 'frontend/src/fresh.js').write_text('z\n')
    (repo / 'frontend/src/fresh.test.js').write_text('')
    pending = []
    new = dm.scan_for_changes(prev, base, {}, pending)
    assert set(new) == {'backend/routes/fresh.py', 'frontend/src/fresh.js'}
    assert dm.WATCHED['frontend/src/fresh.js'][0] == 'Frontend (build)'


def test_unhealthy_restart_restores_previous_files(repo, monkeypatch):
    remote = Remote(healthy=[False, True]).install(monkeypatch)
    pushed = dm.deploy_changes(batch('backend/plain.py'), CFG)
    assert pushed == {}
    activate = remote.matching('cp -p')
    restore = remote.matching('else rm -f')
    assert activate and restore
    assert remote.commands.index(restore[0]) > remote.commands.index(activate[0])
    assert '.deploy-prev' in restore[0]
    assert len(remote.matching('systemctl restart')) == 2  # deploy, then after the restore
    plan = dm.classify_batch(['backend/plain.py'])
    states, prev = {}, {}
    dm.apply_outcome(plan, pushed, prev, {}, states)
    assert states['backend/plain.py'] == 'changed' and prev['backend/plain.py'] == 0


def test_healthy_restart_drops_previous_copies(repo, monkeypatch):
    remote = Remote(healthy=[True]).install(monkeypatch)
    pushed = dm.deploy_changes(batch('backend/plain.py', 'backend/seed_kb/seeder.py'), CFG)
    assert set(pushed) == {'backend/plain.py', 'backend/seed_kb/seeder.py'}
    assert not remote.matching('else rm -f')
    assert [c for c in remote.commands if c.startswith('rm -f') and '.deploy-prev' in c]


def test_health_poll_reads_the_env_address_and_waits():
    cmd = dm._health_poll_cmd()
    assert dm.HEALTH_PATH in cmd and '/opt/hyprchat/.env' in cmd
    assert '0.0.0.0' in cmd and '+ 30' in cmd


def test_missing_venv_means_no_install_and_no_bootstrap(repo, monkeypatch):
    remote = Remote().install(monkeypatch)
    ready_cmds = []

    def ssh(host, user, password, command, timeout=30, attempts=3):
        if 'venv/bin/python' in command:
            ready_cmds.append(command)
            return False, '', 'no venv'
        return remote.ssh(host, user, password, command, timeout, attempts)
    monkeypatch.setattr(dm, 'ssh_cmd', ssh)
    assert dm.deploy_changes(batch('backend/plain.py'), CFG) == {}
    assert remote.copies == [] and ready_cmds
    assert RELEASE_ROOT + '/venv/bin/python' in ready_cmds[0]
    assert 'pip' not in ready_cmds[0]
    assert not hasattr(dm, '_bootstrap_hyprchat_host')


def test_push_all_needs_a_staged_release(repo, monkeypatch):
    remote = Remote(workdir='/opt/hyprchat/backend').install(monkeypatch)
    states = {}
    assert dm.push_all(CFG, states) == {}
    assert remote.copies == [] and remote.builds == 0 and states == {}


def test_push_all_sends_only_hot_patchable_files_and_marks_only_those(repo, monkeypatch):
    remote = Remote().install(monkeypatch)
    states = {}
    pushed = dm.push_all(CFG, states)
    sent = remote.sent()
    assert not sent & dm.release_only_files()
    assert 'backend/requirements.txt' not in sent
    assert {'backend/plain.py', 'backend/main.py', 'CHANGELOG.md'} <= sent
    assert remote.builds == 1
    assert set(pushed) == {p for p, s in states.items() if s == 'deployed'}
    assert 'backend/context_policy.py' not in states and 'backend/hephaestus_api.py' not in states


def test_push_all_marks_nothing_deployed_when_staging_fails(repo, monkeypatch):
    Remote(scp_ok=False).install(monkeypatch)
    states = {}
    dm.push_all(CFG, states)
    backend = {p: s for p, s in states.items() if p.startswith('backend/')}
    assert backend and set(backend.values()) == {'failed'}
