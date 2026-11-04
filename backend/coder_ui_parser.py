"""Literal UI clauses, with quoted values kept out of syntax normalization."""
import re

PARSER_VERSION = 2
NEGATIVE = re.compile(r"\b(?:not|never|don['’]t|doesn['’]t|unless|except|if|when|until|otherwise|or)\b", re.I)
META = re.compile(r'^(?:(?:run|rerun|execute|update|add|write|keep|preserve|do not remove)\s+(?:the\s+|existing\s+|unrelated\s+|regression\s+|all\s+)*(?:tests?|checks?|README|npm|server logic)\b|(?:verify|assert|confirm|report|deliver|publish)\b)', re.I)


def clauses(text):
    """Split outside quotes; retain exact excerpts and quote contents (including dots)."""
    start = 0
    closing = None
    pairs = {'"': '"', "'": "'", '`': '`', '“': '”', '‘': '’'}
    for i, char in enumerate(text):
        if closing:
            if char == closing: closing = None
            continue
        if char in pairs and not (char in {"'", '‘'} and i and text[i-1].isalnum()):
            closing = pairs[char]
        elif char in '\n;' or (char in '.!?' and (i+1 == len(text) or text[i+1].isspace()) and not (char == '.' and re.fullmatch(r'\s*\d+', text[start:i]))):
            end = i if char in '\n;' else i+1
            if text[start:end].strip(): yield text[start:end].strip()
            start = i+1
    if text[start:].strip(): yield text[start:].strip()


def tokenize(excerpt):
    values = {}
    pattern = r'"([^"\n]*)"|“([^”\n]*)”|`([^`\n]*)`|(?<!\w)\x27([^\x27\n]*)\x27|‘([^’\n]*)’'
    def quoted(match):
        key = f'QREF{len(values)}X'
        values[key] = next(v for v in match.groups() if v is not None)
        return key
    syntax = re.sub(pattern, quoted, excerpt)
    syntax = re.sub(r'^\s*(?:[-*+]\s+|\d+[.)]\s+)', '', syntax)
    syntax = re.sub(r'\s+', ' ', syntax).strip().rstrip('.!?')
    return syntax, values


def action_name(syntax, values):
    token = r'(?:QREF\d+X|[A-Z][\w-]*)'
    patterns = [
        rf'\b(?:the|a|an)\s+({token})\s+button\b',
        rf'\b(?:Clicking|clicking|Click|click|Pressing|pressing|Press|press)\s+(?:the\s+)?({token})(?=\s+(?:button\b|must\b|should\b|now\b|still\b|sets?\b|shows?\b|clears?\b|empties\b)|[,.;]|$)',
        rf'^(?:(?:Make|Change)\s+)?({token})\s+(?:(?:must|should|now|still)\s+)*(?:sets?|shows?|clears?|empties|empty)\b',
    ]
    found = []
    for pattern in patterns:
        found.extend(values.get(m[1], m[1]) for m in re.finditer(pattern, syntax))
    names = list(dict.fromkeys(found))
    return names[0] if len(names) == 1 and names[0].lower() not in {'the', 'it', 'adding', 'make', 'change'} else ''


def target(part, values):
    part = re.sub(r'^(?:all\s+)?(?:the\s+)?', '', part.strip(), flags=re.I)
    if re.fullmatch(r'inputs?|fields?', part, re.I):
        return {'kind': 'input', 'all': part.lower().endswith('s'), 'name': ''}
    if part in {'Result display', 'Output display'}:
        return {'kind': 'result', 'all': False, 'name': part.split()[0]}
    if re.fullmatch(r'(?:result|output)(?:\s+(?:text|label|display))?', part, re.I):
        return {'kind': 'result', 'all': False, 'name': ''}
    m = re.fullmatch(r'(?:(input|field|result|output|label)\s+)?(?:with\s+id\s+|id\s+)?#([\w-]+)', part, re.I)
    if m:
        return {'kind': 'input' if (m[1] or '').lower() in {'input', 'field'} else 'element', 'all': False, 'selector': '#'+m[2]}
    m = re.fullmatch(r'(input|field|result|output|label|display)\s+(?:named\s+)?(QREF\d+X)', part, re.I)
    if not m:
        m = re.fullmatch(r'(QREF\d+X|[A-Z][\w-]*)\s+(input|field|display|label)(?:\s+field)?', part)
        if m: kind, name = m[2], values.get(m[1], m[1])
        else: return None
    else: kind, name = m[1], values[m[2]]
    return {'kind': 'input' if kind.lower() in {'input', 'field'} else 'result', 'all': False, 'name': name}


def parse(excerpt):
    syntax, values = tokenize(excerpt)
    if re.match(r'^#{1,6}\s', syntax) or (syntax.startswith('**') and syntax.endswith('**')) or syntax.endswith(':') or re.match(r'^(?:clearly\s+)?(?:report|run|rerun|execute|deliver|publish)\b', syntax, re.I) or META.search(syntax): return []
    name = action_name(syntax, values)
    # Scope qualifiers are never thrown away to make a positive assertion pass.
    # A precise replacement suffix is the sole recognized contrast, not negation.
    syntax = re.sub(r'\s+instead of leaving (?:it|the result|the output) empty$', '', syntax, flags=re.I)
    # Preservation remains a separate unverified requirement; it does not alter
    # an explicitly requested clear/set transition.
    pieces = re.split(r'\s+(?:while preserving|without removing or changing|without removing)\s+', syntax, maxsplit=1, flags=re.I)
    syntax = pieces[0]
    residual = pieces[1] if len(pieces) == 2 else ''
    if not name or NEGATIVE.search(syntax):
        return [{'supported': False, 'reason': 'This behavior has no unambiguous supported action and exact outcome.'}]
    rows = []
    for outcome in re.split(r'\s+and\s+(?=(?:sets?|shows?|clears?|empties|empty)\b)', syntax, flags=re.I):
        exact = re.search(r'\bsets?\s+(.+?)\s+to\s+(?:exactly\s+)?(QREF\d+X|[A-Z][\w-]*|-?\d+(?:\.\d+)?)$', outcome)
        show = re.search(r'\bshows?\s+(QREF\d+X)\s+(?:in|on)\s+(.+)$', outcome, re.I)
        clear = re.search(r'\b(?:clears?|empties|empty)\s+(?!(?:must|should|now|still|button|clears?|empties|empty|sets?|shows?)\b)(.+)$', outcome, re.I) if not exact and not show else None
        expected = values.get(exact[2], exact[2]) if exact else values[show[1]] if show else '' if clear else None
        targets = exact[1] if exact else show[2] if show else clear[1] if clear else ''
        if not targets:
            rows.append({'supported': False, 'reason': 'The named action has no supported exact outcome.'})
            continue
        for part in re.split(r'\s+and\s+|\s*,\s*', targets, flags=re.I):
            binding = target(part, values)
            if binding:
                rows.append({'supported': True, 'action': {'action': 'click', 'name': name}, 'target': binding,
                             'property': 'value' if binding['kind'] == 'input' else 'text', 'expected': expected})
            else:
                rows.append({'supported': False, 'reason': 'The requested target is not an explicit supported input or result.'})
    if residual:
        rows.append({'supported': False, 'reason': 'Preservation of saved data is outside the clear/exact-value UI contract.'})
    # One unresolved item per original requirement, not one per failed regex.
    seen = set()
    return [row for row in rows if not ((key := 'unverified' if not row['supported'] else repr(row)) in seen or seen.add(key))]
