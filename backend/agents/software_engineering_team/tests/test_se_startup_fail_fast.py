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


def test_assert_temporal_ready_raises_when_connect_returns_none(monkeypatch):
    monkeypatch.setattr(
        "shared.temporal.client.is_temporal_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "shared.temporal.client.connect_temporal_client",
        AsyncMock(return_value=None),
    )

    with pytest.raises(RuntimeError, match="Temporal connect probe returned no client"):
        asyncio.run(lifecycle._assert_temporal_ready())


def test_assert_temporal_ready_succeeds_when_connect_ok(monkeypatch):
    monkeypatch.setattr(
        "shared.temporal.client.is_temporal_enabled",
        lambda: True,
    )
    client = object()
    connect = AsyncMock(return_value=client)
    monkeypatch.setattr(
        "shared.temporal.client.connect_temporal_client",
        connect,
    )

    asyncio.run(lifecycle._assert_temporal_ready())

    connect.assert_awaited_once()


def _stub_non_worker_startup(monkeypatch, calls: list[str]) -> None:
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


def _stub_worker_ready_waits(monkeypatch, calls: list[str] | None = None) -> None:
    """Skip real await_client / thread polls in unit tests."""

    async def _client_ready() -> None:
        if calls is not None:
            calls.append("client_ready")

    async def _thread_ready(team: str) -> None:
        if calls is not None:
            calls.append(f"thread_ready:{team}")

    monkeypatch.setattr(lifecycle, "_wait_for_shared_temporal_client", _client_ready)
    monkeypatch.setattr(lifecycle, "_wait_for_team_worker_thread", _thread_ready)


def test_se_startup_awaits_assert_before_workers(monkeypatch):
    """Fail-fast assert must run before either Temporal worker start."""
    calls: list[str] = []

    async def _assert() -> None:
        calls.append("assert")

    monkeypatch.setattr(lifecycle, "_assert_temporal_ready", _assert)
    _stub_worker_ready_waits(monkeypatch, calls)

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
    _stub_non_worker_startup(monkeypatch, calls)

    asyncio.run(lifecycle._se_startup())

    assert calls[0] == "assert"
    assert calls.index("assert") < calls.index("se_worker")
    assert calls.index("se_worker") < calls.index("client_ready")
    assert calls.index("client_ready") < calls.index("thread_ready:software_engineering")
    assert calls.index("assert") < calls.index("ct_worker")
    assert calls.index("ct_worker") < calls.index("thread_ready:coding_team")


def test_se_startup_propagates_assert_failure_before_workers(monkeypatch):
    """A failing Temporal assert must abort startup and never start workers."""
    calls: list[str] = []

    async def _assert() -> None:
        calls.append("assert")
        raise RuntimeError("TEMPORAL_ADDRESS required")

    monkeypatch.setattr(lifecycle, "_assert_temporal_ready", _assert)
    _stub_worker_ready_waits(monkeypatch, calls)
    monkeypatch.setattr(
        "software_engineering_team.temporal.worker.start_se_temporal_worker_thread",
        lambda: calls.append("se_worker") or True,
    )
    monkeypatch.setattr(
        "software_engineering_team.temporal.coding_team_worker.start_coding_team_temporal_worker_thread",
        lambda: calls.append("ct_worker") or True,
    )
    _stub_non_worker_startup(monkeypatch, calls)

    with pytest.raises(RuntimeError, match="TEMPORAL_ADDRESS"):
        asyncio.run(lifecycle._se_startup())

    assert calls == ["assert"]


def test_se_startup_raises_when_se_worker_fails(monkeypatch):
    calls: list[str] = []

    async def _assert() -> None:
        calls.append("assert")

    monkeypatch.setattr(lifecycle, "_assert_temporal_ready", _assert)
    _stub_worker_ready_waits(monkeypatch, calls)
    monkeypatch.setattr(
        "software_engineering_team.temporal.worker.start_se_temporal_worker_thread",
        lambda: (_ for _ in ()).throw(RuntimeError("SE worker boom")),
    )
    monkeypatch.setattr(
        "software_engineering_team.temporal.coding_team_worker.start_coding_team_temporal_worker_thread",
        lambda: calls.append("ct_worker") or True,
    )
    _stub_non_worker_startup(monkeypatch, calls)

    with pytest.raises(RuntimeError, match="SE worker boom"):
        asyncio.run(lifecycle._se_startup())

    assert "ct_worker" not in calls
    assert "client_ready" not in calls


def test_se_startup_raises_when_coding_team_worker_fails(monkeypatch):
    calls: list[str] = []

    async def _assert() -> None:
        calls.append("assert")

    monkeypatch.setattr(lifecycle, "_assert_temporal_ready", _assert)
    _stub_worker_ready_waits(monkeypatch, calls)
    monkeypatch.setattr(
        "software_engineering_team.temporal.worker.start_se_temporal_worker_thread",
        lambda: calls.append("se_worker") or True,
    )
    monkeypatch.setattr(
        "software_engineering_team.temporal.coding_team_worker.start_coding_team_temporal_worker_thread",
        lambda: (_ for _ in ()).throw(RuntimeError("coding_team worker boom")),
    )
    _stub_non_worker_startup(monkeypatch, calls)

    with pytest.raises(RuntimeError, match="coding_team worker boom"):
        asyncio.run(lifecycle._se_startup())

    assert "se_worker" in calls
    assert "client_ready" in calls
    assert "thread_ready:coding_team" not in calls


def test_se_startup_raises_when_se_worker_start_returns_false(monkeypatch):
    async def _assert() -> None:
        return None

    monkeypatch.setattr(lifecycle, "_assert_temporal_ready", _assert)
    _stub_worker_ready_waits(monkeypatch)
    monkeypatch.setattr(
        "software_engineering_team.temporal.worker.start_se_temporal_worker_thread",
        lambda: False,
    )
    monkeypatch.setattr(
        "software_engineering_team.temporal.coding_team_worker.start_coding_team_temporal_worker_thread",
        lambda: True,
    )
    _stub_non_worker_startup(monkeypatch, [])

    with pytest.raises(RuntimeError, match="SE Temporal worker"):
        asyncio.run(lifecycle._se_startup())


def test_se_startup_raises_when_coding_team_worker_start_returns_false(monkeypatch):
    calls: list[str] = []

    async def _assert() -> None:
        return None

    monkeypatch.setattr(lifecycle, "_assert_temporal_ready", _assert)
    _stub_worker_ready_waits(monkeypatch, calls)
    monkeypatch.setattr(
        "software_engineering_team.temporal.worker.start_se_temporal_worker_thread",
        lambda: True,
    )
    monkeypatch.setattr(
        "software_engineering_team.temporal.coding_team_worker.start_coding_team_temporal_worker_thread",
        lambda: False,
    )
    _stub_non_worker_startup(monkeypatch, calls)

    with pytest.raises(RuntimeError, match="coding_team Temporal worker"):
        asyncio.run(lifecycle._se_startup())

    assert "client_ready" in calls
    assert "thread_ready:coding_team" not in calls


def test_se_startup_raises_when_shared_client_never_ready(monkeypatch):
    async def _assert() -> None:
        return None

    async def _client_never_ready() -> None:
        raise RuntimeError("Temporal client not available; is the team's worker running?")

    monkeypatch.setattr(lifecycle, "_assert_temporal_ready", _assert)
    monkeypatch.setattr(lifecycle, "_wait_for_shared_temporal_client", _client_never_ready)
    monkeypatch.setattr(
        lifecycle,
        "_wait_for_team_worker_thread",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "software_engineering_team.temporal.worker.start_se_temporal_worker_thread",
        lambda: True,
    )
    monkeypatch.setattr(
        "software_engineering_team.temporal.coding_team_worker.start_coding_team_temporal_worker_thread",
        lambda: True,
    )
    _stub_non_worker_startup(monkeypatch, [])

    with pytest.raises(RuntimeError, match="Temporal client not available"):
        asyncio.run(lifecycle._se_startup())


def test_wait_for_team_worker_thread_raises_when_thread_missing(monkeypatch):
    from shared.temporal import runner as temporal_runner
    from shared.temporal import worker as temporal_worker

    monkeypatch.setattr(temporal_runner, "CLIENT_READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(temporal_runner, "CLIENT_READY_POLL_S", 0.01)
    temporal_worker._worker_threads.pop("coding_team", None)

    with pytest.raises(RuntimeError, match="coding_team Temporal worker thread never became ready"):
        asyncio.run(lifecycle._wait_for_team_worker_thread("coding_team"))


def test_wait_for_shared_temporal_client_delegates_to_await_client(monkeypatch):
    called: list[bool] = []

    def _await_client(timeout_s=None):
        called.append(True)
        return object(), object()

    monkeypatch.setattr("shared.temporal.await_client", _await_client)

    asyncio.run(lifecycle._wait_for_shared_temporal_client())

    assert called == [True]
