"""Working context for one agent-builder session.

The SDK resends the whole conversation on every model call. Compacting that from
scratch each time meant that, once the history crossed the threshold, EVERY later
turn paid a summary call and lost what the agent had just read (a live build spent
60 of 120 calls on compaction and re-read the same three files 50 times).

This window keeps a compaction boundary instead: history before it is replaced by
one stored summary, everything after it stays verbatim, and the boundary only moves
when the working context is genuinely full again. Estimates are calibrated with the
prompt usage the model reports, because bytes/3 over-counts code by roughly 45%.
Settings still own every limit: the window never exceeds the input budget.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

from context_policy import estimate_tokens

TAILS = (6, 4, 2)       # recent messages kept verbatim, largest first
WORKING_SHARE = 0.8     # of the room left above the fixed prompt, usable before compacting
HARD_SHARE = 0.92       # calibrated estimates stay this far under the input budget
SUMMARY_ALLOWANCE = 1200  # tokens assumed for a summary when a tail is sized BEFORE paying for one
LOW_SHARE = 0.5         # of that room a fresh boundary may occupy, so a compaction buys several turns
RATIO_CEILING = 1.6     # dense JSON/code can exceed bytes/3; a 1.0 ceiling let a session reach 58,033 of 58,163 tokens uncompacted


def _digest(messages):
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class ContextWindow:
    def __init__(self, state=None, save=None):
        state = state or {}
        self.ratio = float(state.get('ratio') or 1.0)
        # Older window files carry no flag: a ratio below 1.0 can only have come from an observation.
        self.calibrated = bool(state.get('calibrated', self.ratio < 1.0))
        self.boundary = state.get('boundary') or None
        # The prompt size the model last reported, and the (instructions, boundary, mode) it was sent under.
        self.last_prompt = int(state.get('last_prompt') or 0)
        self.last_prompt_key = state.get('last_prompt_key') or None
        self._pending_key = None
        self._save = save or (lambda _state: None)

    def state(self):
        return {'ratio': self.ratio, 'calibrated': self.calibrated, 'boundary': self.boundary,
                'last_prompt': self.last_prompt, 'last_prompt_key': self.last_prompt_key}

    def estimate(self, messages, tools):
        return math.ceil(estimate_tokens({'messages': messages, 'tools': tools}) * self.ratio)

    def observe(self, raw_estimate, prompt_tokens):
        """Track the highest observed tokens-per-estimate so the correction stays conservative."""
        if not raw_estimate or not prompt_tokens:
            return
        seen = min(RATIO_CEILING, max(0.5, prompt_tokens / raw_estimate))
        # `ratio >= 1.0` used to mean "uncalibrated" too, so one dense turn that clamped to 1.0 let the
        # next observation drop the ratio to 0.5 and the following prompt went out ~2x over its estimate.
        self.ratio = max(self.ratio, seen) if self.calibrated else seen
        self.calibrated = True
        # What was actually sent is a floor on the next call under the same key (the conversation
        # only grows between compactions); a key change (new boundary, rewritten history, new
        # instructions, compaction toggled) retires it.
        self.last_prompt, self.last_prompt_key = int(prompt_tokens), self._pending_key
        self._save(self.state())

    @staticmethod
    def _key(prefix, boundary, policy):
        return [_digest(prefix), boundary['digest'] if boundary else None, bool(policy.compaction)]

    def threshold(self, prefix, tools, policy):
        fixed = self.estimate(prefix, tools)
        room = policy.input_budget - fixed
        if room <= 0:
            raise ValueError('Current instructions/tool result exceed configured context; request narrower source ranges or increase context in Settings')
        return min(max(policy.compact_at, fixed + int(WORKING_SHARE * room)), int(policy.input_budget * HARD_SHARE))

    @staticmethod
    def split(messages):
        prefix_end = next((index + 1 for index, message in enumerate(messages) if message.get('role') == 'user'), 0)
        prefix = [*messages[:prefix_end], *[m for m in messages[prefix_end:] if m.get('role') == 'system']]
        return prefix, [m for m in messages[prefix_end:] if m.get('role') != 'system']

    @staticmethod
    def rebuild(prefix, summary, tail):
        return [*prefix, {'role': 'user', 'content': 'Earlier execution checkpoint (source files remain authoritative):\n' + summary}, *tail]

    def compile(self, messages, tools, policy, summarize):
        """Return (messages to send, whether a new summary was made for this call)."""
        prefix, body = self.split(messages)
        held = self.boundary
        if held and not (len(body) >= held['count'] and _digest(body[:held['count']]) == held['digest']):
            held = self.boundary = None
        key = self._pending_key = self._key(prefix, held, policy)
        if not policy.compaction:
            # Size what is actually sent: a boundary stored while compaction was on is not applied.
            if self.estimate(messages, tools) <= policy.input_budget:
                return messages, False
            raise ValueError('Context is full and automatic compaction is disabled. Adjust Settings to continue.')
        working = self.rebuild(prefix, held['summary'], body[held['count']:]) if held else messages
        # The last reported prompt is a floor while the same key is in force: even calibrated, bytes/3
        # under-counted a dense Kanban session to 58,033 of 58,163 tokens without crossing the threshold.
        floor_tokens = self.last_prompt if self.last_prompt and self.last_prompt_key == key else 0
        size = max(self.estimate(working, tools), floor_tokens)
        threshold = self.threshold(prefix, tools, policy)
        if size <= threshold:
            return working, False
        # Size each candidate tail BEFORE paying for a summary (the old loop could buy two per call), and
        # compact down to a LOW-water mark. Keeping the largest tail that merely fit left the window at the
        # threshold again one turn later: measured on ten 15 KB reads, 14 paid summaries became 10, with
        # the ordinary 7.5 KB workload unchanged.
        floor = held['count'] if held else 0
        starts = []
        for keep in TAILS:
            start = max(0, len(body) - keep)
            while start > 0 and body[start].get('role') == 'tool':
                start -= 1
            if start > floor and start not in starts:
                starts.append(start)
        placeholder = 'x' * (SUMMARY_ALLOWANCE * 3)
        sized = [(start, self.estimate(self.rebuild(prefix, placeholder, body[start:]), tools)) for start in starts]
        fixed = self.estimate(prefix, tools)
        low = fixed + int(LOW_SHARE * (policy.input_budget - fixed))
        # The largest verbatim tail that leaves real room; when recent results are too big for that, the smallest tail.
        choice = next((start for start, estimate in sized if estimate <= low), sized[-1][0] if sized else None)
        if choice is None:
            if size <= policy.input_budget:
                return working, False
            raise ValueError('Current tool evidence still exceeds context after compaction; increase context or request narrower ranges')
        summary = summarize(body[:choice])
        rebuilt = self.rebuild(prefix, summary, body[choice:])
        if self.estimate(rebuilt, tools) > policy.input_budget:
            if size <= policy.input_budget:
                return working, False
            raise ValueError('Current tool evidence still exceeds context after compaction; increase context or request narrower ranges')
        self.boundary = {'count': choice, 'digest': _digest(body[:choice]), 'summary': summary}
        self._pending_key = self._key(prefix, self.boundary, policy)   # the floor retires with the old boundary
        self._save(self.state())
        return rebuilt, True


READ_COMMAND = re.compile(r'^\s*(?:cd\s+\S+\s*&&\s*)?(?:cat|ls|head|tail|sed\s+-n|grep|rg|find|wc|pwd|tree|stat|file|echo)\b')
NUDGE_AFTER, STOP_AFTER = 5, 10


class StallGuard:
    """Notice an agent that only re-reads files it has already seen.

    A live build spent three whole passes (52 turns) viewing the same three files. After
    NUDGE_AFTER read-only actions with a repeated target the next model call carries one
    specific correction; after STOP_AFTER the pass ends at its checkpoint so the
    controller's checks judge what exists instead of paying for more reading.
    """

    def __init__(self):
        self.streak, self.targets = 0, []

    @staticmethod
    def target(event):
        """The path or command of a read-only action, or '' for anything that does work."""
        if event.get('kind') != 'ActionEvent':
            return None
        action, tool = event.get('action') or {}, str(event.get('tool_name') or '')
        command = str(action.get('command') or '')
        if tool == 'file_editor' or action.get('kind') == 'FileEditorAction':
            return str(action.get('path') or 'view') if command == 'view' else ''
        if tool in {'grep', 'glob'} or action.get('kind') in {'GrepAction', 'GlobAction'}:
            return tool + ':' + str(action.get('pattern') or action.get('path') or '')
        if tool in {'terminal', 'execute_bash', 'bash'} or 'command' in action:
            return command.strip()[:160] if READ_COMMAND.match(command) and not re.search(r'>\s*\S|\btee\b', command) else ''
        return ''

    def record(self, event):
        target = self.target(event)
        if target is None:
            return
        if target == '':
            self.streak, self.targets = 0, []
        else:
            self.streak += 1
            self.targets.append(target)

    @property
    def repeating(self):
        return len(set(self.targets)) < len(self.targets)

    def stalled(self):
        return self.streak >= STOP_AFTER and self.repeating

    def correction(self, missing=()):
        if self.streak < NUDGE_AFTER or not self.repeating:
            return ''
        seen = ', '.join(dict.fromkeys(self.targets))[:400]
        text = ('CONTROLLER NOTICE: your last ' + str(self.streak) + ' actions only re-read content you already have (' + seen + '). '
                'Reading again adds nothing. ')
        if missing:
            text += 'These planned files do not exist yet: ' + ', '.join(list(missing)[:12]) + '. Create them now with the file editor. '
        return text + ('Your next action must change a file or run the project (install, build, start, tests). '
                       'If everything requested exists and runs, call finish.')
