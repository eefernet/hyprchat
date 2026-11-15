"""Offline tests for chat_turns: when an incoming user message is new, and what is saved with it."""
import sys
from pathlib import Path

from .optional_deps import HAS_AIOSQLITE, install_aiosqlite_stub

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if not HAS_AIOSQLITE:
    install_aiosqlite_stub()

import chat_turns  # noqa: E402

SAVED = {"id": 41, "content": "yes"}


def test_the_same_words_sent_again_are_a_new_message():
    # Found 2026-11-13: "yes" sent twice kept one user row; after a reload two replies stood back to back.
    assert chat_turns.is_retry(SAVED, ("yes", "yes"), turn="new") is False


def test_a_regenerate_reuses_the_saved_message():
    assert chat_turns.is_retry(SAVED, ("yes", "yes"), turn="retry") is True
    assert chat_turns.is_retry(SAVED, ("something shown", "yes"), turn="retry") is True
    assert chat_turns.is_retry(SAVED, ("no", "no"), turn="retry") is False


def test_a_request_that_does_not_say_keeps_the_old_rule():
    # Older clients, and the replay of a request whose response was lost: Daedalus receipts depend on it.
    assert chat_turns.is_retry(SAVED, ("yes",)) is True
    assert chat_turns.is_retry(SAVED, ("no",)) is False


def test_continue_reuses_and_an_empty_chat_never_does():
    assert chat_turns.is_retry(SAVED, ("other",), continuing=True, turn="new") is True
    assert chat_turns.is_retry(None, ("yes",), continuing=True) is False


def test_text_the_model_saw_but_the_bubble_does_not_show_is_kept():
    # Found 2026-11-13: a PDF's text went to the model but was not saved; after a reload the model had forgotten it.
    meta = chat_turns.user_metadata({"pdfs": [{"name": "a.pdf", "pages": 2}]}, "What does it say?", "What does it say?\n\n[PDF a.pdf]\n...")
    assert meta["model_content"].endswith("...") and meta["pdfs"][0]["name"] == "a.pdf"
    assert chat_turns.user_metadata(None, "hello", "hello") is None
    assert chat_turns.user_metadata({"a": 1}, "hi", "x" * (chat_turns.MODEL_TEXT_LIMIT + 1)) == {"a": 1}


# ── Chat bug-fix pass, stage 4 (2026-11-15) ──────────────────────────────────────────────────────────────

def test_only_an_assistant_reply_of_this_chat_can_be_continued():
    # The id was trusted as sent: one from another chat had that chat's reply overwritten.
    chat = {'messages': [{'id': 7, 'role': 'user', 'content': 'q'}, {'id': 8, 'role': 'assistant', 'content': 'half', 'metadata': {}}]}
    row, problem = chat_turns.continue_target(chat, '8')
    assert row['id'] == 8 and problem == ''
    for bad in (7, 99, 'abc', None):
        row, problem = chat_turns.continue_target(chat, bad)
        assert row is None and problem
    assert chat_turns.continue_target(None, 8)[0] is None


def test_a_failed_reply_keeps_its_text_and_names_the_reason_once():
    assert chat_turns.failure_text('', 'model  crashed\nhard') == ('_[No response — model crashed hard]_', True)
    assert chat_turns.failure_text('', '') == ('_[No response; generation failed.]_', True)
    assert chat_turns.failure_text('Half an answer \n', 'boom') == ('Half an answer\n\n_[interrupted: boom]_', False)
    assert chat_turns.failure_text('Half', '') == ('Half', False)
    assert len(chat_turns.failure_text('', 'x' * 500)[0]) < 200
