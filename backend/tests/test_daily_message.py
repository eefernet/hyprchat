"""The greeting line (daily_message.py): one model call at a time, one line per user and daypart.

Found 2026-11-17: every fresh page load asked the model, with no output cap and a non-streamed call that
Ollama kept running after HyprChat gave up; a burst of loads stalled the shared server for ten minutes.
"""
import asyncio

import pytest

import daily_message


@pytest.fixture(autouse=True)
def fresh():
    daily_message.forget()
    yield
    daily_message.forget()


def counted(text="Alex, the rack missed you.", delay=0.0):
    calls = []

    async def generate():
        calls.append(1)
        if delay:
            await asyncio.sleep(delay)
        return text
    return generate, calls


@pytest.mark.asyncio
async def test_twenty_rapid_loads_make_one_model_call():
    generate, calls = counted(delay=0.05)
    results = await asyncio.gather(*[
        daily_message.get("u1", "Alex", "morning", generate, date="2026-11-18") for _ in range(20)])
    assert len(calls) == 1
    assert sum(1 for r in results if not r.get("fallback")) == 1
    assert all(r["message"] for r in results)
    # Everyone after that gets the kept line without another call.
    again = await daily_message.get("u1", "Alex", "morning", generate, date="2026-11-18")
    assert again == {"message": "Alex, the rack missed you."} and len(calls) == 1


@pytest.mark.asyncio
async def test_line_is_kept_per_user_and_daypart():
    generate, calls = counted()
    await daily_message.get("u1", "Alex", "morning", generate, date="2026-11-18")
    await daily_message.get("u1", "Alex", "evening", generate, date="2026-11-18")
    await daily_message.get("u2", "Sam", "morning", generate, date="2026-11-18")
    await daily_message.get("u1", "Alex", "morning", generate, date="2026-11-19")
    assert len(calls) == 4
    await daily_message.get("u2", "Sam", "morning", generate, date="2026-11-18")
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_busy_server_answers_at_once_with_a_fallback_that_is_not_kept():
    slow, slow_calls = counted(delay=0.2)
    first = asyncio.create_task(daily_message.get("u1", "Alex", "morning", slow, date="d"))
    await asyncio.sleep(0.02)
    other, other_calls = counted("Sam, feed me.")
    busy = await asyncio.wait_for(daily_message.get("u2", "Sam", "morning", other, date="d"), 0.05)
    assert busy["fallback"] and busy["busy"] and "Sam" in busy["message"] and not other_calls
    await first
    later = await daily_message.get("u2", "Sam", "morning", other, date="d")
    assert later == {"message": "Sam, feed me."} and len(other_calls) == 1


@pytest.mark.asyncio
async def test_slow_model_is_cancelled_and_the_fallback_is_not_kept():
    closed = []

    async def hangs():
        try:
            await asyncio.sleep(30)
        finally:
            closed.append(True)       # the stream's context manager exits here, which stops Ollama

    result = await daily_message.get("u1", "", "night", hangs, date="d", seconds=0.05)
    assert result == {"message": daily_message.fallback(""), "fallback": True}
    assert closed == [True]
    generate, calls = counted("The rack hums.")
    assert (await daily_message.get("u1", "", "night", generate, date="d")) == {"message": "The rack hums."}


@pytest.mark.asyncio
async def test_failed_or_unusable_reply_falls_back():
    async def broken():
        raise RuntimeError("model not found")
    assert (await daily_message.get("u1", "Alex", "morning", broken, date="d"))["fallback"]
    generate, _ = counted("x" * 200)
    assert (await daily_message.get("u1", "Alex", "morning", generate, date="d"))["fallback"]


def test_reply_is_cleaned_to_one_plain_line():
    assert daily_message.clean('"Greeting: Alex, the fans are screaming."\nsecond line') == "Alex, the fans are screaming."
    assert daily_message.clean("") == "" and daily_message.clean("y" * 111) == ""
    assert daily_message.clean_name("Main") == "" and daily_message.clean_name(" Alex\nB ") == "Alex B"


@pytest.mark.asyncio
async def test_model_call_is_streamed_with_an_output_cap():
    sent = {}

    class Response:
        def raise_for_status(self): pass
        async def aiter_lines(self):
            yield '{"message":{"content":"Alex, "}}'
            yield '{"message":{"content":"hello."},"done":true}'

    class Stream:
        async def __aenter__(self): return Response()
        async def __aexit__(self, *a): sent["closed"] = True

    class Http:
        def stream(self, method, url, json=None, timeout=None):
            sent.update(url=url, json=json)
            return Stream()

    text = await daily_message.ask_model(Http(), "http://o", "small", 2048, "Alex", "morning")
    assert text == "Alex, hello." and sent["closed"]
    assert sent["json"]["stream"] is True and sent["json"]["think"] is False
    assert sent["json"]["options"] == {"num_ctx": 2048, "num_predict": daily_message.MAX_TOKENS, "temperature": 1.15}
