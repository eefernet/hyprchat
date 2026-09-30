"""Revision-bound captures and measured layout findings on managed services."""
from __future__ import annotations

import re
import time


def required(task):
    for clause in re.split(r'[.!?;\n]', task or ''):
        for match in re.finditer(r'\b(?:visual|screenshot)\s+(?:review|verification|inspection)\b|\b(?:review|inspect|verify)\s+(?:the\s+)?screenshots\b', clause, re.I):
            prefix = clause[:match.start()].lower()
            if not re.search(r"\b(?:no|not|never|skip|without|don't|do not)\b[^,]{0,45}$", prefix):
                return True
    return False


def capture(store, op, service, url, path, candidates=()):
    from coder_evidence import attach
    from coder_worker_runtime import _boundary
    operation = store.get(op)
    payload, identity = operation['payload'], operation['job_id']
    settings = payload['settings']
    folder = store.root / 'checks' / identity / op / ('visual-' + service['id'])
    folder.mkdir(parents=True, exist_ok=True)
    row = {'id': 'visual:' + service['id'], 'origin': 'controller', 'phase': 'visual', 'kind': 'policy7_browser',
           'service_id': service['id'], 'cwd': service.get('cwd', '.'), 'page_path': path,
           'passed': False, 'screenshots': [], 'traces': [], 'observations': [], 'layout_defects': [],
           'outcomes': [], 'evidence_types': [], 'execution_succeeded': False,
           'optional_visual': not payload.get('visual_required'), 'revision_id': payload['revision_id']}
    from coder_sandbox_call import invoke
    try:
        row = invoke('coder_policy7_visual', 'capture_browser',
            [str(folder), row, payload, list(candidates), url, path], {}, writable=[folder],
            timeout=min(payload['seconds_remaining'], settings['daedalus_command_seconds']),
            boundary=lambda: _boundary(store, op))
        for action in row.pop('actions', []):
            store.event(op, 'browser_action', revision_id=payload['revision_id'], action=action)
    except InterruptedError:
        raise
    except Exception as error:
        row.update(skipped=True, classification='environment', environment_fault=True, reason=str(error))
    return attach(store, identity, row, payload['revision_id'])


def capture_browser(folder, row, payload, candidates, url, path):
    from pathlib import Path
    from coder_sandbox_call import browser_options
    folder = Path(folder)
    settings = payload['settings']
    operation = {'started': time.time()}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True, timeout=settings['daedalus_browser_startup_seconds'] * 1000, **browser_options())
            try:
                for viewport in settings['daedalus_browser_viewports']:
                    remaining = payload['seconds_remaining'] - (time.time() - operation['started'])
                    timeout = max(1, min(remaining, settings['daedalus_browser_step_seconds'])) * 1000
                    context = browser.new_context(viewport={k: viewport[k] for k in ('width', 'height')})
                    context.tracing.start(screenshots=True, snapshots=True, sources=True)
                    try:
                        page = context.new_page()
                        page.set_default_timeout(timeout)
                        page.goto(url + path, wait_until='load', timeout=timeout)
                        page.wait_for_timeout(500)
                        layout = page.evaluate('() => ({width:innerWidth, scrollWidth:document.documentElement.scrollWidth})')
                        row['observations'].append({'viewport': viewport['name'], 'title': page.title(), 'layout': layout})
                        if layout['scrollWidth'] > layout['width'] + 2:
                            row['layout_defects'].append({'summary': 'Horizontal overflow at ' + viewport['name'],
                                                         'viewport': viewport['name'], 'measurement': layout})
                        for candidate in candidates:
                            if candidate['viewport'] != viewport['name']:
                                continue
                            from coder_browser import locate
                            target = locate(page, candidate['selector'])
                            if target.count() != 1 or not target.is_visible():
                                continue  # no objective reproduction: remains advisory
                            measured = target.evaluate('''e => {const r=e.getBoundingClientRect(), x=r.x+r.width/2,y=r.y+r.height/2,
                                top=document.elementFromPoint(x,y); return {x:r.x,width:r.width,viewportWidth:innerWidth,
                                obstructed:e.matches('button,input,select,textarea,a[href]') && x>=0 && y>=0 && x<innerWidth && y<innerHeight && !!top && !e.contains(top)}}''')
                            clipped = measured['x'] < -2 or measured['x'] + measured['width'] > measured['viewportWidth'] + 2
                            if candidate['category'] in {'clipping', 'overflow'} and clipped or candidate['category'] == 'overlap' and measured['obstructed']:
                                row['layout_defects'].append({**candidate, 'measurement': measured})
                        shot = folder / (viewport['name'] + '.png')
                        page.screenshot(path=str(shot), full_page=True, timeout=timeout)
                        row['screenshots'].append({'viewport': viewport['name'], 'path': str(shot)})
                        row.setdefault('actions', []).append({
                            'viewport': viewport['name'], 'action': 'visual_capture', 'path': path, 'status': 'passed'})
                    finally:
                        trace = folder / (viewport['name'] + '-trace.zip')
                        context.tracing.stop(path=str(trace))
                        row['traces'].append({'viewport': viewport['name'], 'path': str(trace)})
                        context.close()
            finally:
                browser.close()
        row.update(passed=not row['layout_defects'], classification='application_defect' if row['layout_defects'] else 'passed',
                   reason='; '.join(d['summary'] for d in row['layout_defects']))
    except InterruptedError:
        raise
    except Exception as error:
        row.update(skipped=True, classification='environment', reason='Visual capture unavailable: ' + str(error),
                   environment_fault=True)
        if row['layout_defects']:
            row.update(skipped=False, classification='application_defect', environment_fault=False,
                       reason='; '.join(d['summary'] for d in row['layout_defects']))
    return row


def measure(store, op, payload, findings):
    from coder_worker_runtime import operation_repo
    from coder_project_runtime import contract, run_revision
    candidates = {}
    for check in payload.get('evidence', {}).get('checks', []):
        if check.get('kind') != 'policy7_browser' or check.get('revision_id') != payload['revision_id']:
            continue
        shots = {s['evidence']['id']: s['viewport'] for s in check.get('screenshots', [])}
        rows = [{**f, 'viewport': shots[f['screenshot_id']]} for f in findings if f.get('screenshot_id') in shots
                and f.get('selector') and f.get('category') in {'clipping', 'overflow', 'overlap'}]
        if rows:
            candidates.setdefault(check['service_id'], []).extend(rows)
    if not candidates:
        return []
    current = store.get(op)
    store.update(op, payload={**current['payload'], 'visual_measurements': candidates})
    repo = operation_repo(store, current)
    job = payload.get('policy7_job', {})
    result = run_revision(store, op, repo, contract(repo, job.get('execution_commands')),
                          project_id=payload['project_id'], checks=[])
    observed = {c.get('service_id') for c in result['checks'] if c.get('kind') == 'policy7_browser'}
    for service in candidates.keys() - observed:
        result['checks'].append({'id': 'visual:' + service, 'origin': 'controller', 'phase': 'visual',
            'passed': False, 'skipped': True, 'optional_visual': not payload.get('visual_required'),
            'classification': 'environment', 'revision_id': payload['revision_id'],
            'reason': 'The service could not be started for visual measurement'})
    return result['checks']
