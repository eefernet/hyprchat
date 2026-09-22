"""Controller-owned file assertions against immutable source revisions."""
import hashlib
from pathlib import Path
import re

from coder_api_probe import fields, relative
from coder_repository import safe_relative


def validate_file(check, requirements):
    relative(check.get('path'))
    if check.get('cwd', '.') != '.':
        raise ValueError('File paths are project-relative; cwd must be .')
    if any(r['kind'] not in {'documentation', 'preservation'} for r in requirements):
        raise ValueError('File assertions cannot establish runtime behavior or API interfaces')
    if any(re.search(r'\breadme\b', r.get('text', ''), re.I) for r in requirements):
        if not re.fullmatch(r'readme(?:\.[\w-]+)?', Path(check['path']).name, re.I):
            raise ValueError('A README requirement must bind to the README itself')
    assertions = check.get('assertions')
    if not isinstance(assertions, list) or not assertions:
        raise ValueError('File check needs nonempty assertions')
    for assertion in assertions:
        kind = assertion.get('kind') if isinstance(assertion, dict) else None
        required = {'kind', 'value'} if kind == 'contains' else {'kind'}
        fields(assertion, required, required, 'file assertion')
        if kind not in {'exists', 'nonempty', 'contains', 'unchanged'}:
            raise ValueError('File assertion must be exists, nonempty, contains, or unchanged')
        if kind == 'contains' and (not isinstance(assertion['value'], str) or not assertion['value']):
            raise ValueError('contains requires nonempty literal text')
    if any(r['kind'] == 'preservation' for r in requirements) and not any(a['kind'] == 'unchanged' for a in assertions):
        raise ValueError('File preservation requires unchanged against the baseline revision')


def execute_file(repository, root, check, baseline):
    path = safe_relative(root, relative(check['path']))
    result = {'source_bindings': [], 'assertions': [], 'passed': False}
    if not path.is_file():
        return {**result, 'failure_kind': 'file_assertion_failed', 'error': f"Missing file: {check['path']}"}
    content = path.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    result['source_bindings'] = [{'path': check['path'], 'sha256': sha}]
    for assertion in check['assertions']:
        kind = assertion['kind']
        evidence = dict(assertion)
        if kind == 'unchanged':
            if not baseline or not re.fullmatch(r'[a-f0-9]{40,64}', baseline):
                raise ValueError('File preservation needs an immutable baseline revision')
            # Neither the model nor the working tree supplies preservation hashes.
            original = repository.git('show', f"{baseline}:{check['path']}")
            evidence['baseline_sha256'] = hashlib.sha256(original).hexdigest()
            evidence['baseline_revision'] = baseline
            passed = evidence['baseline_sha256'] == sha
        else:
            passed = kind == 'exists' or (kind == 'nonempty' and bool(content.strip())) or (
                kind == 'contains' and assertion['value'].encode() in content)
        result['assertions'].append({**evidence, 'passed': passed})
    result['passed'] = all(a['passed'] for a in result['assertions'])
    if not result['passed']:
        result.update(failure_kind='file_assertion_failed', error=f"File assertions failed: {check['path']}")
    return result
