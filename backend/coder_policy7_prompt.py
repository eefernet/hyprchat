"""Bound the agent builder's task message to what the input budget leaves after the SDK's own overhead.

Sweep Kanban (2026-09-25): the round-2 repair prompt was rejected by the inference bridge before the model
ran (`estimate_tokens(messages + tools) > input_budget`). Measured on the pinned SDK: system prompt ~21 KB plus
tool schemas ≈ 14K tokens of fixed overhead, so at a 28,979-token budget the task message must stay under
~13K tokens. Sections are shrunk in priority order (checkpoint file listing, focused-defect JSON, evidence
catalog) until the estimate fits; the instructions, the original request and the builder guidance are kept.
"""
from __future__ import annotations

import json

from context_policy import estimate_tokens

OVERHEAD_TOKENS = 15000
# Room the session needs for its own work (file reads, tool results) before the bridge's limit: the v2 Kanban repair
# sessions started at ~22K tokens and overflowed on the ninth call without compaction ever firing.
HEADROOM_TOKENS = 8000
EXCLUDED = {'node_modules', '.venv', 'venv', '.git', 'target', 'build', 'obj', 'bin', 'dist', '__pycache__', '.pytest_cache', '.daedalus'}
# (listing chars, focused-defect chars or None for unbounded, evidence catalog budget divisor)
TIERS = ((12000, None, 3), (3000, 2500, 12), (1500, 1200, 24), (600, 600, 48))


def file_listing(root, limit):
    # Exclusions apply to the path INSIDE the project: the worker's own root is /root/.daedalus/jobs/<id>/workspace,
    # so matching against the absolute path emptied every production repair prompt (live laguna run, 2026-09-30).
    names = [str(p.relative_to(root)) for p in sorted(root.rglob('*')) if p.is_file() and not set(p.relative_to(root).parts) & EXCLUDED]
    text = ', '.join(names)
    return text if len(text) <= limit else text[:limit].rsplit(', ', 1)[0] + f', … ({len(names)} files)'


def focused_defects(failures, limit):
    text = json.dumps(failures)
    if limit is None or len(text) <= limit:
        return text
    slim = [{k: v for k, v in f.items() if k not in {'trace', 'reason', 'log_tail', 'input'}} if isinstance(f, dict) else f for f in failures]
    text = json.dumps(slim)
    return text if len(text) <= limit else text[:limit] + ' …]'


def repair_task(payload, repository, evidence, evidence_path, policy, *, catalog, estimate=estimate_tokens):
    """The policy-6+ task message, shrunk tier by tier until it fits; returns (text, tier_index)."""
    budget = policy.input_budget - OVERHEAD_TOKENS - HEADROOM_TOKENS
    repairing = bool(payload.get('round_id'))
    task, tier = '', 0
    # A repair round starts one tier tighter: its failures are already rendered in the builder guidance, so the
    # evidence catalog carries the brief only and the defect JSON is the compact form.
    for tier, (listing_cap, focused_cap, divisor) in list(enumerate(TIERS))[1 if repairing else 0:]:
        repair = ''
        if repairing:
            repair = ('REPAIR CHECKPOINT: ' + str(payload.get('revision_id')) + '\nCurrent files: ' + file_listing(repository.root, listing_cap)
                      + '\nFocused defects: ' + focused_defects(payload.get('evidence', {}).get('failures', []), focused_cap) + '\n')
        catalogued = {**evidence, 'failures': []} if repairing else evidence
        task = ('Work only in this project: ' + str(repository.root) + '.\n' + repair + 'Original request:\n' + payload['original_task'] +
                '\nImplement the complete requested change. The work brief is guidance. Inspect relevant source before editing; preserve explicit constraints. '
                'Run appropriate project checks and return a meaningful checkpoint when done or blocked. Do not repeat completion summaries. '
                'When more work remains, execute the next tool action instead of ending with a next-step announcement. '
                'Requested tests must be executable with a documented, discoverable test command; README claims and manual HTML pages are not automated tests. '
                'The controller independently verifies the saved revision after you return.\n' +
                payload.get('builder_guidance', '') +
                ('Previous work repeated without progress. Diagnose the specific failing assertion before one focused repair.\n' if payload.get('focused_recovery') else '') +
                'Full evidence and logs are at ' + str(evidence_path) + '; retrieve only relevant ranges.\n' +
                json.dumps({'revision': payload['revision_id'], 'brief': payload.get('brief'), 'evidence': catalog(catalogued, policy.input_budget // divisor)}))
        if estimate(task) <= budget:
            return task, tier
    return task, tier
