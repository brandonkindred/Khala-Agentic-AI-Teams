"""Tests for shared.job_contracts.models — shared job-response base DTOs."""

from __future__ import annotations

from typing import Optional

import pytest
from pydantic import ValidationError

from shared.job_contracts.models import (
    CancelJobResponseBase,
    DeleteJobResponseBase,
    JobListItemBase,
    JobStatusResponseBase,
)

# --- JobStatusResponseBase ---------------------------------------------------


def test_job_status_response_base_requires_job_id_and_status():
    with pytest.raises(ValidationError):
        JobStatusResponseBase(status="running")
    with pytest.raises(ValidationError):
        JobStatusResponseBase(job_id="job-1")


def test_job_status_response_base_defaults():
    status = JobStatusResponseBase(job_id="job-1", status="running")
    assert status.progress is None
    assert status.error is None
    assert status.created_at is None
    assert status.updated_at is None


def test_job_status_response_base_accepts_all_fields():
    status = JobStatusResponseBase(
        job_id="job-1",
        status="failed",
        progress=42,
        error="boom",
        created_at="2026-08-12T00:00:00Z",
        updated_at="2026-08-12T00:05:00Z",
    )
    assert status.job_id == "job-1"
    assert status.status == "failed"
    assert status.progress == 42
    assert status.error == "boom"
    assert status.created_at == "2026-08-12T00:00:00Z"
    assert status.updated_at == "2026-08-12T00:05:00Z"


def test_job_status_response_base_json_roundtrip():
    status = JobStatusResponseBase(
        job_id="job-1",
        status="running",
        progress=42,
        error=None,
        created_at="2026-08-12T00:00:00Z",
        updated_at="2026-08-12T00:05:00Z",
    )
    dumped = status.model_dump()
    assert dumped["job_id"] == "job-1"
    assert dumped["progress"] == 42
    assert JobStatusResponseBase.model_validate(dumped) == status


# --- JobListItemBase ----------------------------------------------------------


def test_job_list_item_base_requires_job_id_and_status():
    with pytest.raises(ValidationError):
        JobListItemBase(status="running")
    with pytest.raises(ValidationError):
        JobListItemBase(job_id="job-1")


def test_job_list_item_base_defaults():
    item = JobListItemBase(job_id="job-1", status="pending")
    assert item.created_at is None
    assert item.updated_at is None


def test_job_list_item_base_json_roundtrip():
    item = JobListItemBase(
        job_id="job-1",
        status="pending",
        created_at="2026-08-12T00:00:00Z",
        updated_at="2026-08-12T00:05:00Z",
    )
    dumped = item.model_dump()
    assert JobListItemBase.model_validate(dumped) == item


# --- CancelJobResponseBase -----------------------------------------------------


def test_cancel_job_response_base_requires_job_id():
    with pytest.raises(ValidationError):
        CancelJobResponseBase()


def test_cancel_job_response_base_defaults():
    cancelled = CancelJobResponseBase(job_id="job-1")
    assert cancelled.status == "cancelled"
    assert cancelled.message == "Job cancellation requested."


def test_cancel_job_response_base_overrides_defaults():
    cancelled = CancelJobResponseBase(job_id="job-1", status="cancel_requested", message="Cancelling now.")
    assert cancelled.status == "cancel_requested"
    assert cancelled.message == "Cancelling now."


# --- DeleteJobResponseBase -----------------------------------------------------


def test_delete_job_response_base_requires_job_id():
    with pytest.raises(ValidationError):
        DeleteJobResponseBase()


def test_delete_job_response_base_defaults():
    deleted = DeleteJobResponseBase(job_id="job-1")
    assert deleted.message == "Job deleted."


# --- Subclassing behavior -------------------------------------------------------


def test_subclass_can_add_extra_fields():
    class _TeamStatus(JobStatusResponseBase):
        client_id: str
        current_phase: str = "draft"

    instance = _TeamStatus(job_id="job-1", status="running", client_id="acme")
    # Inherited fields still present and correctly typed.
    assert instance.job_id == "job-1"
    assert instance.progress is None
    # New required field enforced; new defaulted field applied.
    assert instance.client_id == "acme"
    assert instance.current_phase == "draft"


def test_subclass_missing_new_required_field_raises():
    class _TeamStatus(JobStatusResponseBase):
        client_id: str

    with pytest.raises(ValidationError):
        _TeamStatus(job_id="job-1", status="running")  # missing client_id

    with pytest.raises(ValidationError):
        _TeamStatus(status="running", client_id="acme")  # missing inherited job_id


def test_subclass_can_override_inherited_field_type():
    class _StrictProgressStatus(JobStatusResponseBase):
        progress: int  # tightened: required, no longer Optional

    with pytest.raises(ValidationError):
        _StrictProgressStatus(job_id="job-1", status="running")  # progress now required

    instance = _StrictProgressStatus(job_id="job-1", status="running", progress=10)
    assert instance.progress == 10


def test_subclass_json_roundtrip_preserves_extra_fields():
    class _TeamStatus(JobStatusResponseBase):
        client_id: str
        current_phase: str = "draft"

    instance = _TeamStatus(job_id="job-1", status="running", progress=5, client_id="acme", current_phase="review")
    dumped = instance.model_dump()
    assert dumped["client_id"] == "acme"
    assert dumped["current_phase"] == "review"
    assert _TeamStatus.model_validate(dumped) == instance


def test_job_list_item_base_subclass_adds_fields():
    class _TeamListItem(JobListItemBase):
        brief: Optional[str] = None

    item = _TeamListItem(job_id="job-1", status="pending", brief="A short summary.")
    assert item.job_id == "job-1"
    assert item.brief == "A short summary."

    default_item = _TeamListItem(job_id="job-2", status="pending")
    assert default_item.brief is None
