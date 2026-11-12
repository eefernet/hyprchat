"""
Static guard tests for frontend settings persistence.

These static guards complement frontend behavior tests and protect the small
hydration contract that keeps stale localStorage from overwriting server
settings on page load.
"""
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src" / "main.jsx"
SETTINGS_SYNC = Path(__file__).resolve().parents[2] / "frontend" / "src" / "settingsSync.js"


def _html() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def _sync() -> str:
    return SETTINGS_SYNC.read_text(encoding="utf-8")


def test_settings_hydration_guard_exists():
    html = _html()
    sync = _sync()

    assert "settingsLoadedRef=useRef(false)" in html
    assert "seenSettingEffectRef=useRef({})" in html
    assert "skipSettingPatchRef=useRef({})" in html
    assert "createSettingsSync({" in html
    assert "hydrateServerSetting=(settingKey,setter,value,current)" in sync
    assert "persistServerSetting=(storageKey,settingKey,value" in sync
    assert "if(!settingsLoadedRef.current&&!seen)return;" in sync
    assert "settingsLoadedRef.current=true;" in html


def test_server_persisted_settings_use_guarded_persistence():
    html = _html()
    sync = _sync()
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
        from context_policy import RETIRED_SETTINGS
        if setting_key in RETIRED_SETTINGS:
            assert f'persistServerSetting("{storage_key}","{setting_key}"' not in html
        else:
            assert f'persistServerSetting("{storage_key}","{setting_key}"' in html

    assert 'fetch(`${api}/api/settings`' in sync
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
        from context_policy import RETIRED_SETTINGS
        if key in RETIRED_SETTINGS:
            assert f'hydrateServerSetting("{key}"' not in html
        else:
            assert f'hydrateServerSetting("{key}"' in html

    assert "catch(()=>{settingsLoadedRef.current=true;})" in html


def test_playful_status_preference_is_user_scoped_and_hydrated():
    html = _html()
    assert 'hc-fun-verbs::${hcStoredUserId()||"default"}' in html
    assert '{key:"fun-verbs",legacy:"hc-fun-verbs",set:setFunVerbs,fallback:true}' in html
    assert 'persistPref("fun-verbs",funVerbs)' in html
    assert 'setFunVerbs(true);setPrompts([])' in html
    assert "if(d.key==='fun-verbs')val=typeof val==='boolean'?val:true;" in html
    assert "localStorage.removeItem('hc-ui-font')" not in html


def test_image_preferences_use_profile_sync_without_shared_browser_seeds():
    html = _html()
    assert 'createImageSettingsSync({' in html
    assert 'imageSettingsSyncRef.current.load()' in html
    assert 'saveImageSetting("image_chat_auto_enhance",!imgChatAutoEnhance)' in html
    assert 'imageSettingsSyncRef.current.reset();setImageSettingsError("");setImgChatLists(null)' in html
    assert 'persistServerSetting("hc-img-chat-' not in html
    assert 'lsGet("hc-img-chat-' not in html
    assert 'hydrateServerSetting("image_chat_' not in html
