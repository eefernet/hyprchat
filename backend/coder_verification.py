"""Requirement and evidence contracts for policy-v2 persistent jobs.

Shared with the standalone worker; this module has no backend/DB dependencies.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import shlex

from context_policy import estimate_tokens


POLICY_VERSION = 5
KINDS = {"behavior", "interface", "preservation", "tests", "documentation", "visual"}


def normalized(text):
    return " ".join(str(text).split())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_requirements(answer, task, policy_version=3):
    from coder_checks import validate_plan
    validate_plan(answer, policy_version=policy_version)
    requirements = answer.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("Plan needs requirements grounded in the original request")
    ids = set()
    for item in requirements:
        if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(item.get("id", ""))) or item["id"] in ids:
            raise ValueError("Each requirement needs a unique stable id")
        ids.add(item["id"])
        if item.get("kind") not in KINDS or not str(item.get("text", "")).strip():
            raise ValueError("Each requirement needs text and a supported kind")
        quote = normalized(item.get("source_quote", ""))
        if not quote or quote not in normalized(task):
            raise ValueError(f"Requirement source_quote must quote the original user request. Requirement {item['id']} supplied {item.get('source_quote')!r}; copy a matching phrase from the task verbatim instead of paraphrasing it.")
    if not isinstance(answer.get("ui_required"), bool):
        raise ValueError("Declare ui_required true only when this request needs browser behavior")
    for milestone in answer["milestones"]:
        assigned = milestone.get("requirement_ids", [])
        if not assigned or set(assigned) - ids:
            raise ValueError("Each milestone must reference known requirement_ids")
        if milestone.get("browser_flows") and not answer["ui_required"]:
            raise ValueError("Do not add browser flows to a non-UI task")
    if set().union(*(set(m["requirement_ids"]) for m in answer["milestones"])) != ids:
        raise ValueError("Milestones must cover every requirement")


def validate_probes(answer, requirements, policy_version=3):
    checks = answer.get("checks")
    if not isinstance(checks, list):
        raise ValueError("Return checks as a list")
    needed = {r["id"] for r in requirements if r["kind"] != "visual"}
    kinds = {r["id"]:r["kind"] for r in requirements}
    covered, ids = set(), set()
    for check in checks:
        if not isinstance(check, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(check.get("id", ""))) or check["id"] in ids:
            raise ValueError("Independent checks need unique ids")
        ids.add(check["id"])
        refs = check.get("requirement_ids")
        if not isinstance(refs, list) or not refs or set(refs) - needed:
            raise ValueError("Each independent check must reference nonvisual requirements")
        covered.update(refs)
        runner, program = check.get("runner"), check.get("program", "")
        if runner == 'file' and policy_version >= 5:
            from coder_file_probe import validate_file
            validate_file(check, [r for r in requirements if r['id'] in refs])
        elif runner == "api" and policy_version >= 4:
            from coder_api_probe import validate_api
            validate_api(check)
        elif runner == "project_tests":
            if any(kinds[identity]!='tests' for identity in refs):
                raise ValueError(f"Check {check['id']}: project_tests can only verify requested tests. "
                    f"These requirements have kinds { {identity:kinds[identity] for identity in refs} }. "
                    "For behavior, interface, or preservation use runner node/python with independent throwing assertions against the public API. Inspect existing source/tests for regression examples; do not delegate those assertions to the project's own test command.")
        elif runner == "browser":
            from coder_checks import validate_plan
            validate_plan({"milestones":[{"task":"Browser verification","browser_flows":[check]}]}, policy_version=policy_version)
            if not any(step.get("action") in {"exists","visible","text"} for step in check.get("steps",[])):
                raise ValueError("Browser probes need an observable visibility or text assertion")
        elif runner not in {"python", "node", "shell"} or not isinstance(program, str) or not program.strip():
            raise ValueError("Independent checks need runner python/node/shell and an executable program")
        if runner == "python":
            try: tree = ast.parse(program)
            except SyntaxError as error: raise ValueError(f"Invalid probe syntax: {error.msg}") from error
            if not any(isinstance(n, ast.Assert) or isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr.startswith("assert") for n in ast.walk(tree)):
                raise ValueError("Python probes must contain executable assertions, not printed examples")
        elif runner == "node":
            if "console.assert" in program or not re.search(r"(?:node:)?assert|\bthrow\b|process\.exit\s*\(\s*[1-9]", program):
                raise ValueError(f"Check {check['id']}: Node behavior probes must use throwing assertions or nonzero exits, never console.assert. To execute project tests use runner project_tests without a program; the controller discovers the real test filenames and commands.")
            if re.search(r'''(?:[{,]\s*)(?:__proto__|['"]__proto__['"])\s*:\s*['"\d]''',program):
                raise ValueError("Invalid data fixture: JavaScript colon-form __proto__ changes the prototype instead of creating an own data property. Use a computed ['__proto__'] property for data, or test a sentinel value under an ordinary property. Verify the fixture before testing the application.")
        elif runner == "shell" and not str(check.get("expected_behavior", "")).strip():
            raise ValueError("Shell probes must describe what failure their command detects")
        if policy_version >= 4 and runner in {"python","node","shell"}:
            from coder_api_probe import validate_bindings
            validate_bindings(check)
        cwd = Path(check.get("cwd", "."))
        if cwd.is_absolute() or ".." in cwd.parts:
            raise ValueError("Check cwd must stay within the project")
    if covered != needed:
        raise ValueError("Independent checks must cover every nonvisual requirement: " + ", ".join(sorted(needed - covered)))


def probe_checks(probes):
    result = []
    for probe in probes:
        runner, program = probe["runner"], probe.get("program","")
        if runner in {"project_tests", "file"}:
            result.append({**probe,"id":"requirement-"+probe["id"],"kind":runner,"origin":"independent","contract_hash":digest(probe)})
            continue
        if runner == "browser":
            result.append({**probe,"id":"requirement-"+probe["id"],"kind":"browser","origin":"independent","contract_hash":digest(probe)})
            continue
        if runner == "api":
            from coder_api_probe import command
            result.append({**probe,"id":"requirement-"+probe["id"],"command":command(probe),
                "origin":"independent","contract_hash":digest(probe)})
            continue
        if runner == "python":
            command = "python3 -c " + shlex.quote(program)
        elif runner == "node":
            command = "node --input-type=module -e " + shlex.quote(program)
        else:
            command = "bash -e -o pipefail -c " + shlex.quote(program)
        result.append({**probe, "id":"requirement-" + probe["id"], "command":command,
                       "origin":"independent", "contract_hash":digest(probe)})
    return result


def validate_probe_correction(answer,requirements,previous,task):
    validate_probes(answer,requirements)
    old={c['id']:c for c in previous['checks']}
    new={c['id']:c for c in answer['checks']}
    if old.keys()!=new.keys(): raise ValueError('Probe diagnosis cannot remove or add checks')
    reasons={item.get('check_id'):item for item in answer.get('corrections',[])}
    for identity,check in new.items():
        if set(check['requirement_ids'])!=set(old[identity]['requirement_ids']):
            raise ValueError('Probe correction cannot change requirement coverage')
        if digest(check)!=digest(old[identity]):
            explanation=reasons.get(identity,{})
            quote=normalized(explanation.get('source_quote',''))
            if not explanation.get('reason') or not quote or quote not in normalized(task):
                raise ValueError('Each probe correction must explain its defect using the original request')


def test_count(command, log):
    """Return None for unknown runners, never fabricate a count for success."""
    patterns = []
    if "unittest" in command:
        patterns = [r"Ran (\d+) tests?\b"]
    elif "pytest" in command:
        counts = re.findall(r"(\d+) (?:passed|failed|error)(?:s)?\b", log)
        if counts:
            return sum(map(int, counts[-3:]))
        if re.search(r"no tests ran|collected 0 items", log):
            return 0
    elif "node" in command and "--test" in command:
        patterns = [r"(?:#|ℹ) tests (\d+)"]
    if "vitest" in command or "jest" in command or "Tests:" in log:
        patterns += [r"Tests\s*:?\s+(\d+) passed", r"Tests:.*?(\d+) total"]
    if "cargo test" in command:
        counts=re.findall(r"test result:.*? (\d+) passed",log)
        if counts: return sum(map(int,counts))
    if "mvn test" in command:
        patterns += [r"Tests run: (\d+)"]
    if "go test" in command and "[no test files]" in log and not re.search(r"^ok\s",log,re.M):
        return 0
    for pattern in patterns:
        matches = re.findall(pattern, log)
        if matches:
            return int(matches[-1])
    return None


def source_context(repository, task, budget, changed_paths=(), *, include_root=True):
    """Prioritize referenced/changed files, keeping real ranges and hashes."""
    paths = list(dict.fromkeys(changed_paths))
    with repository.connect() as db:
        inventory = list(db.execute("SELECT path FROM files ORDER BY path"))
    paths = list(dict.fromkeys([*(row[0] for row in inventory if row[0] in task or Path(row[0]).name in task), *paths,
                               *(row[0] for row in inventory if include_root and "/" not in row[0])]))
    snippets = []
    for path in paths:
        try:
            snippet = repository.read(path, limit=120)
        except (ValueError, OSError):
            continue
        if estimate_tokens([*snippets, snippet]) <= budget:
            snippets.append(snippet)
    return snippets


def validate_acceptance(answer, requirements, checks, revision, repository, inspections=(), visual=None):
    if not isinstance(answer.get("accepted"), bool) or not isinstance(answer.get("coverage"), list):
        raise ValueError("Acceptance needs accepted and coverage for every requirement")
    known = {r["id"]:r for r in requirements}
    checks_by_id = {c["id"]:c for c in checks if c.get("revision_id") == revision}
    inspected = {row.get("id") for row in inspections}
    seen, all_pass = set(), True
    for row in answer["coverage"]:
        identity = row.get("requirement_id")
        if identity not in known or identity in seen or row.get("status") not in {"passed", "failed", "unverified"}:
            raise ValueError("Coverage must contain each requirement exactly once with passed/failed/unverified")
        seen.add(identity)
        if not str(row.get("reason", "")).strip():
            raise ValueError("Coverage needs an evidence-based reason")
        refs = row.get("check_ids", [])
        if any(c not in checks_by_id for c in refs):
            raise ValueError("Acceptance cited a missing or stale check")
        citations = row.get("sources", [])
        for citation in citations:
            actual = repository.read(citation["path"], start=int(citation.get("start", 1)), limit=int(citation.get("lines", 30)), expected_hash=citation.get("sha256", ""))
            if not citation.get("sha256") or not citation.get("quote") or citation["quote"] not in actual["content"]:
                raise ValueError("Source citations need current hashes, ranges, and exact quotes")
        inspection_ids = row.get("inspection_ids", [])
        if set(inspection_ids) - inspected:
            raise ValueError("Acceptance cited an inspection it did not perform: " + ", ".join(sorted(set(inspection_ids)-inspected)) +
                ". Put executed check IDs in check_ids. inspection_ids may only cite inspection-* IDs returned by your inspection requests; omit this optional field when none apply.")
        if not citations and not inspection_ids and not refs:
            raise ValueError("Every judgment needs source, inspection, or executable evidence")
        if row["status"] == "passed":
            if known[identity]["kind"] == "visual":
                if not visual or visual.get("status") != "passed":
                    raise ValueError("An explicitly requested visual requirement has not been verified")
            elif not any(checks_by_id[c].get("passed") and checks_by_id[c].get("origin")=="independent" and identity in checks_by_id[c].get("requirement_ids", []) for c in refs):
                raise ValueError("A passing requirement needs its own passing independent check")
            if known[identity]["kind"]=="tests" and not any(c.get("passed") and c.get("is_test") and c.get("test_count")!=0 for c in checks_by_id.values()):
                raise ValueError("Requested runnable tests need an executed project test command; reading test source is not execution")
            if any(not checks_by_id[c].get("passed") for c in refs):
                raise ValueError("Cannot claim success using a failed check")
        else:
            all_pass = False
    if seen != set(known):
        raise ValueError("Acceptance omitted requirements")
    if answer["accepted"] != all_pass:
        raise ValueError("Acceptance verdict contradicts requirement coverage")
    if not answer["accepted"] and not answer.get("issues"):
        raise ValueError("A rejection needs actionable issues supported by the coverage evidence")


PLAN_INSTRUCTION = '''Return a milestone plan with requirements:[{id,text,source_quote,kind}], ui_required:boolean,
and milestones:[{id,title,task,requirement_ids,criteria,browser_flows?}].
Kinds: behavior, interface, preservation, tests, documentation, visual. source_quote is an exact phrase from the user's request.
Capture ALL explicit requirements, especially API signatures, input preservation, errors, and existing behavior.
Use visual kind only when the user explicitly requires screenshot/visual verification, never merely because an app has a UI.
ui_required is true only for requested browser behavior, not CLI tools, libraries, backend-only changes or documentation.
For a small task use one milestone including code, tests and docs. Layout suggestions are advisory.
Do not invent test filenames or shell checks. The controller discovers real build/test commands.
Browser flows use cwd,path,steps:[{action:click|fill|select|press|check|scroll|reload|exists|visible|text,selector,value}].
Selectors may be CSS or {role,name} or {label}. Exercise requested behavior, including reload for persistence.
Do not add web interfaces to non-UI tasks. Preserve the original requested scope and the existing project's conventions.'''

PROBE_INSTRUCTION = '''Author independent behavioral checks from the original request and requirements, not the Builder's interpretation.
Return {checks:[{id,requirement_ids,cwd,runner,program,expected_behavior}]} covering every nonvisual requirement.
runner is python, node, or shell. Checks run from cwd in a disposable copy of the project AFTER its dependency setup.
For browser UI behavior use runner:"browser" with path and steps:[{action,selector,value?}] instead of program.
Actions: click, fill, select, press, check, scroll, reload, exists, visible, text. Selectors can be CSS, {role,name}, or {label}.
Use exists for DOM presence, including empty output containers. Use visible for displayed controls and text for expected output.
Use exists with value:false to assert that no matching elements remain after deletion; visible with value:false checks hidden controls. Text with value:"" asserts an empty container; exact:true requires a full text match, otherwise nonempty text matches a substring.
Include observable assertions after interactions. Do not require empty outputs to have visible dimensions before they contain content.
The controller supplies and stops the preview server; never start a web server inside a probe program.
For requirements of kind tests, use runner:"project_tests" without program to run the actual discovered project tests.
project_tests is ONLY for kind tests. Behavior, interface and preservation require independent executable assertions.
To preserve existing behavior, inspect the current source/tests and assert representative public API results yourself.
Do not invent or require specific test filenames. The controller requires runnable, nonempty tests and checks their exit status.
Python programs run with python3 -c and need real assert statements. Node programs run with node --input-type=module -e;
use node:assert/strict, never console.assert. Shell commands must exit nonzero on failure.
Do not edit or generate project files. Check the explicitly requested API, inputs left unchanged, edge cases, tests/docs when requested.
For new projects use the public interface explicitly requested or agreed in the plan, not arbitrary internal filenames.
Inspect existing files/interfaces before writing probes. Programs are private verification data outside editable source.
If correcting an invalid check, preserve the original requirement and explain why the previous probe was invalid.'''

ACCEPT_INSTRUCTION = '''Assess every original requirement against the current revision and executable evidence.
Inspect missing evidence before deciding. Do not invent missing exports, incorrect formulas, or unexecuted successes.
Return {accepted:boolean,summary,issues:[{summary,file,requirement_id}],coverage:[{requirement_id,status,reason,check_ids}]}.
status is passed, failed, or unverified. Each passing nonvisual requirement needs a passed independent check tied to that id.
Optional sources:[{path,start,lines,sha256,quote}] cite current file hashes and an exact quote in the cited range.
Optional inspection_ids cite only inspection-* IDs returned by your own inspection requests. Check IDs belong in check_ids.
Omit optional fields when unnecessary. Initial source context is already available; do not invent inspection IDs for it.
Optional visual review being disabled/unsupported/unavailable does NOT fail ordinary requirements.
An explicitly requested visual requirement needs a passed visual review. Subjective styling suggestions are advisory.
Accept only when every requirement passed; rejected requirements need specific evidence and actionable issues.'''
