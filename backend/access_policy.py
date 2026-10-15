"""Complete, exact method/route policy. Unknown API routes fail closed.

Profile policies still require the handler's owner-scoped resource lookup.
The registry is reviewed alongside endpoint additions and tested against source.
"""
import json
from pathlib import Path
from starlette.routing import Match
from starlette._utils import get_route_path

POLICIES = json.loads(Path(__file__).with_name('route_policy.json').read_text())


def request_policy(app, scope):
    if scope['method'] == 'OPTIONS':
        headers = dict(scope.get('headers', []))
        method = headers.get(b'access-control-request-method', b'').decode('ascii', errors='ignore').upper()
        if method and method != 'OPTIONS':
            requested = request_policy(app, {**scope, 'method': method})
            return 'public' if requested != 'deny' else 'deny'
    for registered in app.routes:
        # FastAPI 0.142 retains included routers instead of flattening them.
        # Effective contexts include prefixes and nested includes in dispatch order.
        contexts = getattr(registered, 'effective_route_contexts', None)
        candidates = contexts() if contexts else (registered,)
        for route in candidates:
            if not getattr(route, 'path', '').startswith('/api/'):
                continue
            if hasattr(route, 'matches'):
                matched = route.matches(scope)[0] == Match.FULL
            else:
                matched = scope['method'] in (route.methods or ()) and bool(route.path_regex.match(get_route_path(scope)))
            if matched:
                return POLICIES.get(scope['method'] + ' ' + route.path, 'deny')
    return 'deny' if scope['path'].startswith('/api/') else 'public'


async def authorize(policy, user_id, session, db):
    if policy in {'public', 'webhook'}:
        return user_id, None
    if policy == 'deny':
        return None, (403, 'Route has no access policy')
    user = await db.get_user_private(user_id)
    if not user:
        return None, (404, 'User not found')
    if user.get('password_hash') and not await db.validate_user_session(user_id, session):
        return None, (401, 'Password required')
    if policy == 'admin' and user_id != db.DEFAULT_USER_ID:
        return None, (403, 'Only the Main profile can do this')
    return user_id, None
