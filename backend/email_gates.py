"""Server-side switches for what the assistant may do with email on its own.

Two switches must both be on: the account's own (``allow_autonomous_send`` / ``allow_autonomous_delete``)
and the Assistant profile's master switch ``allow_autonomous_email``. A user with no profile row has never
opened the Assistant panel, and the master switch counts as on. Prompt rules never stand in for these.
The Email panel's own send and delete buttons are the user acting and do not pass through here.
"""

FLAGS = {"send": "allow_autonomous_send", "delete": "allow_autonomous_delete"}


def allowed(action: str, account: dict | None, profile: dict | None) -> bool:
    master = profile is None or bool(profile.get("allow_autonomous_email"))
    return master and bool((account or {}).get(FLAGS[action]))
