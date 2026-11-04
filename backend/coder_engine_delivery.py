"""Bind requested build products to independently verified source revisions."""
import hashlib
import io
import json
from pathlib import Path
import tarfile


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
