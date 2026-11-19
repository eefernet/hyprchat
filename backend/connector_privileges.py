"""What only the Main profile may do with a connector.

A connector can run a command on the server (an MCP stdio server), read the server's own secrets
(`${NAME}` placeholders come from the environment and the shared secrets file) and reach private
addresses (`allow_private`). Those three are for the Main profile. Any other profile can still add an
ordinary web connector and put its own key straight into a header.

The rule is checked when a connector is saved (a clear refusal) and again when it runs (so a row saved
before this rule, or written some other way, does nothing).
"""
import re

import database as db

STDIO = "Only the Main profile can add a connector that runs a command on the server."
SECRETS = ("Only the Main profile can use server secrets (${NAME} placeholders or connector auth) in a "
           "connector. Put your own key directly in a header instead.")
PRIVATE = "Only the Main profile can allow private or local addresses."

_WHOLE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")
_INSIDE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def is_main(row=None) -> bool:
    """True for the Main profile. A stored row's owner wins over the request's profile."""
    owner = row.get("user_id") if isinstance(row, dict) else None
    return (owner or db.current_user_id()) == db.DEFAULT_USER_ID


def has_placeholder(value) -> bool:
    if isinstance(value, str):
        return bool(_WHOLE.fullmatch(value.strip()) or _INSIDE.search(value))
    if isinstance(value, dict):
        return any(has_placeholder(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_placeholder(item) for item in value)
    return False


def refusal(fields: dict, row=None) -> str:
    """Why this profile may not save these connector fields, or ''. `fields` holds only what is being set."""
    if is_main(row):
        return ""
    fields = fields or {}
    if "transport" in fields and str(fields.get("transport") or "stdio").strip().lower() == "stdio":
        return STDIO
    if fields.get("command") or fields.get("env") or fields.get("args"):
        return STDIO
    if fields.get("allow_private"):
        return PRIVATE
    auth = fields.get("auth")
    if isinstance(auth, dict) and str(auth.get("type") or "").strip():
        return SECRETS
    if has_placeholder(fields.get("headers")) or has_placeholder(fields.get("url")) or has_placeholder(fields.get("spec_url")):
        return SECRETS
    return ""
