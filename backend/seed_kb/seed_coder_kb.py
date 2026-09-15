#!/usr/bin/env python3
"""Audit or incrementally refresh the shared Coder Reference Docs KB.

Run from the deployed backend environment:
  python -m seed_kb.seed_coder_kb --audit
  python -m seed_kb.seed_coder_kb --refresh --report /tmp/coder-docs-report.json
Only catalogued source files are managed. No whole-KB deletion or model calls
are performed by --audit. Existing persona attachments and file IDs survive.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import httpx
from seed_kb.coder_sources import LEGACY_SOURCES, source_catalog

KB_NAME = "Coder Reference Docs"
KB_DESC = "Developer references across languages, frameworks, databases and tools, including Swift and SwiftUI. Source provenance and version/OS notes are recorded per document."
SOURCES = LEGACY_SOURCES  # historical catalog retained for provenance/audits


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def read_inventory(user_id='default', kb_id=None):
    """Audit reads SQLite in read-only mode; it never initializes or migrates it."""
    if not Path(config.DATABASE_PATH).exists():
        return None
    with sqlite3.connect(Path(config.DATABASE_PATH).as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if kb_id:
            row = conn.execute('SELECT * FROM knowledge_bases WHERE id=? AND user_id=?', (kb_id, user_id)).fetchone()
        else:
            row = conn.execute('SELECT * FROM knowledge_bases WHERE name=? AND user_id=?', (KB_NAME, user_id)).fetchone()
        if not row:
            return None
        kb = dict(row)
        kb['files'] = [dict(r) for r in conn.execute('SELECT * FROM kb_files WHERE kb_id=?', (kb['id'],))]
        return kb


def audit_inventory(kb):
    result = []; seen = {}
    legacy = {name.replace('django_reference.py', 'django_reference.md'): url for name, url, _ in LEGACY_SOURCES}
    managed = {s['filename'] for s in source_catalog()}
    for entry in (kb or {}).get('files', []):
        name = entry['filename']; path = Path(entry['filepath']); issues = []
        body = path.read_text(errors='replace') if path.is_file() else ''
        if not body: issues.append('missing or empty file')
        if not entry.get('source_url'): issues.append('missing source URL metadata')
        normalized = '\n'.join(body.splitlines()[2:]).strip()
        key = digest(normalized)
        if normalized and key in seen: issues.append('duplicate content: ' + seen[key])
        seen[key] = name
        if re.search(r'Swift 5 Cheatsheet|SwiftUI 2\.0|Vuex|componentWillMount|NavigationView|Next\.js 14', body):
            issues.append('legacy API/version material; review against target version')
        if re.search(r'curated list of|repository collects resources|udemy\.com/course', body, re.I):
            issues.append('resource list/course material; verify substantive reference coverage')
        result.append({'filename': name, 'managed': name in managed,
                       'previous_source': entry.get('source_url') or legacy.get(name, ''),
                       'chars': len(body), 'issues': issues})
    return result


def canonical_source(url):
    if url.startswith('https://developer.apple.com/tutorials/data/documentation/') and url.endswith('.md'):
        return url.replace('/tutorials/data/documentation/', '/documentation/')[:-3]
    return url


def normalize_document(raw, content_type, source, final_url):
    """Preserve Markdown examples; use the existing HTML extractor for web docs."""
    text = raw.decode('utf-8-sig', errors='replace') if isinstance(raw, bytes) else raw
    availability = []
    # Apple Markdown embeds platform availability in its leading JSON comment.
    first = re.match(r'\s*<!--\s*(\{[\s\S]*?\})\s*-->', text)
    if first:
        try: availability = json.loads(first[1]).get('availability', [])
        except (ValueError, TypeError): pass
    if 'html' in content_type or re.match(r'\s*<!doctype html|\s*<html', text, re.I):
        import trafilatura
        from lxml import html as lxml_html
        # The prose extractor strips indentation inside <pre>. Protect code
        # through extraction and restore only placeholders retained in the article.
        tree = lxml_html.fromstring(text.encode('utf-8'), parser=lxml_html.HTMLParser(encoding='utf-8'))
        examples = {}
        for pre in tree.xpath('//pre'):
            code = pre.text_content().strip('\n')
            if not code.strip(): continue
            classes = ' '.join(pre.xpath('ancestor-or-self::*/@class | .//code/@class'))
            language = re.search(r'(?:language|highlight|lang)-([\w+-]+)', classes)
            language = language[1] if language else ''
            if language in {'default', 'text'}: language = ''
            if language == 'pycon': language = 'python'
            fence = '`' * max(3, 1 + max((len(x) for x in re.findall(r'`+', code)), default=0))
            token = 'HYPRCHATCODE' + uuid.uuid4().hex
            examples[token] = f'{fence}{language}\n{code}\n{fence}'
            placeholder = lxml_html.Element('p'); placeholder.text = token
            placeholder.tail = pre.tail
            pre.getparent().replace(pre, placeholder)
        text = trafilatura.extract(lxml_html.tostring(tree, encoding='unicode'), url=final_url, output_format='markdown',
                                   include_comments=False, include_tables=True,
                                   include_links=True, include_formatting=True) or ''
        for token, code in examples.items(): text = text.replace(token, code)
    elif content_type and not (content_type.startswith('text/') or 'octet-stream' in content_type):
        raise ValueError('unsupported documentation content type: ' + content_type)
    # Swift-book comments contain compiler-negative tests, not user examples.
    text = re.sub(r'(^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?^ {0,3}\2[^\n]*(?:\n|$))|<!--[\s\S]*?-->',
                  lambda match: match[1] or '', text, flags=re.M)
    text = re.sub(r'\n{4,}', '\n\n\n', text).strip()
    if len(text) < 250 or re.search(r'^\s*(?:404[: ]|Access Denied|Just a moment\.\.\.)', text, re.I):
        raise ValueError('empty, blocked, or insubstantial documentation')
    # Source repositories sometimes hold bare code cheat sheets.
    ext = Path(urlparse(final_url).path).suffix
    languages = {'.js':'javascript', '.py':'python', '.sh':'bash', '.css':'css', '.php':'php'}
    if ext in languages and not re.search(r'^```', text, re.M):
        text = f"```{languages[ext]}\n{text}\n```"
    if availability:
        text = 'Platform availability: ' + '; '.join(str(x) for x in availability) + '\n\n' + text
    return text


async def fetch_source(client, source):
    from research import fetch_bytes_safely
    status, headers, final_url, raw = await fetch_bytes_safely(
        client, source['url'], timeout=25, max_bytes=5*1024*1024,
        headers={'User-Agent':'HyprChat-CoderDocs/1.0'},
    )
    if status != 200:
        raise ValueError(f'HTTP {status}')
    text = normalize_document(raw, headers.get('content-type', ''), source, final_url)
    return text, final_url


def render_document(source, text, final_url):
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return (f"# {source['title']}\n\nSource: {canonical_source(final_url)}\n"
            f"Authority: {source['authority']}\nVersion: {source['version']}\n"
            f"Fetched: {now}\nContent-SHA256: {digest(text)}\n\n{text}\n")


def _find_daedalus_config(configs):
    return next((c for c in configs if 'daedalus' in (c.get('name') or '').strip().lower()), None)


async def replace_document(kb, source, text, final_url, manifest, backup_dir):
    """Index a staged file first; only then publish bytes and file metadata."""
    import database as db
    import rag
    name = source['filename']
    if Path(name).name != name:
        raise ValueError('source filename must be a basename')
    matches = [f for f in kb.get('files', []) if f['filename'] == name]
    if len(matches) > 1:
        raise ValueError('multiple existing file rows; refusing ambiguous replacement')
    existing = matches[0] if matches else None
    directory = Path(config.KB_DIR)/kb['id']; directory.mkdir(parents=True, exist_ok=True)
    target = directory/name
    current = manifest.get(name, {})
    checksum = digest(text)
    source_url = canonical_source(final_url)
    if (current and target.is_file() and current.get('file_hash')
            and current['file_hash'] != hashlib.sha256(target.read_bytes()).hexdigest()):
        raise ValueError('managed file was edited locally; preserved for review')
    legacy_urls = {url for filename, url, _ in LEGACY_SOURCES if filename.replace('django_reference.py', 'django_reference.md') == name}
    if (existing and existing.get('source_url') and not current
            and existing['source_url'] not in legacy_urls | {source['url'], source_url}):
        raise ValueError('filename belongs to another source; user document preserved')
    if (target.is_file() and existing and current.get('content_hash') == checksum
            and current.get('source') == source and current.get('source_url') == source_url
            and current.get('file_hash') == hashlib.sha256(target.read_bytes()).hexdigest()
            and not current.get('warnings')):
        return {'filename': name, 'status': 'unchanged', 'chunks': current.get('chunks', 0)}
    original = target.read_bytes() if target.is_file() else None
    original_mode = target.stat().st_mode & 0o777 if original is not None else 0o644
    body = render_document(source, text, final_url)
    if original is not None:
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (backup_dir/name).write_bytes(original)
        (backup_dir/(name+'.metadata.json')).write_text(json.dumps(existing))
    fd, staged_name = tempfile.mkstemp(prefix='.coder-docs-', suffix='.md', dir=directory)
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, 'w') as stream: stream.write(body)
        result = await rag.index_file(kb['id'], name, str(staged))
        if result.get('error') or not result.get('chunks'):
            raise ValueError(result.get('error') or 'no indexed chunks')
        # Publish metadata under the existing row id; saved KB references survive.
        try:
            staged.chmod(original_mode)
            staged.replace(target)
            if existing:
                connection = await db.get_db()
                try:
                    await connection.execute('UPDATE kb_files SET filepath=?, file_size=?, file_type=?, source_url=? WHERE id=? AND kb_id=?',
                        (str(target), len(body.encode()), 'text/markdown', source_url, existing['id'], kb['id']))
                    await connection.commit()
                finally: await connection.close()
            else:
                file_id = await db.add_kb_file(kb['id'], name, str(target), len(body.encode()), 'text/markdown', source_url=source_url)
                kb.setdefault('files', []).append({'id':file_id, 'filename':name, 'filepath':str(target)})
        except Exception:
            # Restore searchable old content if publishing fails after indexing.
            if original is not None:
                staged.write_bytes(original)
                staged.chmod(original_mode)
                staged.replace(target)
                restored = await rag.index_file(kb['id'], name, str(target))
                if restored.get('error'): raise RuntimeError('publication failed and previous index restoration needs recovery')
            else:
                target.unlink(missing_ok=True)
                await rag.remove_file(kb['id'], name)
            raise
        manifest[name] = {'source':source, 'source_url':source_url, 'content_hash':checksum,
                          'file_hash':digest(body), 'chunks':result['chunks'],
                          'warnings':result.get('warnings', []), 'updated_at':datetime.now(timezone.utc).isoformat()}
        return {'filename': name, 'status':'updated' if existing else 'added',
                'chunks':result['chunks'], 'warnings':result.get('warnings', [])}
    finally:
        staged.unlink(missing_ok=True)


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--audit', action='store_true', help='read-only local inventory and source validation (default)')
    mode.add_argument('--refresh', action='store_true', help='incrementally publish validated source updates')
    parser.add_argument('--user-id', default='default')
    parser.add_argument('--kb-id')
    parser.add_argument('--only', nargs='+', help='limit source filenames (inventory audit still covers the KB)')
    parser.add_argument('--report', help='write the audit/update report to this explicit path')
    args = parser.parse_args(argv)
    kb = read_inventory(args.user_id, args.kb_id)
    if args.kb_id and kb is None: raise ValueError('KB does not exist or belongs to another user')
    report = {'mode':'refresh' if args.refresh else 'audit', 'kb_id':(kb or {}).get('id'),
              'checked_at':datetime.now(timezone.utc).isoformat(), 'inventory':audit_inventory(kb), 'sources':[]}
    sources = source_catalog()
    if args.only:
        unknown = set(args.only) - {s['filename'] for s in sources}
        if unknown: raise ValueError('unknown source filenames: '+', '.join(sorted(unknown)))
        sources = [s for s in sources if s['filename'] in args.only]
    manifest = {}; manifest_path = None
    if args.refresh:
        import database as db
        token = db.set_current_user_id(args.user_id)
        if not kb:
            # Existing initialized app DB is required. Do not migrate it from a maintenance CLI.
            if not Path(config.DATABASE_PATH).is_file(): raise ValueError('initialize HyprChat before refreshing docs')
            connection = await db.get_db()
            try:
                row = await (await connection.execute('SELECT id FROM users WHERE id=?', (args.user_id,))).fetchone()
                if row is None: raise ValueError('unknown user')
            finally: await connection.close()
            kb_id = 'kb-'+uuid.uuid4().hex[:12]
            await db.create_kb(kb_id, KB_NAME, KB_DESC)
            kb = {'id':kb_id, 'files':[]}
        report['kb_id'] = kb['id']
        directory = Path(config.KB_DIR)/kb['id']; directory.mkdir(parents=True, exist_ok=True)
        manifest_path = directory/'.coder-docs-manifest.json'
        if manifest_path.is_file(): manifest = json.loads(manifest_path.read_text())
        backup_dir = directory/'.coder-docs-backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=30, verify=config.HTTP_VERIFY_SSL) as client:
        async def fetch(spec):
            async with semaphore:
                try:
                    text, final = await fetch_source(client, spec)
                    return spec, text, final, None
                except Exception as exc: return spec, None, None, str(exc)
        pending = [asyncio.create_task(fetch(s)) for s in sources]
        try:
            for finished in asyncio.as_completed(pending):
                spec, text, final_url, error = await finished
                item = {'filename':spec['filename'], 'authority':spec['authority'], 'url':spec['url']}
                if error:
                    item.update(status='error', error=error)
                elif args.refresh:
                    try:
                        item.update(await replace_document(kb, spec, text, final_url, manifest, backup_dir))
                        temporary = manifest_path.with_suffix('.tmp')
                        temporary.write_text(json.dumps(manifest, indent=2)); temporary.replace(manifest_path)
                    except Exception as exc: item.update(status='error', error=str(exc))
                else:
                    item.update(status='validated', chars=len(text), content_hash=digest(text))
                report['sources'].append(item)
                print(json.dumps(item), flush=True)
        finally:
            for task in pending: task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    if args.refresh:
        await db.update_kb(kb['id'], description=KB_DESC)
        # Keep the established Daedalus attachment behavior; other personas are
        # attached by their existing seed endpoints, never matched by "coder".
        daedalus = _find_daedalus_config(await db.get_model_configs())
        if daedalus and kb['id'] not in daedalus.get('kb_ids', []):
            await db.update_model_config(daedalus['id'], kb_ids=[*daedalus.get('kb_ids', []), kb['id']])
        db.reset_current_user_id(token)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2))
    failures = [r for r in report['sources'] if r['status']=='error' or r.get('warnings')]
    print(json.dumps({'documents':len(report['sources']), 'failures':len(failures), 'kb_id':report['kb_id']}))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
