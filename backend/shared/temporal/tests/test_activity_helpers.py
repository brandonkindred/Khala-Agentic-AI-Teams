"""Tests for shared.temporal.activity_helpers.

These exercise the JSON-normalization and fail-on-final-attempt guard helpers
without a real Temporal server, patching the ``temporalio.activity`` context
APIs and injecting fake job-store callables where needed.
"""

from __future__ import annotations

import pytest

from shared.temporal import activity_helpers


def _fake_info(attempt):
    return type("I", (), {"attempt": attempt})()


class _FakeModel:
    def __init__(self, dumped):
        self._dumped = dumped

    def model_dump(self, mode="json"):
        return self._dumped


# ---------------------------------------------------------------------------
# json_safe
# ---------------------------------------------------------------------------


def test_json_safe_scalars_unchanged() -> None:
    assert activity_helpers.json_safe(1) == 1
    assert activity_helpers.json_safe("x") == "x"
    assert activity_helpers.json_safe(None) is None


def test_json_safe_converts_model() -> None:
    model = _FakeModel({"a": 1})
    assert activity_helpers.json_safe(model) == {"a": 1}


def test_json_safe_recurses_into_list_and_dict() -> None:
    model = _FakeModel({"a": 1})
    value = {"nested": [model, {"deep": model}]}
    assert activity_helpers.json_safe(value) == {"nested": [{"a": 1}, {"deep": {"a": 1}}]}


# ---------------------------------------------------------------------------
# merge_context
# ---------------------------------------------------------------------------


def test_merge_context_overlays_and_normalizes() -> None:
    context = {"a": 1, "b": 2}
    update = {"b": _FakeModel({"c": 3}), "d": 4}
    merged = activity_helpers.merge_context(context, update)
    assert merged == {"a": 1, "b": {"c": 3}, "d": 4}
    # original context is untouched
    assert context == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# is_final_attempt
# ---------------------------------------------------------------------------


def test_is_final_attempt_true_outside_activity_context() -> None:
    assert activity_helpers.is_final_attempt(3) is True


def test_is_final_attempt_reads_activity_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("temporalio.activity.in_activity", lambda: True)
    monkeypatch.setattr("temporalio.activity.info", lambda: _fake_info(3))
    assert activity_helpers.is_final_attempt(3) is True

    monkeypatch.setattr("temporalio.activity.info", lambda: _fake_info(1))
    assert activity_helpers.is_final_attempt(3) is False


# ---------------------------------------------------------------------------
# fail_job
# ---------------------------------------------------------------------------


def test_fail_job_calls_injected_mark_job_failed() -> None:
    calls = []

    def mark_job_failed(job_id, error):
        calls.append((job_id, error))

    activity_helpers.fail_job("job-1", ValueError("boom"), mark_job_failed=mark_job_failed)
    assert calls == [("job-1", "boom")]


# ---------------------------------------------------------------------------
# guarded
# ---------------------------------------------------------------------------


def test_guarded_success_writes_progress_and_returns_work_result() -> None:
    updates = []
    fails = []

    result = activity_helpers.guarded(
        "job-1",
        "phase-a",
        50,
        "working",
        lambda: "done",
        max_attempts=3,
        update_job=lambda job_id, **fields: updates.append((job_id, fields)),
        mark_job_failed=lambda job_id, error: fails.append((job_id, error)),
    )

    assert result == "done"
    assert updates == [("job-1", {"current_phase": "phase-a", "progress": 50, "status_text": "working"})]
    assert fails == []


def test_guarded_writes_status_only_when_supplied() -> None:
    updates = []

    activity_helpers.guarded(
        "job-1",
        "phase-a",
        0,
        "starting",
        lambda: None,
        max_attempts=3,
        status="RUNNING",
        update_job=lambda job_id, **fields: updates.append(fields),
        mark_job_failed=lambda job_id, error: None,
    )

    assert updates == [{"current_phase": "phase-a", "progress": 0, "status_text": "starting", "status": "RUNNING"}]


def test_guarded_non_final_attempt_does_not_mark_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("temporalio.activity.in_activity", lambda: True)
    monkeypatch.setattr("temporalio.activity.info", lambda: _fake_info(1))
    fails = []

    def work():
        raise RuntimeError("transient")

    with pytest.raises(RuntimeError):
        activity_helpers.guarded(
            "job-1",
            "phase-a",
            50,
            "working",
            work,
            max_attempts=3,
            update_job=lambda job_id, **fields: None,
            mark_job_failed=lambda job_id, error: fails.append((job_id, error)),
        )

    assert fails == []


def test_guarded_final_attempt_marks_failed_and_reraises() -> None:
    fails = []

    def work():
        raise RuntimeError("terminal")

    with pytest.raises(RuntimeError):
        activity_helpers.guarded(
            "job-1",
            "phase-a",
            50,
            "working",
            work,
            max_attempts=3,
            update_job=lambda job_id, **fields: None,
            mark_job_failed=lambda job_id, error: fails.append((job_id, error)),
        )

    # outside an activity context, is_final_attempt() defaults to True
    assert fails == [("job-1", "terminal")]


def test_guarded_progress_write_failure_also_marks_failed() -> None:
    fails = []

    def update_job(job_id, **fields):
        raise RuntimeError("store unavailable")

    with pytest.raises(RuntimeError):
        activity_helpers.guarded(
            "job-1",
            "phase-a",
            50,
            "working",
            lambda: "unreached",
            max_attempts=3,
            update_job=update_job,
            mark_job_failed=lambda job_id, error: fails.append((job_id, error)),
        )

    assert fails == [("job-1", "store unavailable")]
