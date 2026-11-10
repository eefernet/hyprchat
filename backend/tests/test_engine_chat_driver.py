"""The composer driver may retry agent selection, never a submitted message."""
import sys
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'evals'))
import run_engine_chat as driver


class Locator:
    def __init__(self, page, name):
        self.page, self.name = page, name
    @property
    def first(self): return self
    def filter(self, **kwargs): return self
    def locator(self, selector): return self
    def get_by_role(self, role, *, name, **kwargs): return Locator(self.page, name)
    def is_visible(self):
        return self.page.returned_to_chat if self.name == 'textarea:visible' else self.page.active
    def count(self): return 0 if self.page.returned_to_chat else 1
    def wait_for(self):
        if self.name == '.hc-app-header .hc-chip' and not self.page.active:
            raise PlaywrightError('Wrong active persona')
    def click(self):
        self.page.clicks.append(self.name)
        if self.name == 'Agents': self.page.returned_to_chat = False
        if self.name == 'Use Agent':
            outcome = self.page.outcomes.pop(0)
            if outcome == 'returned-to-chat':
                self.page.returned_to_chat = True
                raise PlaywrightError('Timeout: agent view disappeared')
            if outcome == 'committed-detach':
                self.page.active = True
                raise PlaywrightError('Element was detached after selection')
            if outcome != 'ok':
                raise PlaywrightError(outcome)
            self.page.active = True


class Page:
    def __init__(self, outcomes):
        self.outcomes, self.clicks, self.active = list(outcomes), [], False
        self.returned_to_chat = False
    def get_by_role(self, role, *, name, **kwargs): return Locator(self, name)
    def get_by_text(self, text): return Locator(self, 'Daedalus')
    def locator(self, selector): return Locator(self, selector)


@pytest.mark.parametrize('outcomes,clicks', [(['ok'],1), (['Element was detached','ok'],2),
                                         (['committed-detach'],1), (['returned-to-chat','ok'],2)])
def test_pre_submit_detachment_recovers_without_new_chat_or_message(outcomes, clicks):
    page = Page(outcomes)
    driver.select_agent(page)
    assert page.active and page.clicks.count('Use Agent') == clicks
    assert page.clicks.count('New Chat') == 1 and 'Send message' not in page.clicks


@pytest.mark.parametrize('outcomes,clicks', [(['Timeout without detachment'],1), (['detached','detached'],2),
                                         (['returned-to-chat','returned-to-chat'],2)])
def test_selection_retries_are_bounded_and_specific(outcomes, clicks):
    page = Page(outcomes)
    with pytest.raises(PlaywrightError):
        driver.select_agent(page)
    assert page.clicks.count('Use Agent') == clicks


def test_uncertain_submission_reattaches_and_never_sends_again(monkeypatch):
    calls = []
    def api(page, path):
        calls.append(path)
        if path == '/api/conversations?limit=500': return [{'id':'saved'}]
        return {'messages':[{'id':42,'role':'user','content':'Build this.'}]}
    monkeypatch.setattr(driver, 'api', api)
    monkeypatch.setattr(driver, 'reopen', lambda page, cid: calls.append(('reopen',cid)))
    turn = {'intent':True,'existing_conversations':[]}
    driver.submit(object(), 'Build this.', turn, lambda: None)
    assert turn['submitted'] and turn['conversation_id']=='saved'
    assert calls[-1]==('reopen','saved')


def test_source_admission_failure_is_a_failed_workflow_not_model_drift():
    environment = {'model':'coder', 'model_digest':'frozen'}
    job = {'engine':'openhands159', 'model':'coder', 'model_digest':None,
           'state':'blocked', 'failure':{'stage':'queued'}, 'calls_used':0,
           'blocker':'503 Service Unavailable while copying source'}
    with pytest.raises(RuntimeError, match='Workflow stopped before coding: 503') as caught:
        driver.verify_coding_model(job, environment)
    assert not isinstance(caught.value, driver.CampaignInterrupted)
    # Any evidence of execution, a conflicting identity, or a candidate still
    # requires a verified digest. It cannot be counted as an ordinary failure.
    for changed in ({'calls_used':1}, {'operation_calls':1}, {'last_patch':{'agent_finished':False}},
                    {'candidate_artifact':{'id':'candidate'}}, {'model_digest':'different'},
                    {'model':'different'}, {'failure':{'stage':'coding'}}):
        with pytest.raises(driver.CampaignInterrupted):
            driver.verify_coding_model({**job, **changed}, environment)
    driver.verify_coding_model({**job, 'model_digest':'frozen'}, environment)
    with pytest.raises(RuntimeError) as source:
        driver.verify_coding_model({**job, 'failure':{'stage':'source_preparation'}}, environment)
    assert not isinstance(source.value, driver.CampaignInterrupted)


@pytest.mark.parametrize('details,interruption', [
    ({'category':'environment','stage':'source_preparation'},False),
    ({'category':'environment','stage':'checking','preparation':[{'category':'preparation_limit','retryable':False}]},False),
    ({'category':'environment','stage':'checking','preparation':[{'category':'preparation_limit','retryable':True}]},True),
    ({'category':'environment','stage':'code','preparation':{'category':'preparation_limit','phase':'setup','retryable':False,'attempt':1}},False),
    ({'category':'environment','stage':'code','preparation':{'category':'preparation_limit','retryable':True}},True),
    ({'category':'environment','preparation':{'category':'network','retryable':False}},True),
    ({'category':'environment','preparation':['preparation_limit']},True),
    ({'category':'environment','preparation':'preparation_limit'},True),
    ({'category':'environment','preparation':None},True),
    ({'category':'environment','stage':'checking'},True),
    ({'category':'network'},True),
])
def test_terminal_preparation_failure_is_a_failed_workflow(details, interruption):
    with pytest.raises(RuntimeError) as error:
        driver.archive_and_accept(None, {'state':'blocked','failure':details}, None,None,None,None,None,None)
    assert isinstance(error.value, driver.CampaignInterrupted) is interruption


@pytest.mark.parametrize('failure', ['TypeError: Failed to fetch', 'NetworkError: connection lost', 'TimeoutError: signal timed out'])
def test_read_only_poll_recovers_with_same_profile(monkeypatch, failure):
    from types import SimpleNamespace
    calls, receipts = [], []
    page = SimpleNamespace(url='http://candidate:8017/', _qualification_user='u',
                           wait_for_timeout=lambda ms: None, _record_recovery=receipts.append)
    def get(page, path):
        calls.append(path)
        if len(calls) == 1: raise PlaywrightError(failure)
        return {'user': {'id': 'u'}} if path == '/api/users/current' else {'id': 'job'}
    monkeypatch.setattr(driver, '_get', get)
    assert driver.api(page, '/api/coder/workflows/job') == {'id': 'job'}
    assert calls == ['/api/coder/workflows/job'] * 2 + ['/api/users/current']
    assert receipts[0]['attempts'] == 2


@pytest.mark.parametrize('error,count', [('Failed to fetch', 3), ('HTTP 403', 1), ('Unexpected token JSON', 1)])
def test_read_only_poll_never_hides_persistent_or_semantic_failures(monkeypatch, error, count):
    from types import SimpleNamespace
    calls = []
    page = SimpleNamespace(url='http://candidate:8017/', wait_for_timeout=lambda ms: None)
    def get(page, path):
        calls.append(path); raise PlaywrightError(error)
    monkeypatch.setattr(driver, '_get', get)
    with pytest.raises(driver.CampaignInterrupted): driver.api(page, '/api/conversations/c')
    assert len(calls) == count


def test_read_only_recovery_rejects_profile_change(monkeypatch):
    from types import SimpleNamespace
    page = SimpleNamespace(url='http://candidate:8017/', _qualification_user='first', wait_for_timeout=lambda ms: None)
    responses = iter([PlaywrightError('Failed to fetch'), {}, {'user': {'id': 'other'}}])
    def get(*args):
        result = next(responses)
        if isinstance(result, Exception): raise result
        return result
    monkeypatch.setattr(driver, '_get', get)
    with pytest.raises(driver.CampaignInterrupted, match='Profile identity'): driver.api(page, '/api/conversations/c')


@pytest.mark.parametrize('metadata', [{}, {'in_progress': True, 'delivery_status': 'completed'},
                                     {'in_progress': False, 'delivery_status': 'running'},
                                     {'in_progress': False, 'delivery_status': 'interrupted'}])
def test_no_job_prose_cannot_prove_dispatch_finished(metadata):
    user = {'id': 1, 'role': 'user'}
    assert driver.completed_reply({'messages': [user, {'id': 2, 'role': 'assistant',
        'content': 'Done!', 'metadata': metadata}]}, user) is None


def test_finished_chat_without_dispatch_retains_ids_and_fails_immediately(monkeypatch):
    from types import SimpleNamespace
    user = {'id': 1, 'role': 'user', 'content': 'Add Reset.'}
    conv = {'id': 'c', 'user_id': 'u', 'model_config_id': 'agent', 'messages': [user,
        {'id': 2, 'role': 'assistant', 'metadata': {'in_progress': False, 'delivery_status': 'completed'}}]}
    calls, saved = [], []
    def api(page, path):
        calls.append(path); return conv if path == '/api/conversations/c' else []
    monkeypatch.setattr(driver, 'api', api)
    page = SimpleNamespace(wait_for_timeout=lambda ms: pytest.fail('Should not wait ten minutes'))
    turn = {'conversation_id': 'c', 'prompt': 'Add Reset.'}
    with pytest.raises(RuntimeError, match='completed without starting'):
        driver.await_job(page, turn, set(), lambda: saved.append(dict(turn)), 1)
    assert turn['message_id'] == 1 and turn['response_id'] == 2
    assert calls.count('/api/coder/workflows?conversation_id=c') == 2 and saved


@pytest.mark.parametrize('problem', ['failed', 'incomplete', 'stuck', 'stale', 'supported_unverified', 'failed_criterion', None])
def test_private_pass_never_overrides_controller_acceptance_gate(tmp_path, monkeypatch, problem):
    import hashlib
    from types import SimpleNamespace
    archive = tmp_path/'job.tar.gz'; archive.write_bytes(b'previously downloaded exact archive')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    summary = {'checks_ok': True, 'revision_id': 'revision',
               'criteria': [{'supported': False, 'status': 'unverified'}]}
    job = {'id': 'job', 'project_id': 'project', 'revision_id': 'revision',
           'candidate_artifact': {'id': 'artifact', 'sha256': digest}, 'verification_summary': summary}
    if problem == 'failed': summary['checks_ok'] = False
    elif problem == 'incomplete': summary['incomplete_reason'] = 'Generation stopped: sdk_stuck'
    elif problem == 'stuck': job['last_patch'] = {'agent_finished': False, 'execution_status': 'conversationexecutionstatus.stuck'}
    elif problem == 'stale': summary['revision_id'] = 'old-revision'
    elif problem == 'supported_unverified': summary['criteria'] = [{'supported': True, 'status': 'unverified'}]
    elif problem == 'failed_criterion': summary['criteria'] = [{'supported': False, 'status': 'failed'}]
    else: job['last_patch'] = {'agent_finished': True, 'execution_status': 'checklist_ended'}
    locator = SimpleNamespace(count=lambda: 0); locator.first = locator
    page = SimpleNamespace(locator=lambda selector: locator)
    calls = []
    def api(page, path):
        calls.append(path)
        assert problem is None, 'A failed revision reached the acceptance API'
        return {'accepted': {'revision_id': 'revision'}}
    monkeypatch.setattr(driver, 'api', api)
    turn = {'prompt': 'Build this.', 'message_id': 1, 'workflow_ids': ['job'],
            'held_out': {'passed': True, 'checks': [{'passed': True}], 'archive_sha256': digest,
                         'judge_identity_sha256': 'judge'}}
    args = (page, job, turn, {}, 0, tmp_path/'report.json', None, lambda: None)
    if problem:
        with pytest.raises(RuntimeError, match='Controller verification'):
            driver.archive_and_accept(*args, judge_identity={'sha256': 'judge'})
        assert calls == [] and not job.get('exact_acceptance_verified')
    else:
        driver.archive_and_accept(*args, judge_identity={'sha256': 'judge'})
        assert len(calls) == 2 and job['exact_acceptance_verified']
    assert job['held_out']['passed']  # Retain measured behavior even when qualification fails.


def test_reconnect_does_not_follow_up_an_already_accepted_failed_controller_job(tmp_path, monkeypatch):
    import hashlib
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace
    case = {'id': 'case', 'family': 'core', 'task': 'Build this.', 'followup': 'Change this.'}
    env = {'url': 'http://candidate:8017', 'isolated_candidate': True,
           'model': 'coder', 'model_digest': 'frozen', 'job_seconds': 1}
    environment = tmp_path/'environment.json'; environment.write_text(json.dumps(env))
    frozen = {'cohort': 'development', 'prompt_bank_version': driver.PROMPT_BANK_VERSION,
              'environment': env, 'bank_sha256': hashlib.sha256(json.dumps([case], sort_keys=True).encode()).hexdigest(),
              'runner_sha256': hashlib.sha256(Path(driver.__file__).read_bytes()).hexdigest()}
    report = {'freeze': frozen, 'trials': [{'key': 'case-1', 'case': 'case', 'repeat': 1, 'family': 'core',
        'passed': False, 'turns': [{'submitted': True, 'conversation_id': 'conversation'}],
        'jobs': [{'id': 'job', 'revision_id': 'revision', 'exact_acceptance_verified': True,
                  'verification_summary': {'revision_id': 'revision', 'checks_ok': False,
                                           'incomplete_reason': 'Generation stopped: sdk_stuck'}}]}]}
    (tmp_path/'report.json').write_text(json.dumps(report))
    button = SimpleNamespace(click=lambda: None, wait_for=lambda **kwargs: None)
    page = SimpleNamespace(set_default_timeout=lambda n: None, goto=lambda *a, **kw: None,
                           get_by_role=lambda *a, **kw: button)
    browser = SimpleNamespace(new_page=lambda **kw: page, close=lambda: None)
    @contextmanager
    def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda: browser))
    monkeypatch.setattr(driver, 'sync_playwright', playwright)
    monkeypatch.setattr(driver, 'BANKS', {'development': [case]})
    monkeypatch.setattr(driver, 'verify_judge', lambda *a: {})
    monkeypatch.setattr(driver, 'api', lambda *a: {'user': {'id': 'owner'}})
    monkeypatch.setattr(driver, 'reopen', lambda *a: None)
    monkeypatch.setattr(driver, 'submit', lambda *a: pytest.fail('Failed baseline must not receive a follow-up'))
    driver.run(SimpleNamespace(environment=environment, output=tmp_path, cohort='development',
                               slot='case-1', resume_driver=None))
    result = json.loads((tmp_path/'report.json').read_text())['trials'][0]
    assert result['finished'] and not result['passed'] and len(result['turns']) == 1
    assert 'Controller verification' in result['error']
