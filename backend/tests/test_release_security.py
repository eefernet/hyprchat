"""Security and data-loss fixes of the 2026-11-18 release polish (offline; temp SQLite, no network).

One test per finding of the fourth bug hunt:
- another website could send state-changing requests through the browser (request_origin.py),
- any profile could run server commands or read server secrets through a connector
  (connector_privileges.py),
- deleting a profile left its tasks, passwords and keys behind (database.USER_OWNED_TABLES),
- a saved notification token was returned in full by the preference routes.
"""
import asyncio
import re
import sqlite3

import pytest

import database as db
import request_origin


OWN = {"host": "chat.example:8000"}


@pytest.mark.parametrize("method,headers,refused", [
    ("GET", {**OWN, "origin": "http://evil.example", "sec-fetch-site": "cross-site"}, False),
    ("POST", {**OWN, "origin": "http://evil.example", "sec-fetch-site": "cross-site"}, True),
    ("POST", {**OWN, "sec-fetch-site": "cross-site"}, True),            # body-less no-cors POST
    ("DELETE", {**OWN, "origin": "http://evil.example"}, True),          # plain-HTTP address: no Sec-Fetch-Site
    ("POST", {**OWN, "origin": "null"}, True),                           # sandboxed frame
    ("POST", {**OWN, "origin": "http://chat.example:8000"}, False),      # the app itself, plain HTTP
    ("POST", {**OWN, "origin": "http://chat.example:9999", "sec-fetch-site": "same-site"}, True),
    ("POST", {**OWN, "origin": "https://evil.example", "sec-fetch-site": "same-origin"}, False),  # browser's own verdict
    ("POST", {**OWN, "sec-fetch-site": "none"}, False),
    ("POST", dict(OWN), False),                                          # curl, scripts, tests
    ("POST", {"host": "100.64.0.1:8000", "x-forwarded-host": "chat.ts.example", "origin": "https://chat.ts.example"}, False),
    ("POST", {**OWN, "origin": "http://localhost:8000", "sec-fetch-site": "cross-site"}, False),  # configured origin
])
def test_cross_site_requests_are_refused(method, headers, refused, monkeypatch):
    monkeypatch.delenv("HYPRCHAT_ORIGIN_CHECK", raising=False)
    assert request_origin.cross_site(method, headers, ["http://localhost:8000"]) is refused


def test_origin_check_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("HYPRCHAT_ORIGIN_CHECK", "off")
    assert request_origin.cross_site("POST", {**OWN, "sec-fetch-site": "cross-site"}, []) is False


def as_user(user_id, factory):
    async def scoped():
        token = db.set_current_user_id(user_id)
        try:
            return await factory()
        finally:
            db.reset_current_user_id(token)
    return asyncio.run(scoped())


def test_only_main_may_save_the_risky_connector_fields():
    import connector_privileges as rules

    async def check(fields):
        return rules.refusal(fields)

    stdio = {"transport": "stdio", "command": "sh"}
    assert as_user("default", lambda: check(stdio)) == ""
    assert as_user("guest", lambda: check(stdio)) == rules.STDIO
    assert as_user("guest", lambda: check({"transport": "http", "url": "https://mcp.example/mcp"})) == ""
    assert as_user("guest", lambda: check({"headers": {"X": "${HEPHAESTUS_TOKEN}"}})) == rules.SECRETS
    assert as_user("guest", lambda: check({"headers": {"Authorization": "Bearer my-own-key"}})) == ""
    assert as_user("guest", lambda: check({"auth": {"type": "bearer"}})) == rules.SECRETS
    assert as_user("guest", lambda: check({"allow_private": True})) == rules.PRIVATE
    # A stored row's owner decides, whoever is asking.
    assert rules.is_main({"user_id": "guest"}) is False


def test_another_profile_cannot_read_server_secrets_or_reach_private_addresses(monkeypatch):
    import connectors
    monkeypatch.setenv("SOME_SERVER_SECRET", "s3cret")

    async def read():
        return connectors.resolve_placeholder("SOME_SERVER_SECRET")

    assert as_user("default", read) == "s3cret"
    with pytest.raises(connectors.ConnectorError):
        as_user("guest", read)

    async def private():
        await connectors.assert_url_allowed("http://127.0.0.1:9/x", allow_private=True)

    as_user("default", private)
    with pytest.raises(connectors.ConnectorError):
        as_user("guest", private)


def test_secret_preference_fields_are_never_returned():
    from routes import settings as routes
    shown = routes._public_pref("ntfy", {"url": "https://ntfy.sh", "topic": "t", "token": "tk_secret"})
    assert shown == {"url": "https://ntfy.sh", "topic": "t", "token_set": True}
    assert routes._public_pref("prompts", [{"title": "x"}]) == [{"title": "x"}]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(db, "DATABASE_PATH", str(tmp_path / "profiles.db"))
    monkeypatch.setattr(config, "KB_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setattr(config, "ARTIFACT_DIR", str(tmp_path / "artifacts"))
    asyncio.run(db.init_db())
    return str(tmp_path / "profiles.db")


def _tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'")]


# Not profile data: a lease list that durable_storage reconciles against the files on disk.
EXEMPT = {"temporary_outputs"}


def test_every_table_with_a_user_column_is_removed_with_its_profile(isolated):
    conn = sqlite3.connect(isolated)
    source = open(db.__file__).read()
    delete_code = re.search(r"async def _delete_user_conn.*?return True", source, re.S).group(0)
    missing = []
    for table in _tables(conn):
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if "user_id" not in columns or table in EXEMPT or table in ("users", "user_sessions"):
            continue
        cascades = any(r[2] == "users" and r[6] == "CASCADE" for r in conn.execute(f"PRAGMA foreign_key_list({table})"))
        named = table in db.USER_OWNED_TABLES or re.search(rf'"{table}"|FROM {table}\b', delete_code)
        if not (cascades or named):
            missing.append(table)
    assert missing == [], f"profile delete leaves rows in: {missing}"


def _fill(conn, table, uid, made):
    """Insert one row owned by `uid`, filling required columns and pointing foreign keys at rows made so far."""
    parents = {r[3]: r[2] for r in conn.execute(f"PRAGMA foreign_key_list({table})")}
    names, values = [], []
    for _, name, kind, notnull, default, pk in conn.execute(f"PRAGMA table_info({table})"):
        if name == "user_id":
            value = uid
        elif name in parents and parents[name] in made:
            value = made[parents[name]]
        elif pk and "INT" not in (kind or "").upper():
            value = f"row-{table}"
        elif notnull and default is None and not pk:
            value = 0 if any(k in (kind or "").upper() for k in ("INT", "REAL", "NUM")) else "x"
        else:
            continue
        names.append(name)
        values.append(value)
    cursor = conn.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' * len(names))})", values)
    key = next((v for n, v in zip(names, values) if n == "id"), cursor.lastrowid)
    made[table] = key


def test_deleting_a_profile_leaves_no_task_password_or_note_behind(isolated):
    user = asyncio.run(db.create_user("Guest", ""))
    uid = user["id"]
    conn = sqlite3.connect(isolated)
    made = {}
    for table in reversed(db.USER_OWNED_TABLES):          # parents first
        _fill(conn, table, uid, made)
    conn.commit()
    for table in db.USER_OWNED_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (uid,)).fetchone()[0] == 1, table
    conn.close()

    assert asyncio.run(db.delete_user(uid)) is True

    conn = sqlite3.connect(isolated)
    left = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (uid,)).fetchone()[0] for t in db.USER_OWNED_TABLES}
    assert not any(left.values()), left


def test_fresh_install_leaves_nothing_for_the_next_main_profile(isolated):
    conn = sqlite3.connect(isolated)
    made = {}
    for table in reversed(db.USER_OWNED_TABLES):
        _fill(conn, table, db.DEFAULT_USER_ID, made)
    conn.commit()
    conn.close()
    asyncio.run(db.delete_all_users_and_data())
    conn = sqlite3.connect(isolated)
    assert {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in db.USER_OWNED_TABLES} == \
        {t: 0 for t in db.USER_OWNED_TABLES}
