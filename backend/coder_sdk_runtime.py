"""OpenHands adapter with a settings-owned guard at the actual HTTP boundary.

SDK 1.11.4 is pinned in worker-requirements.txt. Each operation runs in its own
process, so wrapping its transport cannot affect chat or legacy worker jobs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from context_policy import resolve, estimate_tokens, compaction_segments


def text_tool_calls(content, tools, *, allow_stopped=False):
    """Recognize complete JSON tool envelopes from the assistant response only.

    Some local models advertise native tools but emit this text form mid-run.
    SDK argument validation and its normal tool executor still own dispatch.
    Fenced examples, unknown tools, and incomplete JSON stay as text. Text
    mode may stop at the closing tag; its JSON arguments must still be complete.
    """
    allowed = {tool.get("function", {}).get("name") for tool in tools or []}
    calls, spans = [], []
    end = r'(?:</function>|\Z)' if allow_stopped else r'</function>'
    pattern = r'<function\s+(?:name|call)=["\x27]([\w-]+)["\x27]\s*>\s*(.*?)\s*' + end
    for match in re.finditer(pattern, content or "", re.DOTALL):
        if (content[:match.start()].count("```") % 2 or match[1] not in allowed):
            continue
        try:
            arguments = json.loads(match[2])
        except ValueError:
            continue
        if not isinstance(arguments, dict):
            continue
        calls.append({"id": "call_" + uuid.uuid4().hex, "type": "function",
                      "function": {"name": match[1], "arguments": json.dumps(arguments)}})
        spans.append(match.span())
    for start, end in reversed(spans):
        content = content[:start] + content[end:]
    return content, calls


def pin_task_text(messages, task):
    """Pin current repair evidence before the SDK adds text-tool examples."""
    messages = copy.deepcopy(messages)
    first_user = next((message for message in messages if message.get("role") == "user"), None)
    if first_user is not None:
        first_user["content"] = task
    else:
        messages.append({"role":"user", "content":task})
    return messages


def normalize_text_tool_response(response, tools, *, allow_stopped=False):
    """Use the same JSON-envelope fallback with native and text SDK modes."""
    count = 0
    for choice in getattr(response, "choices", []):
        message = getattr(choice, "message", None)
        if message and not getattr(message, "tool_calls", None):
            content, calls = text_tool_calls(getattr(message, "content", ""), tools,
                allow_stopped=allow_stopped and getattr(choice, "finish_reason", None) == "stop")
            if calls:
                from litellm.types.utils import ChatCompletionMessageToolCall
                message.content = content
                message.tool_calls = [ChatCompletionMessageToolCall(**call) for call in calls]
                choice.finish_reason = "tool_calls"
                count += len(calls)
    return count


def compile_context(messages, tools, policy, summarize):
    """Compact whole older turns; preserve instructions and tool pairing."""
    parts = compaction_segments(messages, tools, policy)
    if parts is None:
        return messages, False
    prefix, older, tail = parts
    summary = summarize(older)
    rebuilt = [*prefix, {"role": "user", "content": "Earlier execution checkpoint (source files remain authoritative):\n" + summary}, *tail]
    if estimate_tokens({"messages": rebuilt, "tools": tools}) > policy.input_budget:
        raise ValueError("Current tool evidence still exceeds context after compaction; increase context or request narrower ranges")
    return rebuilt, True


def drive_to_finish(conversation, events, turns, limit):
    """A narrative message is not an explicit agent completion."""
    while turns() < limit():
        before = turns()
        conversation.max_iteration_per_run = limit() - before
        conversation.run()
        finished = any(event.get("kind") == "ActionEvent" and
                       (event.get("tool_name") == "finish" or (event.get("action") or {}).get("kind") == "FinishAction")
                       for event in events)
        state = str(conversation.state.execution_status).lower()
        if finished and "finish" in state:
            return True
        if "finish" not in state or turns() == before or turns() >= limit():
            return False
        conversation.send_message(
            f"You have {limit() - turns()} model turns left in this attempt. "
            "If this milestone is implemented and checked, call finish now. Otherwise perform the next necessary action with a tool. "
            "Avoid repeated summaries or additional demonstration files; narration does not execute work.")
    return False


def checkpoint_delta(history, checkpoint):
    """Reuse a summary only when it describes an unchanged history prefix."""
    previous = checkpoint.get("history", [])
    summary = checkpoint.get("summary", "")
    if summary and previous and history[:len(previous)] == previous:
        return {"checkpoint": summary, "new_evidence": history[len(previous):]}
    return history


def native_narration_streak(response, tools, native, previous):
    """A successful probe is insufficient when real requests produce no calls."""
    if not native or not tools:
        return 0
    choices = getattr(response, "choices", [])
    if any(getattr(getattr(choice, "message", None), "tool_calls", None) for choice in choices):
        return 0
    return previous + 1


def run_coder(store, operation_id, repository):
    import requests
    from coder_worker_runtime import _boundary, evidence_catalog

    os.environ["ALLOW_SHORT_CONTEXT_WINDOWS"] = "true"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import openhands_worker as worker
    worker._ensure_sdk()
    operation = store.get(operation_id)
    payload = operation["payload"]
    policy = resolve("builder", payload["settings"])
    url = payload["ollama_url"].rstrip("/")
    req = worker.RunRequest(task=payload["task"], model=payload["model"], ollama_url=url,
                            num_ctx=policy.num_ctx, num_predict=policy.num_predict,
                            max_rounds=payload["settings"]["daedalus_attempt_turns"],
                            disable_thinking=payload.get("disable_thinking", True),
                            reasoning_effort=payload.get("reasoning_effort", "medium"))
    native = worker._check_tool_support(url, req.model, num_ctx=req.num_ctx, num_predict=req.num_predict,
                                        on_call=lambda: _boundary(store, operation_id, model_call=True))
    llm, agent = worker._make_llm_and_agent(req, url, native)
    llm.max_input_tokens = policy.input_budget
    llm.max_output_tokens = policy.num_predict
    llm.num_retries = 0  # The job controller classifies failures; no hidden retry budget.
    original_transport = worker._LLM._transport_call
    original_format = worker._LLM.format_messages_for_llm
    original_post_mock = worker._LLM.post_response_prompt_mock
    task = None
    summary_cache = {}
    inference_turns = 0
    narration_streak = 0
    checkpoint_path = store.root / "jobs" / operation["job_id"] / ("context-" + hashlib.sha256(payload.get("milestone_id", "code").encode()).hexdigest()[:16] + ".json")
    try:
        checkpoint = json.loads(checkpoint_path.read_text())
    except (OSError, ValueError):
        checkpoint = {}

    def summarize(older):
        nonlocal checkpoint
        serialized = json.dumps(older, ensure_ascii=False)
        key = hashlib.sha256(serialized.encode()).hexdigest()
        if key in summary_cache:
            return summary_cache[key]
        from coder_inference import summarize_history
        delta = checkpoint_delta(older, checkpoint)
        summary = checkpoint["summary"] if isinstance(delta, dict) and not delta["new_evidence"] else summarize_history(store, operation_id, delta)
        summary_cache[key] = summary
        checkpoint = {"history_hash": key, "history": older, "summary": summary}
        temporary = checkpoint_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(checkpoint))
        temporary.replace(checkpoint_path)
        return summary

    def format_messages(self, messages):
        formatted = original_format(self, messages)
        return pin_task_text(formatted, task) if task is not None else formatted

    def post_prompt_mock(self, response, nonfncall_msgs, tools):
        count = normalize_text_tool_response(response, tools, allow_stopped=True)
        if count:
            store.event(operation_id, "text_tool_fallback", count=count)
            return response
        return original_post_mock(self, response, nonfncall_msgs, tools)

    def transport(self, *, messages, **kwargs):
        nonlocal inference_turns, narration_streak
        current = _boundary(store, operation_id)
        active = resolve("builder", current["payload"]["settings"])
        compiled, compacted = compile_context(copy.deepcopy(messages), kwargs.get("tools"), active, summarize)
        from coder_inference import ensure_context
        ensure_context(url, req.model, active, current["payload"]["seconds_remaining"])
        _boundary(store, operation_id, model_call=True)
        extra = copy.deepcopy(kwargs.get("extra_body") or {})
        extra["options"] = {**extra.get("options", {}), "num_ctx": active.num_ctx, "num_predict": active.num_predict}
        kwargs["extra_body"] = extra
        kwargs["max_tokens"] = active.num_predict
        self.max_input_tokens = active.input_budget
        self.max_output_tokens = active.num_predict
        store.event(operation_id, "model_call", context=active.as_dict(), prompt_tokens_estimate=estimate_tokens({"messages": compiled, "tools": kwargs.get("tools")}), compacted=compacted)
        inference_turns += 1
        response = original_transport(self, messages=compiled, **kwargs)
        count = normalize_text_tool_response(response, kwargs.get("tools"))
        if count:
            store.event(operation_id, "text_tool_fallback", count=count)
        narration_streak = native_narration_streak(response, kwargs.get("tools"), self.native_tool_calling, narration_streak)
        if narration_streak >= 2:
            # Use the SDK's established text protocol on the next request.
            # This is local to this operation, not a permanent model demotion.
            self.native_tool_calling = False
            store.event(operation_id, "tool_mode_fallback", reason="Repeated native requests returned narration without tool calls")
        usage = dict(response.usage) if getattr(response,"usage",None) else {}
        store.event(operation_id, "usage", usage=usage)
        if (usage.get("prompt_tokens") or 0) > active.input_budget:
            raise ValueError("Actual prompt usage exceeded the configured input budget; adjust context or narrow evidence")
        if any(getattr(choice,"finish_reason",None) == "length" for choice in getattr(response,"choices",[])):
            raise ValueError("Model response exhausted its completion allowance; adjust Settings or narrow the milestone")
        return response

    worker._LLM._transport_call = transport
    worker._LLM.format_messages_for_llm = format_messages
    worker._LLM.post_response_prompt_mock = post_prompt_mock
    events = []

    def on_event(event):
        _boundary(store, operation_id)
        data = event.model_dump(mode="json") if hasattr(event, "model_dump") else {"type": type(event).__name__}
        store.event(operation_id, "agent", event=data)
        events.append(data)

    conversation = None
    try:
        session_id = uuid.uuid5(uuid.NAMESPACE_URL, operation["job_id"] + ":" + payload.get("milestone_id", "code"))
        conversation = worker._Conversation(agent=agent, workspace=str(repository.root),
            persistence_dir=str(store.root / "sessions"), conversation_id=session_id,
            callbacks=[on_event], max_iteration_per_run=req.max_rounds, stuck_detection=True,
            visualizer=None, delete_on_close=False)
        evidence = payload.get("evidence", {})
        evidence_path = store.root/"jobs"/operation["job_id"]/f"{operation_id}-evidence.json"
        evidence_path.write_text(json.dumps(evidence))
        task = ("Work only in this project: " + str(repository.root) + ".\n"
                "Original requested outcome:\n" + payload.get("original_task", payload["task"]) + "\n\n"
                "Implement this milestone. Inspect relevant source before editing, use ranged reads for large files, "
                "and execute the listed checks before finishing. Repair the reported failures, including missing test modules. Do not rebuild unrelated packages or alter the requested criteria.\n" + payload["task"] + "\n"
                + "When the milestone and its checks are complete, call finish. Avoid repeated summaries and unnecessary demonstration files.\n"
                + "Complete verification evidence is saved at " + str(evidence_path) + ". Read selected JSON entries or referenced logs when a collection has more pages. Edit only project source.\n"
                + json.dumps({"milestone": payload.get("milestone"), "evidence": evidence_catalog(evidence,policy.input_budget//3)}))
        conversation.send_message(task)
        finished = drive_to_finish(conversation, events, lambda:inference_turns,
                                   lambda:store.get(operation_id)["payload"]["settings"]["daedalus_attempt_turns"])
        state = str(conversation.state.execution_status)
        return {"agent_finished": finished, "execution_status": state,
                "incomplete_reason":"" if finished else "The Builder has not called finish. Complete the next action with a tool, then call finish when the milestone is done.",
                "session_id": str(session_id), "events": len(events)}
    finally:
        worker._LLM._transport_call = original_transport
        worker._LLM.format_messages_for_llm = original_format
        worker._LLM.post_response_prompt_mock = original_post_mock
        if conversation:
            conversation.close()
