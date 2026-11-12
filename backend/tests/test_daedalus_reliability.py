import copy
import json
import subprocess
import sys

import pytest

from coder_failures import classify_failure,malformed_native_call
from context_policy import DEFAULTS,validate_patch,resolve,operation_settings,thinking_options


def test_stage_output_policy_preserves_old_jobs():
    patch=validate_patch({'daedalus_role_outputs':{'qa':16384},'daedalus_role_thinking':{'qa':'on'}},DEFAULTS)
    payload={'settings':{**DEFAULTS,**patch},'policy_version':3}
    assert resolve('qa',operation_settings(payload)).num_predict==16384
    assert resolve('qa',operation_settings({**payload,'policy_version':2})).num_predict==4096
    assert thinking_options('qa',payload,{'capabilities':[]})==({},'unsupported')
    assert thinking_options('qa',payload,{'capabilities':['thinking']})[0]=={'think':True}
    # Live laguna switch (2026-09-30): a configured LEVEL reaches a model whose /api/show advertises boolean-only
    # thinking values; send `true`, not a string Ollama rejects. Models that advertise levels keep the level.
    level={'settings':{**DEFAULTS,**validate_patch({'daedalus_role_thinking':{'builder':'low'}},DEFAULTS)},'policy_version':7}
    assert thinking_options('builder',level,{'capabilities':['thinking'],'thinking':{'values':[False,True],'default':True}})==({'think':True},'on')
    assert thinking_options('builder',level,{'capabilities':['thinking'],'thinking':{'values':[False,'low','medium','xhigh'],'default':'medium'}})==({'think':'low'},'low')
    assert thinking_options('builder',level,{'capabilities':['thinking']})==({'think':'low'},'low')   # older Ollama: no advertised values
    with pytest.raises(ValueError,match='cannot fit'):
        validate_patch({'daedalus_role_outputs':{'qa':DEFAULTS['openhands_num_ctx']}},DEFAULTS)


def test_failure_categories_do_not_retry_application_or_network_errors():
    malformed='Ollama_chatException XML syntax error on line 7'
    assert malformed_native_call(malformed)
    assert not malformed_native_call('Connection refused')
    assert not malformed_native_call('Application XML parser failed')
    assert classify_failure('Response reached the configured completion allowance','verify')['category']=='output_limit'
    assert classify_failure('Preview failed to start','code')['category']=='preview_startup'
    assert classify_failure(malformed,'code',recovery_attempted=True)['recovery_attempted']
