"""Real controller/worker development runs in a separate, explicitly chosen database.

Assertions in this module are held outside the coding sandbox. Never publish this
state as a user's project or make evaluation outcomes available to repair prompts.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import config
import database as db
import coder_jobs as controller
from db import coder_jobs as jobs, coder_promotion
from context_policy import DEFAULTS
from run_engine_smoke import TASK


async def run(args):
    root = args.output.resolve(); root.mkdir(parents=True, exist_ok=True)
    if not str(root).startswith('/opt/daedalus-openhands159-'):
        raise ValueError('Evaluation requires the isolated candidate release')
    db.DATABASE_PATH = str(root/'jobs.db')
    config.SANDBOX_OUTPUTS_DIR = str(root/'outputs')
    config.ARTIFACT_DIR = str(root/'artifacts')
    config.OLLAMA_URL = args.ollama
    config.CONTEXT_SETTINGS = {**DEFAULTS, 'daedalus_v3_enabled': True,
        'daedalus_engine': 'openhands159', 'daedalus_role_thinking': {'builder': 'on', 'compaction': 'on'},
        'daedalus_model_calls': 40, 'daedalus_job_seconds': 900, 'daedalus_command_seconds': 120}
    os.environ['DAEDALUS_ENGINE_URL'] = args.worker
    controller.spawn = lambda *args: None
    await db.init_db()
    conversation = root.name
    await db.create_conversation(conversation, model=args.model)
    report = {'started': time.time(), 'settings': config.CONTEXT_SETTINGS, 'jobs': []}
    def save():
        (root/'report.json').write_text(json.dumps(report, indent=2, default=str)+'\n')
    async with httpx.AsyncClient(timeout=30) as http:
        controller.configure(http, None)
        project = ''
        for index, task in enumerate([TASK, 'Add a mul command to calc.py for integer multiplication. Preserve all existing add, sub, negative-number, and error behavior. Add regression tests and update the README.']):
            job = await controller.create(conversation, task, 'build_from_prompt' if not project else 'edit_project',
                project, args.model, f'development-{index}', engine='openhands159')
            report['jobs'].append({'id': job['id'], 'status': 'running'}); save()
            runner = asyncio.create_task(controller.run(job['id']))
            deadline = time.monotonic()+1600
            while not runner.done():
                if time.monotonic() > deadline:
                    await controller.cancel(job['id']); raise TimeoutError('Development run deadline')
                current = await jobs.get(job['id'])
                report['jobs'][-1] = current; save()
                await asyncio.sleep(3)
            await runner
            job = await jobs.get(job['id']); report['jobs'][-1] = job; save()
            print(index, job['state'], job.get('blocker'), flush=True)
            if not job.get('candidate_artifact'):
                raise RuntimeError('No candidate delivered: '+str(job.get('failure') or job.get('blocker')))
            project = job['project_id']
            before = await db.get_coding_project(project)
            if index == 1:
                assert job['source_revision_id'] == report['jobs'][0]['revision_id']
                assert before['accepted_revision_id'] == report['jobs'][0]['revision_id']
            try:
                await coder_promotion.accept(project, '0'*40)
            except (ValueError, LookupError):
                pass
            else:
                raise AssertionError('Accepted an undelivered revision')
            promoted = await coder_promotion.accept(project, job['revision_id'])
            after = await db.get_coding_project(project)
            assert after['accepted_revision_id'] == job['revision_id']
            report['jobs'][-1]['exact_acceptance_verified'] = True
            report['jobs'][-1]['project_after_acceptance'] = after
            save()
    report['finished'] = time.time(); save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', default='http://127.0.0.1:8587')
    parser.add_argument('--ollama', default=os.environ.get('OLLAMA_URL','http://127.0.0.1:11434'))
    parser.add_argument('--model', default='qwen3.6-35b-a3b-gguf:latest')
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__': main()
