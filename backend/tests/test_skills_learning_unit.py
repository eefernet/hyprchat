"""The post-turn skill reviewer and the curator, with the model call faked."""
import asyncio
import json

import pytest

import database as db
import skills
import skills_learning
from db import skill_store

BODY = "## When to use\nWhenever images need resizing.\n\n## Steps\n1. Run the resize script.\n"
DESCRIPTION = "Use when many images need resizing in one batch"
OK = "**SUCCESS** | python | exit 0 | 0.1s\ndone"
FAILED = "**FAILED** | python | exit 1 | 0.1s\nModuleNotFoundError: PIL"


class Events:
    def __init__(self):
        self.emitted = []

    async def emit(self, conv_id, kind, payload):
        self.emitted.append((kind, payload))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(db, "DATABASE_PATH", str(tmp_path / "skills.db"))
    monkeypatch.setattr(config, "KB_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setattr(config, "ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setattr(skills_learning, "DEBOUNCE_SECONDS", 0)
    asyncio.run(db.init_db())
    return tmp_path


def make_turn(calls=(), *, rows=(), **options):
    config = {**skills.DEFAULTS, **options.pop("settings", {})}
    turn = skills.SkillTurn("conv-1", "default", config, list(rows), model=options.pop("model", "qwen-chat"),
                            num_ctx=options.pop("num_ctx", 32768), **options)
    for name, result in calls:
        code = {"code": "from PIL import Image\nprint('resized')", "language": "python"} if name == "execute_code" else {"q": "x"}
        asyncio.run(turn.record(name, code, result))
    return turn


def reply(monkeypatch, answer, seen=None):
    async def complete_chat(http, model, prompt, **options):
        if seen is not None:
            seen.append({"model": model, "prompt": prompt, **options})
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)
    monkeypatch.setattr(skills_learning.model_providers, "complete_chat", complete_chat)


CREATE = {"action": "create", "name": "Batch Resize Images", "description": DESCRIPTION, "category": "media",
          "body": BODY, "mode": "", "script_step": 0, "reason": "worked after installing pillow"}


# ── When to review ──

@pytest.mark.parametrize("calls,options,expected", [
    ([("execute_code", OK)] * 5, {}, "tool_calls"),
    ([("execute_code", OK)] * 4, {}, ""),
    ([("execute_code", FAILED), ("research", "results"), ("execute_code", OK)], {}, "recovered"),
    ([("execute_code", FAILED), ("execute_code", OK)], {}, ""),                       # too little work
    ([("manage_notes", "ok")] * 9, {}, ""),                                           # bookkeeping is not a procedure
    ([("execute_code", OK)] * 5, {"settings": {"auto_learn": False}}, ""),
    ([("execute_code", OK)], {"query": "Great. Remember how to do this next time."}, "asked"),
    ([], {"query": "remember how to do this"}, ""),                                   # nothing to write it from
    ([], {"query": "remember how to do this", "recent": [("assistant", "I ran pandoc with xelatex.")]}, "asked"),
    ([], {"query": "To free a port run ss -ltnp, find the pid, then kill it. " * 4 + "Save this as a skill."}, "asked"),
    ([("skill_manage", "ERROR: load the skill with skill_view first")], {}, "save_failed"),
    ([("skill_manage", "ERROR: no"), ("skill_manage", "Saved skill 'x'. It is active.")], {}, ""),
    ([("execute_code", OK)] * 3, {"settings": {"min_tool_calls": 3}}, "tool_calls"),
])
def test_review_trigger(calls, options, expected):
    assert skills_learning.should_review(make_turn(calls, **options)) == expected


def test_a_turn_that_already_saved_a_skill_is_not_reviewed_but_a_bodyless_request_is():
    saved = make_turn([("execute_code", OK)] * 6)
    saved.managed = True
    asked = make_turn([])
    asked.learn_request = {"action": "create", "name": "resize-images", "description": DESCRIPTION}
    assert skills_learning.should_review(saved) == "" and skills_learning.should_review(asked) == "asked"


def test_a_loaded_skill_that_still_hit_an_error_is_reviewed():
    turn = make_turn([("execute_code", FAILED)])
    turn.viewed["resize-images"] = BODY
    assert skills_learning.should_review(turn) == "skill_failed"


def test_a_loaded_skill_that_still_took_real_work_is_reviewed():
    """A quiet failure (exit 0, "0 tables") shows no error, so the amount of work after loading is the signal."""
    worked = make_turn([("skill_view", "## SKILL resize-images")] + [("execute_code", OK)] * 3)
    worked.viewed["resize-images"] = BODY
    smooth = make_turn([("skill_view", "## SKILL resize-images")] + [("execute_code", OK)] * 2)
    smooth.viewed["resize-images"] = BODY
    assert skills_learning.should_review(worked) == "skill_used" and skills_learning.should_review(smooth) == ""


def test_trace_is_numbered_marks_errors_and_keeps_the_ending_when_long():
    turn = make_turn([("execute_code", FAILED), ("execute_code", OK)])
    trace = skills_learning.build_trace(turn)
    assert trace.splitlines()[0].startswith("1. execute_code(") and "-> ERROR: **FAILED**" in trace
    assert trace.splitlines()[1].startswith("2. execute_code(") and "-> OK: **SUCCESS**" in trace
    long = make_turn([("execute_code", OK)] * 40)
    cut = skills_learning.build_trace(long, limit=1200)
    assert len(cut) < 1300 and "[... steps omitted ...]" in cut and cut.rstrip().splitlines()[-1].startswith("40. ")


# ── Reading the model's answer ──

@pytest.mark.parametrize("raw", ["", "not json", '{"action":"none"}', '{"action":"delete","name":"x","body":"%s"}' % ("x" * 60),
                                 '{"action":"create","name":"resize","body":"short"}', "[1,2,3]",
                                 '{"action":"create","name":"resize","body":"' + "x" * 30])
def test_unusable_answers_mean_nothing_is_saved(raw):
    assert skills_learning.parse_decision(raw) is None


@pytest.mark.parametrize("raw, said", [
    ("", "empty reply"),
    ("not json", "unreadable reply: not json"),
    ("[1,2,3]", "unreadable reply: [1,2,3]"),
    ('{"action":"none","reason":"a one-off lookup"}', "a one-off lookup"),
    ('{"action":"none"}', "no reason given"),
    ('{"action":"create","name":"resize","body":"short"}', "reply had no usable name or body"),
])
def test_the_log_says_why_nothing_was_kept(raw, said):
    # Live 2026-11-13: a 7-call turn logged only "nothing to keep", which hid that the model had judged it too obvious.
    assert skills_learning.declined(raw) == said


def test_common_knowledge_is_not_a_reason_to_keep_nothing():
    # Same run: "standard PIL operations ... common knowledge" was the model's reason on 2 of 3 replays of the old wording.
    assert "common knowledge" in skills_learning.PROMPT and "Most turns teach nothing" not in skills_learning.PROMPT


def test_a_usable_answer_is_normalised():
    decision = skills_learning.parse_decision("Sure:\n" + json.dumps({**CREATE, "mode": "whatever", "script_step": "2"}) + "\nDone")
    assert (decision["action"], decision["name"], decision["mode"], decision["script_step"]) == ("create", "batch-resize-images", "append", 2)


def test_a_duplicate_becomes_a_patch_only_when_the_reviewer_saw_the_skill():
    rows = [{"name": "resize-images", "description": DESCRIPTION, "body": BODY}]
    decision = skills_learning.parse_decision(json.dumps(CREATE))
    assert skills_learning.settle(decision, rows, {}) is None
    patched = skills_learning.settle(decision, rows, {"resize-images": BODY})
    assert (patched["action"], patched["name"], patched["mode"], patched["description"]) == ("patch", "resize-images", "append", "")
    blind = {**decision, "action": "patch", "name": "resize-images"}
    assert skills_learning.settle(blind, rows, {}) is None
    assert skills_learning.settle({**decision, "name": "parse-invoices", "description": "Use when reading PDF invoices"}, rows, {}) is not None


# ── Which model ──

def test_model_choice_prefers_the_loaded_chat_model_and_never_inherits_a_cloud_one(monkeypatch):
    import config
    monkeypatch.setattr(config, "WORKSPACE_MODEL", "small-local")
    monkeypatch.setattr(config, "DEFAULT_MODEL", "default-local")
    monkeypatch.setattr(skills_learning.context_policy, "helper_context", lambda profile: 8192)
    assert skills_learning.pick_model(make_turn(model="qwen-chat", num_ctx=65536)) == ("qwen-chat", 65536)   # same window: no reload
    assert skills_learning.pick_model(make_turn(model="anthropic:claude-opus")) == ("small-local", 8192)
    assert skills_learning.pick_model(make_turn(model="auto")) == ("small-local", 8192)
    # Only an explicit Skills model may be a cloud model.
    assert skills_learning.pick_model(make_turn(model="qwen-chat", settings={"model": "openai:gpt-5"})) == ("openai:gpt-5", 8192)
    monkeypatch.setattr(config, "WORKSPACE_MODEL", "openai:gpt-5")
    assert skills_learning.pick_model(make_turn(model="anthropic:claude-opus")) == ("default-local", 8192)


# ── The whole review ──

def test_review_creates_an_active_skill_with_the_working_script(isolated, monkeypatch):
    seen, events = [], Events()
    reply(monkeypatch, {**CREATE, "script_step": 5}, seen)
    turn = make_turn([("execute_code", FAILED)] + [("execute_code", OK)] * 4, query="resize the photos in this folder")
    saved = asyncio.run(skills_learning.review_turn(turn, None, events, "resize the photos in this folder", "Done: 12 files resized."))
    skill = asyncio.run(skill_store.get_by_name("batch-resize-images"))
    files = asyncio.run(skill_store.files(skill["id"], content=True))
    assert saved["state"] == "active" and (skill["status"], skill["origin"], skill["category"]) == ("active", "reviewer", "media")
    assert files == [{"path": "scripts/batch-resize-images.py", "content": "from PIL import Image\nprint('resized')\n",
                      "updated_at": files[0]["updated_at"]}]
    assert events.emitted == [("tool_done", {"tool": "skill_manage", "icon": "star", "status": "Learned skill: batch-resize-images"})]
    call = seen[0]
    assert (call["model"], call["num_ctx"], call["format_json"]) == ("qwen-chat", 32768, True)
    assert "TOOL TRACE:\n1. execute_code(" in call["prompt"] and "resize the photos in this folder" in call["prompt"]
    assert "Done: 12 files resized." in call["prompt"] and "EXISTING SKILLS:\n(none yet)" in call["prompt"]


def test_review_never_attaches_code_from_a_failed_step(isolated, monkeypatch):
    reply(monkeypatch, {**CREATE, "script_step": 1})
    turn = make_turn([("execute_code", FAILED)] + [("execute_code", OK)] * 4)
    asyncio.run(skills_learning.review_turn(turn, None, Events(), "resize", "done"))
    skill = asyncio.run(skill_store.get_by_name("batch-resize-images"))
    assert asyncio.run(skill_store.files(skill["id"])) == []


def test_review_patches_the_skill_that_was_loaded_and_keeps_history(isolated, monkeypatch):
    asyncio.run(skill_store.create("resize-images", DESCRIPTION, BODY))
    seen = []
    reply(monkeypatch, {"action": "patch", "name": "resize-images", "description": "", "mode": "append",
                        "body": "## Pitfalls\n- Pillow is not installed by default: pip install pillow first.",
                        "reason": "the import failed"}, seen)
    turn = make_turn([("execute_code", FAILED), ("execute_code", OK)], rows=asyncio.run(skill_store.list_skills()))
    turn.viewed["resize-images"] = BODY
    saved = asyncio.run(skills_learning.review_turn(turn, None, Events(), "resize these", "done"))
    skill = asyncio.run(skill_store.get_by_name("resize-images"))
    assert saved["state"] == "active" and skill["version"] == 2 and skill["body"].startswith(BODY.strip())
    assert "pip install pillow" in skill["body"] and skill["description"] == DESCRIPTION
    assert skill["body"].count("## Pitfalls") == 1
    assert "LOADED SKILLS:\n### resize-images\n" in seen[0]["prompt"]


def test_an_appended_patch_keeps_the_description_and_a_rewrite_may_change_it(isolated, monkeypatch):
    asyncio.run(skill_store.create("resize-images", DESCRIPTION, BODY))
    answer = {"action": "patch", "name": "resize-images", "description": "Use when photos of any size need shrinking for the web",
              "mode": "append", "body": "## Pitfalls\n- Very large files need the streaming mode to avoid running out of memory."}
    for mode, expected in (("append", DESCRIPTION), ("rewrite", answer["description"])):
        reply(monkeypatch, {**answer, "mode": mode, "body": answer["body"] + f"\n- Checked in {mode} mode as well, twice."})
        turn = make_turn([("execute_code", FAILED), ("execute_code", OK)])
        turn.viewed["resize-images"] = BODY
        asyncio.run(skills_learning.review_turn(turn, None, Events(), "resize these", "done"))
        assert asyncio.run(skill_store.get_by_name("resize-images"))["description"] == expected


def test_review_in_a_guarded_context_only_drafts_and_parks_changes(isolated, monkeypatch):
    reply(monkeypatch, CREATE)
    turn = make_turn([("execute_code", OK)] * 5, headless=True)
    saved = asyncio.run(skills_learning.review_turn(turn, None, Events(), "resize", "done"))
    assert saved["state"] == "draft" and asyncio.run(skill_store.get_by_name("batch-resize-images"))["status"] == "draft"

    asyncio.run(skill_store.create("parse-invoices", "Use when extracting totals from PDF invoices", BODY))
    reply(monkeypatch, {"action": "patch", "name": "parse-invoices", "mode": "append", "script_step": 1,
                        "body": "## Pitfalls\n- Scanned invoices need OCR before the totals can be read."})
    mail = make_turn([("execute_code", OK)] * 5, taint="email")
    mail.viewed["parse-invoices"] = BODY
    parked = asyncio.run(skills_learning.review_turn(mail, None, Events(), "invoices", "done"))
    skill = asyncio.run(skill_store.get_by_name("parse-invoices"))
    assert parked["state"] == "pending" and skill["body"] == BODY and "OCR" in skill["pending"]["body"]
    assert asyncio.run(skill_store.files(skill["id"])) == []        # no script on a change that is only parked


@pytest.mark.parametrize("answer", ['{"action":"none"}', "garbage", "", RuntimeError("ollama down")])
def test_review_fails_silent_and_saves_nothing(isolated, monkeypatch, answer):
    reply(monkeypatch, answer)
    turn = make_turn([("execute_code", OK)] * 5)
    assert asyncio.run(skills_learning.review_turn(turn, None, Events(), "resize", "done")) is None
    assert asyncio.run(skill_store.list_skills()) == []


def test_review_gives_up_when_the_model_is_too_slow(isolated, monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(5)
        return json.dumps(CREATE)
    monkeypatch.setattr(skills_learning.model_providers, "complete_chat", slow)
    monkeypatch.setattr(skills_learning, "REVIEW_SECONDS", 0.05)
    turn = make_turn([("execute_code", OK)] * 5)
    assert asyncio.run(skills_learning.review_turn(turn, None, Events(), "resize", "done")) is None
    assert asyncio.run(skill_store.list_skills()) == []


def test_a_new_message_cancels_the_pending_review(isolated, monkeypatch):
    started = []

    async def slow(*args, **kwargs):
        started.append(True)
        await asyncio.sleep(5)
        return json.dumps(CREATE)
    monkeypatch.setattr(skills_learning.model_providers, "complete_chat", slow)

    async def scenario():
        turn = make_turn_async = skills.SkillTurn("conv-1", "default", dict(skills.DEFAULTS), [], model="qwen-chat", num_ctx=8192)
        for _ in range(5):
            await turn.record("execute_code", {"code": "print(1)"}, OK)
        task = asyncio.create_task(turn.review(None, Events(), "resize", "done"))
        await asyncio.sleep(0.05)
        assert "default" in skills_learning._PENDING and started
        from types import SimpleNamespace
        await skills.begin_turn(SimpleNamespace(model="qwen-chat", user_metadata=None), "conv-2")   # the user's next message
        await asyncio.wait_for(task, 1)
        return await skill_store.list_skills()
    assert asyncio.run(scenario()) == [] and "default" not in skills_learning._PENDING


def test_the_review_runs_once_per_turn_however_often_it_is_started(isolated, monkeypatch):
    """chat.py starts it at the normal end of a turn and again from the stream wrapper, for turns that left early."""
    seen = []
    reply(monkeypatch, CREATE, seen)
    turn = make_turn([("execute_code", OK)] * 5)

    async def twice():
        await turn.review(None, Events(), "resize", "done")
        await turn.review(None, Events(), "resize", "")
    asyncio.run(twice())
    assert len(seen) == 1 and turn.reviewed


def test_a_request_for_a_skill_that_already_exists_becomes_a_patch(isolated, monkeypatch):
    """The model asked to "create" a name that exists: the reviewer is shown that skill, so it can extend it."""
    asyncio.run(skill_store.create("resize-images", DESCRIPTION, BODY))
    seen = []
    reply(monkeypatch, {**CREATE, "name": "resize-images", "body": "## Pitfalls\n- Keep the aspect ratio when only one side is given."}, seen)
    turn = make_turn([], rows=asyncio.run(skill_store.list_skills()))
    turn.learn_request = {"action": "create", "name": "resize-images", "description": DESCRIPTION, "note": ""}
    saved = asyncio.run(skills_learning.review_turn(turn, None, Events(), "save it as a skill", ""))
    skill = asyncio.run(skill_store.get_by_name("resize-images"))
    assert saved["state"] == "active" and skill["version"] == 2 and "aspect ratio" in skill["body"]
    assert "LOADED SKILLS:\n### resize-images\n" in seen[0]["prompt"]


def test_a_bodyless_request_is_handed_to_the_reviewer(isolated, monkeypatch):
    seen = []
    reply(monkeypatch, {**CREATE, "name": "resize-images"}, seen)
    turn = make_turn([("execute_code", OK)])
    turn.learn_request = {"action": "create", "name": "resize-images", "description": DESCRIPTION}
    saved = asyncio.run(skills_learning.review_turn(turn, None, Events(), "resize", "done"))
    assert saved["skill"]["name"] == "resize-images"
    assert 'The assistant asked to create the skill "resize-images"' in seen[0]["prompt"]


def test_an_explicit_ask_gives_the_reviewer_the_earlier_conversation(isolated, monkeypatch):
    seen = []
    reply(monkeypatch, {**CREATE, "name": "markdown-to-pdf"}, seen)
    turn = make_turn([], query="Remember how to do this next time.",
                     recent=[("user", "turn report.md into a pdf"), ("assistant", "Ran pandoc with --pdf-engine=xelatex after installing texlive-xetex.")])
    saved = asyncio.run(skills_learning.review_turn(turn, None, Events(), "Remember how to do this next time.", "Will do."))
    assert saved["skill"]["name"] == "markdown-to-pdf"
    prompt = seen[0]["prompt"]
    assert "The user asked for this to be kept as a skill." in prompt
    assert "EARLIER IN THIS CONVERSATION:\nuser: turn report.md into a pdf\nassistant: Ran pandoc" in prompt
    assert prompt.index("EARLIER IN THIS CONVERSATION") < prompt.index("USER REQUEST:")


def test_the_prompt_shrinks_for_a_small_helper_window():
    rows = [{"name": f"skill-{n:02d}", "description": "Use when doing thing number %02d for the team" % n, "category": "general",
             "status": "active", "pinned": False, "body": "x" * 4000} for n in range(40)]
    turn = make_turn([("execute_code", OK)] * 30)
    shown = {"skill-00": "y" * 4000}
    small = skills_learning.build_prompt(turn, rows, shown, "r" * 5000, "a" * 5000, 8192)
    large = skills_learning.build_prompt(turn, rows, shown, "r" * 5000, "a" * 5000, 32768)
    assert len(small) < len(large) and len(small) < 11000 and "y" * 1500 in small and "y" * 1501 not in small


# ── Upkeep ──

def test_curator_runs_per_owner_with_their_own_settings(isolated):
    from datetime import datetime, timedelta

    async def scenario():
        for user in ("alice", "bob"):
            token = db.set_current_user_id(user)
            try:
                await skill_store.create("resize-images", DESCRIPTION, BODY, origin="reviewer")
            finally:
                db.reset_current_user_id(token)
        await skills.save_settings({"stale_days": 100, "archive_days": 200}, "bob")
        conn = await db.get_db()
        try:
            await conn.execute("UPDATE skills SET created_at=?", ((datetime.utcnow() - timedelta(days=45)).isoformat(),))
            await conn.commit()
        finally:
            await conn.close()
        preview = await skills_learning.curate_all(dry_run=True)
        moved = await skills_learning.curate_all()
        return preview, moved, db.current_user_id()
    preview, moved, user_after = asyncio.run(scenario())
    assert set(preview) == {"alice"} and preview == moved            # bob keeps skills for 100 days
    assert moved["alice"][0]["to"] == "stale" and user_after == "default"   # the contextvar is put back
