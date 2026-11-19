"""An image job that ComfyUI no longer knows about.

ComfyUI keeps its queue and history in memory. If it restarts (or crashes) after a job was submitted,
the job is in neither, and the poll used to answer "queued" for ever: the page kept its spinner and the
monitor loop asked again every three seconds without end.

A job is briefly in neither place at the moment it leaves the queue and before its history is written,
so one miss means nothing. `LOST_AFTER` misses in a row, each from a queue answer that was actually
read (an unreachable ComfyUI is not a miss), do.
"""
LOST_AFTER = 5
LOST_MESSAGE = "ComfyUI no longer has this job (it was probably restarted). Generate the image again."


def note(job: dict, in_queue: bool | None) -> bool:
    """Record one poll that found no result. `in_queue`: True / False as ComfyUI answered, None when the
    queue could not be read. Returns True when the job should be given up as lost."""
    if in_queue is None:
        return False
    if in_queue:
        job.pop("missing_polls", None)
        return False
    job["missing_polls"] = int(job.get("missing_polls") or 0) + 1
    return job["missing_polls"] >= LOST_AFTER
