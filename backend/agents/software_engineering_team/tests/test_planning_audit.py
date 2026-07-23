"""Unit tests for the SE planning_runs audit helper (software_engineering_team.shared.planning_audit)."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from software_engineering_team.shared import planning_audit


@pytest.fixture
def recorded_calls(monkeypatch):
    """Capture calls to the underlying planning_team writer instead of hitting Postgres."""
    calls: list[tuple[str, Dict[str, Any]]] = []

    def _fake_record_planning_run(job_id: str, **kwargs: Any) -> bool:
        calls.append((job_id, kwargs))
        return True

    monkeypatch.setattr(
        "planning_team.postgres.writer.record_planning_run", _fake_record_planning_run
    )
    return calls


def test_record_se_planning_run_derives_expected_kwargs(recorded_calls) -> None:
    """A full planning_result dict is unpacked into the exact record_planning_run kwargs."""
    planning_result = {
        "success": True,
        "summary": "Planning completed; handoff package ready.",
        "handoff_package": {"summary": "Build a widget API."},
        "open_questions": [{"question": "Which auth scheme?"}],
        "resolved_questions": [{"question": "Which DB?", "answer": "Postgres"}],
    }

    result = planning_audit.record_se_planning_run("se-job-1", planning_result)

    assert result is True
    assert recorded_calls == [
        (
            "se-job-1",
            {
                "client_name": None,
                "summary": "Planning completed; handoff package ready.",
                "handoff_summary": "Build a widget API.",
                "open_questions": [{"question": "Which auth scheme?"}],
                "resolved_questions": [{"question": "Which DB?", "answer": "Postgres"}],
            },
        )
    ]


def test_record_se_planning_run_defaults_missing_optional_fields(recorded_calls) -> None:
    """A minimal planning_result (missing handoff/summary/questions) defaults safely."""
    planning_result = {"success": True}

    result = planning_audit.record_se_planning_run("se-job-2", planning_result)

    assert result is True
    assert recorded_calls == [
        (
            "se-job-2",
            {
                "client_name": None,
                "summary": "",
                "handoff_summary": "",
                "open_questions": [],
                "resolved_questions": [],
            },
        )
    ]


def test_record_se_planning_run_propagates_underlying_result(monkeypatch) -> None:
    """The bool returned by record_planning_run passes through unchanged."""
    monkeypatch.setattr(
        "planning_team.postgres.writer.record_planning_run", lambda job_id, **kwargs: False
    )

    assert planning_audit.record_se_planning_run("se-job-3", {"success": True}) is False
