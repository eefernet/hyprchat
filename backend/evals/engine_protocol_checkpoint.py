"""Resume qualification without replaying an uncertain or completed model probe."""
import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path
import time

CASES = ('native_tool', 'tool_continuation', 'large_edit', 'reasoning', 'compaction', 'termination')


def probe_hash(path):
    tree = ast.parse(Path(path).read_text())
    nodes = [node for node in ast.walk(tree) if (
        isinstance(node, ast.FunctionDef) and node.name == 'chat') or (
        isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name) and node.test.left.id == 'name'
        and isinstance(node.test.ops[0], ast.In)) or (
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {'ADD','WRITE'} for t in node.targets))]
    if len(nodes) != 4: raise ValueError('Unknown protocol probe structure')
    return hashlib.sha256(json.dumps([ast.dump(n, include_attributes=False) for n in nodes]).encode()).hexdigest()


def freeze(runner, endpoint, limits):
    root = Path(runner).resolve().parents[2]
    manifest_path = root/'qualification-files.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    components = {}
    for path in sorted((root/'backend').rglob('*.py')):
        if {'tests','evals','__pycache__'} & set(path.relative_to(root).parts): continue
        name = str(path.relative_to(root)); digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if manifest is not None and manifest.get(name) != digest: raise ValueError('Protocol source differs from manifest')
        components[name] = digest
    runtime = Path('/etc/hephaestus/runtime.json')
    return dict(version=1, endpoint=endpoint, limits=limits, cases=list(CASES), repeats=3,
                probes=probe_hash(runner), components=components,
                sdk=importlib.metadata.version('openhands-sdk'),
                profiles=json.loads(runtime.read_text())['profiles'] if runtime.exists() else None)


def load(path, expected, identity, store, *, resume=False, adopt=None):
    if not path.exists():
        if resume: raise ValueError('No protocol checkpoint to resume')
        return {**expected, 'freeze':identity, 'admission':{}}
    if not resume: raise ValueError('Use --resume or a new matrix directory; never overwrite a prior result')
    raw = path.read_bytes(); report = json.loads(raw)
    if any(report.get(k) != v for k,v in expected.items() if k != 'rows'):
        raise ValueError('Protocol model or settings changed')
    if report.get('freeze') != identity:
        if report.get('freeze') or not adopt: raise ValueError('Protocol frozen inputs changed')
        manifest = json.loads(adopt.read_text())
        if any(manifest.get(n) != h for n,h in identity['components'].items()):
            raise ValueError('Original protocol controller sources changed')
        original = adopt.parent/'backend/evals/run_engine_protocol.py'
        if (hashlib.sha256(original.read_bytes()).hexdigest() != manifest.get('backend/evals/run_engine_protocol.py')
                or probe_hash(original) != identity['probes']):
            raise ValueError('Original protocol prompts or assertions changed')
        report['freeze'] = identity
        report['checkpoint_adoption'] = {'at':time.time(), 'previous_report_sha256':hashlib.sha256(raw).hexdigest(),
                                         'manifest_sha256':hashlib.sha256(adopt.read_bytes()).hexdigest()}
    seen = set()
    for row in report['rows']:
        key = (row['output_limit'], row['repeat'], row['case'])
        if key in seen or key[0] not in identity['limits'] or key[1] not in range(1,4) or key[2] not in CASES:
            raise ValueError('Invalid protocol checkpoint roster')
        seen.add(key)
        op = store.get(f'{key[0]}-{key[1]}-{key[2]}')
        if (not op or op['calls'] != row['calls'] or op['status'] != ('succeeded' if row['passed'] else 'failed')
                or op['payload'].get('model') != expected['model']
                or op['payload'].get('model_digest') != expected['digest']
                or ('settings' in expected and op['payload']['settings'] != expected['settings'])):
            raise ValueError('Protocol checkpoint does not match durable model usage')
    backup = path.with_name('report.before-resume.json')
    if not backup.exists(): backup.write_bytes(raw)
    report.setdefault('resumes', []).append({'at':time.time(), 'retained_rows':len(report['rows'])})
    return report


def admit(store, identity, payload, report, persist):
    operation = store.get(identity)
    if operation and (operation['status'] != 'queued' or operation['calls'] or operation['cancel_requested']):
        raise ValueError('Unreported protocol probe may have executed; automatic replay is forbidden')
    if operation and operation['payload'] != payload:
        raise ValueError('Queued protocol input changed')
    ledger = report.setdefault('admission', {}).setdefault(identity, {
        'deadline':time.time() + payload['settings'].get('daedalus_preparation_seconds',600), 'waits':0})
    persist()
    while True:
        if time.time() >= ledger['deadline']: raise TimeoutError('Protocol admission allowance exhausted')
        try:
            store.create(identity,'protocol','code',payload)
            store.update(identity,status='running',started=time.time())
            return
        except BlockingIOError:
            ledger['waits'] += 1; persist()
            time.sleep(min(2, max(0, ledger['deadline'] - time.time())))
