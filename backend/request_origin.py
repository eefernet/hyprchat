"""Refuse a state-changing request that another website sends through the user's browser.

A request with no profile header acts as the Main profile, and a browser will happily send a body-less
POST or a form upload to this server from any page the user has open. So before a request is allowed to
change anything, check where the browser says it came from:

- `Sec-Fetch-Site` is set by the browser itself and a page cannot forge it. `same-origin` and `none`
  (typed address, bookmark) pass; `cross-site` is refused unless its `Origin` is a configured one.
- Browsers leave that header out on plain-HTTP addresses that are not localhost, so there the `Origin`
  header decides: it must be this server's own address (`Host`, or `X-Forwarded-Host` behind a proxy)
  or one of `ALLOWED_ORIGINS`.
- A request with neither header is not from a browser page (curl, scripts, the scheduler's own calls,
  tests) and passes.

`HYPRCHAT_ORIGIN_CHECK=off` switches the check off, for a proxy setup this rule does not understand.
"""
import os
from urllib.parse import urlsplit

SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
REFUSAL = 'This request came from another website and was refused.'


def enabled() -> bool:
    return os.getenv('HYPRCHAT_ORIGIN_CHECK', 'on').strip().lower() not in {'off', '0', 'false', 'no'}


def _netloc(value: str) -> str:
    value = str(value or '').strip().lower()
    if not value:
        return ''
    return urlsplit(value if '//' in value else '//' + value).netloc


def _own_hosts(headers) -> set:
    hosts = {_netloc(headers.get('host'))}
    for part in str(headers.get('x-forwarded-host') or '').split(','):
        hosts.add(_netloc(part))
    hosts.discard('')
    return hosts


def cross_site(method: str, headers, allowed_origins=()) -> bool:
    """True when this request should be refused. `headers` is any mapping with lower-case lookups."""
    if str(method or '').upper() in SAFE_METHODS or not enabled():
        return False
    origin = str(headers.get('origin') or '').strip()
    site = str(headers.get('sec-fetch-site') or '').strip().lower()
    allowed = {_netloc(item) for item in allowed_origins or ()} - {''}
    origin_host = _netloc(origin) if origin and origin.lower() != 'null' else ''
    if site in {'same-origin', 'none'}:
        return False
    if site == 'cross-site':
        return origin_host not in allowed
    # `same-site` (another port or sub-domain of this host) or no hint at all: the Origin header decides.
    if not origin:
        return False
    if not origin_host:
        return True
    return origin_host not in (_own_hosts(headers) | allowed)
