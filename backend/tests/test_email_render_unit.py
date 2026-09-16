"""Offline unit tests for email_render.py — srcDoc preparation of untrusted
email HTML (cid image embedding + sanitize_preview_html pass-through).

No server, no IMAP. artifact_files pulls fastapi/aiosqlite; both are stubbed
when missing. Run: python -m pytest tests/test_email_render_unit.py -v
"""

import importlib.util
import sys
from pathlib import Path

from .optional_deps import HAS_AIOSQLITE, install_aiosqlite_stub, module_stub

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if not HAS_AIOSQLITE:
    install_aiosqlite_stub()
if importlib.util.find_spec("fastapi") is None:
    module_stub("fastapi", HTTPException=type("HTTPException", (Exception,), {}))

import email_render  # noqa: E402


_IMG = [{"cid": "logo@x", "content_type": "image/png", "b64": "UE5H"}]


# ── cid → data URI embedding ─────────────────────────────────────────────

def test_embed_double_quoted_cid():
    out = email_render.embed_inline_images('<img src="cid:logo@x" alt="l">', _IMG)
    assert 'src="data:image/png;base64,UE5H"' in out
    assert "cid:" not in out


def test_embed_single_quoted_and_unquoted_cid():
    out = email_render.embed_inline_images(
        "<img src='cid:logo@x'><img src=cid:logo@x >", _IMG)
    assert out.count('src="data:image/png;base64,UE5H"') == 2


def test_unmatched_cid_left_alone():
    html = '<img src="cid:other@y">'
    assert email_render.embed_inline_images(html, _IMG) == html


def test_embed_handles_regex_metachars_in_cid():
    imgs = [{"cid": "a+b(1)@x", "content_type": "image/gif", "b64": "R0lG"}]
    out = email_render.embed_inline_images('<img src="cid:a+b(1)@x">', imgs)
    assert 'src="data:image/gif;base64,R0lG"' in out


# ── prepare_email_html: sanitize layer ───────────────────────────────────

def test_empty_input_returns_empty():
    assert email_render.prepare_email_html("") == ""
    assert email_render.prepare_email_html("   ") == ""
    assert email_render.prepare_email_html(None) == ""


def test_scripts_and_handlers_stripped():
    out = email_render.prepare_email_html(
        '<p onclick="evil()">hi</p><script>steal()</script>'
        '<a href="javascript:bad()">x</a>')
    assert "<script" not in out.lower()
    assert "onclick" not in out.lower()
    assert "javascript:" not in out.lower()
    assert "<p" in out and "hi" in out


def test_base_target_blank_injected():
    out = email_render.prepare_email_html("<p>hello</p>")
    assert 'target="_blank"' in out


def test_cid_embedded_before_sanitize():
    out = email_render.prepare_email_html('<img src="cid:logo@x">', _IMG)
    assert 'src="data:image/png;base64,UE5H"' in out
