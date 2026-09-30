"""Request-derived obligations. Generated audits never authorize their own trust.

This version deliberately has no trusted behavioral fixture registry. Until a
controller-maintained, criterion-specific fixture is installed, behavior is
unverified even when every generated audit passes. Source presence, model
verdicts and a repaired suite with no requirement mapping are not such fixtures.
"""
import hashlib
import re

EVIDENCE_VERSION = 3


def deliverables(text):
    required = set()
    targets = r'\b(?:test(?:s|ed|ing)?|unit ?tests?|test_[\w.-]+|regression checks?|automated checks?|readme|docs?|document(?:ation|ed|s)?|usage guide)\b'
    negation = r"(?:\b(?:no|without|omit|skip)|\b(?:do not|don't|need not)(?:\s+(?:add|include|write|provide))?|\bno need (?:for|to add))"
    modifiers = r'(?:\s+(?:any|a|the|new|additional|automated|unit|browser|regression|adding|writing|providing))*\s*$'
    for clause in re.split(r'[.;\n]|\bbut\b', text or '', flags=re.I):
        previous_end, previous_negative = 0, False
        for match in re.finditer(targets, clause, re.I):
            prefix = clause[previous_end:match.start()]
            inherited = previous_negative and bool(re.fullmatch(r'\s*(?:,|and|or|nor|and/or)\s*(?:any\s+)?', prefix, re.I))
            negative = inherited or bool(re.search(negation + modifiers, prefix, re.I))
            negative = negative or bool(re.match(r'\s+(?:are |is )?not (?:required|needed)\b', clause[match.end():], re.I))
            if not negative:
                required.add('documentation' if re.match(r'(?:readme|doc|usage)', match[0], re.I) else 'tests')
            previous_end, previous_negative = match.end(), negative
    return required


def criteria(request, outcomes, history=()):
    sources = [('request', request)] if request else []
    sources += [(o['id'], o['text']) for o in outcomes if 'behavior' in o.get('evidence_types', ['behavior'])]
    result, seen = [], set()
    for owner, text in sources:
        # Keep the exact excerpt; never ask the builder to declare its own coverage.
        for excerpt in re.split(r'\n+|;|(?<=[.!?])\s+|\s+and\s+', text or ''):
            excerpt = excerpt.strip()
            if not excerpt:
                continue
            if excerpt in seen:
                prior = next(row for row in result if row['request_excerpt'] == excerpt)
                if owner not in prior['owners']: prior['owners'].append(owner)
                continue
            seen.add(excerpt)
            identity = hashlib.sha256((owner + '\0' + excerpt).encode()).hexdigest()[:20]
            result.append({'id': 'criterion:' + identity, 'owner': owner, 'owners': [owner], 'request_excerpt': excerpt,
                           'status': 'unverified', 'trust_origin': None, 'executions': [],
                           'reason': 'No trusted criterion-specific behavioral check is registered.'})
    for row in result:
        row['parent_ids'] = [parent['parent_id'] for parent in history
            if parent.get('action') == 'retain' and set(parent.get('active_outcome_ids', [])) & set(row['owners'])]
    return result
