"""Bind requested build products to independently verified source revisions."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import re


def portability_checks(repository, revision):
    """Reject observed worker-browser paths in delivered executable source.

    This narrow source guard is not proof of portability; release validation
    also runs downloaded projects without any original browser-cache mounts.
    """
    locations, total, cursor = [], 0, ''
    extensions = {'.js', '.cjs', '.mjs', '.ts', '.tsx', '.jsx', '.py', '.json', '.sh'}
    while True:
        page = repository.inventory(cursor=cursor, limit=200)
        for item in page['items']:
            path = Path(item['path'])
            if path.suffix not in extensions or item.get('binary'): continue
            source = (repository.root / path).read_text(errors='replace')
            if not re.search(r'playwright|chromium|executablePath|executable_path', source, re.I): continue
            for number, line in enumerate(source.splitlines(), 1):
                if not re.search(r'/srv/hephaestus/|/root/\.cache/ms-playwright', line): continue
                total += 1
                if len(locations) < 20: locations.append({'path': str(path), 'line': number})
        cursor = page['next_cursor']
        if not cursor: break
    if not total: return []
    reason = ('Project browser configuration contains worker-specific paths at ' +
              ', '.join(f"{row['path']}:{row['line']}" for row in locations) +
              '. Remove those paths and executablePath overrides. Use the declared Playwright package with normal '
              'browser resolution; preparation supplies PLAYWRIGHT_BROWSERS_PATH. Document the local browser '
              'installation command for downloads. Preserve test names and assertions.')
    return [{'id': 'delivery:browser-portability', 'phase': 'delivery', 'origin': 'controller',
             'revision_id': revision, 'passed': False, 'execution_succeeded': False,
             'classification': 'nonportable_browser', 'reason': reason,
             'diagnostic': {'category': 'nonportable_browser', 'locations': locations,
                            'omitted_count': total - len(locations)}}]


def products(root, task, revision, contract=None):
    from coder_engine_contract import required_files
    from coder_repository import safe_relative
    root = Path(root)
    rows = []
    for name in ([r['path'] for r in contract['criteria']] if contract is not None else required_files(task)):
        if Path(name).suffix.lower() not in {'.jar', '.war', '.zip'}: continue
        path = safe_relative(root, name)
        if path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= 250*1024**2:
            rows.append({'name': name, 'path': str(path), 'revision_id': revision,
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': path.stat().st_size})
    return rows


def append_products(archive, rows, revision, worker_root):
    """Use only the previously observed product; never rebuild silently at delivery."""
    from coder_repository import safe_relative
    archive = Path(archive)
    verified = {}
    for row in rows:
        if row['revision_id'] != revision: raise ValueError('Build product belongs to another revision')
        name = row['name']
        if Path(name).suffix.lower() not in {'.jar', '.war', '.zip'}: raise ValueError('Unsupported packaged product')
        safe_relative(Path('/project'), name)
        source = Path(row['path'])
        if not source.resolve().is_relative_to(Path(worker_root).resolve()) or source.is_symlink():
            raise ValueError('Build product is outside the verification workspace')
        data = source.read_bytes()
        if len(data) != row['size'] or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('Verified build product changed before delivery')
        verified[name] = data
    if not verified: return
    temporary = archive.with_suffix('.products.tmp')
    try:
        with tarfile.open(archive) as original, tarfile.open(temporary, 'w:gz') as target:
            for member in original:
                if member.name not in verified:
                    target.addfile(member, original.extractfile(member) if member.isfile() else None)
            for name, data in verified.items():
                member = tarfile.TarInfo(name); member.size = len(data); member.mode = 0o644
                target.addfile(member, io.BytesIO(data))
        temporary.replace(archive)
    finally: temporary.unlink(missing_ok=True)
