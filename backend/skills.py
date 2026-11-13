"""Skills: procedures HyprChat saves from its own work and reuses in later chats.

A skill is a short reusable procedure for a class of task. Only its name and one-line description
sit in the system prompt (a stable block, so Ollama's prompt-cache prefix survives); the body is loaded
on demand with ``skill_view`` and written with ``skill_manage`` or by the post-turn reviewer in
``skills_learning``.

This module owns the rules that do not need a model:

* ``validate`` / ``scan`` — what may be stored, and which text has to wait for the owner.
* ``build_index`` / ``prompt_block`` / ``match_hint`` — what a chat sees.
* ``SkillTurn`` — per-turn state: which skills were loaded, the tool trace the reviewer reads, and
  whether this conversation read attacker-addressable content.
* ``save_from_agent`` — the ONE write path for the chat tool and the reviewer, so the go-live policy
  (``hold_reason``) cannot be bypassed by either.

Skills are text. They grant no tools and change no server-side gate.
"""
from __future__ import annotations

import contextvars
import json
import re
from datetime import datetime

import database as db
from db import skill_store

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
MAX_DESCRIPTION = 100
MAX_BODY = 6000            # a view costs about 2K tokens; larger bodies crowd a 32K window
FILE_ROOTS = ("scripts", "references", "templates")
FILE_NAME = re.compile(r"^[A-Za-z0-9._/-]+$")
MAX_FILES = 10
MAX_FILE_BYTES = 64 * 1024
VIEW_MARK = "## SKILL "    # prefix of every skill_view result; chat.py keeps these out of its pruner
PREF_KEY = "skills"
DEFAULTS = {"enabled": True, "auto_learn": True, "approval": "auto", "model": "",
            "index_chars": 1500, "min_tool_calls": 5, "stale_days": 30, "archive_days": 90}
INDEX_WINDOW_SHARE = 0.04  # of the context window, in tokens
EMAIL_TOOLS = frozenset({"list_emails", "read_email"})
TASK_PERSONAS = frozenset({"master developer"})   # persona-type profiles that do tool work, not roleplay
TRACE_LIMIT = 60

# A literal credential, not the word "token": skill bodies legitimately say `api_key = os.environ[...]`.
SECRET = re.compile(
    r"(password|passwd|api[_ -]?key|secret|private[_ -]?key|token)\s*[:=]\s*[\"']?[A-Za-z0-9+/_\-]{12,}"
    r"|\bsk-(ant-)?[A-Za-z0-9_\-]{20,}|\bghp_[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b|\bxox[bap]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I)

# Text that would be dangerous as a standing instruction. A hit never blocks: the skill waits for review.
SCAN = (
    ("override", re.compile(
        r"ignore (all |any )?(the )?(previous|prior|above|earlier|system) (instructions?|rules|prompts?)"
        r"|disregard (the |your |all )?(system|previous|prior)|new system prompt|you are now (a|an|in) "
        r"|(do not|don't|never) (tell|inform|mention|reveal)[^.\n]{0,30}\buser\b"
        r"|without (telling|asking|informing|notifying) the user|\bsecretly\b", re.I)),
    ("exfiltration", re.compile(
        r"\b(send|post|upload|forward|exfiltrate|leak|transmit)\b[^.\n]{0,80}\bhttps?://"
        r"|\b(curl|wget)\b[^\n|]*\|\s*(ba|z|da)?sh\b|base64\s+(-d|--decode)[^\n|]*\|\s*(ba|z)?sh\b", re.I)),
    ("credentials", re.compile(
        r"\b(cat|read|print|dump|copy|upload)\b[^.\n]{0,40}(\.env\b|id_rsa|\.ssh/|credentials\b|api keys?\b|passwords?\b)", re.I)),
    ("email", re.compile(
        r"\b(send|forward|bcc|cc|reply)\b[^.\n]{0,60}\b[\w.+-]+@[\w-]+\.[\w.]+|\balways (send|forward|bcc|email)\b", re.I)),
    ("encoded", re.compile(r"[A-Za-z0-9+/]{240,}={0,2}")),
)

# The user asking for something to be kept: "remember how to ...", "save this as a skill", "next time do ...".
ASKED = re.compile(
    r"\b(remember|save|store|keep|note|learn)\b[^.?!\n]{0,40}\b(how (to|you|we|i)\b|as a skill|this (procedure|process|workflow|method))"
    r"|\b(make|create|write|add)\b[^.?!\n]{0,20}\bskill\b|\bnext time\b[^.?!\n]{0,60}\b(do|use|run|remember)\b", re.I)
MENTIONS_SKILLS = re.compile(r"\bskills?\b|skill_manage", re.I)
QUIET_TOOLS = frozenset({       # bookkeeping calls: not evidence that a procedure was worked out
    "skill_view", "skill_manage", "save_memory", "search_history",
    "manage_tasks", "manage_notes", "manage_calendar", "download_file"})

WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset("""
the and for with that this from into how can you your please make get help using have has are was will
would should could some any all not but about what which their them then than also just like new one two
out via per its use used when user users ask asks need needs want wants file files thing things does did
done let lets see show tell give take work works way ways time step steps task tasks""".split())


class SkillError(ValueError):
    """A refusal the model or the owner can act on; its text is shown to them."""


# ── Settings (per user, in user_prefs) ──

def clean_settings(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    for key in ("enabled", "auto_learn"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    if raw.get("approval") in ("auto", "review"):
        out["approval"] = raw["approval"]
    if isinstance(raw.get("model"), str):
        out["model"] = raw["model"].strip()[:200]
    for key, low, high in (("index_chars", 400, 6000), ("min_tool_calls", 2, 30),
                           ("stale_days", 1, 3650), ("archive_days", 2, 3650)):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = max(low, min(high, int(value)))
    out["archive_days"] = max(out["archive_days"], out["stale_days"] + 1)
    return out


async def settings(user_id: str | None = None) -> dict:
    return clean_settings(await db.get_user_pref(PREF_KEY, user_id))


async def save_settings(patch: dict, user_id: str | None = None) -> dict:
    merged = clean_settings({**await settings(user_id), **(patch or {})})
    await db.set_user_pref(PREF_KEY, merged, user_id)
    return merged


# ── What may be stored ──

def slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:48].strip("-")


def one_line(text, limit: int = MAX_DESCRIPTION) -> str:
    line = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(line) <= limit:
        return line
    cut = line[:limit - 1]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,;:.") + "…"


def validate(name, description, body) -> tuple[str, str, str]:
    name = slug(name)
    if not NAME.match(name):
        raise SkillError("name must be 2-48 characters of lowercase letters, digits and hyphens, e.g. batch-resize-images")
    description = one_line(description)
    if len(description) < 12:
        raise SkillError("description must be one line saying when to use the skill")
    body = str(body or "").replace("\r\n", "\n").strip()
    if len(body) < 40:
        raise SkillError("body must describe the procedure: when to use it, the steps, the pitfalls")
    if len(body) > MAX_BODY:
        raise SkillError(f"body is {len(body)} characters; the limit is {MAX_BODY}. Rewrite it shorter.")
    if SECRET.search(description + "\n" + body):
        raise SkillError("refusing to save what looks like a secret or credential; say where it is configured instead")
    return name, description, body


def file_path(path) -> str:
    cleaned = str(path or "").strip().replace("\\", "/").lstrip("/")
    parts = cleaned.split("/")
    if (len(parts) < 2 or parts[0] not in FILE_ROOTS or len(cleaned) > 120 or not FILE_NAME.match(cleaned)
            or any(part in ("", ".", "..") for part in parts)):
        raise SkillError("file path must be scripts/<name>, references/<name> or templates/<name>")
    return cleaned


def validate_file(path, content) -> tuple[str, str]:
    path, content = file_path(path), str(content or "")
    if not content.strip():
        raise SkillError("file content is empty")
    if len(content.encode()) > MAX_FILE_BYTES:
        raise SkillError(f"file is larger than {MAX_FILE_BYTES // 1024} KB")
    if SECRET.search(content):
        raise SkillError("refusing to save what looks like a secret or credential in a skill file")
    return path, content


def scan(*texts) -> list[str]:
    text = "\n".join(t for t in texts if t)
    return [flag for flag, pattern in SCAN if pattern.search(text)]


def tokens(text) -> set[str]:
    found = set()
    for word in WORD.findall(str(text or "").lower()):
        if len(word) >= 3 and word not in STOP:
            found.add(word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word)
    return found


def similar(name: str, description: str, rows) -> dict | None:
    """An existing skill this one would duplicate, judged on name and description words."""
    name_words, words = tokens(name), tokens(f"{name} {description}")
    for row in rows:
        other_name, other = tokens(row["name"]), tokens(f"{row['name']} {row['description']}")
        shared_name = name_words & other_name
        if len(shared_name) >= 2 and len(shared_name) >= 0.67 * min(len(name_words), len(other_name)):
            return row
        if words and other and len(words & other) / len(words | other) >= 0.5:
            return row
    return None


SECTION = re.compile(r"^##\s+(.+?)\s*$", re.M)


def appended(body: str, addition: str) -> str:
    """Add text to a skill. Text under a heading the skill already has joins that section
    instead of repeating the heading (a model asked for "a new pitfall" writes "## Pitfalls" again)."""
    out = body.rstrip()
    for part in re.split(r"(?m)^(?=##\s)", addition.strip()):
        part = part.strip()
        if not part:
            continue
        heading = SECTION.match(part)
        content = part[heading.end():].strip() if heading else ""
        existing = heading and next((found for found in SECTION.finditer(out)
                                     if found.group(1).strip().lower() == heading.group(1).strip().lower()), None)
        if existing and content:
            following = SECTION.search(out, existing.end())
            end = following.start() if following else len(out)
            out = out[:end].rstrip() + "\n" + content + ("\n\n" + out[end:] if following else "")
        elif not existing:
            out = out + "\n\n" + part
    return out


def patched(body: str, *, new_body: str = "", old_text: str = "", new_text: str = "", mode: str = "") -> str:
    """The body after a patch: a full rewrite, a replacement of old_text, or an append."""
    if new_body.strip() and mode != "append":
        return new_body
    addition = new_body if mode == "append" else new_text
    if old_text.strip():
        if old_text in body:
            return body.replace(old_text, new_text, 1)
        found = re.search(r"\s+".join(re.escape(part) for part in old_text.split()), body)
        if not found:
            raise SkillError("old_text was not found in the skill; load it with skill_view and copy the text exactly")
        return body[:found.start()] + new_text + body[found.end():]
    if not addition.strip():
        raise SkillError("a patch needs new_text (appended, or replacing old_text) or a full body")
    return appended(body, addition)


# ── What a chat sees ──

def build_index(rows, budget: int) -> tuple[list[str], int]:
    """Index lines in category/name order, and how many skills did not fit.

    Use counts never affect the order, and recency only decides what is dropped when the budget is
    exceeded (at day granularity), so loading a skill does not change the prompt.
    """
    lines = {row["name"]: f"- {row['name']}: {row['description']}" for row in rows}
    keep = set(lines)
    if sum(len(line) + 1 for line in lines.values()) > budget:
        priority = sorted(rows, key=lambda r: r["name"])
        priority.sort(key=lambda r: (r.get("last_used_at") or r.get("created_at") or "")[:10], reverse=True)
        priority.sort(key=lambda r: (not r.get("pinned"), r.get("status") != "active"))
        keep, used = set(), 0
        for row in priority:
            cost = len(lines[row["name"]]) + 1
            if used + cost <= budget:
                keep.add(row["name"])
                used += cost
    ordered = sorted(rows, key=lambda r: (r.get("category") or "", r["name"]))
    return [lines[row["name"]] for row in ordered if row["name"] in keep], len(rows) - len(keep)


def prompt_block(rows, budget: int) -> str:
    if not rows:
        return ""
    lines, hidden = build_index(rows, budget)
    more = f'\n(+{hidden} more — call skill_view("list") to see them)' if hidden else ""
    return (
        "\n\n## Skills\n"
        "Skills are procedures saved from earlier chats. Before a multi-step task, check this list; "
        "if one fits, call skill_view(name) FIRST and follow it.\n"
        + "\n".join(lines) + more + "\n"
        "After you work out a reusable multi-step procedure (several tool calls, an error you fixed, "
        "or the user corrected you), call skill_manage — patch a close existing skill rather than "
        "creating a similar one. Never store secrets."
    )


def match_hint(query: str, rows) -> dict | None:
    """The one skill a request clearly points at, without asking a model. None when unsure."""
    wanted = tokens(query)
    if len(wanted) < 2 or not rows:
        return None
    described = [(row, tokens(row["name"]), tokens(row["description"])) for row in rows]
    common = set()
    if len(rows) >= 4:      # words most skills share say nothing about which one applies
        for word in wanted:
            if sum(word in name | text for _, name, text in described) > len(rows) / 2:
                common.add(word)
    scored = []
    for row, name, text in described:
        hits = (wanted & (name | text)) - common
        if len(hits) >= 3 or (len(hits) >= 2 and hits & name):
            scored.append((len(hits) + len(hits & name), row["name"], row))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][2]


def hint_text(skill: dict) -> str:
    return (f'[Saved skill may apply: {skill["name"]} — {skill["description"]} '
            f'Load it with skill_view("{skill["name"]}") before starting.]')


def persona_uses_skills(name, params, profile_type: str) -> bool:
    """Whether a chat with this agent profile gets skills.

    Character personas are for roleplay: an index of procedures breaks immersion and their models are
    weaker at tools. Task agents and the Personal Assistant get skills. ``parameters.use_skills`` (a
    boolean) overrides the default either way.
    """
    flag = (params or {}).get("use_skills") if isinstance(params, dict) else None
    if isinstance(flag, bool):
        return flag
    lowered = str(name or "").lower()       # seeded names carry an emoji prefix
    return profile_type != "persona" or any(task in lowered for task in TASK_PERSONAS)


# ── Per-turn state ──

_TURN: contextvars.ContextVar = contextvars.ContextVar("hyprchat_skill_turn", default=None)


def _short(value, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def is_error(result: str) -> bool:
    head = result.lstrip()[:400]
    return (head.startswith(("ERROR", "Error", "BLOCKED", "**FAILED**")) or "Traceback (most recent call last)" in result)


class SkillTurn:
    """What one chat turn knows about skills. An instance made with no settings is inert."""

    def __init__(self, conv_id: str = "", user_id: str = "", config: dict | None = None, rows=(), *,
                 headless: bool = False, num_ctx: int = 0, query: str = "", model: str = "", taint: str = "",
                 recent=()):
        self.conv_id, self.user_id = conv_id, user_id
        self.settings = config or dict(DEFAULTS, enabled=False)
        self.enabled = bool(config and config.get("enabled"))
        self.rows = list(rows) if self.enabled else []
        self.headless, self.num_ctx, self.query, self.model, self.taint = headless, int(num_ctx or 0), query or "", model, taint
        self.recent = list(recent)            # (role, text) of the turns before this one, for the reviewer
        self.viewed: dict[str, str] = {}      # name -> body shown this turn
        self.managed = False                  # a skill was written in chat this turn
        self.learn_request: dict | None = None  # the model asked to save a skill but could not send a body
        self.trace: list[dict] = []
        self.untrusted: frozenset = frozenset()
        self.announced: set = set()           # one bell notification per skill per turn, however many patches
        self.saved: set = set()               # names written this turn; a repeat call is answered, not refused
        self.reviewed = False                 # the post-turn review runs at most once per turn
        budget = self.settings["index_chars"]
        if self.num_ctx:
            budget = min(budget, int(self.num_ctx * INDEX_WINDOW_SHARE * 4))
        self.prompt_block = prompt_block(self.rows, budget) if self.enabled else ""

    def install(self, ollama_tools: list, available_tool_names: set, extra_text_tool_defs: list,
                messages: list, untrusted=()) -> None:
        """Offer the skill tools and, when one skill clearly applies, say so beside the user's message."""
        if not self.enabled:
            return
        try:
            from tooling.skill_tools import SKILL_TOOLS, SKILL_TEXT_TOOLS
            self.untrusted = frozenset(untrusted or ())
            for name in (("skill_view",) if self.rows else ()) + ("skill_manage",):
                if name not in available_tool_names:
                    ollama_tools.append(SKILL_TOOLS[name])
                    extra_text_tool_defs.append(SKILL_TEXT_TOOLS[name])
                    available_tool_names.add(name)
            hinted = match_hint(self.query, self.rows)
            if hinted:
                for message in reversed(messages):
                    if message.get("role") == "user" and isinstance(message.get("content"), str):
                        message["content"] += "\n\n" + hint_text(hinted)
                        break
        except Exception as error:
            print(f"[SKILLS] install failed (non-fatal): {error}")

    async def record(self, name: str, args, result) -> None:
        """Note one executed tool call for the reviewer. Never raises."""
        if not self.enabled:
            return
        try:
            result = str(result or "")
            entry = {"name": name, "args": _short(args or {}, 200), "result": _short(result, 300), "error": is_error(result)}
            if name == "execute_code" and isinstance(args, dict):
                entry["code"] = str(args.get("code") or "")
                entry["language"] = str(args.get("language") or "python").lower()
            if len(self.trace) < TRACE_LIMIT:
                self.trace.append(entry)
            reason = "email" if name in EMAIL_TOOLS else "connector" if name in self.untrusted else ""
            if reason and not self.taint:
                self.taint = reason
                await skill_store.taint(self.conv_id, reason, user_id=self.user_id)
        except Exception as error:
            print(f"[SKILLS] record failed (non-fatal): {error}")

    async def review(self, http, events, user_text: str, answer: str) -> None:
        """Post-turn learning. Runs in the background, at most once per turn, and is silent on any failure."""
        if not self.enabled or self.reviewed:
            return
        self.reviewed = True
        try:
            import skills_learning
            await skills_learning.review_turn(self, http, events, user_text, answer)
        except Exception as error:
            print(f"[SKILLS] review failed (non-fatal): {error}")


async def begin_turn(req, conv_id: str, *, ephemeral: bool = False, coder: bool = False, roleplay: bool = False,
                     num_ctx: int = 0, query: str = "") -> SkillTurn:
    """Per-turn skill state for a chat request. Never raises; returns an inert turn when skills do not apply."""
    turn = SkillTurn()
    try:
        user_id = db.current_user_id()
        import skills_learning
        skills_learning.cancel_pending(user_id)      # a new message wants the model slot more than a review does
        if conv_id and not (ephemeral or coder or roleplay):
            config = await settings(user_id)
            if config["enabled"]:
                metadata = getattr(req, "user_metadata", None)
                turn = SkillTurn(
                    conv_id, user_id, config, await skill_store.list_skills(skill_store.USABLE, user_id),
                    headless=isinstance(metadata, dict) and metadata.get("source") == "scheduled_task",
                    num_ctx=num_ctx, query=query, model=str(getattr(req, "model", "") or ""),
                    taint=await skill_store.taint_reason(conv_id, user_id), recent=recent_turns(req))
    except Exception as error:
        print(f"[SKILLS] begin_turn failed (non-fatal): {error}")
        turn = SkillTurn()
    _TURN.set(turn)
    return turn


def recent_turns(req, limit: int = 6, chars: int = 600) -> list[tuple[str, str]]:
    """The conversation just before the current message. "Remember how to do this" refers to it."""
    messages = [m for m in (getattr(req, "messages", None) or []) if isinstance(m, dict)
                and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip()]
    if messages and messages[-1]["role"] == "user":
        messages = messages[:-1]
    return [(m["role"], m["content"].strip()[:chars]) for m in messages[-limit:]]


def current_turn(conv_id: str = "") -> SkillTurn | None:
    turn = _TURN.get()
    return turn if turn and turn.enabled and (not conv_id or turn.conv_id == conv_id) else None


# ── Writing (the one path for the chat tool and the reviewer) ──

def unprompted(turn: SkillTurn) -> bool:
    """A save nobody asked for in a turn where nothing was worked out with tools.

    Weak models do this while answering a plain question (seen live: two near-identical skills from one
    "how do I ..." turn). The text is kept, as a draft.
    """
    worked = any(entry["name"] not in QUIET_TOOLS for entry in turn.trace)
    return not worked and not (ASKED.search(turn.query) or MENTIONS_SKILLS.search(turn.query))


def hold_reason(turn: SkillTurn, flags=()) -> str:
    """Why a write must wait for the owner; '' when it may go live now."""
    if turn.settings.get("approval") == "review":
        return "review-first mode"
    if turn.headless:
        return "learned in a scheduled run"
    if turn.taint:
        return f"this conversation read {turn.taint} content"
    if flags:
        return "safety scan: " + ", ".join(flags)
    return ""


async def _announce(turn: SkillTurn, skill: dict, what: str, held: str) -> None:
    titles = {("created", False): "Learned a skill: {}", ("created", True): "Skill needs review: {}",
              ("patched", False): "Updated skill: {}", ("patched", True): "Skill update needs review: {}",
              ("archived", False): "Archived skill: {}"}
    key = (skill["name"], bool(held), what == "archived")
    if key in turn.announced:
        return
    turn.announced.add(key)
    try:
        import notifications
        body = skill["description"] + (f" (held: {held})" if held else "")
        await notifications.notify(titles[(what, bool(held))].format(skill["name"]), body, kind="skill",
                                   conversation_id=turn.conv_id, user_id=turn.user_id)
    except Exception as error:
        print(f"[SKILLS] notification failed (non-fatal): {error}")


async def save_from_agent(turn: SkillTurn, action: str, *, name: str, description: str = "", body: str = "",
                          category: str = "", old_text: str = "", new_text: str = "", mode: str = "",
                          origin: str = "agent", reason: str = "") -> dict:
    """Create, patch or archive a skill for the model. Raises SkillError with a message it can act on.

    Returns ``{"skill", "state": active|draft|pending|archived, "held": reason}``.
    """
    user_id = turn.user_id
    if action == "create":
        name, description, body = validate(name, description, body)
        existing = await skill_store.get_by_name(name, user_id)
        if existing:
            raise SkillError(f"a skill named '{name}' already exists ({existing['status']}); load it with skill_view and patch it")
        twin = similar(name, description, await skill_store.list_skills(("active", "stale", "draft"), user_id))
        if twin:
            raise SkillError(f"the skill '{twin['name']}' already covers this ({twin['description']}); "
                             "patch it instead of creating a similar one")
        flags = scan(name, description, body)
        held = hold_reason(turn, flags)
        if not held and origin == "agent" and unprompted(turn):
            held = "saved without being asked, in a turn with no tool work"
        skill = await skill_store.create(
            name, description, body, category=slug(category)[:24] or "general", status="draft" if held else "active",
            origin=origin, source_conv_id=turn.conv_id, scan_flags=flags, user_id=user_id)
        await _announce(turn, skill, "created", held)
        return {"skill": skill, "state": skill["status"], "held": held}

    skill = await skill_store.get_by_name(slug(name), user_id)
    if not skill:
        raise SkillError(f"no skill named '{name}'")
    if skill["status"] == "archived":
        raise SkillError(f"the skill '{skill['name']}' is archived; ask the user to restore it first")

    if action == "archive":
        if skill["pinned"]:
            raise SkillError("that skill is pinned by the user and cannot be archived")
        held = hold_reason(turn)
        if held:
            raise SkillError(f"cannot archive a skill here ({held}); ask the user")
        skill = await skill_store.set_fields(skill["id"], status="archived", user_id=user_id)
        await _announce(turn, skill, "archived", "")
        return {"skill": skill, "state": "archived", "held": ""}

    if action != "patch":
        raise SkillError("action must be create, patch or archive")
    new_description = one_line(description) if description.strip() else skill["description"]
    _, new_description, new_body = validate(
        skill["name"], new_description,
        patched(skill["body"], new_body=body, old_text=old_text, new_text=new_text, mode=mode))
    if (new_description, new_body) == (skill["description"], skill["body"]):
        raise SkillError("the patch changes nothing")
    flags = scan(new_description, new_body)
    held = hold_reason(turn, flags)
    if skill["status"] == "draft":           # not live yet, so the draft itself is edited
        updated = await skill_store.update(skill["id"], description=new_description, body=new_body,
                                           origin=origin, reason=reason, user_id=user_id)
        await skill_store.set_fields(skill["id"], scan_flags=flags, user_id=user_id)
        return {"skill": updated, "state": "draft", "held": held or "draft"}
    if held:                                 # the live text is not touched until the owner approves
        updated = await skill_store.set_pending(skill["id"], {
            "description": new_description, "body": new_body, "origin": origin, "reason": reason[:300],
            "held": held, "flags": flags, "at": datetime.utcnow().isoformat()}, user_id)
        await _announce(turn, updated, "patched", held)
        return {"skill": updated, "state": "pending", "held": held}
    updated = await skill_store.update(skill["id"], description=new_description, body=new_body,
                                       origin=origin, reason=reason, user_id=user_id)
    await _announce(turn, updated, "patched", "")
    return {"skill": updated, "state": "active", "held": ""}


# ── SKILL.md (agentskills.io shape) ──

def to_markdown(skill: dict) -> str:
    return ("---\n"
            f"name: {skill['name']}\n"
            f"description: {json.dumps(skill['description'], ensure_ascii=False)}\n"
            "metadata:\n  hyprchat:\n"
            f"    category: {skill.get('category') or 'general'}\n"
            f"    version: {skill.get('version') or 1}\n"
            "---\n\n" + (skill.get("body") or "").strip() + "\n")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        try:
            return str(json.loads(value)) if value[0] == '"' else value[1:-1]
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def from_markdown(text: str) -> dict:
    """Read a SKILL.md: frontmatter with name and description, then the body."""
    text = str(text or "").lstrip("﻿").replace("\r\n", "\n")
    match = re.match(r"\s*---\n(.*?)\n---[ \t]*\n?(.*)", text, re.S)
    if not match:
        raise SkillError("not a SKILL.md: it must start with a --- block holding name and description")
    fields: dict[str, str] = {}
    lines, index = match.group(1).split("\n"), 0
    while index < len(lines):
        entry = re.match(r"^\s*([A-Za-z_][\w-]*):\s*(.*)$", lines[index])
        index += 1
        if not entry:
            continue
        key, value = entry.group(1).lower(), entry.group(2).strip()
        if value in (">", "|", ">-", "|-", ">+", "|+"):      # a folded or literal YAML block
            block = []
            while index < len(lines) and (lines[index].startswith((" ", "\t")) or not lines[index].strip()):
                block.append(lines[index].strip())
                index += 1
            value = " ".join(part for part in block if part)
        fields.setdefault(key, _unquote(value))
    return {"name": slug(fields.get("name")), "description": one_line(fields.get("description")),
            "category": slug(fields.get("category"))[:24] or "general", "body": match.group(2).strip()}
