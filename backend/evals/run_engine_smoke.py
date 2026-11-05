"""Isolated, real-worker development scenario. Never changes HyprChat settings."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_policy import DEFAULTS

from engine_cases import TASK


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', default='http://127.0.0.1:8587')
    parser.add_argument('--ollama', default='http://192.168.1.110:11434')
    parser.add_argument('--model', default='qwen3.6-35b-a3b-gguf:latest')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    deadline = time.monotonic() + 30
    while True:
        try:
            health = requests.get(args.worker + '/health', timeout=2)
            health.raise_for_status()
            if health.json().get('status') == 'ok':
                break
        except requests.RequestException:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('Candidate worker did not become healthy')
        time.sleep(.5)
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'job_id': 'cw3-' + uuid.uuid4().hex[:24], 'task': TASK, 'operations': []}
    settings = {**DEFAULTS, 'daedalus_role_thinking': {'builder': 'on', 'compaction': 'on'},
                'daedalus_compaction': 'on', 'daedalus_model_calls': 40, 'daedalus_job_seconds': 900,
                'daedalus_command_seconds': 120}
    base = {'engine': 'openhands159', 'policy_version': 7, 'model': args.model,
            'ollama_url': args.ollama, 'settings': settings, 'task': TASK, 'original_task': TASK,
            'mode': 'build_from_prompt', 'project_id': report['job_id'],
            'seconds_remaining': 900, 'calls_remaining': 40, 'revision_id': ''}
    def save():
        (args.output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    def operate(kind, **extra):
        number = len(report['operations']) + 1
        identity = report['job_id'] + '-' + str(number)
        payload = {**base, 'kind': kind, 'request_key': identity, **extra}
        url = args.worker + f"/jobs/{report['job_id']}/operations/{identity}"
        response = requests.post(url, json=payload, timeout=30); response.raise_for_status()
        report['operations'].append({'id': identity, 'kind': kind, 'status': 'running'}); save()
        while True:
            response = requests.get(url, timeout=30); response.raise_for_status()
            operation = response.json()
            report['operations'][-1] = operation; save()
            if operation['status'] not in {'queued', 'starting', 'running', 'cancelling'}:break
            time.sleep(2)
        print(kind, operation['status'], operation.get('calls'), flush=True)
        if operation['status'] != 'succeeded':
            raise RuntimeError(json.dumps(operation['result']))
        result = operation['result']
        if result.get('snapshot'):base['revision_id'] = result['snapshot']['revision']
        if result.get('model_digest'):base['model_digest'] = result['model_digest']
        return result, url
    inspected, _ = operate('inspect')
    built, _ = operate('code')
    verified, _ = operate('check')
    report['verification'] = verified
    packaged, url = operate('package', candidate=True, verification_summary=verified['summary'])
    archive = requests.get(url + '/artifact', timeout=30); archive.raise_for_status()
    report['archive_verified'] = hashlib.sha256(archive.content).hexdigest() == packaged['sha256']
    (args.output/'candidate.tar.gz').write_bytes(archive.content)
    save()
    print(json.dumps({'job_id': report['job_id'], 'summary': verified['summary'],
                      'archive_verified': report['archive_verified']}))


if __name__ == '__main__':
    main()
