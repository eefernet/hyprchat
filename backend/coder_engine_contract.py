"""Literal deliverable guards; these never invent a project layout or acceptance verdict."""
from pathlib import Path
import re

# Only explicit creation/delivery wording identifies a source deliverable.
# Input fixtures mentioned as examples are deliberately excluded.
FILE_PATH = r'[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|java|html|json|jsx|js|mjs|tsx|ts|go|rs|cpp|cc|cs|c|h|md|jar|war|zip)(?![\w/-]|\.\w)'
FILE = '(' + FILE_PATH + ')'
CREATED = re.compile(r'\b(?:create|build|write|deliver|include|save(?:\s+it)?\s+as)\s+(?:a\s+|an\s+)?`?'+FILE, re.I)
PACKAGED = re.compile(r'\b(?:deliver|produce)\b[^\n.!?]{0,100}?\b'+FILE, re.I)
PACKAGING = re.compile(r'\bpackage\s+(?:it\s+)?(?:as|into)\s+(?:an?\s+)?(?:executable\s+|archive\s+)?`?'+FILE, re.I)
ENTRY = re.compile(r'\b(?:python3?|node|java\s+-jar)\s+`?'+FILE, re.I)


FILE_LIST = r'`?' + FILE_PATH + r'`?(?:\s*(?:,\s*|(?:,?\s+)?and\s+)`?' + FILE_PATH + r'`?)*'
SCOPED = re.compile(r'`?([A-Za-z0-9_./-]+/)`?\s+(?:package|directory|folder)\s+(?:with|containing)\s+(' + FILE_LIST + r')', re.I)
CREATED_LIST = re.compile(r'\b(?:create|write|deliver|include)\s+(' + FILE_LIST + r')', re.I)
NEGATED = re.compile(r'\b(?:do not|don.t|never|not required|example only)\b|^\s*(?:input|example)\s*:', re.I)


def freeze(request, inherited=None):
    criteria = {row['path']: dict(row) for row in (inherited or {}).get('criteria', []) if row.get('path')}
    unresolved = []
    for line in re.split(r'\n|(?<=[.!?])\s+', request or ''):
        if NEGATED.search(line): continue
        scoped = list(SCOPED.finditer(line))
        names = set()
        for match in scoped:
            for file in re.finditer(FILE, match.group(2)):
                name = file.group(1)
                names.add(match.group(1) + name if '/' not in name else name)
        remainder = SCOPED.sub('', line)
        names.update(match.group(1) for pattern in (CREATED, ENTRY, PACKAGED, PACKAGING) for match in pattern.finditer(remainder))
        for match in CREATED_LIST.finditer(remainder):
            names.update(file.group(1) for file in re.finditer(FILE, match.group(1)))
        readme = re.search(r'\b(?:include|add|create|provide|write|update)\b[^\n]{0,100}\b(README(?:\.[A-Za-z0-9]+)?)\b', line, re.I)
        if readme: names.add(readme.group(1))
        for name in names:
            if Path(name).is_absolute() or '..' in Path(name).parts: continue
            criteria[name] = {'id': 'deliverable:' + name, 'path': name, 'request_excerpt': line[:1000], 'provenance': 'user_request'}
        if not scoped and re.search(r'\b(?:package|directory|folder)\s+with\s+' + FILE, line, re.I):
            unresolved.append({'request_excerpt': line[:1000], 'reason': 'The requested file location is ambiguous.'})
    return {'version': 1, 'criteria': list(criteria.values()), 'unverified': unresolved}


def required_files(task):
    text=task or ''
    files={match.group(1) for pattern in (CREATED,ENTRY,PACKAGED,PACKAGING) for match in pattern.finditer(text)}
    if re.search(r'\b(?:include|add|create|provide|write|update)\b[^.!?\n]{0,100}\bREADME\b',text,re.I):
        files.add('README')
    return sorted(p for p in files if not Path(p).is_absolute() and '..' not in Path(p).parts)


def checks(root,task,revision,contract=None):
    root=Path(root);rows=[]
    for name in ([r['path'] for r in contract['criteria']] if contract is not None else required_files(task)):
        paths=[root/name] if name!='README' else [p for p in root.iterdir() if p.name.lower() in {'readme','readme.md','readme.rst','readme.txt'}]
        present=any(p.is_file() and p.stat().st_size>0 for p in paths)
        rows.append({'id':'deliverable:'+name,'passed':present,'origin':'literal_request',
                     'revision_id':revision,'evidence_version':3,'classification':'passed' if present else 'missing_deliverable',
                     'reason':'' if present else 'The request explicitly names '+name+' but it is missing or empty.'})
    return rows


def apply_summary(summary, contract):
    """Ambiguous user wording is review evidence, never a fabricated failed file."""
    unresolved = (contract or {}).get('unverified', [])
    if unresolved:
        summary['checks_passed'] = False
        summary['unverified_deliverables'] = unresolved
        summary.setdefault('criteria', []).extend({**row, 'id': 'deliverable:unverified:' + str(index),
            'status': 'unverified', 'revision_id': summary['revision_id']} for index, row in enumerate(unresolved))
