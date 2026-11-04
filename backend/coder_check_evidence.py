"""Shared controller check classification and requested interface parsing."""
from __future__ import annotations

import hashlib
import json
import re

# Additional call/time allowance never resets exhausted application repairs.
DURABLE_LIMITS = {'application_repairs', 'audit_corrections', 'no_progress'}


def runnable(job):
    current = [c for c in job.get('checks', []) if c.get('revision_id') == job.get('revision_id')]
    launches = [c for c in current if c.get('phase') == 'launch']
    if launches:
        return all(c.get('passed') for c in launches)
    if re.search(r'\bweb|browser|frontend|fullstack|react', job.get('user_task', job.get('task', '')), re.I):
        # A static site has no launch command; an observed browser/service check proves it runs.
        return any(c.get('passed') and (c.get('service_bindings') or 'browser' in c.get('test_interfaces', [])) for c in current)
    return any(c.get('execution_succeeded') and c.get('passed') for c in current)


def requested_interfaces(task):
    """Explicit controls the user named: CSS id selectors and long CLI flags.

    Model-written tests, audits and reviews share the model's misreadings, so
    these are checked by the controller straight from the request text.
    """
    text = task or ''
    selectors = list(dict.fromkeys(re.findall(r'(?<![\w&/(\]])#([A-Za-z][\w-]{1,40})(?![\w-])', text)))
    # Hex colours are not controls; three-letter words such as #add are.
    selectors = [v for v in selectors if not re.fullmatch(r'[0-9a-fA-F]{6}|[0-9a-fA-F]{8}|(?=[0-9a-fA-F]{3,4}$)(?=.*\d).*|[fF]{3}|0{3}', v)]
    flags = list(dict.fromkeys(re.findall(r'(?<![\w-])(--[a-z][a-z0-9-]{1,40})(?![\w-])', text)))
    # Launcher/package-manager options in a quoted run command are not application interfaces.
    flags = [v for v in flags if v not in {'--host', '--port', '--reload', '--bind', '--save', '--save-dev', '--prefix',
                                           '--workers', '--app-dir', '--no-audit', '--no-fund', '--package-lock'}]
    labels, buttons, conditional = requested_names(text)
    return {'selectors': selectors, 'flags': flags, 'labels': labels, 'buttons': buttons, 'conditional_buttons': conditional}


_NAME_WORD = r"[A-Za-z0-9][\w'/-]*"
_LEADING_VERB = re.compile(r'^(?:show|display|provide|include|render|offer|add|use|have|expose|give|put)\s+', re.I)
# A Title-Case name of one to four words ("Save task"), and a comma/and list of them ("Edit and Delete").
_NAME = r"[A-Z][\w'/-]*(?:\s+(?!buttons?\b|and\b|or\b)[a-z][\w'/-]*){0,3}"
_NAME_LIST = _NAME + r'(?:\s*,\s*' + _NAME + r')*(?:,?\s+(?:and|or)\s+' + _NAME + r')?'
_CONDITIONAL = re.compile(r'\bfor each\b|\bper (?:item|task|row|entry|record|project)\b|\bwhen editing\b|\bwhile editing\b|\bon each\b', re.I)


def _name_tokens(listing):
    """Title-Case names of one to four words from a comma/and list; quotes and backticks stripped."""
    names = []
    for item in re.split(r'\s*,\s*|\s+and\s+|\s+or\s+|\s*/\s*(?=[A-Z])', listing or ''):
        item = item.strip().strip('`"\'.:;()').strip()
        item = re.sub(r'\s+(?:and|or)$', '', item)
        # "Show Edit" -> "Edit", but "Add task" stays: the verb is only dropped when a Title-Case name remains.
        stripped = _LEADING_VERB.sub('', item) if len(item.split()) > 1 else item
        item = stripped if stripped[:1].isupper() else item
        words = item.split()
        if not 1 <= len(words) <= 4 or not words[0][:1].isupper():
            continue
        if not all(re.fullmatch(_NAME_WORD, w) for w in words):
            continue
        if item not in names:
            names.append(item)
    return names


def requested_names(text):
    """(labels, buttons, conditional_buttons) the request names verbatim.

    "accessible labels A, B, C" and "labels: A, B" name form controls the page must expose through their
    label text; "buttons X and Y" / "X and Y buttons" name visible buttons; a clause with "for each" or
    "when editing" names buttons that only exist once an item does (Edit, Delete, Save task). The medium
    board (2026-09-26) failed every browser verdict at get_by_label('Project name') while its own tests
    and audits never used a label, so the controller reads them from the request itself.
    """
    labels, buttons, conditional = [], [], []
    for sentence in re.split(r'[.;\n]+', text or ''):
        sentence = sentence.strip()
        if not sentence:
            continue
        for match in re.finditer(r'\b(?:accessible\s+labels?|labels?\s*:)\s*(?P<list>.+?)(?=,?\s+and\s+(?:the\s+)?buttons?\b|$)', sentence, re.I):
            labels.extend(n for n in _name_tokens(match.group('list')) if n not in labels)
        for part in re.split(r',\s*and\s+', sentence):
            if not re.search(r'\bbuttons?\b', part, re.I):
                # "..., and Save task when editing": a button named by its editing role alone.
                for match in re.finditer(r"(?P<name>(?<![\w-])[A-Z][\w'/-]*(?:\s+[a-z][\w'/-]*){0,3})\s+(?:when|while)\s+editing\b", part):
                    conditional.extend(n for n in _name_tokens(match.group('name')) if n not in conditional)
                continue
            target = conditional if _CONDITIONAL.search(part) else buttons
            found = []
            for match in re.finditer(r'\bbuttons?\s+(?:named\s+|called\s+|labell?ed\s+)?(?P<list>[A-Z`"\'].+?)(?=\s+(?:for|when|while|that|which|to)\b|$)', part):
                found.extend(_name_tokens(match.group('list')))
            for match in re.finditer(r'(?<![\w-])(?P<list>' + _NAME_LIST + r')\s+buttons?\b', part):
                found.extend(_name_tokens(match.group('list')))
            target.extend(n for n in found if n not in target)
    buttons = [b for b in buttons if b not in conditional]
    return labels, buttons, conditional


ADVISORY_PHASES = {'lint', 'typecheck'}


def blocking(check):
    """Discovered lint/typecheck phases are reported, not release gates. Setup, build,
    tests, launch, audits, protected files and retained baseline failures still block."""
    if check.get('passed'):
        return False
    if check.get('origin') == 'controller' and check.get('phase') == 'visual' and check.get('skipped') and check.get('optional_visual'):
        return False
    return not (check.get('origin') == 'project' and check.get('phase') in ADVISORY_PHASES and not check.get('is_test'))


EVIDENCE_VERSION = 4


def failure_signature(checks):
    rows = []
    for check in checks:
        text = str(check.get('log_tail') or check.get('reason') or check.get('classification') or '')
        text = re.sub(r'(?:/[^\s\"\']+)/(?:project|workspace|audit)(?=/|\b)', '<execution>', text)
        text = re.sub(r'127\.0\.0\.1:\d+', '127.0.0.1:<port>', text)
        text = re.sub(r'\b\d+(?:\.\d+)?\s*(?:seconds?|ms|s)\b', '<duration>', text)
        text = re.sub(r'0x[0-9a-fA-F]+', '<address>', text)
        rows.append((check['id'], check.get('classification'), text))
    return hashlib.sha256(json.dumps(sorted(rows), sort_keys=True).encode()).hexdigest()
