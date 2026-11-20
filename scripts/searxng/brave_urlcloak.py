"""Resolve Brave's signed URL-cloak tokens without exposing them to the browser.

Copied to searx/brave_urlcloak.py in a pinned candidate. The offline Rust helper
uses Brave's POPRF library; only blinded elements and the per-search public SID
leave this process. No browser scripts or non-oblivious redirects are executed.
"""
import asyncio
import base64
import concurrent.futures
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import threading
import time
from timeit import default_timer
from urllib.parse import urlsplit

from searx.exceptions import SearxEngineResponseException
from searx.network import THREADLOCAL, get_context_network
from searx.network.client import get_loop

KEY_URL = 'https://search.brave.com/go/v1/__data.json'
EVALUATE_URL = 'https://search.brave.com/api/urlkey'
HELPER = Path(__file__).resolve().parents[2] / 'bin/hyprchat-brave-urlcloak'
MAX_LINKS = 64
MAX_SECONDS = 6.0
_KEY_CACHE = None
_KEY_LOCK = threading.Lock()


def _error(message):
    # Never include source URLs, search text, tokens or cryptographic state.
    return SearxEngineResponseException('Brave link resolution: ' + message)


def _remaining(deadline):
    remaining = deadline - default_timer()
    if remaining <= 0:
        raise _error('time budget exhausted')
    return remaining


def _token(url):
    if not isinstance(url, str) or len(url) > 16384:
        raise _error('invalid result URL')
    if url.startswith('/go/v1/'):
        raw = url[len('/go/v1/'):]
    elif url.startswith('/go/v1#'):
        raw = url[len('/go/v1#'):]
    else:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise _error('invalid cloak URL') from None
        if parsed.hostname != 'search.brave.com' or not parsed.path.startswith('/go/'):
            return None
        if parsed.scheme != 'https' or parsed.username or parsed.password or port not in (None, 443):
            raise _error('unsupported cloak URL')
        if parsed.path.startswith('/go/v1/') and not parsed.query and not parsed.fragment:
            raw = parsed.path[len('/go/v1/'):]
        elif parsed.path == '/go/v1' and parsed.fragment and not parsed.query:
            raw = parsed.fragment
        else:
            raise _error('unsupported cloak URL')
    if not re.fullmatch(r'[A-Za-z0-9_-]{144,8192}', raw):
        raise _error('malformed cloak token')
    return raw


def _destination(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if parsed.scheme not in ('http', 'https') or not host or parsed.username or parsed.password:
            raise ValueError()
        if any(ord(c) < 33 or ord(c) == 127 for c in url) or '\\' in url or len(url) > 8192:
            raise ValueError()
        if parsed.port not in (None, 80, 443):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host = host.rstrip('.').encode('idna').decode('ascii').lower()
            if '.' not in host or host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid')):
                raise ValueError()
            if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', host):
                raise ValueError()
        else:
            if not address.is_global:
                raise ValueError()
        if host == 'search.brave.com' and parsed.path.startswith('/go/'):
            raise ValueError()
        return url
    except (ValueError, UnicodeError):
        raise _error('unsafe or unresolved destination') from None


def _json_response(response, maximum):
    if response.status_code != 200 or len(response.content) > maximum:
        raise _error('unexpected protocol response')
    try:
        return response.json()
    except (TypeError, ValueError):
        raise _error('invalid protocol JSON') from None


def _extract_key(data):
    # Svelte's devalue format is an index table, not ordinary nested JSON.
    # Decode only the one public-key object; never interpret arbitrary types.
    try:
        for node in data['nodes']:
            if not isinstance(node, dict) or not isinstance(node.get('data'), list):
                continue
            values = node['data']
            if not values or not isinstance(values[0], dict) or 'cloakKey' not in values[0]:
                continue
            def at(index):
                if type(index) is not int or not 0 <= index < len(values):
                    raise ValueError()
                return values[index]
            record = at(values[0]['cloakKey'])
            key = {name: at(record[name]) for name in ('public_key', 'sig_public_key', 'version')}
            if type(key['version']) is not int or not 0 <= key['version'] <= 255:
                raise ValueError()
            for name in ('public_key', 'sig_public_key'):
                if not isinstance(key[name], str) or len(base64.b64decode(key[name], validate=True)) != 32:
                    raise ValueError()
            return key
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    raise _error('public verification key unavailable')


def _request(network, method, url, deadline, **kwargs):
    async def request():
        return await network.request(method, url, timeout=_remaining(deadline), allow_redirects=False,
                                     raise_for_httperror=True,
                                     headers={'Accept': 'application/json', 'Referer': ''}, **kwargs)
    future = asyncio.run_coroutine_threadsafe(request(), get_loop())
    try:
        return future.result(_remaining(deadline))
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise _error('time budget exhausted') from None


def _public_key(network, deadline, version):
    global _KEY_CACHE
    with _KEY_LOCK:
        cached = _KEY_CACHE
    if cached and cached[0] > default_timer() and cached[1]['version'] == version:
        return cached[1]
    key = _extract_key(_json_response(_request(network, 'GET', KEY_URL, deadline), 256 * 1024))
    if key['version'] != version:
        raise _error('public key version unavailable')
    with _KEY_LOCK:
        _KEY_CACHE = (default_timer() + 300, key)
    return key


def _crypto(operation, key, items, deadline):
    request = json.dumps({'op': operation, 'key': key, 'items': items, 'now': int(time.time())})
    if len(request) > 512 * 1024:
        raise _error('cryptographic input too large')
    try:
        result = subprocess.run([str(HELPER)], input=request, text=True, capture_output=True,
                                timeout=min(2, _remaining(deadline)), check=False)
        if result.returncode or len(result.stdout) > 512 * 1024:
            raise ValueError()
        output = json.loads(result.stdout)['items']
        if not isinstance(output, list) or len(output) != len(items):
            raise ValueError()
        return output
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        raise _error('signature, proof or decryption validation failed') from None


def _evaluate(network, prepared, deadline):
    async def batch():
        semaphore = asyncio.Semaphore(4)
        async def one(item):
            async with semaphore:
                response = await network.request('POST', EVALUATE_URL,
                    json={'sid': item['sid'], 'blinded_element': item['blinded_element']},
                    headers={'Accept': 'application/json', 'Referer': ''},
                    timeout=_remaining(deadline), allow_redirects=False, raise_for_httperror=True)
                data = _json_response(response, 4096)
                if not isinstance(data, dict):
                    raise _error('invalid evaluation response')
                for name, length in [('evaluated_element', 32), ('proof', 64)]:
                    try:
                        if len(base64.b64decode(data[name], validate=True)) != length:
                            raise ValueError()
                    except (KeyError, TypeError, ValueError):
                        raise _error('invalid evaluation response') from None
                return {**item, 'evaluated_element': data['evaluated_element'], 'proof': data['proof']}
        tasks = [asyncio.create_task(one(item)) for item in prepared]
        try:
            return await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    future = asyncio.run_coroutine_threadsafe(batch(), get_loop())
    try:
        return future.result(_remaining(deadline))
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise _error('time budget exhausted') from None


def resolve_result_links(results):
    rows = [result for result in results if getattr(result, 'url', None)]
    tokens = {}
    for result in rows:
        raw = _token(result.url)
        if raw:
            tokens[result.url] = raw
        else:
            _destination(result.url)
    if not tokens:
        return results
    unique = list(dict.fromkeys(tokens.values()))
    if len(unique) > MAX_LINKS:
        raise _error('too many cloaked results')
    now = default_timer()
    start = getattr(THREADLOCAL, 'start_time', None) or now
    budget = getattr(THREADLOCAL, 'timeout', None) or MAX_SECONDS
    deadline = min(now + MAX_SECONDS, start + budget)
    versions = {base64.urlsafe_b64decode(token[:40])[13] for token in unique}
    if len(versions) != 1:
        raise _error('mixed public key versions')
    network = get_context_network()
    key = _public_key(network, deadline, versions.pop())
    prepared = _crypto('prepare', key, unique, deadline)
    evaluated = _evaluate(network, prepared, deadline)
    destinations = _crypto('finish', key, evaluated, deadline)
    resolved = dict(zip(unique, [_destination(url) for url in destinations]))
    # Publish no changes until every link validates; no silent partial success.
    for result in rows:
        if result.url in tokens:
            result.url = resolved[tokens[result.url]]
    return results
