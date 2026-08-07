"""SE ASGI startup must fail fast when Temporal is missing or unreachable."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from software_engineering_team.api import lifecycle


def test_assert_temporal_ready_raises_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "shared.temporal.client.is_temporal_enabled",
        lambda: False,
    )
    connect = AsyncMock()
    monkeypatch.setattr(
        "shared.temporal.client.connect_temporal_client",
        connect,
    )

    with pytest.raises(RuntimeError, match="TEMPORAL_ADDRESS"):
        asyncio.run(lifecycle._assert_temporal_ready())

    connect.assert_not_awaited()


def test_assert_temporal_ready_propagates_connect_failure(monkeypatch):
    monkeypatch.setattr(
        "shared.temporal.client.is_temporal_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "shared.temporal.client.connect_temporal_client",
        AsyncMock(side_effect=OSError("connection refused")),
    )

    with pytest.raises(OSError, match="connection refused"):
        asyncio.run(lifecycle._assert_temporal_ready())


def test_assert_temporal_ready_succeeds_when_connect_ok(monkeypatch):
    monkeypatch.setattr(
        "shared.temporal.client.is_temporal_enabled",
        lambda: True,
    )
    client = object()
    monkeypatch.setattr(
        "shared.temporal.client.connect_temporal_client",
        AsyncMock(return_value=client),
    )

    asyncio.run(lifecycle._assert_temporal_ready())


def test_se_startup_awaits_assert_before_workers(monkeypatch):
    """Fail-fast assert must run before either Temporal worker start."""
    calls: list[str] = []

    async def _assert() -> None:
        calls.append("assert")

    monkeypatch.setattr(lifecycle, "_assert_temporal_ready", _assert)

    def _se_worker() -> bool:
        calls.append("se_worker")
        return True

    def _ct_worker() -> bool:
        calls.append("ct_worker")
        return True

    monkeypatch.setattr(
        "software_engineering_team.temporal.worker.start_se_temporal_worker_thread",
        _se_worker,
    )
    monkeypatch.setattr(
        "software_engineering_team.temporal.coding_team_worker.start_coding_team_temporal_worker_thread",
        _ct_worker,
    )
    monkeypatch.setattr(
        "software_engineering_team.shared.cost_tracker.register_cost_observer",
        lambda: calls.append("telemetry"),
    )
    monkeypatch.setattr(
        "software_engineering_team.shared.trace_flusher.register_trace_flusher",
        lambda: None,
    )
    monkeypatch.setattr(
        "software_engineering_team.coding_engine_provider.SECodeEngineProvider",
        lambda: object(),
    )
    monkeypatch.setattr(
        "software_engineering_team.engine_provider.set_engine_provider",
        lambda _p: calls.append("engine"),
    )

    asyncio.run(lifecycle._se_startup())

    assert calls[0] == "assert"
    assert "se_worker" in calls
    assert "ct_worker" in calls
