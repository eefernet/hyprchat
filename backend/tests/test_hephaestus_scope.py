import asyncio
import base64
import json
import time

import pytest

from hephaestus_scope import headers, key, scope, verify


def test_owner_and_project_namespaces_are_distinct():
    assert key({'owner':'one','project':'p'})!=key({'owner':'two','project':'p'})
    assert key({'owner':'one','project':'p'})!=key({'owner':'one','project':'q'})
    assert key({'owner':'one','conversation':'c'})==key({'owner':'one','conversation':'c','nonce':'different'})


def test_signed_scope_rejects_tampering_and_expiration():
    values={k.lower():v for k,v in headers('owner',secret='secret',project='p').items()}
    assert verify(values,'secret')['project']=='p'
    with pytest.raises(ValueError): verify(values,'different')
    with pytest.raises(ValueError): verify(values,'secret',now=time.time()+121)
    payload=json.loads(base64.urlsafe_b64decode(values['x-hephaestus-scope']))
    payload['owner']='other'
    values['x-hephaestus-scope']=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(ValueError): verify(values,'secret')


def test_context_does_not_leak_between_concurrent_projects():
    async def run(project):
        with scope(project=project):
            await asyncio.sleep(0)
            signed={k.lower():v for k,v in headers('owner',secret='secret').items()}
            return verify(signed,'secret')['project']
    async def together(): return await asyncio.gather(run('one'),run('two'))
    assert asyncio.run(together())==['one','two']


def test_project_workspace_survives_selection_in_another_conversation():
    assert key({'owner':'a','project':'p','conversation':'original'})==key({'owner':'a','project':'p','conversation':'other'})
    assert key({'owner':'a','project':'p','conversation':'same'})!=key({'owner':'a','project':'q','conversation':'same'})


def test_authenticated_owner_cannot_be_overridden_by_context():
    with scope(owner='stale'):
        signed={k.lower():v for k,v in headers('authenticated',secret='secret').items()}
        assert verify(signed,'secret')['owner']=='authenticated'
