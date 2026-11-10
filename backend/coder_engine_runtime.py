"""OpenHands 1.59 adapter using public SDK interfaces only.

Runs only in coder_sandbox_agent's private child. The supervisor's inference
bridge owns limits; SDK events and the stop hook are diagnostic, never acceptance.
"""
from __future__ import annotations

import importlib.metadata
import json
import math
from pathlib import Path
import shlex
import sys
import time
import uuid

from context_policy import resolve, operation_settings, thinking_options

ENGINE = 'openhands159'
SDK_VERSION = '1.59.0'


def send_request(conversation, operation_id, initial_task, feedback):
    """SDK message persistence is the receipt; a sidecar is not atomic with it."""
    senders = {getattr(event, 'sender', None) for event in conversation.state.events}
    marker = 'daedalus-operation:' + operation_id
    if marker in senders:
        return False
    first = not any(isinstance(sender, str) and sender.startswith('daedalus-operation:') for sender in senders)
    conversation.send_message(initial_task if first else feedback, sender=marker)
    return True


def repair_count(path, configured):
    """A stopped child may have persisted a denial before returning its result."""
    previous = json.loads(path.read_text()).get('denials', 0) if path.exists() else 0
    if type(previous) is not int or not 0 <= previous <= 2:
        raise ValueError('Cannot recover the persisted repair allowance')
    return max(previous, min(2, int(configured)))


def regression_context(payload):
    """Controller-observed baseline, available before edits and on every repair.

    These are the project's existing tests, never private evaluator assertions.
    Do not silently truncate the required identities: the normal context policy
    owns an oversized request just as it owns any other coding input.
    """
    if not payload.get('required_tests'):
        return ''
    return ('\n\nExisting regression tests that must remain executable under their original identities:\n' +
            json.dumps({'baseline_revision': payload.get('baseline_revision'),
                        'current_revision': payload.get('revision_id'),
                        'required_tests': payload['required_tests'],
                        'test_files': payload.get('baseline_test_files', []),
                        'test_commands': payload.get('baseline_test_commands', [])}) +
            '\nPreserve these tests and their reporting commands while adding coverage. A plain Node assertion '
            'script run with node --test may report its filename as the test identity. Keep that script and its '
            'node --test command; add named tests in a separate file if needed. Running node file.js instead '
            'does not report trusted identities, even if its assertions pass. Do not rename or wrap an existing '
            'file-level test into new named tests merely to change reporting. UI scenario identities follow their launch '
            'and interaction steps, not array position. Keep existing UI scenarios and append new flows; do not replace '
            'an error scenario with a new feature scenario. Read the baseline test files before editing them.')


def run(store, operation_id, repository):
    if importlib.metadata.version('openhands-sdk') != SDK_VERSION:
        raise ValueError('This engine requires its isolated OpenHands 1.59.0 worker')
    from pydantic import SecretStr
    from openhands.sdk import LLM, Agent, Conversation
    from openhands.sdk.tool import Tool
    from openhands.sdk.hooks import HookConfig, HookDefinition, HookMatcher
    from coder_engine_condenser import EngineCondenser, CountedLLM
    from coder_engine_tools import EngineToolSet
    from coder_ui_tool import UITool
    from coder_engine_prepare_tool import PrepareEnvironmentTool
    from coder_worker_runtime import _boundary
    from coder_inference import require_local_model

    operation = store.get(operation_id)
    payload = operation['payload']
    settings = operation_settings(payload)
    policy = resolve('builder', settings)
    details = require_local_model(payload['ollama_url'], payload['model'])
    thinking, thinking_mode = thinking_options('builder', payload, details)
    common = dict(model='ollama_chat/' + payload['model'], base_url=payload['ollama_url'],
                  api_key=SecretStr('local-worker'), max_input_tokens=policy.num_ctx,
                  max_output_tokens=policy.num_predict, temperature=0.2,
                  native_tool_calling=payload.get('tool_profile', 'native') == 'native',
                  capability_overrides={'supports_sampling_params': True},
                  num_retries=0, timeout=max(1, math.ceil(payload['seconds_remaining'])),
                  reasoning_effort=None, litellm_extra_body=thinking,
                  input_cost_per_token=0, output_cost_per_token=0)
    llm = CountedLLM(usage_id='builder', extra_headers={'X-Daedalus-Role': 'builder'}, **common)
    condenser = None
    if policy.compaction:
        compaction = resolve('compaction', settings)
        compaction_thinking, _ = thinking_options('compaction', payload, details)
        condenser_llm = LLM(usage_id='compaction', **{**common,
            'max_input_tokens': compaction.num_ctx, 'max_output_tokens': compaction.num_predict,
            'litellm_extra_body': compaction_thinking,
            'extra_headers': {'X-Daedalus-Role': 'compaction'}})
        condenser = EngineCondenser(llm=condenser_llm,
            max_tokens=int(policy.input_budget * settings['context_compaction_threshold'] / 100),
            keep_first=2)
    tools = [Tool(name=EngineToolSet.name, params={'output_directory': str(store.root / 'tool-output'),
                                                 'max_chars': min(16000, policy.input_budget // 2),
                                                 'command_seconds': settings['daedalus_command_seconds']}),
             Tool(name=UITool.name, params={'directory': str(store.root / 'tool-output'), 'preflight': {
                 'version': payload.get('ui_preflight_version', 0), 'root': str(repository.root),
                 'operation_id': operation_id, 'commands': payload.get('execution_commands'),
                 'timeout': settings['daedalus_command_seconds']}}),
             Tool(name=PrepareEnvironmentTool.name, params={'url': payload['ollama_url'],
                 'timeout': settings['daedalus_preparation_seconds'] + 10})]
    if payload.get('progress_version') == 1:
        from openhands.tools.task_tracker import TaskTrackerTool
        tools.append(Tool(name=TaskTrackerTool.name))
    agent = Agent(llm=llm, tools=tools, condenser=condenser, tool_concurrency_limit=1)
    session_id = uuid.uuid5(uuid.NAMESPACE_URL, operation['job_id'] + ':' + ENGINE)
    directory = store.root / 'engine'; directory.mkdir(exist_ok=True)
    state_path = directory / 'stop.json'
    previous = json.loads(state_path.read_text()) if state_path.is_file() else {}
    state = {'operation_id': operation_id, 'plan': previous.get('plan', []),
             'root': str(repository.root), 'finish_seen': False, 'passed': False,
             'denials': repair_count(state_path, payload.get('repair_round', 0)), 'nudges': 0,
             'command_seconds': settings['daedalus_command_seconds'],
             'execution_commands': payload.get('execution_commands') or {},
             'baseline_test_commands': payload.get('baseline_test_commands', []),
             'required_tests': payload.get('required_tests', []),
             'plan_finalization_version': payload.get('plan_finalization_version', 0),
             'ui_preflight_version': payload.get('ui_preflight_version', 0),
             'ui_directory': str(store.root / 'tool-output'),
             'bridge_url': payload['ollama_url']}
    if previous.get('operation_id') == operation_id and previous.get('finalizing'):
        state.update({k: previous[k] for k in ('finalizing', 'passed', 'finish_seen') if k in previous})
    state_path.write_text(json.dumps(state))
    events = 0
    activity = {'tool_counts': {}, 'recent_tools': [], 'trailing_task_tracker': 0}
    from coder_source_activity import Observer
    observer = Observer(repository.root, directory / 'source-activity.json',
        lambda **data: store.event(operation_id, 'source_activity', **data), repository.excludes) if payload.get('progress_version') == 1 else None

    def on_event(event):
        nonlocal events
        _boundary(store, operation_id)
        data = event.model_dump(mode='json')
        store.event(operation_id, 'agent', event=data)
        if observer: observer.event(data)
        events += 1
        if data.get('kind') == 'ObservationEvent' and data.get('tool_name') == 'task_tracker' and not (data.get('observation') or {}).get('is_error'):
            state = json.loads(state_path.read_text())
            state['plan'] = (data.get('observation') or {}).get('task_list', [])
            state_path.write_text(json.dumps(state))
        if data.get('kind') == 'ActionEvent':
            name = str(data.get('tool_name') or 'unknown')
            activity['tool_counts'][name] = activity['tool_counts'].get(name, 0) + 1
            activity['recent_tools'] = [*activity['recent_tools'], name][-12:]
            activity['trailing_task_tracker'] = activity['trailing_task_tracker'] + 1 if name == 'task_tracker' else 0
            state = json.loads(state_path.read_text())
            state['finish_seen'] = (data.get('tool_name') == 'finish' or
                                    (data.get('action') or {}).get('kind') == 'FinishAction')
            state_path.write_text(json.dumps(state))

    command = shlex.join([sys.executable, str(Path(__file__).with_name('coder_engine_hook.py')), str(state_path)])
    hooks = HookConfig(stop=[HookMatcher(matcher='*', hooks=[HookDefinition(
        command=command, timeout=settings['daedalus_command_seconds'] + 10)])])
    conversation = Conversation(agent=agent, workspace=repository.root,
        persistence_dir=directory / 'sessions', conversation_id=session_id,
        callbacks=[on_event], hook_config=hooks, max_iteration_per_run=payload['calls_remaining'],
        visualizer=None, delete_on_close=False)
    request_marker = directory / ('request-' + operation_id)
    task = ('Implement the complete request in this project. Inspect before editing. Make a short plan, then execute it. '
            'Run the application and its tests as you work; repair concrete failures. Preserve earlier behavior except where '
            'this request changes it. Retain existing test identities; update outdated expectations when adding requested '
            'behavior and explain the change. Changed baseline tests require owner review. '
            'Add useful tests for new behavior. Do not replace the project to avoid a failing test. '
            'Declare runtime and test dependencies and reproducible test commands in the project. '
            'Tools installed in the worker are not automatically available in the delivered project. '
            'Dependency links point to controller-managed environments. Declare all dependencies and use prepare_environment; do not create alternate venv or pip-target directories. '
            'If you need additional packages while coding, declare them in the project dependency manifest and '
            'call the prepare_environment tool to refresh the managed environment, then use its returned commands. '
            'The independent runner provisions the declared dependencies afresh. '
            'Only for an application with a browser or desktop GUI, include .daedalus-ui.json as an array of UI test flows. '
            'CLI projects use their normal tests and need no UI manifest or additional GUI. '
            'UI flows must exercise the requested application entrypoint, never a substitute test page: '
            'each has kind web or desktop, a command field (or project-relative file_path for HTML), '
            'optional url/toolkit, and steps. Each step is an object with an action field set to '
            'click, fill, press, text, observe, or reload. Other fields are selector '
            '(CSS for web), name/role (desktop), value (fill), and equals or contains directly on a subsequent '
            'text/observe result. Web text with a CSS selector returns that element\'s text; web observe returns '
            'the whole page snapshot and supports contains, not an element selector or numeric equals. '
            'For web displays explicitly named by the user, put that stable accessible name (aria-label or aria-labelledby) '
            'on the exact element containing the changing result text. A surrounding heading alone does not name the display. '
            'Exercise real controls and assert exact expected results. These project-authored '
            'flows are replayed by the controller on a fresh revision; page loading alone is not a test. '
            'Exact flow shapes (replace placeholders with this project\'s values): '
            '{"kind":"desktop","command":"<application launch command>","toolkit":"<application toolkit>",'
            '"steps":[{"action":"click","name":"<button name>"},'
            '{"action":"text","name":"<result name>","role":"label","equals":"<expected text>"}]}; '
            '{"kind":"web","file_path":"<relative HTML path>","steps":'
            '[{"action":"click","selector":"<button selector>"},'
            '{"action":"text","selector":"<result selector>","equals":"<expected text>"}]}. '
            'Set toolkit to qt, gtk, or swing to match the application. The launch field is named command, never launch. '
            'For Python/Node browser scripts declare Playwright dependencies and use that package with normal chromium.launch() browser resolution. '
            'Never hardcode executablePath or a Hephaestus/controller browser path. Document npx playwright install chromium '
            '(or python -m playwright install chromium) for a downloaded project. Use prepare_environment after dependency changes; '
            'only a successful result supplies a usable environment. Once preparation succeeds, matching Chromium is already installed; '
            'run tests without running the browser installer inside this worker, whose shared browser cache is read-only. '
            'During controller verification, '
            'navigate to the injected DAEDALUS_APP_URL for a static web project, or '
            'DAEDALUS_<SERVICE_ID>_URL for a declared service. Read that environment variable before starting '
            'a local fallback server; use the fallback only when the variable is absent. '
            'Resolve one base URL from the supplied variable or the fallback and use it for every navigation/request. '
            'Do not merely check the variable to skip server startup while still navigating to a hardcoded fallback URL. The controller URL '
            'connects browser requests to this revision. A separately started server or a hardcoded localhost '
            'URL bypasses that observation even when its page works. Project-relative file:// interactions '
            'belong in .daedalus-ui.json using file_path. '
            'Use standard test runners and assertion APIs (such as Python unittest or Node node:assert/strict); '
            'printing success messages is not an executed assertion. '
            'Test the actual imported application code or its running interface. Do not copy application functions '
            'into test helpers and assert against those copies. Mock DOM objects plus duplicated event handlers '
            'do not verify the application. For a plain HTML app, exercise the real page through UI flows or a '
            'declared browser test; these are preferable to a second implementation of its logic in the tests. '
            'Use the tools to perform work. Call finish when the requested implementation and checks are done. '
            'Final acceptance is performed separately on an immutable copy.\n\nREQUEST:\n' + payload['task'])
    if payload.get('repair_feedback'):
        task += '\n\nFresh verification failures to repair:\n' + json.dumps(payload['repair_feedback'])
    task += regression_context(payload)
    criteria = [row for row in (payload.get('ui_contract') or {}).get('criteria', []) if row.get('supported')]
    if criteria:
        task += ('\n\nLiteral user-request UI outcomes (not private evaluator assertions):\n' +
                 json.dumps([{k: row[k] for k in ('action', 'target', 'property', 'expected', 'request_excerpt')}
                             for row in criteria]) +
                 '\nKeep each named target unambiguous and accessible in the real interface. For web result text, '
                 'apply aria-label or aria-labelledby to the exact changing-text element. A nearby heading does not name it. '
                 'These expected values come only from the original user request; do not redefine them in project tests or manifests.')
    if payload.get('execution_commands'):
        task += '\n\nRequired execution commands:\n' + json.dumps(payload['execution_commands'])
    if payload.get('builder_guidance'):
        task += '\n\nEnvironment preparation feedback:\n' + payload['builder_guidance']
    if payload.get('progress_version') == 1:
        task += '\nAfter inspection, publish a short implementation plan with task_tracker (plan), then continue automatically. Update task status as you work, and reconcile every task status before Finish, including after repairs. Mark done only for completed work; leave incomplete work unresolved. Task status is progress, not independent verification.'
    if payload.get('ui_preflight_version') == 1:
        task += ('\nFor browser/desktop projects, create .daedalus-ui.json alongside the implementation, not after Finish. '
                 'Inspect actual controls and use the UI tool action check_flows before Finish, after manifest edits, and after repairs. '
                 'This replays every flow with clean application state. Resolve every reported flow failure before finishing. '
                 'Put assertions on text for displayed results or value for inputs; observe reads the whole interface and cannot target an element. '
                 'Do not guess validation messages. When the user specifies exact wording, retain it and fix the application. '
                 'When only a visible error is requested, choose consistent application/test wording after exercising the real control. '
                 'Never replace a literal user expectation with observed wrong output, remove unrelated assertions, or weaken a test to make it pass. '
                 'Normal Finish also preflights UI flows within the same allowance and two-repair limit; its results are diagnostic, not final verification.')
    if payload.get('engine_behavior_version', 0) >= 1:
        task = task.replace('click, fill, press, text, observe, or reload.', 'click, fill, press, text, value, observe, or reload.')
        task += ('\nEvery coding turn must use a declared tool, including finish for completion. Keep edits and diagnostics small. '
                 'For desktop flows, observe the actual application before choosing locators. Qt objectName and widget '
                 'class names (such as lineedit) are not accessible names or roles. Use observed name/role pairs; '
                 'omit role only when the accessible name plus action capability is unique. Raw node ids require their '
                 'current session_id and must never be saved in .daedalus-ui.json. Assign stable accessible names to '
                 'editable inputs and result labels; displayed result text may change independently. Use value to '
                 'read editable inputs, text for labels. Visible GUI launch commands must not set QT_QPA_PLATFORM=offscreen. '
                 'Offscreen settings belong only to unit-test commands.')
    if payload.get('engine_behavior_version', 0) >= 2:
        task = task.replace('Assign stable accessible names to editable inputs and result labels; displayed result text may change independently.',
                            'Assign stable accessible names to editable inputs. Follow the user\'s requested result-label naming; when the name must mirror visible text, keep them synchronized.')
        task += ('\nDesktop launch commands inherit the controller-owned DISPLAY and accessibility environment. '
                 'Do not assign DISPLAY, run Xvfb/xvfb-run, or start another DBus session. The controller binds '
                 'accessible controls to your application windows on the same display as its preview. '
                 'After adding or replacing tests, run the manifest default test command and confirm it selects '
                 'the intended tests. Inline HTML executed by JSDOM has unsupported source provenance; use a '
                 'browser test against the actual application or import its real source module. Reading an HTML '
                 'file alone does not establish execution. Task-list updates alone do not implement or verify work; '
                 'follow planning with a concrete file edit or execution, keeping each tool action bounded.')
    store.event(operation_id, 'tool_mode', engine=ENGINE, sdk_version=SDK_VERSION,
                profile=payload.get('tool_profile', 'native'), thinking=thinking_mode)
    try:
        # Operation idempotency prevents a reconnect from appending the request twice.
        if payload.get('engine_behavior_version', 0) >= 1:
            feedback = 'Continue the same request from the saved project and conversation. Use tools; reconcile task_tracker statuses before finish, including repaired work.\n'
            if payload.get('repair_feedback'):
                feedback += 'Repair these observed failures:\n' + json.dumps(payload['repair_feedback'])
            feedback += regression_context(payload)
            if payload.get('builder_guidance'):
                feedback += '\nEnvironment feedback:\n' + payload['builder_guidance']
            send_request(conversation, operation_id, task, feedback)
        elif not request_marker.exists():
            conversation.send_message(task)
            request_marker.write_text(str(time.time()))
        conversation.run()
        state = json.loads(state_path.read_text())
        status = str(conversation.state.execution_status).lower()
        finished = 'finish' in status and state['finish_seen'] and state['passed']
        return {'engine': ENGINE, 'sdk_version': SDK_VERSION, 'agent_finished': finished,
                'execution_status': status, 'session_id': str(session_id), 'events': events,
                'hook_denials': state['denials'], 'hook_nudges': state['nudges'],
                **({'activity_diagnostic': activity} if payload.get('engine_behavior_version', 0) >= 2 else {}),
                'incomplete_reason': '' if finished else state.get('reason', 'Agent stopped before verified completion'),
                'hook_denied_unresolved': not state['passed'] and state['denials'] > 0}
    finally:
        try:
            if payload.get('engine_behavior_version', 0) >= 2:
                store.event(operation_id, 'execution_activity', **activity)
        finally:
            conversation.close()
