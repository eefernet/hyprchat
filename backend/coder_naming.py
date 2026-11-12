"""Readable download names for delivered projects.

The stored file, its URL and artifacts.filename stay internal and unique (job id + revision hash).
Only the name the browser saves and the title a person reads come from here.
"""
import json
import re
import tarfile
import tomllib
import unicodedata

NAME = re.compile(r'^[a-z0-9][a-z0-9.-]{0,79}\.tar\.gz$')
SUFFIX = {'accepted': '', 'review': '-review', 'unverified': '-unverified'}
MANIFEST_BYTES = 256 * 1024
GENERIC = {'app', 'application', 'demo', 'project', 'main', 'test', 'tests', 'example', 'sample', 'untitled',
           'my-app', 'myapp', 'program', 'src', 'index', 'package', 'root', 'build', 'solution', 'hello', 'new',
           'workspace', 'temp', 'tmp'}
_FILLER = re.compile(r"^(?:(?:please|kindly|hey|hi|ok|okay|so|now|just|can|could|would|will|you|i|we|want|need|like|"
                     r"to|have|us|me|make|build|create|write|generate|give|code|develop|implement|produce|design|"
                     r"a|an|the|some|simple|small|basic|plain|new|quick|little)\b[\s,]*)+", re.I)
_TRAILING = {'a', 'an', 'the', 'in', 'with', 'that', 'to', 'for', 'and', 'of', 'i', 'can', 'as', 'called', 'named',
             'which', 'using', 'by', 'from', 'on', 'at', 'or', 'it', 'is'}
_BOUNDARY = {'that', 'which', 'called', 'named', 'using', 'runnable', 'where', 'so', 'with', 'in'}
_ORDER = ('pyproject.toml', 'package.json', 'Cargo.toml', 'pom.xml', '.sln', 'CMakeLists.txt')


def _ascii(text):
    return unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode().lower()


def slug(text, limit=40, words=6):
    """Short lowercase name from the first clause of a request or a manifest name; '' when nothing usable."""
    text = _ascii(text).strip()
    clause = re.split(r'(?<=[a-z0-9)\]])[.!?;:](?:\s|$)|,\s|\n', text, maxsplit=1)[0]
    # "a tool called lines": the name the person gave it is the best name there is.
    given = re.search(r'\b(?:called|named)\s+["\'`]?([a-z0-9][a-z0-9_-]*)', clause)
    if given and given.group(1) not in GENERIC and given.group(1) not in _TRAILING:
        clause = given.group(1)
    clause = _FILLER.sub('', clause.strip())
    kept, size = [], 0
    for word in [w for w in re.split(r'[^a-z0-9]+', clause) if w][:words]:
        if word in _BOUNDARY and len(kept) >= 2: break
        if size + len(word) + bool(kept) > limit: break
        kept.append(word); size += len(word) + (len(kept) > 1)
    while kept and kept[-1] in _TRAILING: kept.pop()
    return '-'.join(kept)


def _manifest_name(name, data):
    text = data.decode('utf-8', 'replace')
    if name == 'pyproject.toml':
        table = tomllib.loads(text)
        return (table.get('project') or {}).get('name') or ((table.get('tool') or {}).get('poetry') or {}).get('name')
    if name == 'Cargo.toml':
        return (tomllib.loads(text).get('package') or {}).get('name')
    if name == 'package.json':
        value = json.loads(text).get('name')
        return value.rsplit('/', 1)[-1] if isinstance(value, str) else None
    if name == 'pom.xml':
        found = re.search(r'<artifactId>\s*([^<\s]+)\s*</artifactId>', re.sub(r'<parent>.*?</parent>', '', text, flags=re.S))
        return found and found.group(1)
    if name == 'CMakeLists.txt':
        found = re.search(r'\bproject\s*\(\s*"?([A-Za-z0-9_.+-]+)', text, re.I)
        return found and found.group(1)
    return None


def manifest_title(archive_path):
    """The package or app name a delivered archive declares, read in place from its shallowest manifests."""
    try:
        with tarfile.open(archive_path, 'r:*') as bundle:
            found = {}
            for member in bundle:
                if not member.isfile(): continue
                parts = [p for p in member.name.split('/') if p not in ('', '.')]
                if not parts or len(parts) > 2 or '..' in parts: continue
                kind = '.sln' if parts[-1].endswith('.sln') else parts[-1]
                if kind not in _ORDER: continue
                key = (len(parts), _ORDER.index(kind))
                if key in found: continue
                if kind == '.sln':
                    found[key] = parts[-1][:-4]
                elif member.size <= MANIFEST_BYTES:
                    try: found[key] = _manifest_name(kind, bundle.extractfile(member).read(MANIFEST_BYTES + 1))
                    except (ValueError, KeyError, AttributeError, TypeError): continue
            for key in sorted(found):
                title = slug(found[key]) if isinstance(found[key], str) else ''
                if title and title not in GENERIC: return title
    except (tarfile.TarError, OSError, EOFError):
        pass
    return None


def base_for(job, archive_path=None):
    """Manifest name, else the user's own first clause, else 'project'."""
    title = manifest_title(archive_path) if archive_path else None
    return title or slug(job.get('original_user_request') or job.get('user_task') or '') or 'project'


def download_name(base, kind='accepted', version=1):
    tail = (f'-v{int(version)}' if int(version) > 1 else '') + SUFFIX[kind] + '.tar.gz'
    base = (slug(base, limit=80 - len(tail), words=12) or 'project')
    name = base + tail
    if not NAME.fullmatch(name): raise ValueError('Unusable download name')
    return name


def display_name_for(row):
    """The validated readable name recorded on an artifact row, or None to keep the stored name."""
    meta = (row or {}).get('metadata')
    if not isinstance(meta, dict):
        try: meta = json.loads((row or {}).get('metadata_json') or '{}')
        except (ValueError, TypeError): meta = {}
    name = meta.get('download_name') if isinstance(meta, dict) else None
    return name if isinstance(name, str) and NAME.fullmatch(name) else None


def title_of(base):
    return ' '.join(str(base or '').split('-')).strip().capitalize()
