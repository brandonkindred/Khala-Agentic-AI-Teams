"""Tests for ``social_media_marketing_team.api.trend_scheduler``.

The scheduler uses APScheduler + a global cached digest. These tests
swap out the scheduler and trend agent with fakes to verify every code
path without scheduling real cron jobs or hitting the LLM/web search.
"""

from __future__ import annotations

from typing import Any

import pytest

from social_media_marketing_team.api import trend_scheduler as ts
from social_media_marketing_team.trend_models import TrendDigest


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch):
    """Reset scheduler module globals between tests."""
    monkeypatch.setattr(ts, "_latest_digest", None)
    monkeypatch.setattr(ts, "_scheduler", None)
    yield


def test_get_latest_digest_returns_none_initially() -> None:
    assert ts.get_latest_digest() is None


def test_run_trend_job_updates_cached_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_trend_job should construct an agent and stash its digest."""
    digest = TrendDigest(
        generated_at="2026-01-01T00:00:00+00:00",
        topics=[],
        platforms_searched=["X/Twitter"],
        search_query_count=8,
    )

    class _FakeAgent:
        def __init__(self, web_search):
            self.web_search = web_search

        def run(self):
            return digest

    monkeypatch.setattr(ts, "TrendDiscoveryAgent", _FakeAgent)
    monkeypatch.setattr(ts, "OllamaWebSearch", lambda: object())

    ts.run_trend_job()
    assert ts.get_latest_digest() is digest


def test_run_trend_job_swallows_exceptions(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Any failure during trend discovery is logged, not raised."""

    def _bad_search():
        raise RuntimeError("network down")

    monkeypatch.setattr(ts, "OllamaWebSearch", _bad_search)

    with caplog.at_level("ERROR"):
        ts.run_trend_job()  # must not raise

    assert any("TrendDiscovery: run failed" in r.message for r in caplog.records)
    # Digest unchanged
    assert ts.get_latest_digest() is None


def test_start_scheduler_creates_and_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """start_scheduler should construct a BackgroundScheduler, add a cron
    job, and call start()."""
    captured: dict[str, Any] = {}

    class _FakeSched:
        def __init__(self, timezone=None):
            captured["timezone"] = timezone
            self.jobs: list[Any] = []
            self.started = False
            self.running = False

        def add_job(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

        def start(self):
            self.started = True
            self.running = True

        def shutdown(self, wait=False):
            self.running = False
            captured["shutdown_wait"] = wait

    monkeypatch.setattr(ts, "BackgroundScheduler", _FakeSched)
    ts.start_scheduler()
    assert ts._scheduler is not None
    assert ts._scheduler.started is True
    assert captured["kwargs"]["id"] == "trend_discovery_daily"


def test_stop_scheduler_when_none_is_noop() -> None:
    ts._scheduler = None
    ts.stop_scheduler()  # must not raise


def test_stop_scheduler_when_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Sched:
        running = False

        def shutdown(self, wait=False):
            raise AssertionError("should not be called")

    monkeypatch.setattr(ts, "_scheduler", _Sched())
    ts.stop_scheduler()  # must not call shutdown


def test_stop_scheduler_when_running(monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict[str, Any] = {"shutdown_called": False}

    class _Sched:
        running = True

        def shutdown(self, wait=False):
            state["shutdown_called"] = True
            state["wait"] = wait

    monkeypatch.setattr(ts, "_scheduler", _Sched())
    ts.stop_scheduler()
    assert state["shutdown_called"] is True
    assert state["wait"] is False
