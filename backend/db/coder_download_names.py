"""One fixed download slug per project, and a job's version number within it."""
import json
import database as db

COLUMN = {"download_slug": "TEXT DEFAULT ''"}


async def assign(project_id, job_id, base):
    """Fix the project's slug the first time something is delivered; later requests cannot rename it.

    The version counts earlier jobs of the project that delivered an archive, so read-only questions and
    jobs that produced nothing do not leave gaps.
    """
    if not project_id:
        return base, 1
    connection = await db.get_db()
    try:
        owner = db.current_user_id()
        await connection.execute(
            "UPDATE coding_projects SET download_slug=? WHERE id=? AND user_id=? AND COALESCE(download_slug,'')=''",
            (base, project_id, owner))
        await connection.commit()
        rows = await connection.execute_fetchall(
            "SELECT download_slug FROM coding_projects WHERE id=? AND user_id=?", (project_id, owner))
        slug = (rows[0]["download_slug"] if rows else "") or base
        earlier = 0
        for row in await connection.execute_fetchall(
                "SELECT w.id,w.job_json FROM coder_workflows w JOIN conversations c ON c.id=w.conversation_id "
                "WHERE w.project_id=? AND c.user_id=? AND w.workflow_version=3 ORDER BY w.created_at,w.id",
                (project_id, owner)):
            if row["id"] == job_id: break
            try: payload = json.loads(row["job_json"] or "{}")
            except ValueError: payload = {}
            earlier += bool(payload.get("artifact") or payload.get("candidate_artifact") or payload.get("candidate_history"))
        return slug, earlier + 1
    finally:
        await connection.close()
