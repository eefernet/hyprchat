"""The builder's activity summary reaches the controller, bounded and without authority (finding 6, 2026-11-08)."""
import json

from coder_activity_diagnostic import NAME, RECENT, TOOLS, bounded
from coder_engine_recovery import execution_failure
from coder_sandbox_agent import checkpoint_result, relay_events


class Store:
    def __init__(self): self.events = []
    def event(self, operation_id, kind, **data): self.events.append((operation_id, kind, data))


def telemetry(tmp_path, *events):
    folder = tmp_path / 'telemetry'; folder.mkdir()
    (folder / 'op.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
    return tmp_path


ACTIVITY = {'tool_counts': {'file_editor': 2, 'task_tracker': 6, 'think': 1},
            'recent_tools': ['file_editor', 'task_tracker', 'task_tracker', 'task_tracker', 'task_tracker'], 'trailing_task_tracker': 4}


def test_stuck_result_carries_the_activity_summary_into_the_repair_diagnostic():
    result = checkpoint_result({'engine': 'openhands159', 'execution_status': 'ConversationExecutionStatus.STUCK',
                                'agent_finished': False, 'activity_diagnostic': ACTIVITY})
    assert result['activity_diagnostic'] == ACTIVITY
    failure = execution_failure(result)
    assert failure['category'] == 'sdk_stuck' and failure['activity_diagnostic']['trailing_task_tracker'] == 4


def test_child_event_is_relayed_as_an_untrusted_diagnostic(tmp_path):
    store = Store()
    relay_events(store, 'op', telemetry(tmp_path, {'type': 'execution_activity', 'data': ACTIVITY}), 0)
    assert store.events == [('op', 'execution_activity', {**ACTIVITY, 'authority': 'untrusted_agent_diagnostic'})]


def test_result_fields_outside_the_allowlist_and_authority_spoofing_are_dropped(tmp_path):
    spoof = {**ACTIVITY, 'calls': 0, 'usage': {'input_tokens': 0}, 'status': 'succeeded', 'accepted': True,
             'operation_id': 'other', 'identity': 'x', 'authority': 'controller', 'checks_ok': True}
    result = checkpoint_result({'engine': 'openhands159', 'activity_diagnostic': spoof, 'calls': 0, 'status': 'succeeded',
                                'failure': None, 'checks_ok': True, 'usage': {}})
    assert set(result) == {'engine', 'activity_diagnostic'}
    assert set(result['activity_diagnostic']) == {'tool_counts', 'recent_tools', 'trailing_task_tracker'}
    store = Store()
    relay_events(store, 'op', telemetry(tmp_path, {'type': 'execution_activity', 'data': spoof}), 0)
    (_, kind, data), = store.events
    assert kind == 'execution_activity' and data['authority'] == 'untrusted_agent_diagnostic'
    assert set(data) == {'tool_counts', 'recent_tools', 'trailing_task_tracker', 'authority'}


def test_malformed_and_oversized_summaries_are_bounded_or_dropped(tmp_path):
    huge = bounded({'tool_counts': {str(i) + ('t' * 500): 10 ** 12 for i in range(500)},
                    'recent_tools': ['x' * 500] * 500, 'trailing_task_tracker': 10 ** 12})
    assert len(huge['tool_counts']) == TOOLS and all(len(k) <= NAME for k in huge['tool_counts'])
    assert len(huge['recent_tools']) == RECENT and all(len(v) <= NAME for v in huge['recent_tools'])
    assert huge['trailing_task_tracker'] == 1_000_000 and len(json.dumps(huge)) < 8000
    odd = bounded({'tool_counts': {'a\nb"<script>': True, 'ok': -3, 'n': '7', 'fine': 2}, 'recent_tools': [{'x': 1}, None, 'think'],
                   'trailing_task_tracker': 'many'})
    assert odd == {'tool_counts': {'a_b__script_': 0, 'ok': 0, 'n': 0, 'fine': 2}, 'recent_tools': ['__x_:_1_', 'None', 'think'],
                   'trailing_task_tracker': 0}
    for junk in (None, 'text', 7, [], {}, {'tool_counts': 'x', 'recent_tools': 'y'}):
        assert bounded(junk) is None
    assert 'activity_diagnostic' not in checkpoint_result({'engine': 'openhands159', 'activity_diagnostic': 'junk'})
    store = Store()
    relay_events(store, 'op', telemetry(tmp_path, {'type': 'execution_activity', 'data': {'tool_counts': 'x'}},
                                        {'type': 'execution_activity', 'data': 'junk'},
                                        {'type': 'builder_nudge', 'data': {'text': 'no producer exists'}}), 0)
    assert store.events == []
