import asyncio
import json
from unittest.mock import AsyncMock
import pytest
import email_triage


@pytest.mark.parametrize('response',[None,'not json','{"1":{"urgency":"normal"}}'])
def test_invalid_or_failed_triage_is_retryable_not_normal_priority(monkeypatch,response):
    import pim
    message={'id':'message','from_addr':'fixture@example.test','subject':'Fixture','snippet':'Fixture','triage_attempts':0}
    monkeypatch.setattr(email_triage.db,'untriaged_email_messages',AsyncMock(return_value=[message]))
    update=AsyncMock();monkeypatch.setattr(email_triage.db,'update_email_message',update)
    monkeypatch.setattr(email_triage,'_triage_model',lambda:'fixture-local')
    monkeypatch.setattr(pim,'user_timezone',AsyncMock(return_value='UTC'))
    call=AsyncMock(side_effect=ConnectionError('private connection details')) if response is None else AsyncMock(return_value=response)
    monkeypatch.setattr(email_triage.model_providers,'complete_chat',call)
    async def run():
        assert await email_triage.triage_new_messages(None,{'id':'account','user_id':'default'})==0
        fields=update.call_args.args[1]
        assert fields['triage_status']=='retry_wait' and fields['triage_attempts']==1
        assert 'urgency' not in fields and 'private connection' not in fields['triage_error']
    asyncio.run(run())
