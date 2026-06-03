"""
Static guard tests for the single-file frontend settings persistence.

The frontend has no build/test pipeline, so these tests protect the small
hydration contract that keeps stale localStorage from overwriting server
settings on page load.
"""
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html"


def _html() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def test_settings_hydration_guard_exists():
    html = _html()

    assert "settingsLoadedRef=useRef(false)" in html
    assert "seenSettingEffectRef=useRef({})" in html
    assert "skipSettingPatchRef=useRef({})" in html
    assert "hydrateServerSetting=(settingKey,setter,value,current)" in html
    assert "persistServerSetting=(storageKey,settingKey,value" in html
    assert "if(!settingsLoadedRef.current&&!seen)return;" in html
    assert "settingsLoadedRef.current=true;" in html


def test_server_persisted_settings_use_guarded_persistence():
    html = _html()
    expected_pairs = [
        ("hc-num-ctx", "default_num_ctx"),
        ("hc-ws-model", "workspace_model"),
        ("hc-planning-model", "planning_model"),
        ("hc-coder-model", "coder_model"),
        ("hc-architect-model", "architect_model"),
        ("hc-reviewer-model", "reviewer_model"),
        ("hc-acceptance-model", "acceptance_model"),
        ("hc-builder-model", "builder_model"),
        ("hc-fixer-model", "fixer_model"),
        ("hc-qa-model", "qa_model"),
        ("hc-coder-num-ctx", "openhands_num_ctx"),
        ("hc-openhands-enabled", "openhands_enabled"),
        ("hc-openhands-max-rounds", "openhands_max_rounds"),
        ("hc-openhands-reasoning-effort", "openhands_reasoning_effort"),
    ]

    for storage_key, setting_key in expected_pairs:
        assert f'persistServerSetting("{storage_key}","{setting_key}"' in html

    assert 'localStorage.setItem("hc-num-ctx",String(numCtx));fetch(`${API}/api/settings`' not in html
    assert 'localStorage.setItem("hc-acceptance-model",acceptanceModel);fetch(`${API}/api/settings`' not in html


def test_settings_fetch_hydrates_server_values_without_echo_patch():
    html = _html()
    hydrated_keys = [
        "default_num_ctx",
        "workspace_model",
        "planning_model",
        "coder_model",
        "architect_model",
        "reviewer_model",
        "acceptance_model",
        "builder_model",
        "fixer_model",
        "qa_model",
        "openhands_num_ctx",
        "openhands_enabled",
        "openhands_max_rounds",
        "openhands_reasoning_effort",
    ]

    for key in hydrated_keys:
        assert f'hydrateServerSetting("{key}"' in html

    assert "catch(()=>{settingsLoadedRef.current=true;})" in html
