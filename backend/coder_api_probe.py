"""Controller-authored API assertions. Models provide data, never assertion code."""
import ast
import hashlib
import json
from pathlib import Path
import re
import shlex


def obj(properties, required):
    return {"type":"object","properties":properties,"required":required,"additionalProperties":False}


STRING = {"type":"string","minLength":1}
TARGET_SCHEMA = obj({"language":{"enum":["python","node"]},"path":STRING,"export":STRING},["language","path","export"])
EXPECT_SCHEMA = {"oneOf":[
    obj({"kind":{"const":"equal"},"value":{}},["kind","value"]),
    obj({"kind":{"const":"map_entries"},"value":{"type":"array","items":{"type":"array","minItems":2,"maxItems":2}}},["kind","value"]),
    obj({"kind":{"const":"raises"},"error_type":STRING,"message":{"type":"string"}},["kind","error_type"]),
    obj({"kind":{"const":"export_identity"},"export":STRING},["kind","export"]),
]}
CASES_SCHEMA = {"type":"array","minItems":1,"items":obj({"args":{"type":"array"},"kwargs":{"type":"object"},
    "expect":EXPECT_SCHEMA,"preserve_inputs":{"type":"boolean"}},["args","expect","preserve_inputs"])}
BINDINGS_SCHEMA = {"type":"array","minItems":1,"items":obj({"path":STRING,"export":STRING,"local_name":STRING},["path"])}


def fields(value, allowed, required, label):
    if not isinstance(value,dict): raise ValueError(f"{label}: expected an object")
    if set(value)-set(allowed): raise ValueError(f"{label}: unsupported fields {sorted(set(value)-set(allowed))}")
    if set(required)-set(value): raise ValueError(f"{label}: missing {sorted(set(required)-set(value))}")


def relative(path):
    if not isinstance(path,str) or not path.strip() or Path(path).is_absolute() or ".." in Path(path).parts or "\\" in path:
        raise ValueError("target.path: use a project-relative source file")
    return path


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r"[A-Za-z_$][\w$]*",value):
        raise ValueError("target.export: use an exported symbol name")


def validate_api(check):
    target = check.get("target")
    fields(target,{"language","path","export"},{"language","path","export"},"target")
    if target["language"] not in {"python","node"}: raise ValueError("target.language: choose python or node")
    relative(target["path"]); identifier(target["export"])
    if Path(target['path']).suffix not in ({'.py'} if target['language']=='python' else {'.js','.mjs','.cjs'}):
        raise ValueError("target.path: use a loadable Python/JavaScript module; complex runtimes use a reviewed raw probe")
    cases=check.get("cases")
    if not isinstance(cases,list) or not cases: raise ValueError("cases: supply at least one API call")
    for index,case in enumerate(cases):
        label=f"cases[{index}]"
        fields(case,{"args","kwargs","expect","preserve_inputs"},{"args","expect","preserve_inputs"},label)
        if not isinstance(case['args'],list) or not isinstance(case.get('kwargs',{}),dict): raise ValueError(f"{label}: args must be an array and kwargs an object")
        if target['language']=='node' and case.get('kwargs'): raise ValueError(f"{label}.kwargs: JavaScript takes positional args")
        if type(case['preserve_inputs']) is not bool: raise ValueError(f"{label}.preserve_inputs: expected boolean")
        expect=case['expect'];kind=expect.get('kind') if isinstance(expect,dict) else None
        required={'equal':{'kind','value'},'map_entries':{'kind','value'},'raises':{'kind','error_type'},'export_identity':{'kind','export'}}
        if kind not in required: raise ValueError(f"{label}.expect.kind: choose equal, map_entries, raises, or export_identity")
        fields(expect,required[kind]|({'message'} if kind=='raises' else set()),required[kind],label+'.expect')
        if kind=='map_entries' and (target['language']!='node' or not isinstance(expect['value'],list) or any(not isinstance(pair,list) or len(pair)!=2 for pair in expect['value'])):
            raise ValueError(f"{label}.expect.value: map_entries requires JavaScript Map entries as [key,value] pairs")
        if kind=='export_identity': identifier(expect['export'])
        if kind=='raises':
            identifier(expect['error_type'])
            if 'message' in expect and not isinstance(expect['message'],str): raise ValueError(f"{label}.expect.message: expected string")
    try: json.dumps(check,allow_nan=False)
    except (TypeError,ValueError) as error: raise ValueError("API inputs and expectations must be finite JSON values") from error


def validate_bindings(check):
    """Reject common copied/shadowed subjects before running raw assertions.

    Complex probes additionally undergo a fresh read-only semantic review. This
    check establishes direct imports/calls; it is not an untrusted-code sandbox.
    """
    bindings=check.get('bindings')
    if not isinstance(bindings,list) or not bindings: raise ValueError('bindings: raw probes must identify the actual project source')
    runner=check['runner'];program=check['program']
    for binding in bindings:
        fields(binding,{'path','export','local_name'},{'path'},'bindings')
        path=relative(binding['path']);export=binding.get('export');local=binding.get('local_name',export)
        if export:
            identifier(export);identifier(local)
        if runner in {'python','node'} and not export:
            raise ValueError('bindings.export: Python/Node raw probes must import and call the actual API; use shell for CLI/file checks')
        if runner=='python':
            tree=ast.parse(program)
            module=str(Path(path).with_suffix('')).replace('/','.')
            imported=any(isinstance(n,ast.ImportFrom) and n.module==module and any(a.name==export and (a.asname or a.name)==local for a in n.names) for n in tree.body)
            if not imported: raise ValueError(f'bindings: import {export} from {module} as {local}; do not copy the subject into the probe')
            for node in ast.walk(tree):
                if (isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and node.name in {export,local}) or (isinstance(node,ast.Name) and isinstance(node.ctx,ast.Store) and node.id in {export,local}):
                    raise ValueError(f'bindings: probe shadows the subject {local}')
            if not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id==local for n in ast.walk(tree)):
                raise ValueError(f'bindings: probe never calls {local}')
        elif runner=='node':
            imports=re.findall(r"import\s+(.+?)\s+from\s+['\"](.+?)['\"]",program,re.S)
            spec = re.escape(export)+(r'\s+as\s+'+re.escape(local) if local!=export else '')
            imported=any(p.removeprefix('./')==path.removeprefix('./') and (
                bool(re.search(r'(?:\{|,)\s*'+spec+r'\s*(?:,|\})',names)) or export=='default' and names.strip()==local) for names,p in imports)
            if not imported: raise ValueError(f'bindings: import {export} from ./{path}; do not copy the subject into the probe')
            if re.search(r'\b(?:function|class|const|let|var)\s+(?:'+re.escape(local)+'|'+re.escape(export)+r')\b',program) or re.search(r'(?<![\w$.])'+re.escape(local)+r'\s*=(?!=)',program):
                raise ValueError(f'bindings: probe shadows the subject {local}')
            if not re.search(r'(?<![\w$.])'+re.escape(local)+r'\s*\(',program): raise ValueError(f'bindings: probe never calls {local}')
        elif path not in program:
            raise ValueError(f'bindings: raw command does not reference {path}')


def source_bindings(root,check):
    root=Path(root).resolve()
    bindings=[check['target']] if check.get('runner')=='api' else check.get('bindings',[])
    result=[]
    for binding in bindings:
        path=root/relative(binding['path'])
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise FileNotFoundError(f"Requested source binding is missing: {binding['path']}")
        result.append({**binding,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    return result


PYTHON_RUNNER = r'''
import asyncio, copy, importlib, inspect, json, pathlib, sys
spec=json.loads(sys.argv[1]); target=spec['target']; path=pathlib.Path(target['path']).resolve()
sys.path.insert(0,str(pathlib.Path.cwd()))
name=str(pathlib.Path(target['path']).with_suffix('')).replace('/','.')
module=importlib.import_module(name)
assert pathlib.Path(module.__file__).resolve()==path, 'Imported module differs from bound project source'
fn=getattr(module,target['export']); assert callable(fn), 'Requested export is not callable'
def equal(actual,expected):
    if type(actual) in (int,float) and type(expected) in (int,float):
        assert actual==expected, f'Value mismatch: {actual!r} versus {expected!r}'
        return
    assert type(actual) is type(expected), f'Type mismatch: {actual!r} versus {expected!r}'
    if isinstance(expected,dict):
        assert actual.keys()==expected.keys(), f'Keys mismatch: {actual!r} versus {expected!r}'
        for key in expected: equal(actual[key],expected[key])
    elif isinstance(expected,list):
        assert len(actual)==len(expected), f'Length mismatch: {actual!r} versus {expected!r}'
        for a,b in zip(actual,expected): equal(a,b)
    else: assert actual==expected, f'Value mismatch: {actual!r} versus {expected!r}'
for index,case in enumerate(spec['cases']):
    args=copy.deepcopy(case['args']); kwargs=copy.deepcopy(case.get('kwargs',{})); before=copy.deepcopy([args,kwargs])
    expect=case['expect']; error=None
    try:
        actual=fn(*args,**kwargs)
        if inspect.isawaitable(actual): actual=asyncio.run(actual)
    except Exception as exc: error=exc
    try:
        if case['preserve_inputs']: equal([args,kwargs],before)
        if expect['kind']=='raises':
            assert error is not None, 'Expected an exception'
            assert type(error).__name__==expect['error_type'], f'Wrong exception: {type(error).__name__}: {error}'
            if 'message' in expect: equal(str(error),expect['message'])
        else:
            if error is not None: raise error
            if expect['kind']=='export_identity': assert actual is getattr(module,expect['export']), 'Wrong exported sentinel'
            else: equal(actual,expect['value'])
    except Exception as exc: raise AssertionError(f'API case {index}: {exc}') from exc
print(f"Verified {len(spec['cases'])} API cases against {target['path']}:{target['export']}")
'''

NODE_RUNNER = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
const spec=JSON.parse(process.argv[1]);
const module=await import(pathToFileURL(resolve(spec.target.path)).href);
const fn=module[spec.target.export]; assert.equal(typeof fn,'function','Requested export is not callable');
for (const [index,test] of spec.cases.entries()) {
  const args=structuredClone(test.args), before=structuredClone(args), expected=test.expect;
  let actual,error,threw=false;
  try { actual=await fn(...args); } catch (e) { error=e; threw=true; }
  try {
    if (test.preserve_inputs) assert.deepStrictEqual(args,before,'Input was changed');
    if (expected.kind==='raises') {
      assert.ok(threw,'Expected an exception');
      assert.equal(error?.constructor?.name,expected.error_type,'Wrong exception type');
      if (Object.hasOwn(expected,'message')) assert.equal(error?.message,expected.message);
    } else {
      if (threw) throw error;
      if (expected.kind==='map_entries') {
        assert.ok(actual instanceof Map,'Expected a Map');
        assert.deepStrictEqual([...actual.entries()],expected.value);
      } else if (expected.kind==='export_identity') {
        assert.ok(Object.hasOwn(module,expected.export),'Expected sentinel export is missing');
        assert.strictEqual(actual,module[expected.export]);
      } else assert.deepStrictEqual(actual,expected.value);
    }
  } catch (e) { throw new Error(`API case ${index}: ${e?.message ?? e}`,{cause:e}); }
}
console.log(`Verified ${spec.cases.length} API cases against ${spec.target.path}:${spec.target.export}`);
'''


def command(check):
    validate_api(check)
    data=json.dumps({'target':check['target'],'cases':check['cases']},allow_nan=False)
    if check['target']['language']=='python': return 'python3 -c '+shlex.quote(PYTHON_RUNNER)+' '+shlex.quote(data)
    return 'node --input-type=module -e '+shlex.quote(NODE_RUNNER)+' '+shlex.quote(data)


INSTRUCTION = '''For ordinary Python/JavaScript public functions use runner:"api", cwd, target:{language:"python"|"node",path,export},
cases:[{args:[...],expect:{kind:"equal",value:...},preserve_inputs:true|false}]. The controller imports and calls the real export.
Each args array is one function call's positional arguments; kwargs is optional for Python. Values must be plain JSON data.
Expect kinds: equal (strict value/type equality), map_entries (value is [[key,value],...] and result MUST be a Map),
raises (error_type and optional exact message), export_identity (export names a sentinel from the SAME module).
Do not convert numeric Map values to arrays. Example countBy: expect:{kind:"map_entries",value:[["fruit",2]]}.
Use preserve_inputs:true when inputs must remain unchanged. Include falsy inputs when relevant. Never provide a program for api.
For complex APIs, CLI commands, documentation or other runtimes, raw python/node/shell programs remain available,
with bindings:[{path,export,local_name?}] identifying their real imports/calls (shell file/CLI bindings need only path).
Raw Python/Node probes must directly import and call the subject; never copy, reimplement, shadow, or monkeypatch it.
The controller independently reviews raw probes. For greenfield projects use the agreed public interface; after build inspect real files
before diagnosing a missing binding. File-layout suggestions are advisory unless the original user requested that path.'''
