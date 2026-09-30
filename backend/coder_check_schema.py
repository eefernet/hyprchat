"""Policy-5 authoring schemas. Older policies retain their original grammar."""
import copy
import re
import shlex

from coder_api_probe import TARGET_SCHEMA, EXPECT_SCHEMA, BINDINGS_SCHEMA, obj
from coder_browser_schema import STEPS_SCHEMA

STRING = {'type': 'string', 'minLength': 1}
JSON_VALUE = {'$ref': '#/$defs/json_value'}
DEFINITIONS = {'json_value': {'anyOf': [
    {'type': 'null'}, {'type': 'boolean'}, {'type': 'number'}, {'type': 'string'},
    {'type': 'array', 'items': JSON_VALUE},
    {'type': 'object', 'additionalProperties': JSON_VALUE},
]}}


def probe_schema():
    expect = copy.deepcopy(EXPECT_SCHEMA)
    expect['oneOf'][0]['properties']['value'] = JSON_VALUE
    expect['oneOf'][1]['properties']['value']['items']['items'] = JSON_VALUE
    cases = {'type': 'array', 'minItems': 1, 'items': obj({
        'args': {'type': 'array', 'items': JSON_VALUE},
        'kwargs': {'type': 'object', 'additionalProperties': JSON_VALUE},
        'expect': expect, 'preserve_inputs': {'type': 'boolean'},
    }, ['args', 'expect', 'preserve_inputs'])}
    assertions = {'type': 'array', 'minItems': 1, 'items': {'oneOf': [
        obj({'kind': {'enum': ['exists', 'nonempty', 'unchanged']}}, ['kind']),
        obj({'kind': {'const': 'contains'}, 'value': STRING}, ['kind', 'value']),
    ]}}
    runners = {
        'api': {'target': TARGET_SCHEMA, 'cases': cases},
        'file': {'path': STRING, 'assertions': assertions},
        'browser': {'path': STRING, 'steps': STEPS_SCHEMA},
        **{r: {'program': STRING, 'bindings': BINDINGS_SCHEMA} for r in ('python', 'node', 'shell')},
    }
    return {'$defs': DEFINITIONS, 'oneOf': [obj({
        'runner': {'const': runner}, 'cwd': STRING, 'expected_behavior': STRING, **properties,
    }, ['runner', 'cwd', 'expected_behavior', *properties]) for runner, properties in runners.items()]}


def executable_identity(check):
    """Exclude narration and provenance; compare the assertions that execute."""
    from coder_verification import digest
    fields = {'api': ('target', 'cases'), 'file': ('path', 'assertions'),
              'browser': ('path', 'steps')}.get(check['runner'], ('program', 'bindings'))
    value = {k: check[k] for k in ('runner', 'cwd', *fields) if k in check}
    if check['runner'] == 'python':
        import ast
        tree = ast.parse(check['program'])
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                node.msg = None
            if hasattr(node, 'body') and isinstance(node.body, list):
                node.body = [child for child in node.body if not (
                    isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant)
                    and isinstance(child.value.value, str))]
        value['program'] = ast.dump(tree)
    elif check['runner'] == 'node':
        value['program'] = javascript_tokens(check['program'])
    elif check['runner'] == 'shell' and '<<' not in check['program']:
        # Heredoc bodies are literal fixture data; their # characters are not
        # shell comments. Leave those programs to the independent comparison.
        lexer = shlex.shlex(check['program'], posix=False, punctuation_chars=True)
        lexer.whitespace_split = True
        value['program'] = list(lexer)
    return digest(value)


def javascript_tokens(program):
    """Ignore comments/spacing while retaining strings, templates and regex data.

    This is a conservative lexical guard, not a proof of semantic difference.
    Raw replacements also receive an independent old/new assertion review.
    """
    tokens, index, expression = [], 0, True
    while index < len(program):
        char = program[index]
        if char.isspace():
            index += 1
            continue
        if program.startswith('//', index):
            end = program.find('\n', index)
            index = len(program) if end < 0 else end
            continue
        if program.startswith('/*', index):
            end = program.find('*/', index + 2)
            if end < 0: raise ValueError('Unterminated JavaScript comment')
            index = end + 2
            continue
        start = index
        if char in "\"'`" or (char == '/' and expression):
            quote, in_class = char, False
            index += 1
            while index < len(program):
                current = program[index]
                index += 1
                if current == '\\':
                    index += 1
                    continue
                if quote == '/':
                    if current == '[': in_class = True
                    elif current == ']': in_class = False
                if current == quote and not in_class: break
            tokens.append(program[start:index])
            expression = False
            continue
        word = re.match(r'[\w$]+', program[index:])
        if word:
            token = word.group()
            index += len(token)
            expression = token in {'return', 'throw', 'case', 'typeof', 'void', 'delete', 'yield', 'await', 'in', 'of'}
        else:
            token = char
            index += 1
            expression = char in '([{,:;=!?&|+-*%^~<>'
        tokens.append(token)
    return tokens
