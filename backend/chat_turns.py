"""Rules about chat turns that are easier to state and test outside the streaming loop."""
from __future__ import annotations

import database as db

MODEL_TEXT_LIMIT = 400_000     # characters of model-facing text kept with a user message


def is_retry(latest_user: dict | None, texts, *, continuing: bool = False, turn: str | None = None) -> bool:
    """True when the incoming user message is the one already saved, so it must not be saved again.

    The server is the only place user messages are saved. The old rule skipped the save whenever the
    text matched the latest user message, so sending "yes" (or "continue", "retry") twice in a row kept
    only the first: after a reload the second turn was gone and two replies stood back to back.

    The browser now says what a request is (``turn``): "new" for a message the user just sent, "retry"
    for a regenerate of the last one. A new message is always saved. A retry, a continue, and a request
    that does not say (older clients, and the replay of a request whose response was lost, which the
    Daedalus dispatch receipts rely on) reuse the saved message when the text is the same.
    """
    if not latest_user:
        return False
    if continuing:
        return True
    if turn == "new":
        return False
    return (latest_user.get("content") or "") in [text for text in texts if text is not None]


def user_metadata(metadata: dict | None, shown: str, sent: str) -> dict | None:
    """Metadata to save with a user message. When what the model received differs from what the bubble
    shows (the text of an attached PDF is sent, not shown), the model-facing text is kept so a reload or
    a chat switch does not make the model forget the attachment."""
    if not sent or sent == shown or len(sent) > MODEL_TEXT_LIMIT:
        return metadata
    return {**(metadata or {}), "model_content": sent}


def continue_target(conversation: dict | None, message_id) -> tuple[dict | None, str]:
    """(row, problem) for a Continue request. The row must be an assistant message of THIS conversation.

    The id used to be trusted as sent: ``int(req.continue_message_id)`` went straight to the final save,
    so an id from another chat had that chat's reply overwritten, and a non-numeric id raised inside the
    stream."""
    try:
        wanted = int(message_id)
    except (TypeError, ValueError):
        return None, 'The reply to continue could not be identified. Reload the chat and try again.'
    for row in (conversation or {}).get('messages', []):
        if row.get('id') == wanted:
            if row.get('role') != 'assistant':
                break
            return row, ''
    return None, 'The reply to continue is not part of this chat any more. Reload the chat and try again.'


NO_RESPONSE = '_[No response — {reason}]_'
INTERRUPTED = '_[interrupted: {reason}]_'


def failure_text(visible: str, reason: str) -> tuple[str, bool]:
    """(text to keep on the saved reply, whether it is new text the browser has not been sent).

    An empty reply becomes a one-line note; a partial reply keeps its text with the reason under it."""
    reason = ' '.join(str(reason or '').split())[:160]
    if not visible.strip():
        return (NO_RESPONSE.format(reason=reason) if reason else '_[No response; generation failed.]_'), True
    return (visible.rstrip() + '\n\n' + INTERRUPTED.format(reason=reason) if reason else visible), False
