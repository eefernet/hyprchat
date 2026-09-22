"""Static hygiene for model-written HTTP audits.

Seen on the isolated rerun of a live Kanban build: the application passed an independent
HTTP exercise, but its audit asserted `len(tasks) == 1` against a database that already
held rows, called `GET /api/tasks/{id}` (an endpoint the request never listed), and hid
the failing line behind `except Exception as e: print(e)`. Two correction rounds were
spent on a working app. These defects are visible without running anything.
"""
from __future__ import annotations

import re

ENDPOINT = re.compile(r'\b(GET|POST|PUT|PATCH|DELETE)\s+(/[\w/:{}.\-]*)', re.I)
# The path may contain f-string interpolations with quotes/brackets inside braces ({task['id']}); they must stay
# one segment so shape() reads them as a wildcard, or a valid DELETE /api/tasks/{id} looks unrequested.
CALL = re.compile(r'''requests\.(get|post|put|patch|delete)\(\s*f?["']\{?[\w.\[\]"']*\}?(/(?:\{[^}]*\}|[^"'?{])*)''', re.I)
SWALLOW = re.compile(r'^(?P<indent>[ \t]*)except Exception as (?P<name>\w+):\n(?P<body>[ \t]+)(?=print\()', re.M)


def shape(path):
    return re.sub(r'/(?::\w+|\{[^}]*\}|\d+)', '/*', path.rstrip('/')) or '/'


def requested_endpoints(request):
    return {(method.upper(), shape(path)) for method, path in ENDPOINT.findall(request or '')}


def web_audit_faults(name, text, request=''):
    faults = []
    if 'DAEDALUS_APP_URL' not in text:
        return faults
    if re.search(r'assert\s+len\(\s*\w+\s*\)\s*==\s*\d+', text):
        faults.append(f'{name} asserts an exact number of records (assert len(...) == N). The application database is NOT empty: '
                      'it keeps rows from earlier runs. Create a record with a unique title, then find THAT record by its id or title '
                      'in the listing; after deleting, assert that id is absent. Never assert total counts.')
    listed = requested_endpoints(request)
    if listed:
        extra = sorted({f'{method.upper()} {path}' for method, path in CALL.findall(text)
                        if path.startswith('/api') and (method.upper(), shape(path)) not in listed})
        if extra:
            faults.append(f'{name} calls endpoints the request never specified: ' + ', '.join(extra[:6]) + '. Use ONLY the requested '
                          'endpoints (' + ', '.join(sorted(m + ' ' + p for m, p in listed)) + '); read one record by filtering the list response.')
    return faults


def show_failing_line(text):
    """A bare AssertionError prints nothing: keep the traceback so a correction can see which assert failed."""
    if 'traceback.print_exc' in text:
        return text
    return SWALLOW.sub(lambda m: f"{m['indent']}except Exception as {m['name']}:\n{m['body']}import traceback; traceback.print_exc()\n{m['body']}", text)
