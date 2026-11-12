"""Private user-prompt acceptance checks keep actions and expectations separate."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parents[1] / 'evals'))
from engine_cases import DEVELOPMENT
from engine_judge import ui_checks


def test_versioned_prompts_expose_exact_format_without_changing_private_inputs():
    from engine_cases import DEVELOPMENT_V1, CASES, CASES_V1, PROMPT_BANK_VERSION
    assert PROMPT_BANK_VERSION == 2
    for old, new in zip(DEVELOPMENT_V1 + CASES_V1, DEVELOPMENT + CASES):
        assert {k:v for k,v in old.items() if k != 'task'} == {k:v for k,v in new.items() if k != 'task'}
    old = next(c for c in DEVELOPMENT_V1 if c['id'] == 'dev-qt')
    new = next(c for c in DEVELOPMENT if c['id'] == 'dev-qt')
    assert 'without units or a prefix' in new['task'] and 'without units or a prefix' not in old['task']
    assert '77.0' not in new['task'] and '25' not in new['task']


def test_reset_is_clicked_once_and_both_field_values_are_observed():
    case = next(c for c in DEVELOPMENT if c['id']=='dev-web')
    state = {}
    resets = []
    def action(request):
        name = request['action']
        if name == 'fill':
            state[request['selector']] = request['value']
        if name == 'click':
            if request['selector'] == '#reset':
                resets.append(True)
                state.update({'#amount':'','#percent':'','#total':''})
            elif float(state['#amount']) < 0:
                state['#total'] = 'Error'
            else:
                state['#total'] = f"{float(state['#amount'])*(1+float(state['#percent'])/100):.2f}"
        if name in ('value','text'):
            return {name:state[request['selector']]}
        assert not ({'equals','feedback_pattern','contains'} & request.keys())
        return {'accessibility':state.get('#total',''), 'nodes':[
            {'tag':'button','showing':True,'activatable':True,'selector':'#calculate','name':'Calculate total','text':'Calculate'},
            {'tag':'button','showing':True,'activatable':True,'selector':'#reset','name':'Reset','text':'Reset'}]}
    rows = ui_checks(action,case,True)
    assert all(row['passed'] for row in rows)
    assert [r['actual'] for r in rows if r['request']['action']=='value'] == ['20','10','','']
    assert resets == [True]


def test_public_prompt_sets_have_frozen_distinct_slots():
    from collections import Counter
    from engine_cases import CASES
    from engine_cases_everyday import CASES as everyday, POCKET, breadth_cases
    assert Counter(c['family'] for c in everyday) == {'core':8,'web':4,'desktop':4}
    assert {c['id'] for c in everyday}.isdisjoint(c['id'] for c in CASES)
    assert len({c['id'] for c in breadth_cases()}) == 29
    assert len(POCKET) == 1 and '“Pocket Tasks,”' in POCKET[0]['task']
    assert all(c['task'] and c['followup'] for c in everyday+POCKET+breadth_cases())


def test_visible_button_and_accessible_alias_must_still_identify_one_control():
    import pytest
    from engine_judge import named_button, UIObservationFailure
    visible={'role':'button','showing':True,'activatable':True,'text':'Calculate','name':'Calculate total','selector':'#calculate'}
    accessible={**visible,'text':'Other','name':'Calculate','selector':'#another'}
    request={'action':'click','role':'button','name':'Calculate'}
    assert named_button(lambda _: {'nodes':[visible]},request)['selector']=='#calculate'
    with pytest.raises(UIObservationFailure,match='found 2'):
        named_button(lambda _: {'nodes':[visible,accessible]},request)


def test_marked_private_judge_reclaims_only_reconstructible_caches(tmp_path, monkeypatch):
    import hashlib
    import tarfile
    import config
    import engine_judge
    from context_policy import DEFAULTS
    from coder_worker_runtime import WorkerStore
    monkeypatch.setenv('HEPHAESTUS_STORE_ROLE','test')
    monkeypatch.setattr(config,'CONTEXT_SETTINGS',dict(DEFAULTS))
    monkeypatch.setattr(engine_judge,'cli_checks',lambda *args: [{'id':'control','passed':True}])
    source=tmp_path/'app.py'
    source.write_text('print(1)\n')
    archive=tmp_path/'candidate.tar.gz'
    with tarfile.open(archive,'w:gz') as bundle:
        bundle.add(source,arcname=source.name)
    job={'id':'test-job','project_id':'project','candidate_artifact':{
        'storage_path':str(archive),'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}}
    store=WorkerStore(tmp_path/'evaluator-worker')
    old=store.root/'project-environments'/'old'
    old.mkdir(parents=True)
    (old/'wheel').write_bytes(b'cache')
    log=store.root/'evidence.log'
    log.write_text('keep')
    assert engine_judge.evaluate(tmp_path,job,{'family':'core'})['passed'] and old.exists()
    (tmp_path/'qualification-judge').touch()
    assert engine_judge.evaluate(tmp_path,job,{'family':'core'})['passed']
    assert not old.exists() and log.read_text()=='keep' and archive.is_file()
