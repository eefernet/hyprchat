import pytest

from evals.legacy_runner import dispatch_with_recovery


@pytest.mark.asyncio
@pytest.mark.parametrize('tool,next_tool',[
    ('run_aider_fix','deep_research'),
    ('run_review','generate_code'),
])
async def test_legacy_evaluation_follows_explicit_recovery(tool,next_tool):
    calls=[]
    async def dispatch(name,args):
        calls.append((name,args))
        if len(calls)==1:
            return f'BLOCKED — recover first\nYour VERY NEXT tool call MUST be:\n  {next_tool}()',[]
        return 'Completed',[{'id':str(len(calls))}]
    result=await dispatch_with_recovery(dispatch,tool,{'project_dir':'/root/projects/eval-test'},'Add counts','eval-test')
    assert [name for name,_ in calls]==[tool,next_tool,tool]
    assert calls[0][1]==calls[2][1]
    assert result==[{'id':'3'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('message,expected_calls',[
    ('BLOCKED — hard ceiling reached; do not call deep_research',1),
    ('BLOCKED — environment fault',1),
    ('ERROR: worker unavailable',1),
    ('BLOCKED — Your VERY NEXT tool call MUST be:\n deep_research()',3),
])
async def test_legacy_evaluation_does_not_bypass_terminal_or_repeated_gates(message,expected_calls):
    calls=[]
    async def dispatch(name,args):
        calls.append(name)
        return ('Research completed' if name=='deep_research' else message),[]
    with pytest.raises(RuntimeError):
        await dispatch_with_recovery(dispatch,'run_aider_fix',{},'Repair','eval-test')
    assert len(calls)==expected_calls
