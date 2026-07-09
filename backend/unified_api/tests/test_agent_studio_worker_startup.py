"""Tests for the unified-API lifespan hook that boots the Agent Studio worker.

Agent Studio is an in-process team, so its Temporal worker is started from the
unified-API lifespan (not a separate ``team_service`` container). The helper is gated
on the team being enabled and must never let a worker-start failure break app startup.
"""

from __future__ import annotations

import types

import pytest

import unified_api.main as main


def _fake_team_configs(enabled: bool) -> dict:
    return {"agent_studio": types.SimpleNamespace(enabled=enabled)}


def test_worker_start_invoked_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(main, "TEAM_CONFIGS", _fake_team_configs(True))
    monkeypatch.setattr(
        "agent_studio.temporal.worker.start_agent_studio_temporal_worker_thread",
        lambda: called.append(True),
    )
    main._start_agent_studio_temporal_worker()
    assert called == [True]


def test_worker_start_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(main, "TEAM_CONFIGS", _fake_team_configs(False))
    monkeypatch.setattr(
        "agent_studio.temporal.worker.start_agent_studio_temporal_worker_thread",
        lambda: called.append(True),
    )
    main._start_agent_studio_temporal_worker()
    assert called == []


def test_worker_start_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> bool:
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(main, "TEAM_CONFIGS", _fake_team_configs(True))
    monkeypatch.setattr("agent_studio.temporal.worker.start_agent_studio_temporal_worker_thread", _boom)
    # Must not raise — startup is log-and-continue.
    main._start_agent_studio_temporal_worker()
