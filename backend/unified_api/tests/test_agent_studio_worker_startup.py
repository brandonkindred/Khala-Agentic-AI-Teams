"""Tests for the unified-API lifespan hook that boots the Agent Studio worker.

Agent Studio is an in-process team, so its Temporal worker is started from the
unified-API lifespan (not a separate ``team_service`` container). The helper is gated
on the team being enabled, must never let a worker-start failure break app startup,
and must log honestly: INFO both when a worker actually started and when
``start_team_worker`` returns ``False`` (``TEMPORAL_ADDRESS`` unset → no worker) —
authoring CRUD falls back to direct in-process dispatch in that case, so it is a mode
switch, not a degraded state, and does not warrant a WARNING. A genuine worker-start
failure (an exception) still logs a WARNING.

Log assertions patch ``main.logger`` methods directly rather than using ``caplog``,
which is unreliable here because importing the unified API configures logging.
"""

from __future__ import annotations

import inspect
import types

import pytest

import unified_api.main as main


def _fake_team_configs(enabled: bool) -> dict:
    return {"agent_studio": types.SimpleNamespace(enabled=enabled)}


def _capture_logs(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    """Capture ``main.logger`` info/warning messages into two lists."""
    infos: list[str] = []
    warns: list[str] = []
    monkeypatch.setattr(main.logger, "info", lambda msg, *a, **k: infos.append(msg))
    monkeypatch.setattr(main.logger, "warning", lambda msg, *a, **k: warns.append(msg))
    return infos, warns


def test_worker_start_invoked_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    def _start() -> bool:
        called.append(True)
        return True

    monkeypatch.setattr(main, "TEAM_CONFIGS", _fake_team_configs(True))
    monkeypatch.setattr("agent_platform.studio.temporal.worker.start_agent_studio_temporal_worker_thread", _start)
    infos, warns = _capture_logs(monkeypatch)

    main._start_agent_studio_temporal_worker()

    assert called == [True]
    assert any("Started Agent Studio Temporal worker" in m for m in infos)
    assert warns == []


def test_worker_start_logs_info_when_temporal_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``False`` return (worker not started, e.g. TEMPORAL_ADDRESS unset) logs an
    INFO note that authoring falls back to direct dispatch — not a WARNING, since
    that path is fully functional — and never raises."""
    monkeypatch.setattr(main, "TEAM_CONFIGS", _fake_team_configs(True))
    monkeypatch.setattr(
        "agent_platform.studio.temporal.worker.start_agent_studio_temporal_worker_thread",
        lambda: False,
    )
    infos, warns = _capture_logs(monkeypatch)

    main._start_agent_studio_temporal_worker()

    assert any("NOT started" in m for m in infos)
    assert warns == []


def test_worker_start_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(main, "TEAM_CONFIGS", _fake_team_configs(False))
    monkeypatch.setattr(
        "agent_platform.studio.temporal.worker.start_agent_studio_temporal_worker_thread",
        lambda: called.append(True),
    )
    main._start_agent_studio_temporal_worker()
    assert called == []


def test_worker_start_skipped_when_temporal_worker_flag_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER=false must skip starting the worker
    even when the agent_studio team itself is enabled."""
    called: list[bool] = []
    monkeypatch.setattr(main, "UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER", False)
    monkeypatch.setattr(main, "TEAM_CONFIGS", _fake_team_configs(True))
    monkeypatch.setattr(
        "agent_platform.studio.temporal.worker.start_agent_studio_temporal_worker_thread",
        lambda: called.append(True),
    )
    infos, warns = _capture_logs(monkeypatch)

    main._start_agent_studio_temporal_worker()

    assert called == []
    assert any("disabled" in m for m in infos)
    assert warns == []


def test_worker_start_invoked_when_temporal_worker_flag_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (flag true, or unset) must preserve today's always-on behavior."""
    called: list[bool] = []

    def _start() -> bool:
        called.append(True)
        return True

    monkeypatch.setattr(main, "UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER", True)
    monkeypatch.setattr(main, "TEAM_CONFIGS", _fake_team_configs(True))
    monkeypatch.setattr("agent_platform.studio.temporal.worker.start_agent_studio_temporal_worker_thread", _start)

    main._start_agent_studio_temporal_worker()

    assert called == [True]


def test_worker_start_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> bool:
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(main, "TEAM_CONFIGS", _fake_team_configs(True))
    monkeypatch.setattr("agent_platform.studio.temporal.worker.start_agent_studio_temporal_worker_thread", _boom)
    infos, warns = _capture_logs(monkeypatch)

    # Must not raise — startup is log-and-continue.
    main._start_agent_studio_temporal_worker()

    # The swallowed exception is surfaced as a WARNING (not a silent success line).
    assert any("failed to start" in m for m in warns)
    assert infos == []


def test_stop_in_process_temporal_workers_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(
        "shared.temporal.worker.stop_all_team_workers",
        lambda: called.append(True),
    )
    main._stop_in_process_temporal_workers()
    assert called == [True]


def test_stop_in_process_temporal_workers_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> None:
        raise RuntimeError("worker shutdown exploded")

    monkeypatch.setattr("shared.temporal.worker.stop_all_team_workers", _boom)
    infos, warns = _capture_logs(monkeypatch)

    main._stop_in_process_temporal_workers()

    assert any("failed" in m for m in warns)
    assert infos == []


def test_lifespan_stops_temporal_workers_before_usage_flusher() -> None:
    src = inspect.getsource(main.lifespan)
    assert src.index("_stop_in_process_temporal_workers") < src.index("usage_flush_shutdown")
