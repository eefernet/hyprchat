"""Run one isolated policy-7 request; never enables or publishes product jobs.

Usage: python evals/run_policy7_proof.py --root /disk/proof/case --request case.json
Request: task, model, ollama_url, settings, files, explicit, protected, project_id.
Existing cases can only continue with --resume. Keep generated evidence ignored.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coder_policy7 import Experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--request')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--clarification', help='Answer a parked follow-up scope question with --resume')
    args = parser.parse_args()
    root = Path(args.root)
    if (root / 'job.json').exists() and not args.resume:
        raise SystemExit('Evidence exists. Use a fresh case directory or explicit --resume.')
    request = json.loads(Path(args.request).read_text()) if args.request else {}
    experiment = Experiment(root, **request)
    if args.resume:
        experiment.resume(clarification=args.clarification)
    result = experiment.run()
    print(json.dumps({k: result.get(k) for k in ('id', 'state', 'reason', 'revision_id', 'calls', 'seconds',
        'repair_round', 'audit_corrections', 'runnable', 'artifact')}, indent=2), flush=True)


if __name__ == '__main__':
    main()
