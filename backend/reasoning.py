"""Model-owned reasoning controls. Boolean thinking is not a reasoning ladder."""
import asyncio
import re
import time

import config

_cache = {}
_TTL = 60


def normalize(model, metadata=None, *, source='unknown'):
    metadata = metadata if isinstance(metadata, dict) else {}
    values = metadata.get('values') or []
    if not isinstance(values, list):
        values = []
    values = [v for v in values if type(v) is bool or isinstance(v, str)]
    values = list({(type(v).__name__, str(v)): v for v in values}.values())
    native = sum(isinstance(v, str) for v in values) >= 2
    default = metadata.get('default')
    if not contains(values, default):
        default = None
    return {'model': model, 'mode': 'native' if native else 'review',
            'values': values, 'default': default, 'source': source}


def cloud_capabilities(model):
    # Exact families (optionally dated snapshots), never infer from 'gpt'/'claude'.
    # Verified 2026-10-15:
    # https://developers.openai.com/api/docs/guides/latest-model
    # https://developers.openai.com/api/docs/guides/reasoning
    # https://platform.claude.com/docs/en/build-with-claude/effort
    families = {
        'openai:gpt-5.1': (['none', 'low', 'medium', 'high'], 'none'),
        'openai:gpt-5': (['minimal', 'low', 'medium', 'high'], 'medium'),
        'openai:gpt-5-mini': (['minimal', 'low', 'medium', 'high'], 'medium'),
        'openai:gpt-5-nano': (['minimal', 'low', 'medium', 'high'], 'medium'),
        'openai:o3': (['low', 'medium', 'high'], 'medium'),
        'openai:o3-mini': (['low', 'medium', 'high'], 'medium'),
        'openai:o4-mini': (['low', 'medium', 'high'], 'medium'),
        'openai:gpt-6.1-sol': (['low', 'medium', 'high', 'xhigh', 'max'], 'medium'),
        'anthropic:claude-opus-4-5': (['low', 'medium', 'high'], 'high'),
        'anthropic:claude-sonnet-4-6': (['low', 'medium', 'high', 'max'], 'high'),
        'anthropic:claude-opus-4-6': (['low', 'medium', 'high', 'max'], 'high'),
    }
    for name in ('gpt-5.6', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'):
        families['openai:' + name] = (['none', 'low', 'medium', 'high', 'xhigh', 'max'], 'medium')
    for name in ('claude-opus-4-7', 'claude-opus-4-8', 'claude-opus-5', 'claude-opus-5-5',
                 'claude-sonnet-5', 'claude-sonnet-5-5', 'claude-haiku-5-5',
                 'claude-fable-5', 'claude-fable-5-1', 'claude-mythos-5', 'claude-mythos-5-1'):
        families['anthropic:' + name] = (['low', 'medium', 'high', 'xhigh', 'max'],
            'medium' if name in {'claude-opus-5-5', 'claude-haiku-5-5'} else 'high')
    for family, (values, default) in families.items():
        if re.fullmatch(re.escape(family) + r'(?:-\d{4}-?\d{2}-?\d{2})?', model):
            return normalize(model, {'values': values, 'default': default}, source='provider registry')
    return normalize(model)


async def capabilities(http, model, *, digest=None):
    if model.startswith(('openai:', 'anthropic:', 'custom:')):
        return cloud_capabilities(model)
    if model == 'auto':
        return {**normalize(model), 'source': 'resolved after routing'}
    name = model.removeprefix('ollama:')
    try:
        async with asyncio.timeout(3):
            if digest is None:
                tags = await http.get(f'{config.OLLAMA_URL}/api/tags', timeout=2)
                tags.raise_for_status()
                digest = next((m.get('digest', '') for m in tags.json().get('models', []) if m.get('name') == name), '')
            key = (config.OLLAMA_URL, name, digest)
            cached = _cache.get(key)
            if cached and time.monotonic() - cached[0] < _TTL:
                return {**cached[1], 'model': model}
            response = await http.post(f'{config.OLLAMA_URL}/api/show', json={'model': name}, timeout=2)
            response.raise_for_status()
            result = normalize(model, response.json().get('thinking'), source='Ollama metadata')
            if len(_cache) >= 128:
                _cache.clear()
            _cache[key] = (time.monotonic(), result)
            return result
    except (Exception, TimeoutError):
        return {**normalize(model), 'source': 'metadata unavailable'}


def contains(values, value):
    return any(type(v) is type(value) and v == value for v in values)


def resolve(req, caps, thinking=None, thinking_source='model default'):
    rounds = max(0, min(3, int(getattr(req, 'effort_rounds', 0) or 0)))
    result = {'mode': 'review', 'review_rounds': rounds, 'thinking': thinking,
              'source': thinking_source, 'model': req.model, 'notice': ''}
    # Preserve old API clients, including scheduled runs with no effort request.
    if getattr(req, 'effort_mode', None) != 'auto':
        return result
    if caps['mode'] != 'native':
        if caps.get('source') == 'metadata unavailable':
            result['notice'] = 'Model controls unavailable; using the saved self-review setting.'
        elif getattr(req, 'reasoning_level', None) is not None:
            result['notice'] = 'Native levels are no longer available; using the saved self-review setting.'
        return result
    requested = getattr(req, 'reasoning_level', None)
    bound_model = getattr(req, 'reasoning_model', '')
    if bound_model and bound_model != req.model:
        value = caps['default']
        notice = 'Model changed; using its native reasoning default.'
    elif requested is None:
        value, notice = caps['values'][0], ''
    elif contains(caps['values'], requested):
        value, notice = requested, ''
    else:
        value = caps['default']
        notice = 'Saved reasoning level is unavailable; using the model default.'
    return {**result, 'mode': 'native', 'review_rounds': 0, 'thinking': value,
            'requested': requested, 'source': 'effort menu', 'notice': notice}


# Explicit built-ins only. Connector annotations and arbitrary code are not proof
# of a read-only operation. Unknown tools cannot run during answer review.
REVIEW_TOOLS = frozenset({'research', 'fetch_url', 'read_file', 'list_files',
                          'search_files'})
