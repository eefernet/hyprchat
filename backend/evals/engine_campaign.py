"""Frozen qualification identities and explicit infrastructure pause semantics."""
import hashlib
import importlib.metadata
import json
import subprocess
from pathlib import Path
import sys


class CampaignPaused(RuntimeError):
    pass


def collect_evaluator_caches(release):
    """Private evaluation dependencies are reconstructible; reports are not."""
    from types import SimpleNamespace
    from coder_engine_cache import collect
    release = Path(release).resolve()
    if not (release / 'codex-candidate').is_file():
        raise ValueError('Evaluator cache collection requires a marked candidate release')
    receipts = []
    for root in sorted(release.glob('*/evaluator-worker')):
        if root.is_symlink() or not root.resolve().is_relative_to(release) or not (root.parent / 'report.json').is_file():
            continue
        removed = collect(SimpleNamespace(root=root), limit_bytes=0)
        if removed:
            receipts.append({'store': str(root.relative_to(release)), 'removed': removed})
    return receipts


def freeze(root, settings, cohort):
    root = Path(root)
    files = {}
    for name in ('backend/engine-requirements.lock.txt', 'installed-requirements.txt', 'worker.service',
                 'daedalus-candidate.slice', 'scripts/qualify_engine_report.py', 'tokenizer/manifest.json', 'tokenizer/tokenizer.json',
                 'tokenizer/chat_template.jinja'):
        path = root / name
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    browsers = Path(importlib.metadata.distribution('playwright').locate_file('playwright/driver/package/browsers.json'))
    service = 'daedalus-reliability-159.service' if cohort == 'reliability' else 'daedalus-qualification-159.service'
    properties = subprocess.check_output(['systemctl', 'show', service, '-p', 'MemoryMax', '-p', 'TasksMax',
        '-p', 'OOMPolicy', '-p', 'Delegate', '-p', 'DelegateSubgroup', '-p', 'Slice'], text=True)
    aggregate = subprocess.check_output(['systemctl', 'show', 'daedalus-candidate.slice', '-p', 'MemoryMax', '--value'], text=True).strip()
    return {'cohort': cohort, 'settings': settings, 'files': files, 'python': sys.version,
            'sdk': importlib.metadata.version('openhands-sdk'), 'tools': importlib.metadata.version('openhands-tools'),
            'controller_browser_requirements': json.loads(browsers.read_text()),
            'service_properties': properties, 'aggregate_memory_max': aggregate,
            'limits': {'aggregate_memory_bytes': 5*1024**3, 'operation_memory_bytes': 3*1024**3,
                       'operation_tasks': 256, 'worker_tasks': 512},
            'tokenizer_revision': '995ad96eacd98c81ed38be0c5b274b04031597b0'}


def interruption(job):
    """Application errors, bad dependency declarations and OOMs remain scored failures."""
    failure = job.get('failure') or {}
    category = failure.get('category')
    if category in {'worker_interrupted', 'network', 'upstream_service'}:
        return job.get('blocker') or category
    text = str(job.get('blocker', '')).lower()
    if any(value in text for value in ('worker interrupted', 'reconnecting to the worker', 'connection refused',
                                       'free disk space is below', 'no space left on device',
                                       'worker storage allowance exceeded', '507 insufficient storage',
                                       'insufficient worker disk space', 'artifact storage is below')):
        return job.get('blocker')
    return None


def exception_interruption(error):
    """Storage failures in the private evaluator pause just like worker failures."""
    return interruption({'blocker': str(error)})


def paused(report, trial, reason):
    detail = {'reason': str(reason), 'trial': trial['key'], 'budget_mode': 'preserve'}
    report['paused'] = detail
    trial.setdefault('interruptions', []).append(detail)
    return detail
