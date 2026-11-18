"""Chat tools for the skill library: ``skill_view`` loads a saved procedure, ``skill_manage`` saves or fixes one.

Kept in their own dict (like ``DOCUMENT_TOOLS``) rather than in ``CODEAGENT_TOOLS``, because the
``codeagent`` suite hands every entry of that dict to Daedalus conversations and skills must stay out of
those. ``skills.SkillTurn.install`` is the only place that offers them.

Text-fallback models cannot carry a multi-line markdown body through tool-call parsing
(``tooling/parser.py`` strips code fences and parses strict JSON), so ``SKILL_TEXT_TOOLS`` offers
``skill_manage`` without ``body``: a body-less create records a request and the post-turn reviewer
writes the body in JSON mode.
"""
from __future__ import annotations

import difflib

import config
import skills
from db import skill_store

STRING = {"type": "string"}


def _definition(name: str, description: str, properties: dict, required: list) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


_VIEW = ("Load a saved skill (a procedure learned in earlier chats) BEFORE doing a task it covers. "
         "name = exact name from the Skills list; 'list' shows all of them.")
_MANAGE = ("Save or fix a reusable skill after working out a multi-step task. create = new skill; "
           "patch = correct or extend an existing one (prefer this over a similar new skill, and load it "
           "with skill_view first); archive = retire one. Never store secrets.")

SKILL_TOOLS = {
    "skill_view": _definition("skill_view", _VIEW, {
        "name": {"type": "string", "description": "Skill name, or 'list'"},
        "file": {"type": "string", "description": "Optional: one of the skill's files to read, e.g. scripts/convert.py"},
    }, ["name"]),
    "skill_manage": _definition("skill_manage", _MANAGE, {
        "action": {"type": "string", "enum": ["create", "patch", "archive", "write_file", "remove_file"]},
        "name": {"type": "string", "description": "lowercase-hyphen name, e.g. batch-resize-images"},
        "description": {"type": "string", "description": "One line, at most 100 characters: when to use it (starts 'Use when ...')"},
        "body": {"type": "string", "description": "Markdown: ## When to use / ## Steps / ## Pitfalls. Rules and why, "
                                                  "not a log of this chat. On patch this replaces the whole text."},
        "old_text": {"type": "string", "description": "patch: exact text to replace"},
        "new_text": {"type": "string", "description": "patch: replaces old_text, or is appended when old_text is empty"},
        "path": {"type": "string", "description": "write_file/remove_file: scripts/<file>, references/<file> or templates/<file>"},
        "content": {"type": "string", "description": "write_file: the file's full text"},
    }, ["action", "name"]),
}

SKILL_TEXT_TOOLS = {
    "skill_view": _definition("skill_view", _VIEW, {"name": STRING}, ["name"]),
    "skill_manage": _definition(
        "skill_manage",
        "Save or fix a reusable skill after working out a multi-step task. Give the action (create, patch or "
        "archive), a lowercase-hyphen name and a one-line description; the procedure text is written from this "
        "conversation after you reply.",
        {"action": STRING, "name": STRING, "description": STRING, "new_text": STRING}, ["action", "name"]),
}


async def _upload(http, remote_path: str, data: bytes) -> None:
    from routes.chat_files import upload_bytes_to_codebox   # call time: routes import the app's router stack
    await upload_bytes_to_codebox(http, remote_path, data)


async def stage(http, skill: dict, files: list[dict]) -> tuple[list[str], str]:
    """Copy a skill's files into the sandbox so execute_code can run them. Returns (paths, error)."""
    if not files or http is None or not getattr(config, "CODEBOX_URL", ""):
        return [], "" if not files else "no sandbox"
    staged = []
    try:
        for item in files:
            remote = f"/root/skills/{skill['name']}/{item['path']}"
            await _upload(http, remote, item["content"].encode())
            staged.append(remote)
    except Exception as error:
        return staged, str(error)[:200]
    return staged, ""


def _listing(rows) -> str:
    if not rows:
        return "No skills are saved yet."
    lines = [f"- {row['name']}: {row['description']}" + (" [unused lately]" if row["status"] == "stale" else "")
             for row in rows[:80]]
    return "Saved skills (load one with skill_view(name)):\n" + "\n".join(lines)


def _closest(name: str, rows) -> list[str]:
    names = [row["name"] for row in rows]
    close = difflib.get_close_matches(skills.slug(name), names, n=3, cutoff=0.5)
    wanted = skills.tokens(name)
    for row in rows:
        if len(close) < 3 and row["name"] not in close and wanted & skills.tokens(f"{row['name']} {row['description']}"):
            close.append(row["name"])
    return close


async def view(turn: skills.SkillTurn, args: dict, http) -> str:
    name = str(args.get("name") or args.get("input") or "").strip()
    rows = await skill_store.list_skills(skill_store.USABLE, turn.user_id)
    if name.lower() in ("", "list", "all"):
        return _listing(rows)
    skill = next((row for row in rows if row["name"] in (name, skills.slug(name))), None)
    if not skill:
        other = await skill_store.get_by_name(skills.slug(name), turn.user_id)
        if other:
            raise skills.SkillError(f"the skill '{other['name']}' is {other['status']} and cannot be used until the user "
                                    + ("approves it" if other["status"] == "draft" else "restores it"))
        close = _closest(name, rows)
        raise skills.SkillError(f"no skill named '{name}'." + (f" Closest: {', '.join(close)}" if close else ""))
    files = await skill_store.files(skill["id"], turn.user_id, content=True)
    wanted_file = str(args.get("file") or "").strip()
    if wanted_file:
        item = next((f for f in files if f["path"] == wanted_file.lstrip("/")), None)
        if not item:
            raise skills.SkillError(f"'{skill['name']}' has no file '{wanted_file}'. Files: "
                                    + (", ".join(f["path"] for f in files) or "none"))
        return f"{skills.VIEW_MARK}{skill['name']} — {item['path']}\n\n{item['content']}"
    await skill_store.touch(skill["id"], turn.user_id)
    turn.viewed[skill["name"]] = skill["body"]
    parts = [f"{skills.VIEW_MARK}{skill['name']} (v{skill['version']})", skill["description"], "", skill["body"].strip()]
    if files:
        staged, error = await stage(http, skill, files)
        if staged and not error:
            parts += ["", "Files, ready in the sandbox for execute_code:"] + [f"- {path}" for path in staged]
        else:
            parts += ["", f'Files (read one with skill_view("{skill["name"]}", file=path)):']
            parts += [f"- {item['path']}" for item in files]
    parts += ["", "Reference procedure — the user's current request wins."]
    return "\n".join(parts)


async def _manage_file(turn: skills.SkillTurn, action: str, name: str, args: dict) -> str:
    skill = await skill_store.get_by_name(skills.slug(name), turn.user_id)
    if not skill:
        raise skills.SkillError(f"no skill named '{name}'")
    if skill["status"] == "archived":
        raise skills.SkillError(f"the skill '{skill['name']}' is archived; ask the user to restore it first")
    if action == "remove_file":
        held = skills.hold_reason(turn)
        if held and skill["status"] != "draft":
            raise skills.SkillError(f"cannot change the files of a live skill here ({held}); ask the user")
        path = skills.file_path(args.get("path"))
        if not await skill_store.delete_file(skill["id"], path, turn.user_id):
            raise skills.SkillError(f"'{skill['name']}' has no file '{path}'")
        return f"Removed {path} from skill '{skill['name']}'."
    path, content = skills.validate_file(args.get("path"), args.get("content"))
    held = skills.hold_reason(turn, skills.scan(content))
    if held and skill["status"] != "draft":
        raise skills.SkillError(f"cannot change the files of a live skill here ({held}); ask the user")
    existing = await skill_store.files(skill["id"], turn.user_id)
    if len(existing) >= skills.MAX_FILES and path not in {item["path"] for item in existing}:
        raise skills.SkillError(f"a skill holds at most {skills.MAX_FILES} files")
    await skill_store.put_file(skill["id"], path, content, turn.user_id)
    turn.managed = True
    return f"Saved {path} with skill '{skill['name']}'. skill_view stages it in the sandbox for execute_code."


async def manage(turn: skills.SkillTurn, args: dict) -> str:
    action = str(args.get("action") or "").strip().lower()
    name = str(args.get("name") or args.get("input") or "").strip()
    if not name:
        raise skills.SkillError("skill_manage needs a name")
    if action in ("write_file", "remove_file"):
        return await _manage_file(turn, action, name, args)
    body, new_text = str(args.get("body") or ""), str(args.get("new_text") or "")
    key = skills.slug(name)
    # Weak models repeat a call that already succeeded; refusing it ("already exists") makes them loop.
    if action == "create" and key in turn.saved:
        return f"'{key}' is already saved. Do not call skill_manage again; reply to the user now."
    if action == "create" and turn.saved:
        # One new skill per turn: a model that keeps saving variations is looping, not learning.
        return (f"You already saved '{sorted(turn.saved)[0]}' this turn. One new skill per turn; "
                "do not call skill_manage again, reply to the user now.")
    if action in ("create", "patch") and turn.learn_request and turn.learn_request.get("name") == key:
        return "Already noted. Do not call skill_manage again; reply to the user now."
    if action == "patch" and not await skill_store.get_by_name(key, turn.user_id):
        # A model that "patches" a skill that does not exist means to create it (seen live with a
        # text-fallback model, which then lost the thread on the error).
        action, body, new_text = "create", body or new_text, ""
    if action == "patch" and key not in turn.viewed:
        raise skills.SkillError(f'load the skill with skill_view("{key}") first, then patch what you read')
    thin = len(body.strip()) < 40 if action == "create" else not (body.strip() or new_text.strip())
    if action in ("create", "patch") and thin:
        # A text-fallback model could not send the procedure text; the reviewer writes it after the reply.
        if not turn.settings.get("auto_learn"):
            raise skills.SkillError("this needs a body: markdown with When to use / Steps / Pitfalls")
        turn.learn_request = {"action": action, "name": key, "description": skills.one_line(args.get("description")),
                              "note": skills.one_line(body or new_text, 300)}
        return ("Noted. The skill will be written from this conversation after your reply. "
                "Do not call skill_manage again; reply to the user now.")
    saved = await skills.save_from_agent(
        turn, action, name=name, description=str(args.get("description") or ""), body=body,
        old_text=str(args.get("old_text") or ""), new_text=new_text, category=str(args.get("category") or ""),
        origin="agent", reason="saved in chat")
    skill, state, held = saved["skill"], saved["state"], saved["held"]
    if action != "archive":
        turn.managed = True
        turn.saved.add(skill["name"])
        turn.viewed[skill["name"]] = skill["body"]
    if state == "archived":
        return f"Archived skill '{skill['name']}'. The user can restore it from the Skills panel."
    if state == "pending":
        return (f"The change to '{skill['name']}' is waiting for the user's approval ({held}); "
                "the current version stays in use.")
    if state == "draft":
        return (f"Saved skill '{skill['name']}' as a draft ({held}). It is used once the user approves it "
                "in the Skills panel.")
    return f"Saved skill '{skill['name']}'. It is active and listed in future chats."


async def run(name: str, args: dict, *, http, events, conv_id: str) -> str:
    """Dispatch entry used by tools._exec_tool_scoped. Always returns a string."""
    turn = skills.current_turn(conv_id)
    if not turn:
        return "ERROR: skills are not available in this chat."
    try:
        result = await (view(turn, args or {}, http) if name == "skill_view" else manage(turn, args or {}))
    except skills.SkillError as error:
        result = f"ERROR: {error}"
    except Exception as error:
        print(f"[SKILLS] {name} failed: {error}")
        result = f"ERROR: the skill tool failed: {error}"
    try:
        status = result.split("\n", 1)[0].replace(skills.VIEW_MARK, "Loaded skill: ")[:90]
        await events.emit(conv_id, "tool_end", {"tool": name, "icon": "star", "status": status})
    except Exception:
        pass
    return result
