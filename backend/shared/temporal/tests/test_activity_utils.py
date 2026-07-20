"""Tests for shared.temporal.activity_utils.

These exercise the retry/cancellation helpers without a real Temporal server by
patching the ``temporalio.activity`` context APIs.
"""

from __future__ import annotations

import pytest
from temporalio.common import RetryPolicy
from temporalio.exceptions import CancelledError

from shared.temporal import activity_utils


def _fake_info(retry_policy, attempt):
    return type("I", (), {"retry_policy": retry_policy, "attempt": attempt})()


# ---------------------------------------------------------------------------
# is_cancelled
# ---------------------------------------------------------------------------


def test_is_cancelled_false_outside_activity_context() -> None:
    # No activity context -> activity.is_cancelled() raises RuntimeError -> False.
    assert activity_utils.is_cancelled() is False


def test_is_cancelled_reads_activity_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("temporalio.activity.is_cancelled", lambda: True)
    assert activity_utils.is_cancelled() is True


# ---------------------------------------------------------------------------
# is_last_attempt
# ---------------------------------------------------------------------------


def test_is_last_attempt_true_outside_activity_context() -> None:
    # No activity context -> treat as last attempt so the caller marks terminal.
    assert activity_utils.is_last_attempt() is True


def test_is_last_attempt_reads_scheduled_retry_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "temporalio.activity.info", lambda: _fake_info(RetryPolicy(maximum_attempts=3), 3)
    )
    assert activity_utils.is_last_attempt() is True

    monkeypatch.setattr(
        "temporalio.activity.info", lambda: _fake_info(RetryPolicy(maximum_attempts=3), 1)
    )
    assert activity_utils.is_last_attempt() is False

    # maximum_attempts <= 0 means unlimited retries -> never the last attempt.
    monkeypatch.setattr(
        "temporalio.activity.info", lambda: _fake_info(RetryPolicy(maximum_attempts=0), 9)
    )
    assert activity_utils.is_last_attempt() is False


def test_is_last_attempt_none_retry_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("temporalio.activity.info", lambda: _fake_info(None, 5))
    # No policy -> max_attempts treated as 0 (unlimited) -> not the last attempt.
    assert activity_utils.is_last_attempt() is False


# ---------------------------------------------------------------------------
# raise_if_cancelled
# ---------------------------------------------------------------------------


def test_raise_if_cancelled_reraises_cancellederror() -> None:
    marks: list[str] = []
    exc = CancelledError("cancelled")
    with pytest.raises(CancelledError) as info:
        try:
            raise exc
        except CancelledError as e:
            activity_utils.raise_if_cancelled(e, "phase cancelled", lambda: marks.append("hit"))
    assert info.value is exc  # original re-raised unchanged
    assert marks == ["hit"]


def test_raise_if_cancelled_maps_cancelled_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("temporalio.activity.is_cancelled", lambda: True)
    marks: list[str] = []
    with pytest.raises(CancelledError) as info:
        try:
            raise RuntimeError("boom")
        except RuntimeError as e:
            activity_utils.raise_if_cancelled(e, "phase cancelled", lambda: marks.append("hit"))
    assert str(info.value) == "phase cancelled"
    assert isinstance(info.value.__cause__, RuntimeError)
    assert marks == ["hit"]


def test_raise_if_cancelled_noop_when_not_cancelled() -> None:
    marks: list[str] = []
    # Not a CancelledError and not cancelled -> returns None, no side effect, no raise.
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        result = activity_utils.raise_if_cancelled(
            e, "phase cancelled", lambda: marks.append("hit")
        )
    assert result is None
    assert marks == []


def test_raise_if_cancelled_without_on_cancelled_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("temporalio.activity.is_cancelled", lambda: True)
    with pytest.raises(CancelledError):
        try:
            raise RuntimeError("boom")
        except RuntimeError as e:
            activity_utils.raise_if_cancelled(e, "phase cancelled")
