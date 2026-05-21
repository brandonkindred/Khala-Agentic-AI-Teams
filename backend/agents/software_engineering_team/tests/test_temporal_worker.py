"""Tests for the SE Temporal worker module.

Exercises the public surface: ``create_se_worker``, ``start_se_temporal_worker_thread``,
``_worker_thread_target``. No live Temporal cluster is started — the
``is_temporal_enabled`` and ``Worker`` factory are patched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_create_se_worker_returns_none_when_disabled(monkeypatch) -> None:
    from software_engineering_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: False)
    assert worker.create_se_worker(MagicMock()) is None


def test_create_se_worker_returns_none_when_client_is_none(monkeypatch) -> None:
    from software_engineering_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    assert worker.create_se_worker(None) is None


def test_create_se_worker_returns_worker(monkeypatch) -> None:
    from software_engineering_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    fake_worker = MagicMock(name="Worker-instance")
    fake_worker_cls = MagicMock(return_value=fake_worker)
    monkeypatch.setattr(worker, "Worker", fake_worker_cls)
    # Reset the cached executor so this test is independent
    worker._activity_executor = None
    result = worker.create_se_worker(MagicMock(name="client"))
    assert result is fake_worker
    fake_worker_cls.assert_called_once()
    # Cached executor reused on subsequent call
    result2 = worker.create_se_worker(MagicMock())
    assert result2 is fake_worker
    assert fake_worker_cls.call_count == 2


def test_start_se_temporal_worker_thread_disabled(monkeypatch) -> None:
    from software_engineering_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: False)
    assert worker.start_se_temporal_worker_thread() is False


def test_start_se_temporal_worker_thread_starts(monkeypatch) -> None:
    from software_engineering_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    # Stub thread target so we don't actually run a worker
    monkeypatch.setattr(worker, "_worker_thread_target", lambda: None)
    worker._worker_thread = None
    started = worker.start_se_temporal_worker_thread()
    assert started is True
    # Calling again with already-alive thread returns True
    fake_thread = MagicMock()
    fake_thread.is_alive.return_value = True
    worker._worker_thread = fake_thread
    assert worker.start_se_temporal_worker_thread() is True


def test_worker_thread_target_disabled_short_circuits(monkeypatch) -> None:
    from software_engineering_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: False)
    # Should not raise
    worker._worker_thread_target()


def test_worker_thread_target_runs_async(monkeypatch) -> None:
    """When Temporal is enabled, the thread target opens an event loop and
    calls ``_run_worker_async``; failures bubble to the except branch."""
    from software_engineering_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    async def fake_run_async():
        return None

    monkeypatch.setattr(worker, "_run_worker_async", fake_run_async)
    monkeypatch.setattr(worker, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda loop: None)
    worker._worker_thread_target()


def test_worker_thread_target_handles_exception(monkeypatch) -> None:
    from software_engineering_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    async def boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(worker, "_run_worker_async", boom)
    monkeypatch.setattr(worker, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda loop: None)
    worker._worker_thread_target()


@pytest.mark.asyncio
async def test_run_worker_async_no_client(monkeypatch) -> None:
    from software_engineering_team.temporal import worker

    async def no_client():
        return None

    monkeypatch.setattr(worker, "connect_temporal_client", no_client)
    monkeypatch.setattr(worker, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda loop: None)
    monkeypatch.setattr(worker, "create_se_worker", lambda c: None)
    await worker._run_worker_async()


@pytest.mark.asyncio
async def test_run_worker_async_with_worker(monkeypatch) -> None:
    from software_engineering_team.temporal import worker

    fake_client = MagicMock(name="client")

    async def conn():
        return fake_client

    fake_worker = MagicMock()

    async def fake_run():
        return None

    fake_worker.run = fake_run

    monkeypatch.setattr(worker, "connect_temporal_client", conn)
    monkeypatch.setattr(worker, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda loop: None)
    monkeypatch.setattr(worker, "create_se_worker", lambda c: fake_worker)
    await worker._run_worker_async()
