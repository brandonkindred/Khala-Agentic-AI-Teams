"""Unit tests for ``validate_job_for_action`` and its typed exceptions.

No direct unit coverage existed before this (see
``test_job_service_client_factory.py`` for the sibling shared-infra suite)
despite 6+ call sites across teams depending on its exact raise/return
contract.
"""

from __future__ import annotations

import pytest

from job_service_client import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JobNotFoundError,
    JobStateError,
    validate_job_for_action,
)

_ALLOWED = frozenset({JOB_STATUS_PENDING, JOB_STATUS_RUNNING})


def test_raises_job_not_found_error_when_job_data_is_none():
    with pytest.raises(JobNotFoundError, match="job-1 not found"):
        validate_job_for_action(None, "job-1", _ALLOWED, "resumed")


def test_raises_job_not_found_error_when_job_data_is_empty_dict():
    """``if not job_data`` treats an empty (falsy) dict the same as ``None``."""
    with pytest.raises(JobNotFoundError, match="job-1 not found"):
        validate_job_for_action({}, "job-1", _ALLOWED, "resumed")


def test_raises_job_state_error_when_status_not_allowed():
    job = {"status": JOB_STATUS_COMPLETED}
    with pytest.raises(JobStateError, match="cannot be resumed"):
        validate_job_for_action(job, "job-1", _ALLOWED, "resumed")


def test_job_not_found_error_is_a_value_error():
    """Backward-compat: every existing call site catches broad ``ValueError``."""
    assert issubclass(JobNotFoundError, ValueError)


def test_job_state_error_is_a_value_error():
    assert issubclass(JobStateError, ValueError)


def test_returns_job_data_on_success():
    job = {"status": JOB_STATUS_PENDING, "foo": "bar"}
    assert validate_job_for_action(job, "job-1", _ALLOWED, "resumed") is job


def test_missing_status_key_defaults_to_pending_for_the_check():
    job = {"foo": "bar"}
    assert validate_job_for_action(job, "job-1", _ALLOWED, "resumed") is job
