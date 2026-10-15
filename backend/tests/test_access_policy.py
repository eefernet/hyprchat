import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from fastapi import FastAPI
from access_policy import POLICIES, authorize, request_policy


def test_every_source_route_has_exact_policy():
    root = Path(__file__).resolve().parents[1]
    for path in [root / 'main.py', *(root / 'routes').glob('*.py')]:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in {'get','post','put','patch','delete','head','options'} and d.args and isinstance(d.args[0], ast.Constant) and str(d.args[0].value).startswith('/api/'):
                    assert d.func.attr.upper() + ' ' + d.args[0].value in POLICIES, (path, node.name)


def test_unknown_endpoint_and_similar_prefix_fail_closed():
    app = FastAPI()
    app.get('/api/users-unsafe')(lambda: None)
    for path in ['/api/users-unsafe', '/api/users/login/other']:
        assert request_policy(app, {'type':'http','path':path,'method':'GET','root_path':''}) == 'deny'


def test_passwordless_password_protected_and_admin_access():
    async def user(uid):
        return {'id': uid, 'password_hash': 'hash' if uid == 'locked' else ''} if uid != 'missing' else None
    async def session(uid, token):
        return token == 'correct'
    db = SimpleNamespace(DEFAULT_USER_ID='default', get_user_private=user, validate_user_session=session)
    async def run():
        assert (await authorize('profile','locked','',db))[1][0] == 401
        assert (await authorize('admin','locked','correct',db))[1][0] == 403
        assert (await authorize('profile','missing','',db))[1][0] == 404
        assert await authorize('profile','open','',db) == ('open',None)
        assert await authorize('admin','default','',db) == ('default',None)
    asyncio.run(run())


def test_cors_preflight_checks_the_registered_target_without_password():
    app=FastAPI()
    app.get('/api/users/current')(lambda: None)
    scope={'type':'http','path':'/api/users/current','method':'OPTIONS','root_path':'',
           'headers':[(b'access-control-request-method',b'GET')]}
    assert request_policy(app,scope)=='public'
    assert request_policy(app,{**scope,'path':'/api/not-registered'})=='deny'


def test_included_and_nested_routers_use_effective_paths():
    from fastapi import APIRouter
    nested=APIRouter()
    nested.get('/users')(lambda:None)
    outer=APIRouter();outer.include_router(nested,prefix='/api')
    app=FastAPI();app.include_router(outer)
    scope={'type':'http','path':'/api/users','method':'GET','root_path':''}
    assert request_policy(app,scope)=='public'
    assert request_policy(app,{**scope,'path':'/api/users/unknown'})=='deny'
