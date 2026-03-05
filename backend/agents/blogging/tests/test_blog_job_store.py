"""Tests for blog job store."""

import uuid
from pathlib import Path

import pytest


def test_mark_all_running_jobs_failed(tmp_path: Path) -> None:
    """mark_all_running_jobs_failed sets all running/pending blog jobs to failed with reason."""
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
    assert job_data.get("status") == "failed"
    assert job_data.get("error") == "test"
