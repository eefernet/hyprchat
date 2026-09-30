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
import time
import uuid

from context_policy import resolve, estimate_tokens, compaction_segments, operation_settings, thinking_options, compile_context


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


def pin_task_text(messages, task, *, deduplicate=False):
    """Pin current repair evidence before the SDK adds text-tool examples."""
    messages = copy.deepcopy(messages)
    first_user = next((message for message in messages if message.get("role") == "user"), None)
    if first_user is not None:
        first_user["content"] = task
        if deduplicate:
            for message in messages:
                if message is first_user or message.get("role") != "user":
                    continue
                content=message.get("content")
                if isinstance(content,list) and all(isinstance(item,dict) and item.get("type")=="text" for item in content):
                    content="".join(item.get("text","") for item in content)
                if content==task:
                    message["content"]="Continue from the saved workspace. The current request and verification evidence are pinned in the first user message above."
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


def text_tool_history(messages):
    """Serialize native batches for the SDK's single-call text converter.

    This changes only the inference copy of already recorded history. The SDK
    retains ownership of dispatch; no action or observation is dropped/replayed.
    """
    result=[]
    for message in copy.deepcopy(messages):
        calls=message.get('tool_calls') or []
        if message.get('role')=='assistant' and len(calls)>1:
            for index,call in enumerate(calls):
                result.append({**message,'content':message.get('content') if index==0 else '', 'tool_calls':[call]})
        else:result.append(message)
    return result


def tool_mode_is_definitive(worker, url, model, native):
    """Whether a probe result may be saved for later operations.

    The worker's probe answers False both for "this model cannot call tools" (which it records in
    its own cache) and for a transient failure such as Ollama restarting (which it deliberately does
    not). Saving the second kind demoted the model to text mode for every later build.
    """
    return bool(native) or getattr(worker, '_tool_support_cache', {}).get(f'{url}:{model}') is False


def drive_to_finish(conversation, events, turns, limit):
    """A narrative message is not an explicit agent completion."""
    while turns() < limit():
        before, event_start = turns(), len(events)
        conversation.max_iteration_per_run = limit() - before
        conversation.run()
        # Only this pass: an earlier finish event must not end a later pass that finished nothing.
        finished = any(event.get("kind") == "ActionEvent" and
                       (event.get("tool_name") == "finish" or (event.get("action") or {}).get("kind") == "FinishAction")
                       for event in events[event_start:])
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


def drive_to_checkpoint(conversation, events, turns, limit, source_tree, recovered, mark_recovery):
    """Return after a real checkpoint; reads and next-step narration are not edits."""
    while turns() < limit():
        before, event_start, tree = turns(), len(events), source_tree()
        conversation.max_iteration_per_run = limit() - before
        conversation.run()
        fresh = events[event_start:]
        finished = any(e.get('tool_name') == 'finish' or (e.get('action') or {}).get('kind') == 'FinishAction' for e in fresh)
        last_message = next((e for e in reversed(fresh) if e.get('kind') == 'MessageEvent'), {})
        message = last_message.get('llm_message') or last_message.get('message') or {}
        contents = message.get('content', [])
        text = contents if isinstance(contents, str) else ' '.join(c.get('text', '') for c in contents if isinstance(c, dict))
        next_step = bool(re.search(r'\b(?:now (?:let me|I will|I.ll)|let me also|next I(?: will|.ll))\s+(?:create|check|write|add|run|fix|implement|inspect|read|update|build|test)\b', text, re.I))
        if finished:
            return True
        if source_tree() != tree and (not next_step or recovered()):
            return False
        if recovered() or turns() == before or turns() >= limit():
            return False
        mark_recovery()
        conversation.send_message('Only inspection or a next-step announcement was returned. Execute the next necessary implementation or test action now. '
                                  'Complete the requested deliverables, or return a concrete blocker. Do not repeat a summary.')
    return False


def tool_mode_key(model_digest, runtime, tool_configuration):
    return hashlib.sha256(json.dumps([model_digest, runtime, tool_configuration], sort_keys=True).encode()).hexdigest()


def native_narration_streak(response, tools, native, previous):
    """A successful probe is insufficient when real requests produce no calls."""
    if not native or not tools:
        return 0
    choices = getattr(response, "choices", [])
    if any(getattr(getattr(choice, "message", None), "tool_calls", None) for choice in choices):
        return 0
    return previous + 1


def run_coder(store, operation_id, repository):
    from coder_sandbox_agent import run_isolated
    return run_isolated(store, operation_id, repository)


def _run_coder_local(store, operation_id, repository):
    import requests
    from coder_worker_runtime import _boundary, evidence_catalog

    os.environ["ALLOW_SHORT_CONTEXT_WINDOWS"] = "true"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import openhands_worker as worker
    worker._ensure_sdk()
    operation = store.get(operation_id)
    payload = operation["payload"]
    policy = resolve("builder", operation_settings(payload))
    url = payload["ollama_url"].rstrip("/")
    req = worker.RunRequest(task=payload["task"], model=payload["model"], ollama_url=url,
                            num_ctx=policy.num_ctx, num_predict=policy.num_predict,
                            max_rounds=payload["settings"]["daedalus_attempt_turns"],
                            disable_thinking=payload.get("disable_thinking", True),
                            reasoning_effort=payload.get("reasoning_effort", "medium"))
    mode_path=store.root/'jobs'/operation['job_id']/'tool-mode.json'
    if payload.get('policy_version', 1) >= 6:
        import importlib.metadata
        response = requests.get(url + '/api/tags', timeout=15)
        response.raise_for_status()
        names = {req.model, req.model + ':latest'}
        model_digest = next((m.get('digest') for m in response.json().get('models', []) if m.get('name') in names or m.get('model') in names), None)
        if not model_digest:
            raise ValueError('Installed model digest is unavailable; cannot select a tool mode safely')
        configuration = {'browser':payload.get('ui_required', False), 'worker':hashlib.sha256(Path(worker.__file__).read_bytes()).hexdigest(),
                         'adapter':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        key = tool_mode_key(model_digest, importlib.metadata.version('openhands-sdk'), configuration)
        mode_path = store.root / 'tool-modes' / (key + '.json')
    mode_path.parent.mkdir(parents=True,exist_ok=True)
    try: saved_mode=json.loads(mode_path.read_text())
    except (OSError,ValueError): saved_mode={}
    if payload.get('policy_version', 1) >= 6 and saved_mode.get('mode') in {'native', 'text'}:
        native = saved_mode['mode'] == 'native'
    else:
        native = worker._check_tool_support(url, req.model, num_ctx=req.num_ctx, num_predict=req.num_predict,
                                            on_call=lambda: _boundary(store, operation_id, model_call=True))
        if payload.get('policy_version', 1) >= 6 and tool_mode_is_definitive(worker, url, req.model, native):
            temporary = mode_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'mode':'native' if native else 'text'})); temporary.replace(mode_path)
    llm, agent = worker._make_llm_and_agent(req, url, native)
    if payload.get('policy_version',1)>=3 and saved_mode.get('model')==req.model and saved_mode.get('mode')=='text':
        llm.native_tool_calling=False
    if payload.get("policy_version",1)>=2 and payload.get("ui_required"):
        from coder_browser_tool import install
        install(agent,store,operation_id,repository)
    llm.max_input_tokens = policy.input_budget
    llm.max_output_tokens = policy.num_predict
    llm.num_retries = 0  # The job controller classifies failures; no hidden retry budget.
    original_transport = worker._LLM._transport_call
    original_format = worker._LLM.format_messages_for_llm
    original_post_mock = worker._LLM.post_response_prompt_mock
    original_completion = worker._LLM.completion
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

    window_path = checkpoint_path.with_suffix('.window.json')
    try:
        window_state = json.loads(window_path.read_text())
    except (OSError, ValueError):
        window_state = {}

    def save_window(state):
        temporary = window_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state))
        temporary.replace(window_path)

    from coder_context import ContextWindow, StallGuard
    window = ContextWindow(window_state, save_window)
    # Only whole-project builds are guarded: inspecting many files is normal for a scoped repair.
    guard = StallGuard() if payload.get('builder_until_finish') else None

    def missing_files():
        planned = [name for batch in (payload.get('brief') or {}).get('batches', []) for name in batch.get('files', [])]
        return [name for name in dict.fromkeys(planned) if not (repository.root / name).exists()]

    def format_messages(self, messages):
        formatted = original_format(self, messages)
        if task is not None:
            formatted=pin_task_text(formatted, task, deduplicate=payload.get("policy_version",1)>=2)
        return text_tool_history(formatted) if payload.get('policy_version',1)>=3 and not self.native_tool_calling else formatted

    def post_prompt_mock(self, response, nonfncall_msgs, tools):
        count = normalize_text_tool_response(response, tools, allow_stopped=True)
        if count:
            store.event(operation_id, "text_tool_fallback", count=count)
            return response
        return original_post_mock(self, response, nonfncall_msgs, tools)

    def completion(self, messages, tools=None, **kwargs):
        from coder_failures import malformed_native_call
        try:
            return original_completion(self,messages,tools=tools,**kwargs)
        except Exception as error:
            if payload.get('policy_version',1)<3 or not self.native_tool_calling or not tools or not malformed_native_call(error):
                raise
            # This boundary precedes SDK tool dispatch. No arguments from the
            # rejected response are executed, and earlier actions are not replayed.
            self.native_tool_calling=False
            temporary=mode_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'model':req.model,'mode':'text'}));temporary.replace(mode_path)
            store.event(operation_id,'tool_mode_fallback',reason='Malformed native tool response before execution',recovery_attempted=True)
            return original_completion(self,messages,tools=tools,**kwargs)

    def transport(self, *, messages, **kwargs):
        nonlocal inference_turns, narration_streak
        current = _boundary(store, operation_id)
        active = resolve("builder", operation_settings(current["payload"]))
        if guard and guard.stalled():
            store.event(operation_id, 'builder_stalled', streak=guard.streak, targets=list(dict.fromkeys(guard.targets))[:12])
            raise RuntimeError('Builder stalled: it kept re-reading the same files after a specific correction')
        notice = guard.correction(missing_files()) if guard else ''
        if notice:
            store.event(operation_id, 'builder_nudge', streak=guard.streak)
            messages = [*messages, {'role': 'user', 'content': notice}]
        # One boundary per session: a summary is reused until the working context fills again.
        compiled, compacted = window.compile(copy.deepcopy(messages), kwargs.get("tools"), active, summarize)
        from coder_inference import ensure_context
        # The time LEFT, as local_chat passes it: the whole allowance let a wedged Ollama hold one turn for an hour.
        ensure_context(url, req.model, active, max(1, current["payload"]["seconds_remaining"] - (time.time() - current["started"])))
        _boundary(store, operation_id, model_call=True)
        extra = copy.deepcopy(kwargs.get("extra_body") or {})
        extra["options"] = {**extra.get("options", {}), "num_ctx": active.num_ctx, "num_predict": active.num_predict}
        kwargs["extra_body"] = extra
        if payload.get('policy_version',1)>=3:
            from coder_inference import require_local_model
            details=require_local_model(url,req.model)
            thinking,mode=thinking_options('builder',current['payload'],details)
            extra.update(thinking)
            store.event(operation_id,'inference_policy',role='builder',thinking=mode)
        kwargs["max_tokens"] = active.num_predict
        self.max_input_tokens = active.input_budget
        self.max_output_tokens = active.num_predict
        raw_estimate = estimate_tokens({"messages": compiled, "tools": kwargs.get("tools")})
        store.event(operation_id, "model_call", context=active.as_dict(), prompt_tokens_estimate=raw_estimate,
                    calibration=round(window.ratio, 3), compacted=compacted)
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
            if payload.get('policy_version', 1) >= 6:
                temporary = mode_path.with_suffix('.tmp')
                temporary.write_text(json.dumps({'mode':'text'})); temporary.replace(mode_path)
            store.event(operation_id, "tool_mode_fallback", reason="Repeated native requests returned narration without tool calls")
        usage = dict(response.usage) if getattr(response,"usage",None) else {}
        store.event(operation_id, "usage", usage=usage)
        window.observe(raw_estimate, usage.get("prompt_tokens") or 0)
        if (usage.get("prompt_tokens") or 0) > active.input_budget:
            raise ValueError("Actual prompt usage exceeded the configured input budget; adjust context or narrow evidence")
        if any(getattr(choice,"finish_reason",None) == "length" for choice in getattr(response,"choices",[])):
            raise ValueError("Model response exhausted its completion allowance; adjust Settings or narrow the milestone")
        return response

    worker._LLM._transport_call = transport
    worker._LLM.format_messages_for_llm = format_messages
    worker._LLM.post_response_prompt_mock = post_prompt_mock
    worker._LLM.completion = completion
    events = []

    def on_event(event):
        _boundary(store, operation_id)
        data = event.model_dump(mode="json") if hasattr(event, "model_dump") else {"type": type(event).__name__}
        store.event(operation_id, "agent", event=data)
        events.append(data)
        if guard:
            guard.record(data)

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
                + ("When browser regression checks are requested, provide a discoverable automated test command (for example a package test script or a Python test runner). A manual HTML test page alone is not an automated test command. Keep application runtime dependencies consistent with the request.\n" if payload.get('policy_version',1)>=3 and payload.get('ui_required') else '')
                + "When the milestone and its checks are complete, call finish. Avoid repeated summaries and unnecessary demonstration files.\n"
                + "Complete verification evidence is saved at " + str(evidence_path) + ". Read selected JSON entries or referenced logs when a collection has more pages. Edit only project source.\n"
                + json.dumps({"milestone": payload.get("milestone"), "evidence": evidence_catalog(evidence,policy.input_budget//3)}))
        if payload.get('policy_version', 1) >= 6:
            from coder_policy7_prompt import repair_task
            task, tier = repair_task(payload, repository, evidence, evidence_path, policy, catalog=evidence_catalog)
            if tier:
                store.event(operation_id, 'prompt_bounded', tier=tier, estimate=estimate_tokens(task), input_budget=policy.input_budget)
        conversation.send_message(task)
        # The hybrid policy-7 builder asks for a whole project per operation: keep nudging until the
        # agent finishes or its turn allowance ends, instead of returning at the first checkpoint.
        if payload.get('policy_version', 1) >= 6 and not payload.get('builder_until_finish'):
            recovery_path = checkpoint_path.with_suffix('.recovery.json')
            def mark_recovery():
                recovery_path.write_text(json.dumps({'used':True}))
                store.event(operation_id, 'builder_recovery', round_id=payload.get('round_id', 0))
            finished = drive_to_checkpoint(conversation, events, lambda:inference_turns,
                lambda:store.get(operation_id)['payload']['settings']['daedalus_attempt_turns'],
                lambda:repository.snapshot('Builder progress', parent=payload['revision_id'])['tree'],
                recovery_path.exists, mark_recovery)
        else:
            finished = drive_to_finish(conversation, events, lambda:inference_turns,
                                       lambda:store.get(operation_id)["payload"]["settings"]["daedalus_attempt_turns"])
        state = str(conversation.state.execution_status)
        return {"agent_finished": finished, "execution_status": state,
                "incomplete_reason":"" if finished else 'Builder returned a checkpoint for controller verification.' if payload.get('policy_version',1)>=6 else "The Builder has not called finish. Complete the next action with a tool, then call finish when the milestone is done.",
                "session_id": str(session_id), "events": len(events)}
    finally:
        worker._LLM._transport_call = original_transport
        worker._LLM.format_messages_for_llm = original_format
        worker._LLM.post_response_prompt_mock = original_post_mock
        worker._LLM.completion = original_completion
        if conversation:
            conversation.close()
