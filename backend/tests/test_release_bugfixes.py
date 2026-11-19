"""Smaller fixes of the 2026-11-18 release polish (fourth bug hunt, stage 3b). Offline."""
import asyncio
import sqlite3

import pytest

import database as db
import image_job_lost


def test_an_image_job_comfyui_forgot_is_given_up_after_several_misses():
    job = {"status": "queued"}
    assert [image_job_lost.note(job, False) for _ in range(image_job_lost.LOST_AFTER - 1)] == [False] * (image_job_lost.LOST_AFTER - 1)
    assert image_job_lost.note(job, False) is True


def test_one_miss_between_queue_and_history_is_not_a_lost_job():
    job = {}
    assert image_job_lost.note(job, False) is False
    assert image_job_lost.note(job, True) is False          # seen again: the count starts over
    assert "missing_polls" not in job
    for _ in range(image_job_lost.LOST_AFTER - 1):
        assert image_job_lost.note(job, False) is False


def test_an_unreachable_comfyui_is_not_a_miss():
    job = {"missing_polls": image_job_lost.LOST_AFTER - 1}
    assert image_job_lost.note(job, None) is False
    assert job["missing_polls"] == image_job_lost.LOST_AFTER - 1


def test_the_today_card_counts_today_only():
    from datetime import datetime, timedelta
    from routes import settings as routes
    today = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = [{"date": today, "total_tokens": 10, "request_count": 1}, {"date": yesterday, "total_tokens": 500, "request_count": 9}]
    assert [(r["date"], r["total_tokens"]) for r in routes._agg_by_day(rows, since=routes._usage_today())] == [(today, 10)]
    assert len(routes._agg_by_day(rows, 30)) == 2          # the longer windows are unchanged
