import copy
import json
import subprocess
import pytest

from coder_browser_schema import validate_steps


def api(language='node', **case):
    return {'runner':'api','cwd':'.','target':{'language':language,'path':'subject.mjs' if language=='node' else 'subject.py','export':'subject'},
        'cases':[{'args':[],'expect':{'kind':'equal','value':0},'preserve_inputs':True,**case}]}


@pytest.mark.parametrize('step,field',[
    ({'action':'evaluate','expression':'button.click()'},'action'),
    ({'action':'click','selector':'button','force':True},'force'),
    ({'action':'text','selector':'li','value':2},'value'),
    ({'action':'visible','selector':'button','value':'Delete'},'value'),
    ({'action':'fill','selector':'input'},'value'),
    ({'action':'click','selector':{'role':'button','hasText':'Delete'}},'selector'),
])
def test_browser_errors_identify_exact_field(step,field):
    with pytest.raises(ValueError,match=r'steps\[0\]\.'+field): validate_steps([step])
