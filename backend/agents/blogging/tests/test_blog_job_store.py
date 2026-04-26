"""Unit tests for the blog job store. Backed by an in-memory FakeJobServiceClient
(see backend/conftest.py) — no Postgres or live job service required.
"""

import uuid
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _patched_blog_client(monkeypatch, fake_job_client):
    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    return fake_job_client


def test_mark_all_running_jobs_failed(tmp_path: Path) -> None:
    """mark_all_running_jobs_failed sets all running/pending blog jobs to interrupted with reason."""
    from shared.blog_job_store import (
        create_blog_job,
        get_blog_job,
        mark_all_running_jobs_failed,
        start_blog_job,
    )

    cache_dir = tmp_path
    job_id = str(uuid.uuid4())
    create_blog_job(job_id, "Test brief", cache_dir=cache_dir)
    start_blog_job(job_id, cache_dir=cache_dir)

    mark_all_running_jobs_failed("test", cache_dir=cache_dir)

    job_data = get_blog_job(job_id, cache_dir=cache_dir)
    assert job_data is not None
    assert job_data.get("status") == "interrupted"
    assert job_data.get("error") == "test"


def test_delete_blog_job(tmp_path: Path) -> None:
    """delete_blog_job removes the job and returns True; returns False if not found."""
    from shared.blog_job_store import create_blog_job, delete_blog_job, get_blog_job

    cache_dir = tmp_path
    job_id = str(uuid.uuid4())[:8]
    create_blog_job(job_id, "Brief", cache_dir=cache_dir)
    assert get_blog_job(job_id, cache_dir=cache_dir) is not None

    ok = delete_blog_job(job_id, cache_dir=cache_dir)
    assert ok is True
    assert get_blog_job(job_id, cache_dir=cache_dir) is None

    assert delete_blog_job("nonexistent", cache_dir=cache_dir) is False
