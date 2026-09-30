"""Diagnostic assertion outcomes. These are never a source of trusted acceptance."""
import json
import math

# Appended to the normal provenance tracer. Observe an existing exception; do not
# evaluate its expression again or call repr() on arbitrary application objects.
PYTHON_OUTCOMES = r'''
failures = []
expected = {}
old_trace = trace
import math as _outcome_math
def outcome_value(value):
    if type(value) is str:
        return value[:300].encode('utf-8', 'backslashreplace').decode('utf-8')[:300]
    if type(value) is float and not _outcome_math.isfinite(value):
        return str(value)
    if type(value) is int and value.bit_length() > 256:
        return '<integer: ' + str(value.bit_length()) + ' bits>'
    return value
def expected_assertion(frame):
    filename = frame.f_code.co_filename
    if filename not in expected:
        ranges = []
        try:
            tree = ast.parse(pathlib.Path(filename).read_bytes())
            for node in ast.walk(tree):
                if isinstance(node, (ast.With, ast.AsyncWith)):
                    for item in node.items:
                        call = item.context_expr
                        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in {'raises', 'assertRaises'}:
                            if call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == 'AssertionError':
                                ranges.append((node.lineno, node.end_lineno))
        except Exception:
            pass
        expected[filename] = ranges
    return any(start <= frame.f_lineno <= end for start, end in expected[filename])
def outcome_trace(frame, event, arg):
    old_trace(frame, event, arg)
    if event == 'exception' and isinstance(arg[1], AssertionError):
        if frame.f_globals.get('__name__') == 'unittest.case' and frame.f_code.co_name.startswith('assert'):
            frame = frame.f_back
        filename = frame.f_code.co_filename
        assertion_site = frame.f_lineno in lines.get(str(pathlib.Path(filename).resolve()), set()) or '.assert' in (pathlib.Path(filename).read_text(errors='replace').splitlines()[frame.f_lineno - 1] if pathlib.Path(filename).is_file() and frame.f_lineno <= len(pathlib.Path(filename).read_text(errors='replace').splitlines()) else '')
        if filename.startswith((str(root), str(audit))) and assertion_site:
            caller, anticipated = frame, False
            while caller:
                if expected_assertion(caller):
                    anticipated = True
                    break
                caller = caller.f_back
            if not anticipated and len(failures) < 50:
                values = {str(k)[:80]: outcome_value(v) for k, v in frame.f_locals.items()
                          if type(v) in (str, int, float, bool, type(None))}
                failures.append({'path': filename, 'line': frame.f_lineno, 'values': dict(list(values.items())[:20])})
    return outcome_trace
sys.settrace(outcome_trace)
import subprocess as _diagnostic_subprocess
import time as _diagnostic_time
_subprocess_results = []
_original_subprocess_run = _diagnostic_subprocess.run
def _diagnostic_text(value, limit=2000):
    if type(value) is bytes:
        return value[:limit].decode('utf-8', 'replace')
    if type(value) is str:
        return value[:limit]
    if type(value) is pathlib.PosixPath:
        return str(value)[:limit]
    return None
def _subprocess_context(caller, args, kwargs):
    command = args[0] if args else kwargs.get('args')
    argv = [_diagnostic_text(v, 300) for v in command[:40]] if type(command) in (list, tuple) else _diagnostic_text(command)
    cwd = _diagnostic_text(kwargs.get('cwd')) or os.getcwd()
    row = {'argv': argv, 'cwd': os.path.abspath(cwd), 'caller': caller.f_code.co_filename,
           'line': caller.f_lineno, 'returncode': None, 'stdout': None, 'stderr': None}
    fixtures = []
    if type(argv) is list and caller.f_code.co_filename.startswith(str(audit)):
        # Observe already-created fixture paths while their TemporaryDirectory
        # still exists. Never re-evaluate source expressions or arbitrary repr().
        allowed = [audit, pathlib.Path(os.environ['TMPDIR'])]
        for value in list(caller.f_locals.values())[:40]:
            text = _diagnostic_text(value)
            if not text or len(text) > 1000:
                continue
            path = pathlib.Path(text)
            if not path.is_absolute() or not any(path.is_relative_to(base) for base in allowed):
                continue
            for arg in argv[1:]:
                if not arg or arg.startswith('-') or pathlib.Path(arg).is_absolute() or '..' in pathlib.Path(arg).parts:
                    continue
                candidate = path / arg if path.is_dir() else path
                if candidate.name == pathlib.Path(arg).name and candidate.is_file() and not (pathlib.Path(row['cwd']) / arg).exists():
                    fixtures.append({'argument': arg, 'path': str(candidate)[:1000]})
        row['fixture_candidates'] = fixtures[:5]
    return row
def _observed_subprocess_run(*args, **kwargs):
    caller = sys._getframe(1)
    try:
        row = _subprocess_context(caller, args, kwargs)
    except Exception:
        # Observability must not change the command's behavior, including when
        # a caller has unusual locals, an invalid cwd, or an inaccessible path.
        row = {'caller': caller.f_code.co_filename, 'line': caller.f_lineno}
    try:
        result = _original_subprocess_run(*args, **kwargs)
        row.update(returncode=result.returncode, stdout=_diagnostic_text(result.stdout), stderr=_diagnostic_text(result.stderr))
        return result
    except (_diagnostic_subprocess.CalledProcessError, _diagnostic_subprocess.TimeoutExpired, OSError) as error:
        row.update(error=type(error).__name__, returncode=getattr(error, 'returncode', None),
                   stdout=_diagnostic_text(getattr(error, 'stdout', None)),
                   stderr=_diagnostic_text(getattr(error, 'stderr', None) or getattr(error, 'strerror', None)))
        raise
    finally:
        if caller.f_code.co_filename.startswith((str(root), str(audit))) and len(_subprocess_results) < 20:
            row['time_ns'] = _diagnostic_time.monotonic_ns()
            _subprocess_results.append(row)
_diagnostic_subprocess.run = _observed_subprocess_run
def save_outcomes():
    target = pathlib.Path(os.environ['DAEDALUS_PROVENANCE']) / ('outcomes-python-' + str(os.getpid()) + '.json')
    target.write_text(json.dumps({'version': 3, 'failures': failures, 'subprocesses': _subprocess_results}))
atexit.register(save_outcomes)
'''

NODE_OUTCOMES = r'''
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const original = require('node:assert');
const failures = []; let observed = 0;
const expected = new (require('node:async_hooks').AsyncLocalStorage)();
const wrappers = new WeakMap();
const primitive = v => ['string','number','boolean'].includes(typeof v) ? (typeof v === 'string' ? v.slice(0,300) : v) : v === null ? null : '<nonprimitive>';
function wrap(fn, name) {
  if (wrappers.has(fn)) return wrappers.get(fn);
  const wrapped = function(...args) {
    observed++;
    const anticipated = ['throws', 'rejects'].includes(name);
    if (anticipated && typeof args[0] === 'function') {
      const callback = args[0];
      args[0] = (...a) => expected.run(true, () => callback(...a));
    }
    const fail = error => {
      if (!expected.getStore() && failures.length < 50) failures.push({method: name, stack: String(error.stack || '').slice(0,2000),
        actual: primitive(error.actual), expected: primitive(error.expected)});
      throw error;
    };
    try {
      const result = Reflect.apply(fn, this, args);
      return result && typeof result.then === 'function' ? result.catch(fail) : result;
    } catch (error) { return fail(error); }
  };
  wrappers.set(fn, wrapped);
  return wrapped;
}
for (const target of [original, original.strict]) {
  for (const key of Object.keys(target)) {
    if (typeof target[key] === 'function' && key !== 'AssertionError' && key !== 'strict') target[key] = wrap(target[key], key);
  }
}
const callable = wrap(original, 'assert'); Object.assign(callable, original);
const load = Module._load;
Module._load = function(name, ...args) {
  if (name === 'assert' || name === 'node:assert') return callable;
  return load.call(this, name, ...args);
};
require('node:module').syncBuiltinESMExports();
process.on('exit', () => {
  fs.writeFileSync(path.join(process.env.DAEDALUS_PROVENANCE, `outcomes-node-${process.pid}.json`), JSON.stringify({version:3, failures, observed}));
});
'''


def _finite_float(text):
    value = float(text)
    return value if math.isfinite(value) else text


def _read_json(path):
    # Evidence is untrusted input. Non-finite JSON extensions and overflowing
    # exponents must never make status/events responses unserializable.
    def safe_strings(value):
        if isinstance(value, str):
            return value.encode('utf-8', 'backslashreplace').decode('utf-8')
        if isinstance(value, list):
            return [safe_strings(item) for item in value]
        if isinstance(value, dict):
            return {safe_strings(key): safe_strings(item) for key, item in value.items()}
        return value
    return safe_strings(json.loads(path.read_text(), parse_constant=str, parse_float=_finite_float))


def read_outcomes(directory):
    failures, observed = [], 0
    for path in directory.glob('outcomes-*.json'):
        try:
            if path.stat().st_size > 1024 * 1024:
                continue
            result = _read_json(path)
            if result.get('version') != 3:
                continue
            failures.extend(result.get('failures', [])[:50])
            observed += max(0, int(result.get('observed', 0)))
        except (OSError, ValueError, TypeError):
            continue
    return failures[:50], observed


def read_subprocesses(directory):
    """Bounded child diagnostics, never acceptance evidence."""
    rows = []
    for path in directory.glob('outcomes-python-*.json'):
        try:
            if path.stat().st_size > 1024 * 1024:
                continue
            result = _read_json(path)
            if result.get('version') != 3:
                continue
            for row in result.get('subprocesses', [])[:20]:
                if isinstance(row, dict) and type(row.get('time_ns')) is int:
                    rows.append(row)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(rows, key=lambda row: row['time_ns'])[-30:]


def audit_fixture_path_error(rows):
    """A missing relative argument refers to a fixture observed elsewhere by the audit."""
    import re
    if not rows:
        return None
    row = rows[-1]
    if not row.get('returncode') or not re.search(r"no such file|can't open file|ENOENT", row.get('stderr') or '', re.I):
        return None
    for fixture in row.get('fixture_candidates', []):
        if fixture.get('argument') and fixture['argument'] in row.get('argv', []) and fixture['argument'] in row['stderr']:
            return {'argument': fixture['argument'], 'fixture': fixture.get('path'), 'cwd': row.get('cwd'),
                    'stderr': row.get('stderr')}
    return None
