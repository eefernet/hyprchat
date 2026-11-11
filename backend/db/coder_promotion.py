"""Project versions for Daedalus jobs: the working pointer, the promotion log and user Accept.

A coding project keeps two pointers. The **working** pointer names the latest delivered revision
(review candidates included) and is where a follow-up edit starts. The **accepted** pointer names the
latest promoted revision and only moves on a promotion: the owner's Accept, or every confirmed acceptance
example passing. Every promotion is appended to ``coding_project_promotions`` so an earlier accepted version
stays listed and downloadable. An automatic promotion comes only from a job whose source fence still holds, so
it cannot jump over a version the owner accepted after that job started.

Fences: a job records which pointer it came from (``source_pointer``), the revision it read
(``source_revision_id``) and the working pointer as it was at creation (``working_*_at_start``); creation
refuses a pointer that moved while the request was starting. After that, every delivery moves the working
pointer, so resuming or delivering is refused once working moved to anything but this job's own delivery
or source. Which job was created first says nothing about when it delivered. A user Accept never moves the
working pointer, so it never strands a follow-up that is still running.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path

import config
import database as db

POINTER_COLUMNS = {"working_job_id": "TEXT DEFAULT ''", "working_revision_id": "TEXT DEFAULT ''",
                   "working_artifact_id": "TEXT DEFAULT ''"}
KINDS = ("user", "examples", "controller")
REVIEW_PREFIX = "DAEDALUS_REVIEW_"
# openhands_project_id takes a job id unless it holds an upload's id, which resolve_source still seeds from.
JOB_OR_UPLOAD = ("CASE WHEN COALESCE(openhands_project_id,'')='' OR substr(openhands_project_id,1,4)='cw3-' "
                 "THEN ? ELSE openhands_project_id END")
BACKFILL_VERSION = 2   # PRAGMA user_version step; database.init_db's step 1 runs first


def accepted_name(metadata):
    """The candidate's readable name without its review suffix; None when the candidate had none."""
    from coder_naming import download_name
    try:
        return download_name(metadata["download_base"], "accepted", metadata.get("download_version") or 1)
    except (KeyError, TypeError, ValueError):
        return None


async def migrate(connection):
    """Additive: pointer columns and promotion log, both backfilled from existing jobs (the pointers only once)."""
    existing = {r[1] for r in await connection.execute_fetchall("PRAGMA table_info(coding_projects)")}
    from db.coder_download_names import COLUMN
    for name, definition in {**POINTER_COLUMNS, **COLUMN}.items():
        if name not in existing:
            await connection.execute(f"ALTER TABLE coding_projects ADD COLUMN {name} {definition}")
    await connection.executescript("""
        CREATE TABLE IF NOT EXISTS coding_project_promotions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL, user_id TEXT NOT NULL,
            revision_id TEXT NOT NULL, job_id TEXT NOT NULL, artifact_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS coding_project_promotions_project ON coding_project_promotions(project_id, id);
    """)
    projects = await connection.execute_fetchall(
        "SELECT id,user_id,accepted_job_id,accepted_revision_id,accepted_artifact_id FROM coding_projects")
    for project in projects:
        # Recorded before the backfill below clears an accepted pointer whose job is gone, so the loss stays reported.
        if project["accepted_revision_id"]:
            known = await connection.execute_fetchall(
                "SELECT 1 FROM coding_project_promotions WHERE project_id=? AND revision_id=? LIMIT 1",
                (project["id"], project["accepted_revision_id"]))
            if not known:
                await connection.execute(
                    "INSERT INTO coding_project_promotions(project_id,user_id,revision_id,job_id,artifact_id,kind,created_at) "
                    "VALUES(?,?,?,?,?,'controller',datetime('now'))",
                    (project["id"], project["user_id"], project["accepted_revision_id"], project["accepted_job_id"] or "",
                     project["accepted_artifact_id"] or ""))
    # One-time: fill working for jobs from before the pointer and drop pointers to deleted jobs. Run at every startup,
    # this refilled a pointer that deletion had released, under jobs that had snapshotted it empty.
    version = (await connection.execute_fetchall("PRAGMA user_version"))[0][0]
    if version == BACKFILL_VERSION - 1:
        for project in projects:
            await repoint(connection, project["user_id"], project["id"], fill=True)
        await connection.execute(f"PRAGMA user_version={BACKFILL_VERSION}")


async def _latest_delivery(connection, project_id, leaving="", after=""):
    """(job, revision, artifact, delivered_at) of a project's newest delivery, excluding jobs of conversation ``leaving``
    and, given a promotion time ``after``, deliveries not made after it.

    Read from the delivery events, not updated_at: every save moves that, so accepting an older candidate made its job
    look like the newest delivery. Stopped and parked jobs keep their deliveries too. Promotion times keep whole seconds
    (events keep microseconds), so a delivery in the promotion's own second counts as before it: that errs toward
    reporting a lost accepted version rather than rebasing past it.
    """
    rows = await connection.execute_fetchall(
        "SELECT w.id,w.job_json,e.payload,e.created_at FROM coder_workflow_events e JOIN coder_workflows w ON w.id=e.workflow_id "
        "WHERE w.project_id=? AND w.workflow_version=3 AND (?='' OR w.conversation_id<>?) "
        "AND (?='' OR julianday(e.created_at) >= julianday(?,'+1 second')) "
        "AND json_extract(e.payload,'$.type') IN ('candidate_delivered','delivered') ORDER BY e.created_at DESC,e.seq DESC",
        (project_id, leaving, leaving, after, after))
    for row in rows:
        data = json.loads(row["payload"]).get("data") or {}
        artifact = data.get("candidate_artifact") or data.get("artifact") or {}
        revision = (artifact.get("metadata") or {}).get("revision_id") or json.loads(row["job_json"] or "{}").get("revision_id") or ""
        if artifact.get("id") and revision:
            return row["id"], revision, artifact["id"], row["created_at"]
    return None


async def repoint(connection, user_id, project_id, leaving="", fill=False):
    """Re-point project pointers that name jobs which are gone, or leaving with conversation ``leaving`` (its rows
    still exist inside the delete transaction); ``fill`` also fills an empty working pointer. Returns whether it changed.

    Deletion, job creation and the startup backfill apply one rule: working names the newest surviving delivery.
    Blanking it while another conversation's delivery survived refused the next edit, and the backfill later refilled
    it under jobs started meanwhile. Accepted is cleared, not moved back to an older promotion: the log keeps the
    record and resolve_source reports the loss. Nor does working move back to a delivery from before the newest
    promotion once that promotion's job is gone: such a delivery lacks the accepted fix, and an edit seeded from it
    dropped that fix silently. With nothing newer, working stays empty so resolve_source reports it instead. A
    surviving job that started after the new head was delivered gets that head as its creation snapshot, so its
    fence still refuses only work delivered after it started.
    """
    rows = await connection.execute_fetchall(
        "SELECT working_job_id,working_revision_id,working_artifact_id,accepted_job_id FROM coding_projects WHERE id=? AND user_id=?",
        (project_id, user_id))
    if not rows:
        return False
    project = rows[0]
    async def gone(job_id):
        if not job_id:
            return False
        found = await connection.execute_fetchall("SELECT conversation_id FROM coder_workflows WHERE id=?", (job_id,))
        return not found or bool(leaving) and found[0]["conversation_id"] == leaving
    changed = await gone(project["accepted_job_id"])
    if changed:
        await connection.execute("UPDATE coding_projects SET accepted_job_id='',accepted_revision_id='',accepted_artifact_id='' "
                                 "WHERE id=? AND user_id=?", (project_id, user_id))
    if not (await gone(project["working_job_id"]) or fill and not project["working_job_id"]):
        return changed
    newest = await connection.execute_fetchall(
        "SELECT job_id,created_at FROM coding_project_promotions WHERE project_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
        (project_id, user_id))
    after = newest[0]["created_at"] if newest and await gone(newest[0]["job_id"]) else ""
    latest = await _latest_delivery(connection, project_id, leaving, after)
    head = latest[:3] if latest else ("", "", "")
    if head == tuple(project[c] or "" for c in ("working_job_id", "working_revision_id", "working_artifact_id")):
        return changed
    await connection.execute("UPDATE coding_projects SET working_job_id=?,working_revision_id=?,working_artifact_id=? "
                             "WHERE id=? AND user_id=?", (*head, project_id, user_id))
    delivered = latest[3] if latest else ""
    jobs = await connection.execute_fetchall(
        "SELECT id,created_at,job_json FROM coder_workflows WHERE project_id=? AND workflow_version=3 AND (?='' OR conversation_id<>?)",
        (project_id, leaving, leaving))
    for job in jobs:
        payload = json.loads(job["job_json"] or "{}")
        if "working_job_at_start" not in payload or delivered >= (job["created_at"] or "") or (
                payload["working_job_at_start"], payload.get("working_revision_at_start")) == head[:2]:
            continue
        payload.update(working_job_at_start=head[0], working_revision_at_start=head[1])
        await connection.execute("UPDATE coder_workflows SET job_json=? WHERE id=?", (json.dumps(payload), job["id"]))
    return True


async def accepted_job_deleted(project_id):
    """Whether the newest promotion of a project names a job that was deleted with its conversation."""
    connection = await db.get_db()
    try:
        rows = await connection.execute_fetchall(
            "SELECT p.job_id,w.id AS live FROM coding_project_promotions p LEFT JOIN coder_workflows w ON w.id=p.job_id "
            "WHERE p.project_id=? AND p.user_id=? ORDER BY p.id DESC LIMIT 1", (project_id, db.current_user_id()))
        return bool(rows) and bool(rows[0]["job_id"]) and not rows[0]["live"]
    finally:
        await connection.close()


def pointer_column(source_pointer):
    """The coding_projects column prefix a source pointer is fenced on. An uploaded project has no
    delivered revision yet: its first job is stale only once another job has moved the working pointer."""
    return "accepted" if source_pointer == "accepted" else "working"


def pointer_of(job):
    """Which project pointer a job's source fence compares against. Jobs from before the working
    pointer existed were fenced on the accepted head; new builds and follow-ups use working."""
    return pointer_column(job.get("source_pointer") or "accepted")


async def assert_fresh(connection, project_id, workflow_id, job, message):
    """Refuse to resume or deliver once the working pointer moved to work this job did not start from.

    Allowed: working names nothing or this job, holds this job's source revision, or is unchanged since this
    job was created (an accepted-pointer follow-up while working still names an older delivery). Creation
    order is no test: an older job continued and delivered after a newer one started, and the newer one then
    rewound working (and, completing automatically, accepted) to its stale source.
    """
    rows = await connection.execute_fetchall(
        "SELECT working_job_id,working_revision_id FROM coding_projects WHERE id=? AND user_id=?", (project_id, db.current_user_id()))
    if not rows:
        raise ValueError(message)
    owner, head = rows[0]["working_job_id"] or "", rows[0]["working_revision_id"] or ""
    if not owner or owner == workflow_id or (head and head == (job.get("source_revision_id") or "")):
        return
    if "working_job_at_start" in job:
        if (owner, head) != (job["working_job_at_start"] or "", job.get("working_revision_at_start") or ""):
            raise ValueError(message)
        return
    # Jobs from before the snapshot: only a working version delivered before this job started is its past.
    delivered_before = await connection.execute_fetchall(
        "SELECT 1 FROM coding_projects p JOIN artifacts a ON a.id=p.working_artifact_id AND a.user_id=p.user_id "
        "JOIN coder_workflows self ON self.id=? WHERE p.id=? AND p.user_id=? AND a.created_at < self.created_at",
        (workflow_id, project_id, db.current_user_id()))
    if not delivered_before:
        raise ValueError(message)


async def user_promoted_since(connection, project_id, job):
    """Whether the owner accepted a version after a job started: an automatic promotion must not replace it.

    Compared by log id against the baseline recorded at creation: promotion times keep whole seconds, so an
    Accept earlier in the same second as the job's creation counted as after it. Jobs from before the baseline
    keep the time test, which errs toward keeping the owner's choice.
    """
    if "promotion_baseline_id" in job:
        clause, value = "id > ?", int(job["promotion_baseline_id"] or 0)
    else:
        clause, value = "created_at >= datetime(?)", job.get("created_at") or ""
    rows = await connection.execute_fetchall(
        f"SELECT 1 FROM coding_project_promotions WHERE project_id=? AND user_id=? AND kind='user' AND {clause} LIMIT 1",
        (project_id, db.current_user_id(), value))
    return bool(rows)


async def advance_working(connection, project_id, workflow_id, revision_id, artifact_id):
    await connection.execute(
        "UPDATE coding_projects SET working_job_id=?,working_revision_id=?,working_artifact_id=?,updated_at=datetime('now') "
        "WHERE id=? AND user_id=?", (workflow_id, revision_id, artifact_id, project_id, db.current_user_id()))


async def record_promotion(connection, project_id, workflow_id, revision_id, artifact_id, kind):
    if kind not in KINDS:
        raise ValueError("Unknown promotion kind")
    await connection.execute(
        "INSERT INTO coding_project_promotions(project_id,user_id,revision_id,job_id,artifact_id,kind,created_at) "
        "VALUES(?,?,?,?,?,?,datetime('now'))",
        (project_id, db.current_user_id(), revision_id, workflow_id, artifact_id, kind))


async def promotions(project_id):
    connection = await db.get_db()
    try:
        rows = await connection.execute_fetchall(
            "SELECT revision_id,job_id,artifact_id,kind,created_at FROM coding_project_promotions "
            "WHERE project_id=? AND user_id=? ORDER BY id DESC", (project_id, db.current_user_id()))
        return [dict(r) for r in rows]
    finally:
        await connection.close()


async def pinned_artifact_ids(connection=None):
    """Artifacts that a project pointer or the promotion log references; deletion must refuse them."""
    owned = connection is None
    connection = connection or await db.get_db()
    try:
        rows = await connection.execute_fetchall(
            "SELECT working_artifact_id AS id FROM coding_projects WHERE user_id=? "
            "UNION SELECT accepted_artifact_id FROM coding_projects WHERE user_id=? "
            "UNION SELECT artifact_id FROM coding_project_promotions WHERE user_id=?",
            (db.current_user_id(),) * 3)
        return {r["id"] for r in rows if r["id"]}
    finally:
        if owned:
            await connection.close()


def candidate_for(job, revision_id):
    """The delivered artifact of a job for one exact revision (accepted, current or earlier candidate)."""
    for artifact in [job.get("artifact"), job.get("candidate_artifact"), *(job.get("candidate_history") or [])]:
        if artifact and (artifact.get("metadata") or {}).get("revision_id") == revision_id:
            return artifact
    return None


def empty_candidate(job, revision_id, archive):
    """A delivered revision with nothing to accept: a new build's own starting tree (live cw3-59371f48: a rollback onto
    the empty baseline offered Accept on an archive holding only the review report), or an archive without project files."""
    if job.get("mode") == "build_from_prompt" and revision_id == job.get("baseline_revision"):
        return True
    try:
        with tarfile.open(archive) as members:
            return not any(member.isfile() and not Path(member.name).name.startswith(REVIEW_PREFIX) for member in members)
    except (OSError, tarfile.TarError):
        return False   # an unreadable archive fails later with its own message


def _strip_review(source, target):
    """Copy a candidate archive without its 'Review candidate; not accepted' report file."""
    with tarfile.open(source) as archive, tarfile.open(target, "w:gz") as output:
        for member in archive:
            if Path(member.name).name.startswith(REVIEW_PREFIX) and "/" not in member.name.strip("./"):
                continue
            output.addfile(member, archive.extractfile(member) if member.isfile() else None)
    digest = hashlib.sha256()
    with open(target, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def accept(project_id, revision_id):
    """The owner promotes one exact delivered revision of a project to its accepted version."""
    from db import coder_jobs as store
    project = await db.get_coding_project(project_id)
    if not project:
        raise LookupError("Project not found")
    history = await store.project_history(project_id)
    job = next((j for j in history if candidate_for(j, revision_id)), None)
    if not job:
        raise LookupError("No delivered build of this project has that revision")
    finished = {"ready_for_review", "completed", "blocked", "cancelled"}
    if job["state"] not in finished:
        raise ValueError("Wait for the job to finish before accepting it")
    if job.get('engine') != 'openhands159':
        raise ValueError('This coding engine is retired. Its recorded history remains read-only; upload source into a new project.')
    source = candidate_for(job, revision_id)
    if await asyncio.to_thread(empty_candidate, job, revision_id, Path(source.get("storage_path") or "")):
        raise ValueError("This version has no project files to accept (it is the build's empty starting tree)")
    artifact_id = "artifact-" + hashlib.sha256(f"accepted:{job['id']}:{revision_id}".encode()).hexdigest()[:24]
    # Accepting a version again (restoring it after another was accepted) reuses its accepted archive.
    previous = await db.get_artifact(artifact_id)
    accepted_path = None
    if not previous:
        path = Path(source.get("storage_path") or "")
        if not path.is_file():
            raise ValueError("The archive for this revision is no longer on disk; it cannot be accepted")
        # Staged like every delivery; add_artifact publishes its own content-addressed durable copy.
        accepted_path = Path(config.SANDBOX_OUTPUTS_DIR) / ("accepted-" + path.name.removeprefix("candidate-"))
        accepted_path.parent.mkdir(parents=True, exist_ok=True)
        sha256 = await asyncio.to_thread(_strip_review, path, accepted_path)
    connection = await db.get_db()
    try:
        await connection.execute("BEGIN IMMEDIATE")
        current = await connection.execute_fetchall(
            "SELECT accepted_revision_id,working_revision_id FROM coding_projects WHERE id=? AND user_id=?",
            (project_id, db.current_user_id()))
        if not current:
            raise LookupError("Project not found")
        if current[0]["accepted_revision_id"] == revision_id:
            await connection.rollback()
            if accepted_path:
                accepted_path.unlink(missing_ok=True)
            return {"project_id": project_id, "revision_id": revision_id, "already_accepted": True}
        # The state read above may be stale: a Continue in another tab can have resumed the job since. An
        # in-flight Stop is 'cancelling'; cancel_requested stays 1 after a finished Stop, so it is no test here.
        fresh = (await connection.execute_fetchall(
            "SELECT state,job_json FROM coder_workflows WHERE id=?", (job["id"],)))[0]
        if fresh["state"] not in finished:
            raise ValueError("Wait for the job to finish before accepting it")
        if previous:
            artifact = previous
        else:
            readable = accepted_name(source.get("metadata") or {})
            artifact = await db.add_artifact(_connection=connection, conversation_id=job["conversation_id"], workflow_id=job["id"],
                artifact_id=artifact_id, filename=accepted_path.name, url=f"/api/downloads/{accepted_path.name}", kind="archive",
                mime_type="application/gzip", storage_path=str(accepted_path), size_bytes=accepted_path.stat().st_size,
                sha256=sha256, exists_status="present", status="accepted", **({"title": readable} if readable else {}),
                metadata={**{k: v for k, v in (source.get("metadata") or {}).items() if k != "download_name"},
                          **({"download_name": readable} if readable else {}), "revision_id": revision_id, "accepted_by": "user",
                          "delivery_status": "accepted", "source_artifact_id": source.get("id", "")})
            if not artifact:
                raise ValueError("Artifact registration failed")
        # Overwriting an upload's id left nothing to seed from once this job went with its conversation.
        await connection.execute(
            "UPDATE coding_projects SET accepted_job_id=?,accepted_revision_id=?,accepted_artifact_id=?,"
            f"openhands_project_id={JOB_OR_UPLOAD},updated_at=datetime('now') WHERE id=? AND user_id=?",
            (job["id"], revision_id, artifact["id"], job["id"], project_id, db.current_user_id()))
        await record_promotion(connection, project_id, job["id"], revision_id, artifact["id"], "user")
        payload = json.loads(fresh["job_json"] or "{}")
        if payload.get("revision_id") == revision_id and fresh["state"] != "completed":
            payload.update(artifact=artifact, delivery_status="accepted", accepted_by="user",
                           acceptance={"accepted": True, "revision_id": revision_id, "kind": "user"})
            await connection.execute(
                "UPDATE coder_workflows SET state='completed',job_json=?,artifact_status='delivered',updated_at=datetime('now') WHERE id=?",
                (json.dumps(payload), job["id"]))
            await store._event(connection, job["id"], "accepted_by_user",
                               {"state": "completed", "artifact": artifact, "accepted_by": "user", "artifact_status": "delivered"})
        await connection.commit()
    except BaseException:
        if accepted_path:
            accepted_path.unlink(missing_ok=True)
        raise
    finally:
        await connection.close()
    if accepted_path and artifact.get("storage_path") and Path(artifact["storage_path"]) != accepted_path:
        accepted_path.unlink(missing_ok=True)   # the durable copy is the record now
    return {"project_id": project_id, "revision_id": revision_id, "job_id": job["id"], "artifact": artifact}
