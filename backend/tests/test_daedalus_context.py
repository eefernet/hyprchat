import pytest

import config
from context_policy import DEFAULTS, ROLES, resolve, validate_patch, estimate_tokens


@pytest.mark.parametrize("window", [8192, 24576, 65536, 196608, 300000, 1048576])
def test_every_stage_honors_arbitrary_settings_window(window):
    settings = {**DEFAULTS, "openhands_num_ctx": window}
    for role in ROLES:
        policy = resolve(role, settings)
        assert policy.num_ctx == window
        assert policy.source == "daedalus"
        assert policy.input_budget + policy.num_predict < window


def test_stage_override_and_global_inheritance_are_explicit():
    settings = {**DEFAULTS, "default_num_ctx": 24576, "openhands_num_ctx": 0,
                "daedalus_role_contexts": {"acceptance": 50000}}
    assert resolve("builder", settings).num_ctx == 24576
    assert resolve("builder", settings).source == "global"
    assert resolve("acceptance", settings).num_ctx == 50000
    assert resolve("acceptance", settings).source == "stage:acceptance"


@pytest.mark.parametrize("value", [True, -5, 0, "garbage", 12.5, None])
def test_invalid_explicit_values_are_rejected_not_replaced(value):
    with pytest.raises(ValueError):
        validate_patch({"default_num_ctx": value}, DEFAULTS)


def test_small_windows_work_with_user_selected_output_allowance():
    values = validate_patch({"openhands_num_ctx": 1024, "generation_num_predict": 128}, DEFAULTS)
    assert resolve("builder", values).num_ctx == 1024
    with pytest.raises(ValueError, match="completion allowance"):
        validate_patch({"openhands_num_ctx": 1024}, DEFAULTS)


def test_runtime_settings_apply_at_next_call(monkeypatch):
    monkeypatch.setattr(config, "OPENHANDS_NUM_CTX", 24576)
    first = resolve()
    monkeypatch.setattr(config, "OPENHANDS_NUM_CTX", 60000)
    second = resolve()
    assert second.num_ctx == 60000
    assert first.settings_version != second.settings_version


def test_compaction_off_is_authoritative_and_tool_arguments_count():
    policy = resolve(settings={**DEFAULTS, "daedalus_compaction": "off", "context_compaction": "on"})
    assert not policy.compaction
    assert estimate_tokens({"tools": [{"arguments": "多" * 1000}]}) > 1000


def test_coder_chat_has_no_legacy_character_ceiling_and_obeys_compaction_off():
    import asyncio
    from agents.chat import _coder_chat_context
    messages=[{'role':'system','content':'Keep requirements'}, {'role':'user','content':'x'*90000}]
    large=resolve(settings={**DEFAULTS,'openhands_num_ctx':300000,'daedalus_compaction':'off'})
    assert asyncio.run(_coder_chat_context(None,'local',messages,[],large)) is messages
    small=resolve(settings={**DEFAULTS,'openhands_num_ctx':8192,'daedalus_compaction':'off'})
    with pytest.raises(ValueError,match='disabled'):
        asyncio.run(_coder_chat_context(None,'local',messages,[],small))
    assert len(messages[1]['content'])==90000


def test_compaction_preserves_late_system_instructions_and_tool_pairs():
    from coder_sdk_runtime import compile_context
    policy=resolve(settings={**DEFAULTS,'openhands_num_ctx':8192,'generation_num_predict':512,'daedalus_compaction':'on'})
    messages=[{'role':'system','content':'Rules'}, {'role':'user','content':'Task'}]
    messages += [{'role':'user','content':'old evidence '*1500}]*3
    messages += [{'role':'system','content':'Persistent workflow protocol'}]
    messages += [{'role':'assistant','tool_calls':[{'id':'a'}]}, {'role':'tool','tool_call_id':'a','content':'result'},
                 {'role':'user','content':'continue'}, {'role':'assistant','content':'working'}]
    rebuilt,changed=compile_context(messages,[],policy,lambda older:'prior decisions')
    assert changed
    assert any(m.get('content')=='Persistent workflow protocol' and m['role']=='system' for m in rebuilt)
    assert rebuilt[-4:]==messages[-4:]
