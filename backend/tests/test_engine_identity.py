"""Actual transport preflight must reject stale identity before a user submission."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'evals'))
import engine_identity
import run_engine_chat as driver


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    root = tmp_path / 'release'
    files = ['backend/evals/engine_judge.py', 'backend/evals/engine_cases_everyday.py',
             'backend/coder_project_runtime.py', 'scripts/hephaestus/qualify_languages.py']
    hashes = {}
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# frozen fixture\n')
        hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    (root / 'qualification-files.json').write_text(json.dumps(hashes, sort_keys=True))
    runtime = tmp_path / 'runtime.json'
    profiles = {'core':'sha256:core', 'languages':'sha256:languages'}
    runtime.write_text(json.dumps({'profiles':{k:{'image':v} for k,v in profiles.items()}}))
    monkeypatch.setattr(engine_identity.importlib.metadata, 'version', lambda _: '1.59.0')
    settings = {'daedalus_model_calls':120}
    value = engine_identity.receipt(settings, root=root, runtime_path=runtime)
    adapter = tmp_path / 'adapter.py'
    adapter.write_text('import json,sys\nassert json.load(sys.stdin)=={"kind":"identity"}\nprint(' + repr(json.dumps(value)) + ')\n')
    environment = dict(judge_identity=value, settings=settings, profiles=profiles, sdk_version='1.59.0',
                       judge_sha256=value['judge_sha256'], judge_bank_sha256=value['judge_bank_sha256'],
                       prompt_bank_version=2, judge_command=[sys.executable,str(adapter)],
                       judge_adapter_path=str(adapter), judge_adapter_sha256=hashlib.sha256(adapter.read_bytes()).hexdigest())
    return root, runtime, environment


def test_identity_roundtrip_uses_actual_transport_and_manifest(frozen):
    _, _, env = frozen
    assert driver.verify_judge(env, 'development', env['judge_identity']['banks']['development']) == env['judge_identity']


@pytest.mark.parametrize('field', ['judge_sha256', 'judge_bank_sha256', 'sdk_version', 'profiles', 'settings', 'prompt_bank_version'])
def test_stale_environment_fails_before_browser_or_prompt(frozen, tmp_path, monkeypatch, field):
    _, _, env = frozen
    env = deepcopy(env)
    env[field] = {'wrong':1} if isinstance(env[field], dict) else 'stale'
    env.update(isolated_candidate=True, url='http://127.0.0.1:8017')
    path = tmp_path / 'environment.json'
    path.write_text(json.dumps(env))
    def forbidden():
        pytest.fail('Browser must not open before judge identity matches')
    monkeypatch.setattr(driver, 'sync_playwright', forbidden)
    with pytest.raises(ValueError, match='identity|bank|settings'):
        driver.run(SimpleNamespace(environment=path, output=tmp_path/'run', cohort='development', resume_driver=None, slot=None))


def test_mutated_verification_dependency_and_adapter_are_rejected(frozen):
    root, runtime, env = frozen
    (root/'backend/coder_project_runtime.py').write_text('# changed verifier\n')
    with pytest.raises(ValueError, match='immutable manifest'):
        engine_identity.receipt(env['settings'], root=root, runtime_path=runtime)
    Path(env['judge_adapter_path']).write_text('raise RuntimeError("must not execute")')
    with pytest.raises(ValueError, match='transport adapter'):
        driver.verify_judge(env, 'development', env['judge_identity']['banks']['development'])


def test_wrong_self_digest_or_bank_is_not_a_receipt(frozen):
    _, _, env = frozen
    value = deepcopy(env['judge_identity'])
    value['components']['backend/coder_project_runtime.py'] = 'wrong'
    with pytest.raises(ValueError, match='digest'):
        engine_identity.validate(value, env, 'development', env['judge_identity']['banks']['development'])
    with pytest.raises(ValueError, match='bank'):
        engine_identity.validate(env['judge_identity'], env, 'development', 'wrong-bank')


def test_paired_run_rejects_different_verifier_components_before_dispatch(frozen, tmp_path, monkeypatch):
    import run_engine_paired
    _, _, env = frozen
    before = {**env, 'model':'coder','model_digest':'coder-digest','chat_model':'chat',
              'chat_digest':'chat-digest','limits':{'memory':3},'cache_policy':'private',
              'url':'http://127.0.0.1:8018','release_id':'previous'}
    after = deepcopy(before)
    after.update(url='http://127.0.0.1:8017',release_id='candidate')
    after['judge_identity']['components']['backend/coder_project_runtime.py'] = 'different-verifier'
    paths = [tmp_path/'previous.json',tmp_path/'candidate.json']
    for path, value in zip(paths, (before, after)):
        path.write_text(json.dumps(value))
    monkeypatch.setattr(sys, 'argv', ['paired','--previous',str(paths[0]),'--candidate',str(paths[1]),'--output',str(tmp_path/'paired')])
    def forbidden(*args, **kwargs):
        pytest.fail('No user prompt may dispatch with mismatched private judges')
    monkeypatch.setattr(run_engine_paired.subprocess, 'run', forbidden)
    with pytest.raises(ValueError, match='different or missing'):
        run_engine_paired.main()
