"""Unit tests for the generic off-hot-path sweep primitive (reset_sweep).

ResetSweepState is fully decoupled from provider_store/Postgres via
constructor-injected reset_fn/interval_fn, so every test here constructs a
local instance with fake callables — no provider_store, no fake DB, no
llm_service.provider_store import needed at all. The integration point (that
provider_store's singleton is actually wired to the real reset_entry) is
covered separately in test_provider_store.py.
"""

from __future__ import annotations

import pytest

from llm_service import reset_sweep as reset_sweep_module
from llm_service.reset_sweep import ResetSweepState


class _FakeHeartbeat:
    """Stand-in for shared_concurrency.heartbeat.BackgroundHeartbeat.

    Exercises the real start-once idempotency logic in
    ``ResetSweepState._ensure_started`` without ever spinning up a real OS
    thread — keeps these tests deterministic and avoids leaking daemon
    threads across the test session.
    """

    instances: list["_FakeHeartbeat"] = []

    def __init__(self, beat, interval_s, *, name: str = "") -> None:
        self.beat = beat
        self.interval_s = interval_s
        self.name = name
        self.started = False
        _FakeHeartbeat.instances.append(self)

    def start(self) -> "_FakeHeartbeat":
        self.started = True
        return self

    def stop(self) -> None:
        self.started = False


@pytest.fixture(autouse=True)
def _reset_fake_heartbeat(monkeypatch):
    _FakeHeartbeat.instances.clear()
    monkeypatch.setattr(reset_sweep_module, "BackgroundHeartbeat", _FakeHeartbeat)
    yield
    _FakeHeartbeat.instances.clear()


def _sweep(**overrides) -> ResetSweepState:
    kwargs = {"reset_fn": lambda _id: None, "interval_fn": lambda: 5.0}
    kwargs.update(overrides)
    return ResetSweepState(**kwargs)


def test_enqueue_dedups_same_id():
    sweep = _sweep()
    sweep.enqueue(7)
    sweep.enqueue(7)
    assert sweep.pending_ids == {7}


def test_enqueue_starts_sweep_once():
    sweep = _sweep()
    sweep.enqueue(1)
    sweep.enqueue(2)
    assert len(_FakeHeartbeat.instances) == 1
    assert _FakeHeartbeat.instances[0].started is True
    assert sweep.started is True


def test_ensure_started_is_idempotent():
    sweep = _sweep()
    sweep._ensure_started()
    sweep._ensure_started()
    sweep._ensure_started()
    assert len(_FakeHeartbeat.instances) == 1


def test_tick_drains_pending_and_calls_reset_fn():
    reset_ids: list[int] = []
    sweep = _sweep(reset_fn=lambda i: reset_ids.append(i))
    sweep.pending_ids.update({1, 2, 3})
    sweep.tick()
    assert sorted(reset_ids) == [1, 2, 3]
    assert sweep.pending_ids == set()


def test_tick_noop_when_empty():
    calls = []
    sweep = _sweep(reset_fn=lambda i: calls.append(i))
    sweep.tick()
    assert calls == []


def test_reset_for_test_stops_heartbeat():
    sweep = _sweep()
    sweep.enqueue(1)
    hb = _FakeHeartbeat.instances[0]
    assert hb.started is True
    sweep.reset_for_test()
    assert hb.started is False
    assert sweep.started is False
    assert sweep.pending_ids == set()


def test_ensure_started_passes_resolved_interval_to_heartbeat():
    sweep = _sweep(interval_fn=lambda: 3.0)
    sweep.enqueue(1)
    assert _FakeHeartbeat.instances[0].interval_s == 3.0


def test_ensure_started_floors_interval_at_minimum():
    sweep = _sweep(interval_fn=lambda: 0.0)
    sweep.enqueue(1)
    assert _FakeHeartbeat.instances[0].interval_s == reset_sweep_module._DEFAULT_MIN_INTERVAL_S
    assert _FakeHeartbeat.instances[0].interval_s == 0.1


def test_ensure_started_honors_custom_min_interval():
    sweep = _sweep(interval_fn=lambda: 0.0, min_interval_s=2.0)
    sweep.enqueue(1)
    assert _FakeHeartbeat.instances[0].interval_s == 2.0


def test_ensure_started_uses_custom_name():
    sweep = _sweep(name="my-custom-sweep")
    sweep.enqueue(1)
    assert _FakeHeartbeat.instances[0].name == "my-custom-sweep"
