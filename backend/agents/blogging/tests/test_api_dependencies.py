"""Tests for the shared job-guard dependencies in ``agents.blogging.api.dependencies``.

Exercises each dependency directly (no HTTP round-trip needed since no router
wires these in yet) against the in-memory fake store provided by the
``patched_client`` fixture.
"""

from __future__ import annotations

import pytest
from _api_test_utils import api_main as _api_main
from _api_test_utils import create_job as _create_job
from agents.blogging.api.dependencies import (
    get_job,
    get_job_or_404,
    require_job_store,
    require_job_waiting_for,
)
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# require_job_store
# ---------------------------------------------------------------------------


def test_require_job_store_passes_when_helpers_present(patched_client) -> None:
    require_job_store("get_blog_job")()


def test_require_job_store_501_when_helper_missing(patched_client, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "get_blog_job", None)
    with pytest.raises(HTTPException) as exc_info:
        require_job_store("get_blog_job")()
    assert exc_info.value.status_code == 501
    assert exc_info.value.detail == "Job store not available"


def test_require_job_store_custom_detail(patched_client, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "list_blog_jobs", None)
    with pytest.raises(HTTPException) as exc_info:
        require_job_store(
            "list_blog_jobs", detail="Job listing not available - job store module not found"
        )()
    assert exc_info.value.status_code == 501
    assert exc_info.value.detail == "Job listing not available - job store module not found"


def test_require_job_store_checks_all_named_helpers(patched_client, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "submit_title_selection", None)
    with pytest.raises(HTTPException) as exc_info:
        require_job_store("get_blog_job", "submit_title_selection")()
    assert exc_info.value.status_code == 501


# ---------------------------------------------------------------------------
# get_job_or_404
# ---------------------------------------------------------------------------


def test_get_job_or_404_returns_job(patched_client) -> None:
    job_id = _create_job()
    job = get_job_or_404(job_id)
    assert job["job_id"] == job_id


def test_get_job_or_404_raises_404_for_missing_job(patched_client) -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_job_or_404("does-not-exist")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job does-not-exist not found"


# ---------------------------------------------------------------------------
# require_job_waiting_for
# ---------------------------------------------------------------------------


def test_require_job_waiting_for_returns_job_when_flag_set(patched_client) -> None:
    job_id = _create_job(waiting_for_title_selection=True)
    dependency = require_job_waiting_for(
        "waiting_for_title_selection", "Job is not currently waiting for title selection"
    )
    job = dependency(get_job_or_404(job_id))
    assert job["job_id"] == job_id


def test_require_job_waiting_for_400_when_flag_unset(patched_client) -> None:
    job_id = _create_job()
    dependency = require_job_waiting_for(
        "waiting_for_title_selection", "Job is not currently waiting for title selection"
    )
    with pytest.raises(HTTPException) as exc_info:
        dependency(get_job_or_404(job_id))
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Job is not currently waiting for title selection"


# ---------------------------------------------------------------------------
# get_job (combined dependency)
# ---------------------------------------------------------------------------


def test_get_job_501_when_store_unavailable(patched_client, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "get_blog_job", None)
    dependency = get_job("get_blog_job")
    with pytest.raises(HTTPException) as exc_info:
        dependency("anything")
    assert exc_info.value.status_code == 501
    assert exc_info.value.detail == "Job store not available"


def test_get_job_501_when_get_blog_job_omitted_from_helper_names(
    patched_client, monkeypatch
) -> None:
    """get_blog_job is always store-checked even if the caller forgets to list it."""
    monkeypatch.setattr(_api_main, "get_blog_job", None)
    dependency = get_job()
    with pytest.raises(HTTPException) as exc_info:
        dependency("anything")
    assert exc_info.value.status_code == 501
    assert exc_info.value.detail == "Job store not available"


def test_get_job_404_when_job_missing(patched_client) -> None:
    dependency = get_job("get_blog_job")
    with pytest.raises(HTTPException) as exc_info:
        dependency("does-not-exist")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job does-not-exist not found"


def test_get_job_400_when_waiting_for_flag_unset(patched_client) -> None:
    job_id = _create_job()
    dependency = get_job(
        "get_blog_job",
        waiting_for=(
            "waiting_for_title_selection",
            "Job is not currently waiting for title selection",
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        dependency(job_id)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Job is not currently waiting for title selection"


def test_get_job_returns_job_on_success(patched_client) -> None:
    job_id = _create_job(waiting_for_title_selection=True)
    dependency = get_job(
        "get_blog_job",
        waiting_for=(
            "waiting_for_title_selection",
            "Job is not currently waiting for title selection",
        ),
    )
    job = dependency(job_id)
    assert job["job_id"] == job_id


def test_get_job_without_waiting_for_skips_state_check(patched_client) -> None:
    job_id = _create_job()
    dependency = get_job("get_blog_job")
    job = dependency(job_id)
    assert job["job_id"] == job_id
