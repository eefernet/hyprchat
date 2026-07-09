"""Offline unit tests: cloud pricing lookups, FTS sanitizer, KB URL slug.

No live server needed — pure functions plus module imports behind the
optional-deps stubs.
"""
import sys
from pathlib import Path

import pytest

from .optional_deps import (
    HAS_AIOSQLITE,
    HAS_CHROMADB,
    HAS_FASTAPI,
    install_aiosqlite_stub,
    install_rag_stub,
)

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if not HAS_AIOSQLITE:
    install_aiosqlite_stub()

import database as db  # noqa: E402
import model_providers  # noqa: E402


class TestPriceForModel:
    def test_longest_prefix_wins_dated_opus_4_5(self):
        # opus-4-5 must NOT fall back to the shorter (and 3× pricier) opus-4 key
        assert model_providers.price_for_model("anthropic:claude-opus-4-5-20251101") == (5.00, 25.00)

    def test_dated_opus_4_still_matches_family(self):
        assert model_providers.price_for_model("anthropic:claude-opus-4-20250514") == (15.00, 75.00)

    def test_dated_sonnet_resolves_to_family(self):
        assert model_providers.price_for_model("anthropic:claude-sonnet-4-20250514") == (3.00, 15.00)

    def test_openai_mini_variant_not_swallowed_by_base(self):
        assert model_providers.price_for_model("openai:gpt-4o-mini-2024-07-18") == (0.15, 0.60)
        assert model_providers.price_for_model("openai:gpt-4o") == (2.50, 10.00)

    def test_unpriced_returns_none(self):
        assert model_providers.price_for_model("qwen3:32b") is None
        assert model_providers.price_for_model("") is None
        assert model_providers.price_for_model(None) is None
        # custom provider models are never priced even when the name echoes a priced one
        assert model_providers.price_for_model("custom:gpt-4o") is None

    def test_case_insensitive(self):
        assert model_providers.price_for_model("OpenAI:GPT-4o") == (2.50, 10.00)


class TestCostUsd:
    def test_input_output_math(self):
        assert model_providers.cost_usd("openai:gpt-4o", 1_000_000, 0) == pytest.approx(2.50)
        assert model_providers.cost_usd("openai:gpt-4o", 0, 1_000_000) == pytest.approx(10.00)
        assert model_providers.cost_usd("anthropic:claude-sonnet-4", 200_000, 50_000) == pytest.approx(
            0.2 * 3.00 + 0.05 * 15.00)

    def test_unpriced_returns_none_not_zero(self):
        assert model_providers.cost_usd("qwen3:32b", 5000, 5000) is None

    def test_none_token_counts_treated_as_zero(self):
        assert model_providers.cost_usd("openai:gpt-4o", None, None) == 0


class TestFtsMatchExpr:
    def test_punctuation_becomes_quoted_or_terms(self):
        expr = db._fts_match_expr('what is the deploy monitor? "quoted" test-hyphen')
        assert expr.startswith('"what" OR "is"')
        assert '"hyphen"' in expr
        assert "?" not in expr and "-" not in expr.replace('" OR "', "")

    def test_single_char_noise_dropped(self):
        assert db._fts_match_expr("a x ?") == ""
        assert db._fts_match_expr("") == ""

    def test_term_cap(self):
        expr = db._fts_match_expr(" ".join(f"word{i}" for i in range(30)))
        assert expr.count(" OR ") == 11  # max 12 terms

    def test_underscore_tokens_survive(self):
        assert db._fts_match_expr("kb_chunks_fts sync") == '"kb_chunks_fts" OR "sync"'


@pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi not installed (main.py import)")
class TestKbUrlSlug:
    @pytest.fixture(autouse=True)
    def _main(self, monkeypatch):
        if not HAS_CHROMADB:
            install_rag_stub(monkeypatch)
        import importlib
        self.main = importlib.import_module("main")

    def test_basic_slug(self):
        assert self.main._kb_url_slug("My Page (2024)") == "my-page-2024"

    def test_fallback_on_no_tokens(self):
        assert self.main._kb_url_slug("///") == "page"
        assert self.main._kb_url_slug("", fallback="doc") == "doc"

    def test_length_cap(self):
        assert len(self.main._kb_url_slug("x" * 300)) <= 80
