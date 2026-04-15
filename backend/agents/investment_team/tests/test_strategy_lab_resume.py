"""Regression tests for Strategy Lab resume carry-forward logic.

These tests exercise ``_strategy_lab_worker`` directly (it is a plain
function, not a FastAPI route) with every expensive collaborator patched so
the body runs in-process, deterministically, and without hitting the job
service, the LLM client, or the market-data provider.

The behavior under test: when a run is resumed, the previously-persisted
``skipped_cycles`` must be carried forward. Before the fix the worker set
``skipped = 0`` on entry, so the first post-resume ``_update_run`` call
overwrote the persisted counter with only the new-since-resume count,
making ``/strategy-lab/jobs`` and the UI progress bar move backward for
runs that had ``skipped_cycles > 0`` before failing.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from investment_team.api import main as lab_main
from investment_team.models import (
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    StrategyLabRecord,
    StrategySpec,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_record(record_id: str) -> StrategyLabRecord:
    """Minimal StrategyLabRecord suitable for the worker's post-cycle bookkeeping."""
    strategy = StrategySpec(
        strategy_id=f"strat-{record_id}",
        authored_by="test",
        asset_class="equities",
        hypothesis="test hypothesis",
        signal_definition="test signal",
        entry_rules=[f"entry-{record_id}"],
        exit_rules=[f"exit-{record_id}"],
    )
    backtest = BacktestRecord(
        backtest_id=f"bt-{record_id}",
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        config=BacktestConfig(start_date="2021-01-01", end_date="2024-12-31"),
        submitted_by="test",
        submitted_at="2026-04-15T00:00:00+00:00",
        completed_at="2026-04-15T00:00:01+00:00",
        result=BacktestResult(
            total_return_pct=10.0,
            annualized_return_pct=9.0,
            volatility_pct=5.0,
            sharpe_ratio=1.2,
            max_drawdown_pct=-4.0,
            win_rate_pct=55.0,
            profit_factor=1.5,
        ),
    )
    return StrategyLabRecord(
        lab_record_id=record_id,
        strategy=strategy,
        backtest=backtest,
        is_winning=False,
        strategy_rationale="test",
        analysis_narrative="test",
        created_at="2026-04-15T00:00:02+00:00",
    )


class _PersistSpy:
    """Thread-safe capture of every _persist_run_state invocation."""

    def __init__(self) -> None:
        self.snapshots: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, run_id: str, state: Dict[str, Any], *, create: bool = False) -> None:
        # Store a shallow copy so later mutations don't retro-rewrite history.
        with self._lock:
            self.snapshots.append(dict(state))


class _FakeTimer:
    """Stand-in for threading.Timer that never actually fires."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.daemon = False

    def start(self) -> None:  # pragma: no cover - no-op
        pass


@pytest.fixture
def patched_worker_env(monkeypatch: pytest.MonkeyPatch):
    """Patch the worker's heavyweight collaborators.

    Returns a dict with ``run_id``, ``prior_ids``, and the persist ``spy``.
    Seeds ``_active_runs[run_id]`` with the mid-run state a real resume
    would have repopulated — 3 completed cycles and 2 skipped cycles out of
    a 10-cycle (2-batch x 5) run with ``contiguous_cycles == 3``.
    """
    run_id = "run-test-resume-carry-forward"
    prior_ids = ["r1", "r2", "r3"]

    # Mid-run snapshot mirroring what resume_strategy_lab_run writes into
    # _active_runs before launching the worker thread.
    lab_main._active_runs[run_id] = {
        "run_id": run_id,
        "status": "running",
        "started_at": "2026-04-15T00:00:00+00:00",
        "total_cycles": 10,
        "completed_cycles": 3,
        "contiguous_cycles": 3,
        "skipped_cycles": 2,
        "current_cycle": None,
        "completed_record_ids": list(prior_ids),
        "error": None,
        "batch_size": 5,
        "batch_count": 2,
        "completed_batches": 0,
        "current_batch": None,
    }

    spy = _PersistSpy()
    monkeypatch.setattr(lab_main, "_persist_run_state", spy)
    monkeypatch.setattr(lab_main, "_strategy_lab_signal_expert_enabled", lambda: False)
    # Avoid any real job-service traffic on cancellation polling.
    monkeypatch.setattr(
        lab_main,
        "_get_lab_run_job_client",
        lambda: MagicMock(get_job=MagicMock(return_value=None)),
    )
    # The worker imports publish / cleanup_job lazily inside its body.
    import investment_team.api.job_event_bus as bus

    monkeypatch.setattr(bus, "publish", lambda *a, **kw: None)
    monkeypatch.setattr(bus, "cleanup_job", lambda *a, **kw: None)

    # Don't leave a real 5-minute cleanup Timer dangling after each test.
    monkeypatch.setattr(lab_main.threading, "Timer", _FakeTimer)

    try:
        yield {"run_id": run_id, "prior_ids": prior_ids, "spy": spy}
    finally:
        lab_main._active_runs.pop(run_id, None)


def _build_request() -> "lab_main.RunStrategyLabRequest":
    """Request with ``max_parallel=1`` so waves run one cycle at a time
    (deterministic ordering for assertions)."""
    return lab_main.RunStrategyLabRequest(
        batch_size=5,
        batch_count=2,
        max_parallel=1,
        paper_trading_enabled=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_resume_carries_forward_skipped_cycles(
    patched_worker_env: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skip encountered after resume must *add* to the pre-resume skipped count."""
    run_id = patched_worker_env["run_id"]
    prior_ids = patched_worker_env["prior_ids"]
    spy: _PersistSpy = patched_worker_env["spy"]

    call_idx = {"n": 0}
    call_lock = threading.Lock()

    def fake_cycle(_config, _orchestrator, **_kwargs):
        with call_lock:
            call_idx["n"] += 1
            i = call_idx["n"]
        # First resumed cycle skips (HTTP 502), every remaining cycle succeeds.
        if i == 1:
            raise HTTPException(status_code=502, detail="no market data")
        return _make_record(f"new-{i}")

    monkeypatch.setattr(lab_main, "_run_one_strategy_lab_cycle", fake_cycle)

    # Resume offset = 3 (contiguous completed cycles pre-crash).
    lab_main._strategy_lab_worker(run_id, _build_request(), start_cycle_offset=3)

    final_state = lab_main._active_runs[run_id]
    # 2 prior skips + 1 new skip = 3. Before the fix this was just 1.
    assert final_state["skipped_cycles"] == 3, (
        f"resume must carry forward prior skipped_cycles; got {final_state['skipped_cycles']}"
    )
    # 3 prior completions + 6 new successes = 9 (cycle 4 skipped, 5..10 succeed).
    assert len(final_state["completed_record_ids"]) == 9
    assert final_state["completed_record_ids"][:3] == prior_ids
    assert final_state["status"] == "completed"

    # The last persisted snapshot must reflect the final skipped count too —
    # i.e. the carry-forward is visible to anything polling the job service.
    final_snapshot = spy.snapshots[-1]
    assert final_snapshot["skipped_cycles"] == 3


def test_resume_progress_never_moves_backward(
    patched_worker_env: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Across every persisted snapshot, completed_cycles and skipped_cycles
    must be monotonically non-decreasing from the pre-resume values."""
    run_id = patched_worker_env["run_id"]

    counter = {"n": 0}
    counter_lock = threading.Lock()

    def fake_cycle(_config, _orchestrator, **_kwargs):
        with counter_lock:
            counter["n"] += 1
            i = counter["n"]
        return _make_record(f"new-{i}")

    monkeypatch.setattr(lab_main, "_run_one_strategy_lab_cycle", fake_cycle)

    lab_main._strategy_lab_worker(run_id, _build_request(), start_cycle_offset=3)

    spy: _PersistSpy = patched_worker_env["spy"]
    last_completed = 3  # pre-resume floor
    last_skipped = 2  # pre-resume floor
    for snap in spy.snapshots:
        completed = snap.get("completed_cycles", last_completed)
        skipped = snap.get("skipped_cycles", last_skipped)
        assert completed >= last_completed, (
            f"completed_cycles moved backward: {last_completed} -> {completed}"
        )
        assert skipped >= last_skipped, (
            f"skipped_cycles moved backward: {last_skipped} -> {skipped}"
        )
        last_completed = completed
        last_skipped = skipped

    # Sanity: final state reached the expected totals.
    assert last_completed == 3 + 7  # 3 prior + 7 new successful cycles (4..10)
    assert last_skipped == 2  # no post-resume skips in this scenario
