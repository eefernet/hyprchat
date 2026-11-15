"""How much of the model's window a chat occupies, and summarizing the chat when it fills up.

Two things live here so the chat loop only needs thin hooks.

**The reading.** ``TurnMeter`` follows one reply and reports two numbers: what the conversation holds
(``used``: what will be sent again next turn) and what is only in use while the reply runs
(``working``: thinking, tool results, search and knowledge excerpts added for this turn). The split
matters because the browser sends only message text back on the next turn; a meter that showed
"prompt + everything generated" fell after every reply. ``used`` is anchored on the first model call's
real prompt size, and the reply text is sized with ``context_policy.estimate_tokens`` scaled by that
call's real-to-estimated ratio. The final reading is saved on the reply (``metadata.context``), so a
reload shows it at once and the next turn starts from it.

**Compaction.** ``compact`` replaces older history with one summary block when the last reading plus the
new message crosses the Settings threshold. Everything older than a kept recent window goes into the
summary and everything newer is sent word for word; nothing is dropped unsummarized. The cut is stored
as ``conversations.summary_until_msg_id``, and until the threshold is crossed again every request sends
"summary + all later messages", so the prompt prefix stays stable between compactions. The browser sends
message ids so the cut is exact; a request without ids (headless runs, old clients) keeps using the
older "summary + last 8 messages" path in ``agents/chat.py``.

``relieve`` is the same idea inside one reply: when tool results fill the window, older tool results are
shortened and, if that is not enough, the history is summarized, before the chat gives up.
"""
from __future__ import annotations

import asyncio
import json

import httpx

import config
import context_policy
import database as db
import model_providers

COMPACT_CHOICES = (85, 90, 95, 98, 99)   # Settings: compact when the chat reaches this share of the window
REPLY_RESERVE = 4096          # tokens always left free for the next reply and for the summary itself
RESERVE_SHARE = 0.25          # ... or this share of a small window, whichever is less
MIN_FULL_SHARE = 0.92         # inside one reply, room is made at this share at the earliest
KEEP_SHARE = 0.25             # most of the window kept word for word after a compaction
MIN_KEEP_SHARE = 0.08         # and the least
TARGET_SHARE = 0.5            # how full the window should be right after a compaction
MIN_FOLD_TOKENS = 400         # folding less than this (or 5 % of the window) is not worth a model call
MIN_FOLD_SHARE = 0.05
DEFAULT_RATIO = 0.8           # real tokens per estimated token before a model has reported any
FORCED_KEEP_SHARE = 0.10      # the same, when compacting in the middle of a reply
RELIEF_KEEP_CHARS = 200       # what is left of a tool result from an earlier round
RATIO_MIN, RATIO_MAX = 0.3, 2.0
KEEPALIVE_SECONDS = 10
SUMMARY_READ_SECONDS = 900    # longest silence allowed while the chat model writes a summary
HELPER_PASSES = 12
SUMMARY_MARK = "=== EARLIER CONVERSATION (summarized) ==="
INSTRUCTION = (
    "Stop here. Do not answer the last message and do not call any tool.\n"
    "Write ONE summary of the whole conversation above. It will replace those messages, so later replies "
    "must be able to continue from it alone. Keep decisions, facts, names, numbers, code and file "
    "references, the user's preferences and corrections, and open questions. Merge any earlier summary "
    "into it. At most {words} words. Output only the summary text.")


def estimate(value) -> int:
    return context_policy.estimate_tokens(value)


def ratio_of(measured, estimated, fallback: float = 1.0) -> float:
    """Real tokens per estimated token for this model and this kind of text, within sane bounds."""
    if not measured or not estimated:
        return fallback
    return min(RATIO_MAX, max(RATIO_MIN, measured / estimated))


def payload_estimate(messages, tools=None) -> int:
    """Estimated size of one request. Image data is left out: it is not text tokens and would swamp the figure."""
    slim = [{key: value for key, value in message.items() if key != "images"} for message in messages]
    return estimate({"messages": slim, "tools": tools or []})


def message_id(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


# ── The reading ────────────────────────────────────────────────────────────────

class TurnMeter:
    """Follows one reply. Fed by ``chat_delivery.deliver`` from events of the chat loop."""

    def __init__(self):
        self.window = 0
        self.ratio = DEFAULT_RATIO
        self.base = 0             # the conversation without this reply's text, in tokens
        self.measured = False     # base comes from a real count
        self.first_estimate = 0   # the estimator's figure for the first request
        self.turn_only = 0        # the estimator's figure for blocks sent this turn only
        self.round_prompt = 0
        self.peak = 0
        self.prior = ""
        self.fixed_estimate = 0   # the estimator's figure for the system prompt and tool definitions
        self.round_estimate = 0   # the estimator's figure for the request being made
        self.round_gen = 0        # what the last finished call wrote: in the window until the next request
        self.last_real = 0        # real size of the last finished request, and the estimate it had
        self.last_estimate = 0
        self.parts_units = None   # the request by category, estimator units (context_breakdown.py)
        self.first_parts = None   # the first request by category, real tokens
        self.peak_parts = None    # the fullest moment of the reply by category
        self.kept_units = 0       # this reply's tool record, sent again next turn (chat_tool_memory.py)

    def begin(self, prior: str) -> None:
        """Text the reply already had (continue mode). It is in the prompt, so it must not be counted twice."""
        self.prior = prior or ""

    def info(self, event: dict) -> None:
        """``context_info``: sent before every model call with the window and the request's estimated size."""
        self.window = int(event.get("loaded_window") or event.get("window") or self.window or 0)
        size = int(event.get("prompt_estimate") or 0)
        if not self.measured and int(event.get("round") or 0) == 0 and size:
            self.ratio = ratio_of(float(event.get("ratio") or 0), 1.0, self.ratio)
            self.first_estimate, self.turn_only = size, int(event.get("turn_only") or 0)
            self.fixed_estimate = int(event.get("fixed") or 0)
            self.base = max(0, round((size - self.turn_only) * self.ratio))
        if size:
            self.round_estimate = size
            self.parts_units = event.get("parts") if isinstance(event.get("parts"), dict) else None
            self.round_gen = 0      # the previous call's output is part of this request now
            if self.measured and self.last_real and self.last_estimate:
                # Only what was added since the last real count is an estimate.
                self.round_prompt = max(0, self.last_real + round((size - self.last_estimate) * self.ratio))
            else:
                self.round_prompt = round(size * self.ratio)

    def usage(self, call: dict) -> None:
        """``usage_round``: real counts for one finished model call. The first one anchors the reading."""
        prompt, generated = int(call.get("prompt_tokens") or 0), int(call.get("gen_tokens") or 0)
        if not prompt:
            return
        if not self.measured:
            self.measured = True
            self.ratio = ratio_of(prompt, self.first_estimate, self.ratio)
            self.base = max(0, prompt - round(self.turn_only * self.ratio))
            self.first_parts = self._scaled(prompt)
        self.round_prompt, self.round_gen = prompt, generated
        self.last_real, self.last_estimate = prompt, self.round_estimate
        self._peak(prompt + generated, generated, 0)

    def _scaled(self, prompt: int):
        try:
            import context_breakdown
            return context_breakdown.scale(self.parts_units, prompt, self.ratio)
        except Exception:
            return None

    def _now(self, generated: int, appended: int):
        """The window right now by category: the request, what was added since, and the reply being written."""
        parts = self._scaled(self.round_prompt)
        if not parts:
            return None
        parts["tool_calls"] += appended
        parts["reply"] = generated
        return parts

    def _peak(self, total: int, generated: int, appended: int) -> None:
        if total > self.peak:
            self.peak = total
            self.peak_parts = self._now(generated, appended) or self.peak_parts

    def compacted(self, event: dict) -> None:
        """``context_compacted`` in the middle of a reply: the conversation just got smaller."""
        # Only summarized history shrinks the conversation. Shortening this reply's own tool results does
        # not: they were never part of it (they used to be subtracted, so the figure dipped mid-reply).
        freed = event.get("freed_history")
        if freed is None:      # an event without the split: all of it was history unless it says nothing was summarized
            freed = 0 if event.get("summarized") == 0 else event.get("freed")
        if self.measured:
            self.base = max(0, self.base - int(freed or 0))

    def keep(self, event: dict) -> None:
        """``context_kept``: the size of the record of this reply's tool calls, as the next turn will get it."""
        self.kept_units = max(0, int(event.get("estimate") or 0))

    def kept_tools(self) -> int:
        return round(self.kept_units * self.ratio)

    def kept(self, visible: str) -> int:
        text = visible or ""
        if self.prior and text.startswith(self.prior):
            text = text[len(self.prior):]
        return self.base + self.kept_tools() + (round(estimate(text) * self.ratio) if text else 0)

    def live(self, event: dict, visible: str) -> dict:
        """A ``ctx_update`` with what the browser shows added to it."""
        prompt, generated = int(event.get("prompt_tokens") or 0), int(event.get("gen_tokens") or 0)
        if prompt and not event.get("live"):      # "live" marks the loop's own rough guesses
            if not self.measured:                 # the first real count anchors the reading
                self.usage({"prompt_tokens": prompt, "gen_tokens": generated})
            self.round_prompt, self.round_gen = prompt, generated
        # Between two model calls the window holds the last request, what that call wrote, and what the
        # tools have returned since (``appended``, sized by ``tool_phase``). The loop's old whole-prompt
        # guesses were ignored here and its ``gen_tokens`` was 0, so the figure dipped while a tool ran.
        appended = round(int(event.get("appended") or 0) * self.ratio)
        written = generated or self.round_gen
        total = self.round_prompt + written + appended
        self._peak(total, written, appended)
        kept = self.kept(visible)
        out = {**event, "used": kept, "working": max(0, total - kept), "total": total, "window": self.window,
               "measured": self.measured, "fixed": self.fixed()}
        parts = self._now(written, appended)
        if parts:
            out["parts"] = parts
        return out

    def fixed(self) -> int:
        """Tokens every request spends before any conversation: system prompt and tool definitions.
        About 3,500 for an ordinary chat, which is most of a 4K window."""
        # Instructions and tool definitions tokenize at about DEFAULT_RATIO; the whole-prompt ratio falls
        # as prose is added (0.80 -> 0.61 over a ten-turn chat) and would shrink this figure with it.
        if self.first_parts:      # the same split the breakdown shows
            return sum(self.first_parts.get(name, 0) for name in ("instructions", "tools", "memory", "skills"))
        fixed = round(self.fixed_estimate * max(self.ratio, DEFAULT_RATIO))
        # Never more than the request it is part of (seen live: 3,971 "fixed" in a 3,887-token chat).
        return min(fixed, self.base) if self.measured and self.base else fixed

    def reading(self, visible: str) -> dict | None:
        """What to save on the reply and send with ``done``. None when no model call was made.
        ``window`` is 0 for cloud models: their limit is not known here."""
        if not (self.base or self.peak):
            return None
        kept = self.kept(visible)
        out = {"used": kept, "working_peak": max(0, self.peak - kept), "window": self.window,
               "measured": self.measured, "ratio": round(self.ratio, 3), "fixed": self.fixed()}
        try:
            import context_breakdown
            # This reply's own tool record is known exactly: split the rest, then add it to its row. (Taking
            # it out of "messages" afterwards lost it whenever the fixed rows had left messages nothing.)
            own = min(kept, self.kept_tools())
            parts = context_breakdown.kept(self.first_parts, kept - own)
            if parts:
                parts["kept_tools"] = parts.get("kept_tools", 0) + own
                out["parts"] = parts
            if self.peak_parts:
                out["peak"] = {"total": self.peak, "parts": self.peak_parts}
        except Exception:
            pass
        return out


def tool_phase(messages, round_start: int, done_chars: int = 0) -> dict:
    """The ``ctx_update`` the chat loop sends between two model calls: the size of what has been added
    since the last request (tool results and the loop's notes; the model's own message is already counted
    as what it generated). ``done_chars``: results that are back but not in the list yet."""
    added = [message for message in messages[max(0, int(round_start)):] if message.get("role") != "assistant"]
    units = (estimate({"messages": added}) if added else 0) + max(0, int(done_chars or 0)) // 3
    return {"type": "ctx_update", "live": True, "gen_tokens": 0, "prompt_tokens": 0, "appended": units}


def fixed_estimate(messages, history, tools=None) -> int:
    """The estimator's figure for what every request carries before any conversation: the system blocks
    in front of the sent history, and the tool definitions. 0 when the history cannot be located."""
    place = region(messages, history)
    if not place:
        return 0
    lead = [message for message in messages[:place[0]]
            if not (message.get("content") or "").startswith(SUMMARY_MARK)]
    return payload_estimate(lead, tools)


async def previous_reading(conv_id: str) -> dict | None:
    """The newest saved reading of a conversation."""
    if not conv_id:
        return None
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT m.metadata FROM messages m JOIN conversations c ON c.id = m.conversation_id
               WHERE m.conversation_id = ? AND c.user_id = ? AND m.role = 'assistant'
               ORDER BY m.id DESC LIMIT 4""", (conv_id, db._scope_user()))
    finally:
        await conn.close()
    for row in rows:
        try:
            reading = (json.loads(row["metadata"] or "{}") or {}).get("context")
        except (TypeError, ValueError):
            continue
        if isinstance(reading, dict) and reading.get("used"):
            return reading
    return None


async def known_ids(conv_id: str) -> set[int]:
    """Ids of the messages a conversation really has. The browser's ids are only trusted when found here:
    a made-up id stored as the summary point would hide every message below it."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            """SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conversation_id
               WHERE m.conversation_id = ? AND c.user_id = ?""", (conv_id, db._scope_user()))
    finally:
        await conn.close()
    return {int(row["id"]) for row in rows}


async def loaded_window(http, model: str) -> int:
    """The window Ollama actually loaded the model with, or 0 when it does not say."""
    try:
        response = await http.get(f"{config.OLLAMA_URL}/api/ps", timeout=3)
        for entry in (response.json() or {}).get("models") or []:
            if model in (entry.get("name"), entry.get("model")):
                return int(entry.get("context_length") or 0)
    except Exception:
        pass
    return 0


# ── Compaction ─────────────────────────────────────────────────────────────────

def compact_percent(value=None) -> int:
    """The configured choice. Anything else snaps to the nearest choice (the lower one on a tie)."""
    try:
        number = float(getattr(config, "CHAT_COMPACTION_PERCENT", COMPACT_CHOICES[0]) if value is None else value)
    except (TypeError, ValueError):
        return COMPACT_CHOICES[0]
    return min(COMPACT_CHOICES, key=lambda choice: (abs(choice - number), choice))


def reply_reserve(window: int) -> int:
    return min(REPLY_RESERVE, int(window * RESERVE_SHARE))


def compaction_limit(window: int, percent=None) -> int:
    """Tokens of conversation at which a chat is compacted.

    The chosen share of the window, but never so late that the next reply (or the summary, which the
    chat model writes in the same window) has no room: a chat that starts a turn at 99 % of a 32K
    window would leave 328 tokens and Ollama would silently drop the start of the conversation.
    """
    window = int(window or 0)
    if window <= 0:
        return 0
    return min(round(window * compact_percent(percent) / 100), window - reply_reserve(window))


def full_share(window: int, percent=None) -> float:
    """Share of the window at which room is made in the middle of a reply. It follows the setting above
    92 %, so a tool run does not compact earlier than the owner asked for."""
    window = int(window or 0)
    return max(MIN_FULL_SHARE, compaction_limit(window, percent) / window) if window > 0 else MIN_FULL_SHARE


def enabled() -> bool:
    return getattr(config, "CONTEXT_COMPACTION", "off") == "on"


def has_ids(history) -> bool:
    return any(message_id(identifier) for _, identifier in history or [])


def region(messages, history):
    """(start, end) of the sent history inside the outgoing list, or None when it cannot be located.

    ``history`` holds the message objects built from the request, in order. Hints are inserted in front
    of them later, so positions are found by identity, not by index.
    """
    places = {id(message): index for index, message in enumerate(messages)}
    found = [places.get(id(message)) for message, _ in history or []]
    if not found or None in found or found != list(range(found[0], found[0] + len(found))):
        return None
    return found[0], found[-1] + 1


def summarized_count(ids, cursor: int) -> int:
    """How many leading history messages the stored summary already covers. Never the newest message."""
    count = 0
    for index, identifier in enumerate(ids):
        if 0 < message_id(identifier) <= cursor:
            count = index + 1
    return min(count, max(0, len(ids) - 1))


def keep_start(body, budget: int, ratio: float) -> int:
    """Index in ``body`` where the word-for-word part starts: the newest messages that fit ``budget``,
    opening on a user message so the kept part never begins with half an exchange."""
    total, start = 0.0, len(body) - 1
    for index in range(len(body) - 1, -1, -1):
        total += estimate(body[index].get("content") or "") * ratio
        if total > budget and index < len(body) - 1:
            break
        start = index
    while start < len(body) - 1 and body[start].get("role") != "user":
        start += 1
    return start


def summary_block(summary: str, render=None) -> dict:
    text = f"{SUMMARY_MARK}\n{summary.strip()}"
    return {"role": "system", "content": render(text) if render else text}


def keep_budget(window: int, fixed: int, words: int, forced: bool = False) -> int:
    """Tokens of recent messages to keep word for word.

    The system prompt and tool definitions are a fixed cost (about 3,300 tokens for an ordinary chat). In
    a small window they are most of it: keeping a flat quarter of an 8K window left the chat at 64% right
    after compacting, so it compacted again two turns later (measured 2026-11-13). Aim for half the window
    in total instead, within fixed bounds.
    """
    if forced:
        return round(window * FORCED_KEEP_SHARE)
    room = window * TARGET_SHARE - fixed - words * 1.5
    return round(min(window * KEEP_SHARE, max(window * MIN_KEEP_SHARE, room)))


def summary_words(window: int) -> int:
    return max(350, min(1500, int(window or 0) // 60))


def transcript(fold, limit: int | None = None) -> str:
    parts = [f"{str(message.get('role', '')).upper()}: {(message.get('content') or '').strip()}" for message in fold]
    text = "\n\n".join(part for part in parts if part.split(": ", 1)[-1])
    return text if limit is None else text[:limit]


async def _chat_model_summary(http, model, options, tools, prefix, words) -> str:
    """Ask the chat model itself, with the same prompt prefix the last turn used, so Ollama's cache covers
    the part being summarized and no second model is loaded. Streams, so Stop aborts the runner."""
    payload = {"model": model, "stream": True, "think": False, "keep_alive": "10m",
               "messages": [*prefix, {"role": "user", "content": INSTRUCTION.format(words=words)}],
               "options": {**options, "temperature": 0.2, "num_predict": words * 2 + 200}}
    if tools:
        payload["tools"] = tools
    text = []
    # Bounded: an unreachable Ollama must fail (and fall back) instead of hanging the chat. The read
    # allowance covers a cold cache, when the whole folded prefix is evaluated before the first token.
    async with http.stream("POST", f"{config.OLLAMA_URL}/api/chat", json=payload,
                           timeout=httpx.Timeout(SUMMARY_READ_SECONDS, connect=10)) as response:
        if response.status_code != 200:
            return ""
        async for line in response.aiter_lines():
            if not line.strip():
                continue
            chunk = json.loads(line)
            text.append((chunk.get("message") or {}).get("content") or "")
            if chunk.get("done"):
                break
    return "".join(text).strip()


async def _plain_summary(http, model, window, old_summary, fold, words) -> str:
    prompt = (f"You maintain a running summary of an ongoing conversation.\n\nEXISTING SUMMARY:\n"
              f"{old_summary or '(none)'}\n\nDIALOGUE TO FOLD IN:\n{transcript(fold)}\n\n"
              f"Merge everything into ONE updated summary (at most {words} words). Keep decisions, facts, names, "
              "numbers, code and file references, the user's preferences and open questions. "
              "Output ONLY the summary text.")
    return (await model_providers.complete_chat(
        http, model, prompt, temperature=0.2, num_ctx=window, num_predict=words * 2 + 200,
        timeout=900, ollama_url=config.OLLAMA_URL)).strip()


async def _helper_summary(http, old_summary, fold, ids, words):
    """Cloud chats: the local helper model folds the dialogue in passes that fit its small window.
    Returns (summary, how many leading messages were folded)."""
    model = model_providers.reject_cloud(getattr(config, "WORKSPACE_MODEL", "") or "") \
        or model_providers.reject_cloud(getattr(config, "DEFAULT_MODEL", "") or "")
    if not model:
        return "", 0
    window = context_policy.helper_context("compaction")
    room = max(4000, int(window * 3 * 0.55))          # characters of dialogue per pass
    summary, done = old_summary, 0
    for _ in range(HELPER_PASSES):
        if done >= len(fold):
            break
        taken, size = [], 0
        for message in fold[done:]:
            length = len(message.get("content") or "") + 12
            if taken and size + length > room:
                break
            taken.append(message)
            size += length
        prompt = (f"You maintain a running summary of an ongoing conversation.\n\nEXISTING SUMMARY:\n"
                  f"{summary or '(none)'}\n\nNEW DIALOGUE TO FOLD IN:\n{transcript(taken, room)}\n\n"
                  f"Merge everything into ONE updated summary (at most {min(words, 600)} words). Keep decisions, "
                  "facts, names, numbers, code and file references, and open questions. Output ONLY the summary text.")
        merged = (await model_providers.complete_chat(
            http, model, prompt, temperature=0.2, num_ctx=window, num_predict=1400, timeout=120,
            ollama_url=config.OLLAMA_URL)).strip()
        if not merged:
            break
        summary, done = merged, done + len(taken)
    # A cut is only useful on a message the server can find again.
    while done and not message_id(ids[done - 1]):
        done -= 1
    return (summary if done else ""), done


async def _waiting(task):
    """Yield while ``task`` runs so the caller can keep the HTTP stream alive."""
    while not task.done():
        await asyncio.wait({task}, timeout=KEEPALIVE_SECONDS)
        if not task.done():
            yield None


async def compact(*, http, events, conv_id, messages, history, model, options, tools=None, cloud=False,
                  render=None, previous=None, force=False, new_message=None):
    """Apply the stored summary and, when the chat is near the window, summarize more of it.

    An async generator of steps: ``{"keepalive": True}`` while the summary is written, then exactly one
    ``{"messages": [...], "history": [...], "event": {...} | None}``. ``history`` is what is left of the
    sent history in the returned list; the caller keeps it, or a later call cannot find the history
    again. Any failure returns the request without the new summary: the chat continues uncompacted.
    ``new_message`` is the user's own text, when the last message also carries one-turn excerpts.
    """
    unchanged = {"messages": messages, "history": list(history or []), "event": None}
    try:
        window = int(options.get("num_ctx") or 0)
        place = region(messages, history) if conv_id and enabled() and window > 0 else None
        if not place:
            yield unchanged
            return
        start, end = place
        real = await known_ids(conv_id)
        ids = [identifier if message_id(identifier) in real else None for _, identifier in history]
        old_summary, cursor = await db.get_conversation_summary(conv_id)
        has_summary = bool((old_summary or "").strip())
        covered = summarized_count(ids, int(cursor or 0)) if has_summary else 0
        # An earlier call in this request may already have put the summary block in front of the history.
        lead = list(messages[:start])
        if lead and (lead[-1].get("content") or "").startswith(SUMMARY_MARK):
            lead.pop()
        # A stored summary is always sent, also when the browser sent nothing from before the cut:
        # dropping it there lost the whole earlier chat, and the next compaction overwrote it.
        head = lead + ([summary_block(old_summary, render)] if has_summary else [])
        body, body_ids, tail = list(messages[start + covered:end]), ids[covered:], list(messages[end:])
        rest = list(history)[covered:]
        applied = {"messages": head + body + tail, "history": rest, "event": None}

        ratio = ratio_of(float((previous or {}).get("ratio") or 0), 1.0, DEFAULT_RATIO)
        if previous and previous.get("used"):
            latest = new_message if new_message is not None else (body[-1].get("content") or "")
            used = int(previous["used"]) + round(estimate(latest) * ratio)
        else:
            used = round(payload_estimate(head + body, tools) * ratio)
        if not force and used < compaction_limit(window):
            yield applied
            return

        words = summary_words(window)
        fixed = round(payload_estimate(lead, tools) * ratio)
        keep = keep_start(body, keep_budget(window, fixed, words, force), ratio)
        while keep and not message_id(body_ids[keep - 1]):     # the cut must be findable next time
            keep -= 1
        fold = body[:keep]
        fold_tokens = round(payload_estimate(fold) * ratio) if fold else 0
        # Summarizing a couple of short messages frees nothing. When the system prompt and tools alone
        # are over the limit (3,554 tokens in a 4,096 window, live 2026-11-13) this ran a model call on
        # every turn for no gain; the window is simply too small, and the context pill says so.
        if fold_tokens < max(MIN_FOLD_TOKENS, window * MIN_FOLD_SHARE):
            yield applied
            return
        answer = words * 2 + 300                                   # the summary itself, plus the instruction
        fits_in_place = round(payload_estimate(head + fold, tools) * ratio) + answer <= window
        fits_plain = fold_tokens + estimate(old_summary or "") * ratio + answer <= window
        if not (fits_in_place or fits_plain or cloud):
            yield applied
            return

        await events.emit(conv_id, "tool_start", {"tool": "compaction", "icon": "layers",
                                                  "status": "Summarizing earlier messages"})
        folded = len(fold)

        async def write():
            if cloud:
                return await _helper_summary(http, old_summary if has_summary else "", fold, body_ids[:keep], words)
            text = ""
            if fits_in_place:
                try:
                    text = await _chat_model_summary(http, model, options, tools, head + fold, words)
                except Exception as error:
                    print(f"[CONTEXT] in-place summary failed, retrying as a plain prompt: {error}")
            if not text and fits_plain:
                text = await _plain_summary(http, model, window, old_summary if has_summary else "", fold, words)
            return text, len(fold)

        task = asyncio.ensure_future(write())
        try:
            async for _ in _waiting(task):
                yield {"keepalive": True}
            summary, folded = task.result()
        except BaseException:
            task.cancel()
            raise
        if not summary or not folded:
            await events.emit(conv_id, "tool_end", {"tool": "compaction", "icon": "layers",
                                                    "status": "Could not summarize; continuing without"})
            yield applied
            return
        new_cursor = max(message_id(identifier) for identifier in body_ids[:folded])
        await db.set_conversation_summary(conv_id, summary, new_cursor)
        freed = max(0, round((payload_estimate(fold[:folded]) - estimate(summary)) * ratio))
        count = covered + folded
        await events.emit(conv_id, "tool_end", {
            "tool": "compaction", "icon": "layers",
            "status": f"Summarized {count} earlier message{'s' if count != 1 else ''}"})
        print(f"[CONTEXT] compacted conv={conv_id}: {count} messages summarized, about {freed} tokens freed "
              f"(used {used} of {window})")
        yield {"messages": lead + [summary_block(summary, render)] + body[folded:] + tail, "history": rest[folded:],
               "event": {"type": "context_compacted", "summarized": count, "cursor": new_cursor, "freed": freed}}
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"[CONTEXT] compaction failed, continuing uncompacted: {error}")
        yield unchanged


def shorten_tool_results(messages, end: int, keep=None) -> int:
    """Cut tool results before index ``end`` to a stub. Returns the characters removed."""
    removed = 0
    for message in messages[:max(0, end)]:
        content = message.get("content") or ""
        if message.get("role") != "tool" or len(content) <= RELIEF_KEEP_CHARS * 2 or (keep and keep(message)):
            continue
        stub = content[:RELIEF_KEEP_CHARS] + f"\n\n[... {len(content)} chars cut to free context ...]"
        removed += len(content) - len(stub)
        message["content"] = stub
    return removed


async def relieve(*, http, events, conv_id, messages, history, model, options, tools=None, cloud=False,
                  render=None, used: int, round_start: int, ratio: float = 1.0, keep=None,
                  share: float = 0.92):
    """Make room in the middle of a reply. Steps like ``compact``; the last one is
    ``{"messages": [...], "history": [...], "freed": tokens, "event": {...} | None}``."""
    window = int(options.get("num_ctx") or 0)
    working = freed = round(shorten_tool_results(messages, round_start, keep) / 3 * ratio)
    event = None
    if enabled() and window and used - freed >= window * share:
        async for step in compact(http=http, events=events, conv_id=conv_id, messages=messages,
                                  history=history, model=model, options=options, tools=tools, cloud=cloud,
                                  render=render, force=True):
            if step.get("keepalive"):
                yield step
                continue
            messages, history, event = step["messages"], step["history"], step["event"]
            freed += int((event or {}).get("freed") or 0)
    # ``freed`` is the room made in the window; only ``freed_history`` came out of the conversation.
    if freed and not event:
        event = {"type": "context_compacted", "summarized": 0, "freed": freed, "freed_history": 0, "freed_working": working}
    elif event:
        event = {**event, "freed": freed, "freed_history": max(0, freed - working), "freed_working": working}
    yield {"messages": messages, "history": list(history or []), "freed": freed, "event": event}
