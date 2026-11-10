"""Model-free identity receipts for an immutable private qualification judge."""
import hashlib
import importlib.metadata
import json
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def receipt(settings, *, root=None, runtime_path='/etc/hephaestus/runtime.json'):
    from engine_cases import CASES, DEVELOPMENT, PROMPT_BANK_VERSION
    from engine_cases_everyday import CASES as everyday, POCKET, REHEARSAL, breadth_cases
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    manifest_bytes = (root / 'qualification-files.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    # Hash all controller Python sources, including lazy imports, plus runtime locks
    # and the language fixtures. The builder never receives this receipt.
    paths = sorted(p for p in (root / 'backend').rglob('*') if p.is_file()
                   and 'tests' not in p.relative_to(root).parts
                   and (p.suffix == '.py' or p.name.endswith('.lock.txt')))
    paths.append(root / 'scripts/hephaestus/qualify_languages.py')
    components = {}
    for path in paths:
        name = path.relative_to(root).as_posix()
        value = hashlib.sha256(path.read_bytes()).hexdigest()
        if not path.resolve().is_relative_to(root) or manifest.get(name) != value:
            raise ValueError('Judge source differs from its immutable manifest: ' + name)
        components[name] = value
    runtime = json.loads(Path(runtime_path).read_text())
    profiles = {k: runtime['profiles'][k]['image'] for k in ('core', 'languages')}
    banks = dict(development=DEVELOPMENT, classic=CASES, everyday=everyday,
                 pocket=POCKET, breadth=breadth_cases(), rehearsal=REHEARSAL)
    value = dict(version=1, release_id=hashlib.sha256(manifest_bytes).hexdigest()[:18],
                 manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                 components=components, components_sha256=digest(components),
                 judge_sha256=components['backend/evals/engine_judge.py'],
                 judge_bank_sha256=components['backend/evals/engine_cases_everyday.py'],
                 prompt_bank_version=PROMPT_BANK_VERSION,
                 banks={name: hashlib.sha256(json.dumps(bank, sort_keys=True).encode()).hexdigest()
                        for name, bank in banks.items()},
                 profiles=profiles, sdk_version=importlib.metadata.version('openhands-sdk'),
                 settings_sha256=digest(settings))
    return {**value, 'sha256': digest(value)}


def validate(value, environment, cohort, bank_sha256):
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('Missing judge identity receipt')
    if value.get('sha256') != digest({k: v for k, v in value.items() if k != 'sha256'}):
        raise ValueError('Invalid judge identity receipt digest')
    expected = environment.get('judge_identity')
    if not expected or value != expected:
        raise ValueError('Judge identity differs from the frozen environment')
    for key in ('profiles', 'sdk_version', 'judge_sha256', 'judge_bank_sha256'):
        if not environment.get(key) or environment[key] != value[key]:
            raise ValueError('Judge identity mismatch: ' + key)
    if (value.get('banks', {}).get(cohort) != bank_sha256
            or value['prompt_bank_version'] != environment.get('prompt_bank_version')
            or value['settings_sha256'] != digest(environment.get('settings', {}))):
        raise ValueError('Judge prompt bank or settings differ from the requested run')
