"""One fail-closed Qwen prompt counter for the supervisor and SDK condenser.

Assets are prepared outside the sandbox, pinned to a model digest, and mounted
read-only. Inference never downloads a tokenizer or silently changes templates.
"""
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path

TOKENIZER_REPOSITORY = 'Qwen/Qwen3.6-35B-A3B'
TOKENIZER_REVISION = '995ad96eacd98c81ed38be0c5b274b04031597b0'
ASSET_HASHES = {'tokenizer.json': '5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42',
                'chat_template.jinja': '55d4931433fe502b794226ee7f4d206a6bdd436ac9f80eb7d8ebb4c639f9ea0c'}
MODEL_DIGEST = '793390598026bea9fd308ed376b955c191ff663648a96493c40c6471a52c9d19'


class TokenCountingError(ValueError):
    pass


@lru_cache(maxsize=2)
def assets(directory):
    from jinja2.sandbox import ImmutableSandboxedEnvironment
    from tokenizers import Tokenizer
    root = Path(directory)
    try:
        manifest = json.loads((root / 'manifest.json').read_text())
        if manifest['revision'] != TOKENIZER_REVISION or manifest['model_digest'] != MODEL_DIGEST:
            raise ValueError('Tokenizer/model identity does not match the qualified pair')
        if manifest['files'] != ASSET_HASHES: raise ValueError('Tokenizer assets do not match the pinned checksums')
        for name in ('tokenizer.json', 'chat_template.jinja'):
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != manifest['files'][name]:
                raise ValueError('Tokenizer asset checksum mismatch: ' + name)
        tokenizer = Tokenizer.from_file(str(root / 'tokenizer.json'))
        environment = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
        environment.filters['tojson'] = lambda value, **kwargs: json.dumps(value, ensure_ascii=False, **kwargs)
        def fail(message): raise TokenCountingError(str(message))
        environment.globals['raise_exception'] = fail
        template = environment.from_string((root / 'chat_template.jinja').read_text())
        return tokenizer, template, manifest
    except Exception as error:
        raise TokenCountingError('Cannot initialize the pinned prompt counter: ' + str(error)) from error


def normalized_messages(messages):
    result = []
    for original in messages:
        message = dict(original)
        content = message.get('content') or ''
        if isinstance(content, list):
            if any(item.get('type') != 'text' for item in content):
                raise TokenCountingError('Image/audio inputs need qualified multimodal accounting')
            content = ''.join(item.get('text', '') for item in content)
        message['content'] = content
        message['reasoning_content'] = message.get('thinking') or message.get('reasoning_content') or ''
        calls = []
        for original_call in message.get('tool_calls') or []:
            call = dict(original_call)
            function = dict(call.get('function', call))
            arguments = function.get('arguments', {})
            if isinstance(arguments, str): arguments = json.loads(arguments)
            function['arguments'] = arguments
            calls.append({'function': function})
        message['tool_calls'] = calls
        result.append(message)
    return result


def count_request(body, directory=None):
    directory = directory or os.environ.get('DAEDALUS_TOKENIZER_DIR', '')
    if not directory: raise TokenCountingError('Pinned tokenizer directory is not configured')
    try:
        tokenizer, template, manifest = assets(directory)
        messages = normalized_messages(body.get('messages') or [{'role': 'user', 'content': body.get('prompt', '')}])
        text = template.render(messages=messages, tools=body.get('tools') or [],
                               add_generation_prompt=True, enable_thinking=body.get('think', True))
        count = len(tokenizer.encode(text, add_special_tokens=False).ids)
        if count <= 0: raise ValueError('Prompt counter returned no tokens')
        return count
    except TokenCountingError:
        raise
    except Exception as error:
        raise TokenCountingError('Prompt counting failed: ' + str(error)) from error


def validate_identity(digest, template):
    _, _, manifest = assets(os.environ.get('DAEDALUS_TOKENIZER_DIR', ''))
    if digest != manifest['model_digest'] or hashlib.sha256(template.encode()).hexdigest() != manifest['files']['chat_template.jinja']:
        raise TokenCountingError('The model or its template changed; prepare and qualify a new tokenizer binding')
