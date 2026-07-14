"""Unit tests for planning_team.api.main's small helpers: _get_job_or_404, _handoff_field.

Pure unit tests (mocked get_job / standalone function calls, no live Postgres or job
service) — deliberately kept out of test_api.py's pytest.mark.integration file so they
still run when integration tests are excluded from a test run.
"""

import sys
from pathlib import Path

import pytest

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from fastapi import HTTPException  # noqa: E402

from planning_team.api import main as main_module  # noqa: E402
from planning_team.api.main import _get_job_or_404, _handoff_field  # noqa: E402

# --- _get_job_or_404 ---------------------------------------------------------


def test_get_job_or_404_raises_when_missing(monkeypatch):
    monkeypatch.setattr(main_module, "get_job", lambda job_id: None)
    with pytest.raises(HTTPException) as exc_info:
        _get_job_or_404("missing-job")
    assert exc_info.value.status_code == 404
    assert "missing-job" in exc_info.value.detail


def test_get_job_or_404_returns_data_when_present(monkeypatch):
    monkeypatch.setattr(main_module, "get_job", lambda job_id: {"status": "running"})
    assert _get_job_or_404("some-job") == {"status": "running"}


# --- _handoff_field ------------------------------------------------------------


def test_handoff_field_returns_value_from_dict():
    assert _handoff_field({"prd_path": "/x/prd.md"}, "prd_path") == "/x/prd.md"


def test_handoff_field_returns_none_for_missing_key():
    assert _handoff_field({}, "prd_path") is None


def test_handoff_field_returns_none_for_non_dict():
    assert _handoff_field(None, "prd_path") is None
    assert _handoff_field("not-a-dict", "prd_path") is None
