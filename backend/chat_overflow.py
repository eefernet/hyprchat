"""A request that did not fit the model's context window.

Ollama does not refuse a prompt that is larger than the window: it cuts it, answers from what is left,
and reports nothing. Measured through the chat on 2026-11-15 with an 8,192-token window:

    one message of 2x or 6x the window      the model read 4,098 tokens: exactly half the window
    a history of 2x the window              the model read 7,922: the oldest messages were dropped
    a 50,000-character table of dashes      4,098 again (it did not fit either)
    12,000 characters of prose (it fits)    5,964, a normal 0.73 tokens per estimated token

Compaction cannot help with the first kind: it folds earlier messages, and the one that does not fit
is the newest. So the chat says what happened. ``check`` looks at the first model call of a reply and
recognises the two cuts by the count the model itself reports, together with an estimate that is far
too large for that count. A request that fits is never flagged: its count is neither stuck at half the
window nor far below what its text must come to. It warns; it never refuses.
"""
from __future__ import annotations

HALF_BAND = 0.01      # a cut prompt lands within this share of the window around its half
CUT_BELOW = 0.44      # ...and far under the estimate: real text measures 0.6 to 0.8 tokens per unit
NEARLY_FULL = 0.9     # the other cut: old messages dropped until the rest just fits
CANNOT_FIT = 0.55     # ...while even at this low rate the estimate is clearly over the window
TYPICAL = 0.7         # for the "about N tokens" figure when the chat has no measured rate yet


def needed(estimate, ratio=0.0) -> int:
    """About how many tokens the request would have taken. Never less than the low-rate figure."""
    try:
        ratio = float(ratio or 0)
    except (TypeError, ValueError):
        ratio = 0.0
    rate = ratio if 0.4 <= ratio <= 1.2 else TYPICAL
    return round(max(estimate * rate, estimate * CANNOT_FIT))


def check(estimate, prompt_tokens, window, ratio=0.0) -> dict | None:
    """``{"needed", "window", "read"}`` when the model read less than it was sent, else None.

    ``estimate`` is the request's size in the estimator's units, ``prompt_tokens`` what the model says it
    read, ``ratio`` the chat's measured tokens per unit from an earlier reply (0 when unknown)."""
    try:
        estimate, read, window = int(estimate or 0), int(prompt_tokens or 0), int(window or 0)
    except (TypeError, ValueError):
        return None
    if estimate <= 0 or read <= 0 or window <= 0:
        return None
    halved = abs(read - window / 2) <= max(48, window * HALF_BAND) and read < estimate * CUT_BELOW
    dropped = read >= window * NEARLY_FULL and estimate * CANNOT_FIT > window * 1.1
    if not (halved or dropped):
        return None
    return {"needed": max(needed(estimate, ratio), window + 1), "window": window, "read": read}
