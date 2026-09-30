"""The agent builder must not pay a compaction call on every turn (live job cw3-f7209a44)."""
import pytest

from coder_context import ContextWindow
from context_policy import resolve


def policy(**extra):
    return resolve('builder', {'openhands_num_ctx': 40960, 'generation_num_predict': 8192, 'context_headroom_percent': 5,
                               'context_compaction': 'on', **extra})


def calibrated(**state):
    window = ContextWindow(**state)
    window.observe(30000, 20700)                       # the live run: bytes/3 over-counted by ~45%
    return window


def conversation(turns, prefix_bytes=60000, view_bytes=7500):
    messages = [{'role': 'system', 'content': 's' * prefix_bytes}, {'role': 'user', 'content': 'Build the app'}]
    for index in range(turns):
        messages.append({'role': 'assistant', 'content': '', 'tool_calls': [{'id': f'c{index}', 'function': {'name': 'view', 'arguments': '{}'}}]})
        messages.append({'role': 'tool', 'tool_call_id': f'c{index}', 'content': f'{index}:' + 'x' * view_bytes})
    return messages


def test_a_large_fixed_prompt_does_not_compact_on_every_turn():
    active, calls, window = policy(), [], calibrated()
    for turns in range(1, 21):
        window.compile(conversation(turns), [], active, lambda older: calls.append(len(older)) or 'summary')
    assert 1 <= len(calls) <= 4, calls


def test_the_boundary_keeps_recent_tool_pairs_verbatim_and_never_splits_one():
    active, window = policy(), calibrated()
    compiled, compacted = [], False
    for turns in range(1, 21):
        compiled, changed = window.compile(conversation(turns), [], active, lambda older: 'summary')
        compacted = compacted or changed
    assert compacted
    body = compiled[3:]
    assert compiled[2]['content'].startswith('Earlier execution checkpoint')
    assert body[0]['role'] == 'assistant' and len(body) >= 6
    assert compiled[-1]['content'].startswith('19:')


def test_a_rewritten_history_discards_the_stored_boundary():
    active, window = policy(), calibrated()
    for turns in range(1, 21):
        window.compile(conversation(turns), [], active, lambda older: 'summary')
    assert window.boundary
    compiled, _ = window.compile(conversation(2, view_bytes=10), [], active, lambda older: pytest.fail('nothing to compact'))
    assert window.boundary is None and len(compiled) == 6


def test_disabled_compaction_still_blocks_visibly_and_never_summarizes():
    off = policy(context_compaction='off')
    window = ContextWindow()
    messages, changed = window.compile(conversation(3), [], off, lambda older: pytest.fail('disabled'))
    assert not changed and len(messages) == 8
    with pytest.raises(ValueError, match='compaction is disabled'):
        window.compile(conversation(60), [], off, lambda older: pytest.fail('disabled'))


def test_calibration_is_bounded_conservative_and_persisted():
    saved = []
    window = ContextWindow(save=saved.append)
    window.observe(20000, 2000)
    assert window.ratio == 0.5
    window.observe(20000, 15000)
    window.observe(20000, 12000)
    assert window.ratio == 0.75 and saved[-1]['ratio'] == 0.75
    assert ContextWindow(saved[-1]).ratio == 0.75


def test_instructions_larger_than_the_budget_are_reported():
    with pytest.raises(ValueError, match='exceed configured context'):
        ContextWindow().compile(conversation(1, prefix_bytes=200000), [], policy(), lambda older: 'summary')


def action(tool, **fields):
    kind = {'file_editor': 'FileEditorAction', 'terminal': 'TerminalAction'}[tool]
    return {'kind': 'ActionEvent', 'tool_name': tool, 'action': {'kind': kind, **fields}}


def test_rereading_the_same_files_is_corrected_then_stopped():
    from coder_context import StallGuard
    guard = StallGuard()
    for path in ['server.js', 'database.js', 'package.json', 'server.js']:
        guard.record(action('file_editor', command='view', path=path))
    assert not guard.correction()
    guard.record(action('terminal', command='cat server.js'))
    notice = guard.correction(['README.md'])
    assert 'README.md' in notice and 'server.js' in notice and not guard.stalled()
    for _ in range(5):
        guard.record(action('file_editor', command='view', path='server.js'))
    assert guard.stalled()


def test_writing_or_running_the_project_resets_the_guard():
    from coder_context import StallGuard
    for work in (action('file_editor', command='create', path='README.md'), action('terminal', command='npm test'),
                 action('terminal', command='cat > notes.txt'), action('terminal', command='cd app && node server.js')):
        guard = StallGuard()
        for _ in range(9):
            guard.record(action('file_editor', command='view', path='server.js'))
        guard.record(work)
        assert guard.streak == 0 and not guard.correction()


def test_inspecting_distinct_files_is_not_a_stall():
    from coder_context import StallGuard
    guard = StallGuard()
    for index in range(12):
        guard.record(action('file_editor', command='view', path=f'src/m{index}.py'))
        guard.record({'kind': 'ObservationEvent'})
    assert not guard.correction() and not guard.stalled()


# Code audit 2026-09-21.
def heavy(turns, big=range(6, 9), big_bytes=30000):
    messages = conversation(turns)
    for index in big:
        if index < turns:
            messages[2 + 2 * index + 1]['content'] = f'{index}:' + 'y' * big_bytes
    return messages


def paid_summaries(make, turns=40):
    active, window, paid = policy(), calibrated(), []
    for count in range(1, turns + 1):
        before = len(paid)
        window.compile(make(count), [], active, lambda older: paid.append(count) or 'summary')
        assert len(paid) - before <= 1, (count, paid)          # never two summaries in one call
    return paid


def test_a_run_of_large_reads_compacts_to_a_low_water_mark_not_back_to_the_threshold():
    # Measured before the fix: ten 15 KB reads cost 14 paid summaries (every turn from 9 to 16), six 18 KB
    # reads cost 12. Keeping the largest tail that merely fit left the window at the threshold next turn.
    assert len(paid_summaries(lambda t: heavy(t, big=range(6, 16), big_bytes=15000))) <= 10
    assert len(paid_summaries(lambda t: heavy(t, big=range(6, 12), big_bytes=18000))) <= 8
    # The ordinary workload is not made worse: one summary about every five turns.
    ordinary = paid_summaries(conversation)
    assert len(ordinary) <= 7 and all(b - a >= 4 for a, b in zip(ordinary, ordinary[1:])), ordinary


def test_one_compile_pays_for_at_most_one_summary():
    active, window = policy(), calibrated()
    for turns in range(1, 41):
        paid = []
        window.compile(heavy(turns), [], active, lambda older: paid.append(1) or 'summary')
        assert len(paid) <= 1, (turns, paid)


def test_one_clamped_observation_does_not_reset_the_conservative_ratio():
    # ratio >= 1.0 doubled as "uncalibrated": a dense turn pinned it to 1.0 and the next dropped it to 0.5.
    # Since sweep3 the ceiling is RATIO_CEILING (1.6): a dense turn is recorded as measured, never reset.
    window = ContextWindow()
    for estimate, reported in ((1000, 600), (1000, 900), (1000, 1200), (1000, 400)):
        window.observe(estimate, reported)
    assert window.ratio == 1.2
    restored = ContextWindow(window.state())
    restored.observe(1000, 400)
    assert restored.ratio == 1.2
    restored.observe(1000, 5000)
    assert restored.ratio == 1.6
    legacy = ContextWindow({'ratio': 0.69, 'boundary': None})   # a window file written before the flag existed
    legacy.observe(1000, 500)
    assert legacy.ratio == 0.69


def test_compaction_off_sizes_and_sends_the_same_messages():
    # With a stored boundary the size was measured on the compacted form but the FULL history was returned.
    on, window = policy(), calibrated()
    for turns in range(1, 21):
        window.compile(conversation(turns), [], on, lambda older: 'summary')
    assert window.boundary
    off = policy(context_compaction='off')
    for turns in range(8, 41):
        try:
            sent, changed = window.compile(conversation(turns), [], off, lambda older: pytest.fail('disabled'))
        except ValueError as error:
            assert 'compaction is disabled' in str(error)
            continue
        # Whatever is sent is what was measured: the whole history, within the budget.
        assert not changed and len(sent) == 2 + 2 * turns and window.estimate(sent, []) <= off.input_budget, turns


# sweep3 (2026-09-26): a Kanban session at the 64K window reached 58,033 of 58,163 prompt tokens with
# compaction ON and never compacted. bytes/3 under-counts dense JSON/code, the calibration ratio was
# clamped at 1.0, and nothing used the prompt size the model had just reported.
def test_sweep3_kanban_dense_prompts_calibrate_above_one_and_compact_before_the_budget():
    active = policy(openhands_num_ctx=65536, generation_num_predict=4096)
    assert active.input_budget == 58163
    saved, paid = [], []
    window = ContextWindow(save=saved.append)
    window.observe(30000, 36000)
    assert window.ratio == 1.2 and saved[-1]['ratio'] == 1.2
    for turns in (6, 7):   # neither size crosses the threshold on its estimate alone
        sent, compacted = window.compile(conversation(turns), [], active, lambda older: paid.append(len(older)) or 'summary')
        assert not compacted and not paid and window.estimate(sent, []) < window.threshold(*ContextWindow.split(sent)[:1], [], active)
    # The model reports 58,033 tokens for that call: the estimate stays where it was (ratio unchanged), but
    # the reported size is a floor for the next call under the same instructions and boundary.
    window.observe(round(58033 / 1.2), 58033)
    assert window.ratio == 1.2 and saved[-1]['last_prompt'] == 58033 and saved[-1]['last_prompt_key']
    assert ContextWindow(saved[-1]).last_prompt == 58033
    sent, compacted = window.compile(conversation(7), [], active, lambda older: paid.append(len(older)) or 'summary')
    assert compacted and paid == [paid[0]] and window.boundary and window.estimate(sent, []) <= active.input_budget
    assert sent[2]['content'].startswith('Earlier execution checkpoint')
    # The floor retires with the boundary that replaced it: the next call is sized by its estimate again.
    sent, compacted = window.compile(conversation(7), [], active, lambda older: pytest.fail('floor outlived its boundary'))
    assert not compacted and window.last_prompt_key != window._pending_key
    # A rewritten history (different instructions) retires it as well.
    window.observe(40000, 50000)
    rewritten = conversation(7, prefix_bytes=59000)
    sent, compacted = window.compile(rewritten, [], active, lambda older: pytest.fail('floor outlived its history'))
    assert not compacted
