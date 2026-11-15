"""Offline tests for chat_context: the per-conversation context reading and compaction.

The database, the event bus and every model call are faked.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from .optional_deps import HAS_AIOSQLITE, install_aiosqlite_stub

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if not HAS_AIOSQLITE:
    install_aiosqlite_stub()

import chat_context  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Events:
    def __init__(self):
        self.sent = []

    async def emit(self, conv_id, kind, data):
        self.sent.append((kind, data.get("status")))


def dialogue(count, chars=600, first_id=101):
    """count alternating user/assistant messages with database ids, plus the (message, id) pairs."""
    messages = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"m{index} " + "z" * chars}
                for index in range(count)]
    return messages, [(message, first_id + index) for index, message in enumerate(messages)]


def wire(monkeypatch, *, summary="", cursor=0, reply="THE NEW SUMMARY", threshold=85, on=True):
    state = {"saved": None, "calls": [], "plain": 0}
    monkeypatch.setattr(chat_context.config, "CONTEXT_COMPACTION", "on" if on else "off", raising=False)
    monkeypatch.setattr(chat_context.config, "OLLAMA_URL", "http://ollama.invalid", raising=False)
    monkeypatch.setattr(chat_context.config, "CHAT_COMPACTION_PERCENT", threshold, raising=False)

    async def get_summary(conv_id):
        return (state["saved"][1], state["saved"][2]) if state["saved"] else (summary, cursor)

    async def set_summary(conv_id, text, until):
        state["saved"] = (conv_id, text, until)

    async def in_place(http, model, options, tools, prefix, words):
        state["calls"].append({"prefix": list(prefix), "tools": tools, "words": words})
        return reply

    async def plain(http, model, window, old_summary, fold, words):
        state["plain"] += 1
        return reply

    async def known(conv_id):
        return set(range(1, 1000))

    monkeypatch.setattr(chat_context, "known_ids", known)
    monkeypatch.setattr(chat_context.db, "get_conversation_summary", get_summary)
    monkeypatch.setattr(chat_context.db, "set_conversation_summary", set_summary)
    monkeypatch.setattr(chat_context, "_chat_model_summary", in_place)
    monkeypatch.setattr(chat_context, "_plain_summary", plain)
    return state


def compact(messages, history, *, window=8000, previous=None, events=None, **extra):
    async def collect():
        steps = [step async for step in chat_context.compact(
            http=None, events=events or Events(), conv_id="conv-1", messages=messages, history=history,
            model="chat-model", options={"num_ctx": window}, previous=previous, **extra)]
        return steps[-1]
    return run(collect())


# ── The reading ──

def test_thinking_tool_results_and_search_excerpts_are_working_not_conversation():
    # Live 2026-11-13: the pill showed prompt + everything generated and fell after each reply, because
    # thinking and this turn's tool results are not sent again.
    meter = chat_context.TurnMeter()
    meter.info({"round": 0, "window": 32768, "prompt_estimate": 6000, "turn_only": 900})
    meter.usage({"round": 0, "prompt_tokens": 4000, "gen_tokens": 1500})       # 1500 = thinking + a tool call
    meter.usage({"round": 1, "prompt_tokens": 7000, "gen_tokens": 300})        # the tool result came back
    answer = "The file has 6 rows. " * 10
    reading = meter.reading(answer)
    ratio = 4000 / 6000
    assert reading["used"] == 4000 - round(900 * ratio) + round(chat_context.estimate(answer) * ratio)
    assert reading["working_peak"] == 7300 - reading["used"]
    assert (reading["window"], reading["measured"]) == (32768, True)


def test_before_the_first_real_count_the_reading_is_an_estimate():
    meter = chat_context.TurnMeter()
    meter.info({"round": 0, "window": 16384, "prompt_estimate": 3000, "turn_only": 0, "ratio": 0.7})
    update = meter.live({"type": "ctx_update", "prompt_tokens": 0, "gen_tokens": 40}, "")
    assert (update["used"], update["working"], update["measured"], update["window"]) == (2100, 40, False, 16384)


def test_the_loops_rough_guesses_never_replace_a_real_count():
    # chars/4 guesses during tool execution used to overwrite Ollama's count between rounds.
    meter = chat_context.TurnMeter()
    meter.info({"round": 0, "window": 16384, "prompt_estimate": 3000})
    meter.usage({"round": 0, "prompt_tokens": 2500, "gen_tokens": 100})
    guess = meter.live({"type": "ctx_update", "prompt_tokens": 900, "gen_tokens": 0, "live": True}, "")
    # The 100 tokens that call wrote are still in the window until the next request.
    assert guess["used"] == 2500 and guess["working"] == 100
    real = meter.live({"type": "ctx_update", "prompt_tokens": 3900, "gen_tokens": 50}, "")
    assert real["used"] == 2500 and real["working"] == 1450


def test_continued_text_is_not_counted_twice():
    meter = chat_context.TurnMeter()
    meter.begin("First half. ")
    meter.info({"round": 0, "window": 8192, "prompt_estimate": 1000})
    meter.usage({"round": 0, "prompt_tokens": 1000, "gen_tokens": 20})
    assert meter.kept("First half. ") == 1000
    assert meter.kept("First half. Second half.") == 1000 + chat_context.estimate("Second half.")


def test_a_turn_without_a_model_call_saves_no_reading():
    assert chat_context.TurnMeter().reading("A receipt, not a model reply.") is None


def test_compaction_in_the_middle_of_a_reply_lowers_the_conversation():
    meter = chat_context.TurnMeter()
    meter.info({"round": 0, "window": 8192, "prompt_estimate": 6000})
    meter.usage({"round": 0, "prompt_tokens": 6000, "gen_tokens": 100})
    meter.compacted({"type": "context_compacted", "summarized": 4, "freed": 3500})
    assert meter.kept("") == 2500


def test_the_ratio_is_kept_within_bounds():
    assert chat_context.ratio_of(100, 10000) == chat_context.RATIO_MIN
    assert chat_context.ratio_of(0, 10, fallback=0.8) == 0.8


def test_image_data_is_not_counted_as_text():
    plain = chat_context.payload_estimate([{"role": "user", "content": "look"}])
    assert chat_context.payload_estimate([{"role": "user", "content": "look", "images": ["A" * 500000]}]) == plain


# ── Finding the history and the cut ──

def test_the_history_is_found_after_hints_are_inserted_in_front():
    messages, history = dialogue(4)
    outgoing = [{"role": "system", "content": "tools"}, {"role": "system", "content": "memory"}, *messages]
    assert chat_context.region(outgoing, history) == (2, 6)
    assert chat_context.region([dict(message) for message in outgoing], history) is None     # copies are not it
    assert chat_context.region([messages[1], messages[0]], history[:2]) is None


def test_the_stored_cut_never_swallows_the_newest_message():
    assert chat_context.summarized_count([101, 102, None, 104], 102) == 2
    assert chat_context.summarized_count([101, 102, 103], 999) == 2
    assert chat_context.summarized_count([None, None], 5) == 0
    assert not chat_context.has_ids([({}, None), ({}, "temp-3")])


def test_the_kept_part_opens_on_a_user_message_and_fits_the_budget():
    messages, _ = dialogue(9, chars=300)              # user … user; each about 100 estimated tokens
    start = chat_context.keep_start(messages, budget=350, ratio=1.0)
    assert messages[start]["role"] == "user" and start == 6
    assert chat_context.keep_start(messages, budget=10 ** 9, ratio=1.0) == 0
    assert chat_context.keep_start(messages, budget=1, ratio=1.0) == 8        # always keeps the newest message


# ── Compaction ──

def test_below_the_threshold_nothing_is_summarized(monkeypatch):
    state = wire(monkeypatch)
    messages, history = dialogue(7)
    result = compact(messages, history, window=100000)
    assert result["messages"] == messages and result["event"] is None and not state["calls"]


def test_over_the_threshold_everything_older_is_summarized_and_nothing_is_dropped(monkeypatch):
    # The old path folded 24,000 characters, then sent the summary plus the last 8 messages: with a
    # large window most of the chat was dropped without ever being summarized.
    state = wire(monkeypatch)
    lead = {"role": "system", "content": "You are helpful."}
    messages, history = dialogue(21, chars=900)        # about 300 estimated tokens each
    events = Events()
    result = compact([lead, *messages], history, window=8000, previous={"used": 6500, "ratio": 1.0}, events=events)
    out = result["messages"]
    assert out[0] is lead and out[1]["content"].startswith(chat_context.SUMMARY_MARK)
    kept = out[2:]
    assert kept[0]["role"] == "user" and kept[-1] is messages[-1]
    folded = state["calls"][0]["prefix"][1:]            # what the model was asked to summarize
    assert [*folded, *kept] == messages                 # every message is either summarized or sent
    assert state["saved"] == ("conv-1", "THE NEW SUMMARY", 100 + len(folded))
    assert result["event"]["type"] == "context_compacted" and result["event"]["summarized"] == len(folded)
    assert result["event"]["freed"] > 0
    assert [kind for kind, _ in events.sent] == ["tool_start", "tool_end"]


def test_the_summary_is_reused_until_the_threshold_is_crossed_again(monkeypatch):
    # The old path re-summarized on every message once it had compacted, which also threw away
    # Ollama's prompt cache every turn.
    state = wire(monkeypatch, summary="EARLIER SUMMARY", cursor=110)
    messages, history = dialogue(14)                    # ids 101..114
    result = compact(messages, history, window=8000, previous={"used": 2000, "ratio": 1.0})
    assert not state["calls"] and state["saved"] is None
    out = result["messages"]
    assert out[0]["content"] == f"{chat_context.SUMMARY_MARK}\nEARLIER SUMMARY"
    assert out[1:] == messages[10:]                     # everything after the cut, word for word


def test_a_second_compaction_merges_the_earlier_summary(monkeypatch):
    state = wire(monkeypatch, summary="EARLIER SUMMARY", cursor=104)
    messages, history = dialogue(21, chars=900)
    result = compact(messages, history, window=8000, previous={"used": 7000, "ratio": 1.0})
    prefix = state["calls"][0]["prefix"]
    assert prefix[0]["content"].endswith("EARLIER SUMMARY") and prefix[1] is messages[4]
    assert state["saved"][2] > 104 and result["event"]["summarized"] == 4 + len(prefix) - 1


def test_a_failed_summary_leaves_the_chat_uncompacted(monkeypatch):
    state = wire(monkeypatch, reply="")
    messages, history = dialogue(21, chars=900)
    events = Events()
    result = compact(messages, history, window=8000, previous={"used": 7000, "ratio": 1.0}, events=events)
    assert result["messages"] == messages and result["event"] is None and state["saved"] is None
    assert state["plain"] == 1                          # the plain prompt was tried after the in-place one
    assert events.sent[-1][1].startswith("Could not summarize")


def test_switched_off_or_without_ids_it_does_nothing(monkeypatch):
    wire(monkeypatch, on=False)
    messages, history = dialogue(21, chars=900)
    assert compact(messages, history, previous={"used": 7900})["messages"] is messages
    wire(monkeypatch)
    unknown = [(dict(message), identifier) for message, identifier in history]      # objects not in the request
    assert compact(messages, unknown, previous={"used": 7900})["messages"] is messages


def test_a_database_error_leaves_the_request_as_it_was(monkeypatch):
    wire(monkeypatch)

    async def broken(conv_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(chat_context.db, "get_conversation_summary", broken)
    messages, history = dialogue(9)
    assert compact(messages, history, previous={"used": 7900})["messages"] is messages


def test_the_cut_lands_on_a_message_the_server_can_find_again(monkeypatch):
    state = wire(monkeypatch)
    messages, history = dialogue(21, chars=900)
    history = [(message, identifier if message["role"] == "assistant" else None) for message, identifier in history]
    compact(messages, history, window=8000, previous={"used": 7000, "ratio": 1.0})
    folded = state["calls"][0]["prefix"]
    assert folded[-1]["role"] == "assistant" and state["saved"][2] == 100 + len(folded)


def test_cloud_chats_use_the_local_helper_and_only_cut_what_it_folded(monkeypatch):
    wire(monkeypatch)
    monkeypatch.setattr(chat_context.config, "WORKSPACE_MODEL", "small:3b", raising=False)
    monkeypatch.setattr(chat_context.context_policy, "helper_context", lambda profile: 4096)
    monkeypatch.setattr(chat_context, "HELPER_PASSES", 2)
    prompts = []

    async def complete(http, model, prompt, **options):
        prompts.append((model, prompt))
        return f"SUMMARY {len(prompts)}"

    monkeypatch.setattr(chat_context.model_providers, "complete_chat", complete)
    monkeypatch.setattr(chat_context.model_providers, "reject_cloud", lambda model: model)
    messages, history = dialogue(41, chars=3000)
    result = compact(messages, history, window=64000, previous={"used": 60000, "ratio": 1.0}, cloud=True)
    assert [model for model, _ in prompts] == ["small:3b", "small:3b"]
    assert "SUMMARY 1" in prompts[1][1]                              # the second pass builds on the first
    out = result["messages"]
    assert out[0]["content"].endswith("SUMMARY 2")
    assert out[1:] == messages[result["event"]["summarized"]:]       # what two passes did not reach stays


def test_the_in_place_summary_reuses_the_prompt_prefix(monkeypatch):
    monkeypatch.setattr(chat_context.config, "OLLAMA_URL", "http://ollama.invalid", raising=False)
    seen = {}

    class Response:
        status_code = 200

        async def aiter_lines(self):
            yield json.dumps({"message": {"content": "Summary "}})
            yield json.dumps({"message": {"content": "text."}, "done": True})

    class Stream:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *error):
            return False

    class Http:
        def stream(self, method, url, json=None, timeout=None):
            seen.update(url=url, payload=json)
            return Stream()

    prefix = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "execute_code"}}]
    text = run(chat_context._chat_model_summary(Http(), "chat-model", {"num_ctx": 32768, "temperature": 0.9}, tools, prefix, 500))
    payload = seen["payload"]
    assert text == "Summary text." and seen["url"].endswith("/api/chat")
    assert payload["messages"][:2] == prefix and "at most 500 words" in payload["messages"][2]["content"].lower()
    assert payload["tools"] == tools and payload["think"] is False and payload["stream"] is True
    assert payload["options"]["num_ctx"] == 32768 and payload["options"]["temperature"] == 0.2


# ── Relief in the middle of a reply ──

def relieve(messages, history, **options):
    async def collect():
        steps = [step async for step in chat_context.relieve(
            http=None, events=Events(), conv_id="conv-1", messages=messages, history=history,
            model="chat-model", **options)]
        return steps[-1]
    return run(collect())


def test_earlier_tool_results_are_shortened_first(monkeypatch):
    state = wire(monkeypatch)
    messages, history = dialogue(3)
    turn = [{"role": "assistant", "content": ""}, {"role": "tool", "content": "A" * 9000},
            {"role": "tool", "content": "## SKILL x\n" + "B" * 9000},
            {"role": "assistant", "content": ""}, {"role": "tool", "content": "C" * 9000}]
    outgoing = [*messages, *turn]
    result = relieve(outgoing, history, options={"num_ctx": 8000}, used=7600, round_start=6,
                     keep=lambda message: message["content"].startswith("## SKILL"))
    assert len(turn[1]["content"]) < 300 and len(turn[2]["content"]) > 9000 and len(turn[4]["content"]) == 9000
    assert result["freed"] > 2500 and result["event"] == {
        "type": "context_compacted", "summarized": 0, "freed": result["freed"],
        "freed_history": 0, "freed_working": result["freed"]}
    assert not state["calls"]                           # enough room without touching the history


def test_when_that_is_not_enough_the_history_is_summarized(monkeypatch):
    state = wire(monkeypatch)
    messages, history = dialogue(21, chars=900)
    turn = [{"role": "assistant", "content": ""}, {"role": "tool", "content": "D" * 600}]
    result = relieve([*messages, *turn], history, options={"num_ctx": 8000}, used=7900, round_start=23)
    assert state["calls"] and result["event"]["summarized"] > 0 and result["freed"] > 0
    out = result["messages"]
    assert out[0]["content"].startswith(chat_context.SUMMARY_MARK) and out[-2:] == turn
    assert messages[-1] in out                          # the message being answered is still there


def test_nothing_to_free_reports_nothing(monkeypatch):
    wire(monkeypatch, on=False)
    messages, history = dialogue(3)
    result = relieve(messages, history, options={"num_ctx": 8000}, used=7900, round_start=3)
    assert (result["messages"], result["freed"], result["event"]) == (messages, 0, None)


def test_an_id_the_conversation_does_not_have_is_never_stored_as_the_cut(monkeypatch):
    state = wire(monkeypatch)

    async def known(conv_id):
        return {101, 102, 103, 104}

    monkeypatch.setattr(chat_context, "known_ids", known)
    messages, history = dialogue(21, chars=900)
    history = [(message, identifier if identifier <= 104 else 1794556000000 + identifier) for message, identifier in history]
    compact(messages, history, window=8000, previous={"used": 7000, "ratio": 1.0})
    assert state["saved"][2] == 104 and len(state["calls"][0]["prefix"]) == 4


def test_a_small_window_keeps_less_so_compaction_is_not_needed_again_at_once():
    # Measured 2026-11-13 with an 8K window and 3,300 tokens of system prompt and tools.
    assert chat_context.keep_budget(8192, fixed=3300, words=350) == round(8192 * chat_context.MIN_KEEP_SHARE)
    assert chat_context.keep_budget(32768, fixed=3300, words=546) == round(32768 * chat_context.KEEP_SHARE)
    assert chat_context.keep_budget(12288, fixed=3300, words=350) == round(12288 * 0.5 - 3300 - 525)
    assert chat_context.keep_budget(8192, fixed=3300, words=350, forced=True) == round(8192 * chat_context.FORCED_KEEP_SHARE)


# ── When compaction fires (Settings: 85 / 90 / 95 / 98 / 99 %) ──

@pytest.mark.parametrize("window, expected", [
    (262144, [222822, 235930, 249037, 256901, 258048]),     # the last one is capped: room for a reply
    (32768, [27853, 28672, 28672, 28672, 28672]),
    (8192, [6144, 6144, 6144, 6144, 6144]),
])
def test_the_limit_is_the_chosen_share_but_always_leaves_room_for_a_reply(window, expected):
    assert [chat_context.compaction_limit(window, percent) for percent in chat_context.COMPACT_CHOICES] == expected


def test_only_the_five_choices_exist(monkeypatch):
    assert [chat_context.compact_percent(value) for value in (75, 85, 87, 88, "95", 96.4, 97, 100, 5, "junk", None)] == \
        [85, 85, 85, 90, 95, 95, 98, 99, 85, 85, 85]
    monkeypatch.setattr(chat_context.config, "CHAT_COMPACTION_PERCENT", "98", raising=False)
    assert chat_context.compact_percent() == 98 and chat_context.compaction_limit(262144) == 256901


def test_the_daedalus_threshold_no_longer_moves_chats(monkeypatch):
    # Chats used to borrow context_compaction_threshold, the coding jobs' "Compact at (%)".
    state = wire(monkeypatch, threshold=95)
    monkeypatch.setattr(chat_context.context_policy, "runtime_settings", lambda: {"context_compaction_threshold": 10})
    messages, history = dialogue(21, chars=15000)       # about 5,000 estimated tokens each
    result = compact(messages, history, window=100000, previous={"used": 80000, "ratio": 1.0})
    assert result["event"] is None and not state["calls"]            # 80,000 + the new message is under 95,000
    result = compact(messages, history, window=100000, previous={"used": 95500, "ratio": 1.0})
    assert result["event"] is not None


def test_a_tool_run_makes_room_no_earlier_than_the_setting():
    assert chat_context.full_share(262144, 85) == chat_context.MIN_FULL_SHARE
    assert chat_context.full_share(262144, 99) == 258048 / 262144
    assert chat_context.full_share(32768, 99) == chat_context.MIN_FULL_SHARE     # the cap is below 92 % here
    assert chat_context.full_share(0) == chat_context.MIN_FULL_SHARE


# ── Stage 2 of the chat bug-fix pass (2026-11-14) ──

def test_a_stored_summary_is_sent_even_when_nothing_from_before_the_cut_was(monkeypatch):
    # The browser trims its own oldest messages. Once every message at or before the cut was gone the
    # server stopped sending the summary, and the next compaction overwrote it without merging.
    state = wire(monkeypatch, summary="EARLIER SUMMARY", cursor=50)
    messages, history = dialogue(6)                                  # ids 101..106, all after the cut
    result = compact(messages, history, window=100000, previous={"used": 2000, "ratio": 1.0})
    assert result["messages"][0]["content"].endswith("EARLIER SUMMARY") and result["messages"][1:] == messages
    assert not state["calls"]


def test_folding_a_couple_of_short_messages_is_not_worth_a_model_call(monkeypatch):
    # Live 2026-11-13: 3,554 tokens of system prompt and tools in a 4,096 window. The chat was over the
    # limit from its first message and compaction ran on every turn to free about 170 tokens.
    state = wire(monkeypatch)
    lead = {"role": "system", "content": "s" * 9000}                 # the fixed prompt is most of the window
    messages, history = dialogue(5, chars=300)
    events = Events()
    result = compact([lead, *messages], history, window=4096, previous={"used": 3950, "ratio": 1.0}, events=events)
    assert not state["calls"] and state["plain"] == 0 and not events.sent
    assert result["messages"] == [lead, *messages] and result["event"] is None


def test_a_summary_that_cannot_fit_beside_the_prompt_is_written_from_the_dialogue_alone(monkeypatch):
    state = wire(monkeypatch)
    lead = {"role": "system", "content": "s" * 16000}                # about 5,300 tokens
    messages, history = dialogue(13, chars=1500)                     # about 500 tokens each
    compact([lead, *messages], history, window=8000, previous={"used": 7900, "ratio": 1.0})
    assert not state["calls"] and state["plain"] == 1 and state["saved"]


def test_the_history_that_is_left_can_be_found_and_compacted_again(monkeypatch):
    # Recovery in the middle of a reply could never summarize a chat that had been compacted before:
    # the summarized messages were gone from the list, so the history was "not found".
    state = wire(monkeypatch)
    lead = {"role": "system", "content": "You are helpful."}
    messages, history = dialogue(21, chars=900)
    first = compact([lead, *messages], history, window=8000, previous={"used": 6500, "ratio": 1.0})
    assert first["event"] and chat_context.region(first["messages"], first["history"])
    kept = len(first["history"])
    turn = [{"role": "assistant", "content": ""}, {"role": "tool", "content": "t" * 300}]
    second = compact([*first["messages"], *turn], first["history"], window=8000, force=True)
    blocks = [m for m in second["messages"] if (m.get("content") or "").startswith(chat_context.SUMMARY_MARK)]
    assert len(blocks) == 1 and second["messages"][-2:] == turn
    assert len(second["history"]) <= kept and second["messages"][0] is lead
    assert len(state["calls"]) >= 1


def test_one_turn_excerpts_do_not_count_toward_the_trigger(monkeypatch):
    state = wire(monkeypatch)
    messages, history = dialogue(21, chars=900)
    messages[-1]["content"] = "short question\n\n" + "search excerpt " * 3000        # about 15,000 tokens
    quiet = compact(messages, history, window=100000, previous={"used": 80000, "ratio": 1.0}, new_message="short question")
    assert quiet["event"] is None and not state["calls"]


def test_the_reading_carries_the_fixed_cost_of_every_request():
    meter = chat_context.TurnMeter()
    meter.info({"round": 0, "window": 4096, "prompt_estimate": 5000, "fixed": 4500})
    meter.usage({"round": 0, "prompt_tokens": 3700, "gen_tokens": 100})
    assert meter.reading("ok")["fixed"] == round(4500 * chat_context.DEFAULT_RATIO)      # not dragged down by prose
    assert meter.live({"type": "ctx_update", "prompt_tokens": 0, "gen_tokens": 5}, "")["fixed"] == 3600
    early = chat_context.TurnMeter()
    early.info({"round": 0, "window": 4096, "prompt_estimate": 4130, "fixed": 4100})
    early.usage({"round": 0, "prompt_tokens": 3554, "gen_tokens": 50})
    assert early.reading("ok")["fixed"] == round(4100 * 3554 / 4130)                      # a higher real ratio is used


def test_the_fixed_cost_is_never_more_than_the_request_it_is_part_of():
    # Seen live 2026-11-14 after the stage 2 deploy: fixed 3,971 shown for a chat holding 3,887 tokens.
    meter = chat_context.TurnMeter()
    meter.info({"round": 0, "window": 131072, "prompt_estimate": 5000, "fixed": 4964})
    meter.usage({"round": 0, "prompt_tokens": 3880, "gen_tokens": 20})
    assert meter.reading("ok")["fixed"] == 3880


# ── The reading while tools run (2026-11-15) ──

def test_the_reading_does_not_fall_while_a_tool_runs():
    # Live 2026-11-14: between two model calls the loop sent whole-prompt guesses that were ignored and a
    # generated count of 0, so the figure dropped by the last call's output each time a tool started.
    meter = chat_context.TurnMeter()
    meter.info({"round": 0, "window": 32768, "prompt_estimate": 5000})
    meter.live({"type": "ctx_update", "prompt_tokens": 4000, "gen_tokens": 600}, "")     # the call's real counts
    before = meter.live({"type": "ctx_update", "prompt_tokens": 4000, "gen_tokens": 600}, "")["total"]
    running = meter.live(chat_context.tool_phase([{"role": "assistant", "content": "x"}], 0), "")
    assert before == 4600 and running["total"] == 4600 and running["working"] == 600


def test_tool_results_count_as_soon_as_they_arrive():
    meter = chat_context.TurnMeter()
    meter.info({"round": 0, "window": 32768, "prompt_estimate": 5000})
    meter.usage({"prompt_tokens": 4000, "gen_tokens": 600})
    turn = [{"role": "assistant", "content": "", "tool_calls": [{}]}, {"role": "tool", "content": "R" * 6000}]
    arrived = meter.live(chat_context.tool_phase(turn, 0), "")
    assert arrived["total"] == 4600 + round(chat_context.estimate({"messages": turn[1:]}) * 0.8)
    assert arrived["total"] > 6000 and arrived["used"] == 4000
    # A result that is back but not in the list yet counts too.
    waiting = meter.live(chat_context.tool_phase(turn[:1], 0, done_chars=6000), "")
    assert waiting["total"] == 4600 + round(2000 * 0.8)
    # The next request is predicted from the real count plus what was added, not re-estimated whole.
    meter.info({"round": 1, "window": 32768, "prompt_estimate": 5000 + 2600})
    predicted = meter.live({"type": "ctx_update", "prompt_tokens": 0, "gen_tokens": 0, "live": True}, "")
    assert predicted["total"] == 4000 + round(2600 * 0.8)


def test_shortened_tool_results_do_not_shrink_the_conversation():
    # Making room mid-reply cuts this reply's own tool results. They were never conversation.
    meter = chat_context.TurnMeter()
    meter.info({"round": 0, "window": 8192, "prompt_estimate": 6000})
    meter.usage({"prompt_tokens": 6000, "gen_tokens": 100})
    meter.compacted({"type": "context_compacted", "summarized": 0, "freed": 2000, "freed_history": 0, "freed_working": 2000})
    assert meter.kept("") == 6000
    meter.compacted({"type": "context_compacted", "summarized": 3, "freed": 3000, "freed_history": 1000, "freed_working": 2000})
    assert meter.kept("") == 5000
    meter.compacted({"type": "context_compacted", "summarized": 0, "freed": 900})          # an older server's event
    assert meter.kept("") == 5000


def test_the_reading_carries_what_fills_the_window():
    parts = {"instructions": 1500, "tools": 1500, "memory": 300, "skills": 200, "summary": 0, "messages": 2000,
             "excerpts": 900, "tool_calls": 0}
    meter = chat_context.TurnMeter()
    meter.info({"round": 0, "window": 32768, "prompt_estimate": 6400, "turn_only": 900, "parts": parts})
    live = meter.live({"type": "ctx_update", "prompt_tokens": 5120, "gen_tokens": 300}, "")
    assert sum(live["parts"].values()) == live["total"] == 5420 and live["parts"]["reply"] == 300
    meter.usage({"round": 0, "prompt_tokens": 5120, "gen_tokens": 300})
    later = dict(parts, tool_calls=2500)
    meter.info({"round": 1, "window": 32768, "prompt_estimate": 8900, "parts": later})
    meter.usage({"round": 1, "prompt_tokens": 7100, "gen_tokens": 400})
    reading = meter.reading("The answer. " * 20)
    assert sum(reading["parts"].values()) == reading["used"]
    assert set(reading["parts"]) == {"instructions", "tools", "memory", "skills", "summary", "messages"}
    assert reading["peak"]["total"] == 7500 and sum(reading["peak"]["parts"].values()) == 7500
    assert reading["peak"]["parts"]["tool_calls"] > 1500 and reading["peak"]["parts"]["reply"] == 400
    assert reading["fixed"] == sum(reading["parts"][name] for name in ("instructions", "tools", "memory", "skills"))


def test_a_request_without_a_breakdown_still_gets_a_reading():
    meter = chat_context.TurnMeter()
    meter.info({"round": 0, "window": 32768, "prompt_estimate": 5000, "parts": None})
    update = meter.live({"type": "ctx_update", "prompt_tokens": 4000, "gen_tokens": 100}, "")
    assert update["total"] == 4100 and "parts" not in update
    reading = meter.reading("Answer.")
    assert reading["used"] > 4000 and "parts" not in reading and "peak" not in reading
