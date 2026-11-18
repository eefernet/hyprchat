"""What fills the context window, by category (context_breakdown.py)."""
import chat_context
import context_breakdown as breakdown

TOOLS = [{"type": "function", "function": {"name": "execute_code", "description": "Run code. " * 40, "parameters": {}}}]


def request():
    """A request the way the chat loop assembles it, with the ledger told about each named block."""
    ledger = breakdown.Ledger()
    skills, memory, knowledge, search = "## Skills\n- resize-images\n" * 6, "User likes short answers. " * 12, "[1] An excerpt. " * 60, "Search: a result. " * 50
    ledger.note("skills", skills)
    ledger.note("memory", memory)
    ledger.note("excerpts", knowledge)
    system = {"role": "system", "content": "You are a helpful assistant. " * 30 + skills + knowledge + memory}
    rendering = {"role": "system", "content": "## RENDERING\n" + "Draw diagrams inline. " * 40}
    history = [{"role": "user", "content": "First question " * 30}, {"role": "assistant", "content": "First answer " * 60},
               {"role": "user", "content": "Second question " * 10 + search}]
    ledger.note("excerpts", search, in_history=True)
    messages = [system, rendering, *history, {"role": "system", "content": "Protocol. " * 20}]
    ledger.start(messages)
    return ledger, messages, [(message, index + 1) for index, message in enumerate(history)], (skills, memory, knowledge, search)


def test_every_named_block_is_sized_and_instructions_are_the_rest():
    ledger, messages, history, (skills, memory, knowledge, search) = request()
    parts = ledger.sizes(messages, history, TOOLS)
    assert parts["skills"] == chat_context.estimate(skills) and parts["memory"] == chat_context.estimate(memory)
    assert parts["tools"] == chat_context.estimate(TOOLS) and parts["summary"] == 0 and parts["tool_calls"] == 0
    assert parts["instructions"] > 300
    assert abs(sum(parts.values()) - chat_context.payload_estimate(messages, TOOLS)) <= 2


def test_knowledge_excerpts_are_counted_once():
    # They sat in "Instructions and tools, sent every turn" AND in this turn's working context.
    ledger, messages, history, (skills, memory, knowledge, search) = request()
    parts = ledger.sizes(messages, history, TOOLS)
    assert parts["excerpts"] == chat_context.estimate(knowledge) + chat_context.estimate(search) == ledger.turn_only()
    assert ledger.fixed(messages, history, TOOLS) == sum(parts[name] for name in breakdown.FIXED)
    # The old figure was everything in front of the history. The new one leaves the excerpts out and
    # counts the instructions that come after the history, which are sent every turn as well.
    old_fixed = chat_context.fixed_estimate(messages, history, TOOLS)
    trailing = chat_context.estimate(messages[-1])
    assert abs((old_fixed - chat_context.estimate(knowledge) + trailing) - ledger.fixed(messages, history, TOOLS)) <= 12
    # Search results ride on the user's message: they are not conversation either.
    sent = sum(chat_context.estimate(message) for message, _ in history)
    assert parts["messages"] == sent - chat_context.estimate(search)


def test_messages_added_during_the_reply_are_its_tool_traffic():
    ledger, messages, history, _ = request()
    before = ledger.sizes(messages, history, TOOLS)
    turn = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "execute_code", "arguments": {"code": "print(1)"}}}]},
            {"role": "tool", "content": "**SUCCESS** " + "output " * 400}]
    messages.extend(turn)
    after = ledger.sizes(messages, history, TOOLS)
    assert after["tool_calls"] == sum(chat_context.estimate(message) for message in turn)
    assert after["messages"] == before["messages"] and abs(after["instructions"] - before["instructions"]) <= 2


def test_a_summary_block_is_its_own_row_even_when_written_mid_reply():
    ledger, messages, history, _ = request()
    block = chat_context.summary_block("Earlier they discussed lighthouses. " * 20)
    compacted = [messages[0], messages[1], block, *messages[3:]]          # the first exchange was folded
    parts = ledger.sizes(compacted, history[1:], TOOLS)
    assert parts["summary"] == chat_context.estimate(block) and parts["tool_calls"] == 0


def test_the_breakdown_adds_up_to_the_real_prompt_count():
    ledger, messages, history, _ = request()
    parts = ledger.sizes(messages, history, TOOLS)
    for real in (1, 977, 2431, 5120, 20000):
        scaled = breakdown.scale(parts, real, 0.8)
        assert sum(scaled.values()) == real and all(value >= 0 for value in scaled.values())
    assert breakdown.scale(parts, 0) is None and breakdown.scale(None, 500) is None and breakdown.scale({}, 500) is None


def test_instructions_and_tools_keep_their_own_ratio():
    # Over a ten-turn chat the whole-prompt ratio fell from 0.80 to 0.61. Spread evenly, that shrank the
    # fixed rows by a quarter while nothing about them had changed.
    parts = {"instructions": 3000, "tools": 1500, "memory": 0, "skills": 0, "summary": 0, "messages": 20000, "excerpts": 0, "tool_calls": 0}
    scaled = breakdown.scale(parts, round(24500 * 0.61), 0.61)
    assert scaled["instructions"] == 2400 and scaled["tools"] == 1200
    assert scaled["messages"] == round(24500 * 0.61) - 3600
    only_fixed = breakdown.scale({"instructions": 900, "tools": 100}, 700, 0.8)
    assert only_fixed == {"instructions": 630, "tools": 70, "memory": 0, "skills": 0, "summary": 0, "messages": 0,
                          "kept_tools": 0, "excerpts": 0, "tool_calls": 0}


def test_after_the_reply_messages_take_what_the_rest_leaves():
    first = {"instructions": 2400, "tools": 1200, "memory": 300, "skills": 100, "summary": 500, "messages": 3000, "excerpts": 900, "tool_calls": 0}
    kept = breakdown.kept(first, 8200)
    assert kept == {"instructions": 2400, "tools": 1200, "memory": 300, "skills": 100, "summary": 500, "kept_tools": 0, "messages": 3700}
    # Records of earlier replies' tool calls are sent again, so they stay in the split.
    assert breakdown.kept({**first, "kept_tools": 800}, 9000)["kept_tools"] == 800
    assert sum(breakdown.kept(first, 4400).values()) == 4400          # a small disagreement is absorbed
    assert breakdown.kept(first, 2000) is None                         # a large one shows no split
    assert breakdown.kept(None, 5000) is None


def test_a_history_that_cannot_be_found_still_gets_a_breakdown_and_a_broken_ledger_gets_none():
    ledger, messages, history, _ = request()
    lost = ledger.sizes(messages, [({"role": "user", "content": "not in the list"}, 9)], TOOLS)
    assert lost["messages"] == 0 and lost["instructions"] > 0
    assert breakdown.Ledger().sizes(None, None, None) is None
    assert breakdown.Ledger().fixed([{"role": "system", "content": "x" * 300}], [], None) >= 0
