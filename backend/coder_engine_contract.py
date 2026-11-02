"""Literal deliverable guards; these never invent a project layout or acceptance verdict."""
from pathlib import Path
import re

# Only explicit creation/delivery wording identifies a source deliverable.
# Input fixtures mentioned as examples are deliberately excluded.
FILE = r'([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|java|html|js|mjs|ts|tsx|jsx|go|rs|cpp|cc|c|h|cs|json|md|jar|war|zip))'
CREATED = re.compile(r'\b(?:create|build|write|deliver|include|save(?:\s+it)?\s+as)\s+(?:a\s+|an\s+)?`?'+FILE, re.I)
PACKAGED = re.compile(r'\b(?:package[ds]?|deliver|produce)\b[^\n.!?]{0,100}?\b'+FILE, re.I)
ENTRY = re.compile(r'\b(?:python3?|node|java\s+-jar)\s+`?'+FILE, re.I)


def required_files(task):
    text=task or ''
    files={match.group(1) for pattern in (CREATED,ENTRY,PACKAGED) for match in pattern.finditer(text)}
    if re.search(r'\b(?:include|add|create|provide|write|update)\b[^.!?\n]{0,100}\bREADME\b',text,re.I):
        files.add('README')
    return sorted(p for p in files if not Path(p).is_absolute() and '..' not in Path(p).parts)


def checks(root,task,revision):
    root=Path(root);rows=[]
    for name in required_files(task):
        paths=[root/name] if name!='README' else [p for p in root.iterdir() if p.name.lower() in {'readme','readme.md','readme.rst','readme.txt'}]
        present=any(p.is_file() and p.stat().st_size>0 for p in paths)
        rows.append({'id':'deliverable:'+name,'passed':present,'origin':'literal_request',
                     'revision_id':revision,'evidence_version':3,'classification':'passed' if present else 'missing_deliverable',
                     'reason':'' if present else 'The request explicitly names '+name+' but it is missing or empty.'})
    return rows
