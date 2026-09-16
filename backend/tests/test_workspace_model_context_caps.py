"""Helper contexts are separate, visible profiles rather than fixed caps."""
import config
import context_policy


def test_helpers_do_not_implicitly_inherit_chat_window(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_NUM_CTX", 262144)
    monkeypatch.setattr(config, "CONTEXT_SETTINGS", {**context_policy.DEFAULTS, "helper_contexts":{"workspace":6144,"title":3072}})
    assert context_policy.helper_context("workspace") == 6144
    assert context_policy.helper_context("title") == 3072


def test_explicit_helper_window_has_no_hidden_ceiling(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_SETTINGS", {**context_policy.DEFAULTS, "helper_contexts":{"extraction":196608}})
    assert context_policy.helper_context("extraction") == 196608
    assert context_policy.helper_context("title") == context_policy.DEFAULTS["helper_contexts"]["title"]
