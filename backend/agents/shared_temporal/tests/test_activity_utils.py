"""Tests for shared_temporal.activity_utils.is_last_attempt."""

from __future__ import annotations

from types import SimpleNamespace

import temporalio.activity as ta

from shared_temporal.activity_utils import is_last_attempt


def test_is_last_attempt_outside_activity_context() -> None:
    """No activity context (direct/thread use) -> treated as the last attempt."""
    assert is_last_attempt() is True


def test_is_last_attempt_reads_scheduled_retry_policy(monkeypatch) -> None:
    """The check reads maximum_attempts off the scheduled policy (activity.info())."""

    def _info(attempt, max_attempts):
        return SimpleNamespace(
            attempt=attempt, retry_policy=SimpleNamespace(maximum_attempts=max_attempts)
        )

    monkeypatch.setattr(ta, "info", lambda: _info(1, 3))
    assert is_last_attempt() is False
    monkeypatch.setattr(ta, "info", lambda: _info(3, 3))
    assert is_last_attempt() is True


def test_is_last_attempt_unlimited_or_missing_policy(monkeypatch) -> None:
    """maximum_attempts <= 0 (unlimited) or a missing policy -> never the last attempt."""
    monkeypatch.setattr(
        ta,
        "info",
        lambda: SimpleNamespace(attempt=9, retry_policy=SimpleNamespace(maximum_attempts=0)),
    )
    assert is_last_attempt() is False
    monkeypatch.setattr(ta, "info", lambda: SimpleNamespace(attempt=9, retry_policy=None))
    assert is_last_attempt() is False
