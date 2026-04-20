"""Tests for sequential multi-batch execution in the Strategy Lab worker.

Covers the user-configurable ``batch_size`` and ``batch_count`` knobs added to
``RunStrategyLabRequest``: each batch must run after the previous one finishes,
each new strategy must see all prior strategies, and the signal-intelligence
brief must be regenerated once per batch (not once per run).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import pytest

# NOTE: import via the same module path used inside ``main.py`` so Pydantic
# treats the model classes as identical (otherwise ``agents.investment_team.X``
# and ``investment_team.X`` are two distinct module objects with two distinct
# class identities, and Pydantic v2 rejects cross-instance model assignment).
from investment_team.api import main as lab_main  # noqa: E402
from investment_team.api.main import (  # noqa: E402
    RunStrategyLabRequest,
    _strategy_lab_worker,
)
from investment_team.models import (  # noqa: E402
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    StrategyLabRecord,
    StrategySpec,
)


def _stub_backtest_result() -> BacktestResult:
    return BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=5.0,
        volatility_pct=12.0,
        sharpe_ratio=0.5,
        max_drawdown_pct=-3.0,
        win_rate_pct=55.0,
        profit_factor=1.2,
    )


def _make_record(idx: int, config: BacktestConfig) -> StrategyLabRecord:
    """Build a fully-populated StrategyLabRecord stub for cycle ``idx``."""
    strategy_id = f"strat-test-{idx:04d}-{uuid.uuid4().hex[:6]}"
    backtest_id = f"bt-test-{idx:04d}-{uuid.uuid4().hex[:6]}"
    lab_record_id = f"lab-test-{idx:04d}-{uuid.uuid4().hex[:6]}"
    strategy = StrategySpec(
        strategy_id=strategy_id,
        authored_by="test",
        asset_class="stocks",
        hypothesis=f"hypothesis #{idx}",
        signal_definition="sig",
        entry_rules=["e"],
        exit_rules=["x"],
        sizing_rules=["s"],
        risk_limits={},
        speculative=False,
    )
    now = lab_main._now()
    backtest = BacktestRecord(
        backtest_id=backtest_id,
        strategy_id=strategy_id,
        strategy=strategy,
        config=config,
        submitted_by="test",
        submitted_at=now,
        completed_at=now,
        result=_stub_backtest_result(),
        notes=[],
        trades=[],
    )
    return StrategyLabRecord(
        lab_record_id=lab_record_id,
        strategy=strategy,
        backtest=backtest,
        is_winning=False,  # avoid paper-trading branch
        strategy_rationale="r",
        analysis_narrative="ok",
        created_at=now,
        quality_gate_results=[],
    )


@pytest.fixture
def empty_lab_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the persistent stores with plain dicts and reset run state."""
    monkeypatch.setattr(lab_main, "_strategy_lab_records", {})
    monkeypatch.setattr(lab_main, "_strategies", {})
    monkeypatch.setattr(lab_main, "_backtests", {})
    monkeypatch.setattr(lab_main, "_active_runs", {})


def _seed_run_state(run_id: str, request: RunStrategyLabRequest) -> None:
    lab_main._active_runs[run_id] = {
        "run_id": run_id,
        "status": "running",
        "started_at": lab_main._now(),
        "total_cycles": request.batch_size * request.batch_count,
        "completed_cycles": 0,
        "skipped_cycles": 0,
        "current_cycle": None,
        "completed_record_ids": [],
        "error": None,
        "request_payload": request.model_dump(),
        "batch_size": request.batch_size,
        "batch_count": request.batch_count,
        "completed_batches": 0,
        "current_batch": None,
    }


def test_multi_batch_run_completes_all_cycles_and_learns_from_priors(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """batch_size=2, batch_count=3, max_parallel=1 → 6 strategies; each cycle
    sees all priors; signal brief regenerates once per batch."""

    cycle_calls: List[Dict[str, Any]] = []

    class _StubOrchestrator:
        """Replaces StrategyLabOrchestrator. ``run_cycle`` records the priors it sees."""

        _counter = 0

        def __init__(self, convergence_tracker: Any = None) -> None:
            self.convergence_tracker = _StubTracker()

        def run_cycle(
            self,
            prior_records: List[StrategyLabRecord],
            config: BacktestConfig,
            signal_brief: Any = None,
            on_phase: Any = None,
            exclude_asset_classes: Any = None,
        ) -> StrategyLabRecord:
            type(self)._counter += 1
            idx = type(self)._counter
            cycle_calls.append(
                {
                    "n_priors": len(prior_records),
                    "had_brief": signal_brief is not None,
                }
            )
            return _make_record(idx, config)

    class _StubTracker:
        def snapshot(self) -> "_StubTracker":
            return _StubTracker()

        def record(self, *_a: Any, **_kw: Any) -> None:
            pass

    monkeypatch.setattr(lab_main, "StrategyLabOrchestrator", _StubOrchestrator)
    monkeypatch.setattr(lab_main, "ConvergenceTracker", _StubTracker)
    monkeypatch.setattr(lab_main, "_strategy_lab_signal_expert_enabled", lambda: False)
    monkeypatch.setattr(lab_main, "_persist_run_state", lambda *a, **kw: None)

    # Force strictly-sequential execution so each cycle definitely sees priors
    # from every previous cycle in the same run (otherwise cycles in the same
    # parallel wave see the same prior set).
    request = RunStrategyLabRequest(
        batch_size=2,
        batch_count=3,
        max_parallel=1,
        paper_trading_enabled=False,
    )
    run_id = "run-test-multi"
    _seed_run_state(run_id, request)

    _strategy_lab_worker(run_id, request)

    state = lab_main._active_runs[run_id]
    assert state["status"] == "completed", state
    assert len(lab_main._strategy_lab_records) == 6
    assert len(cycle_calls) == 6
    assert state["completed_cycles"] == 6
    assert state["completed_batches"] == 3
    assert len(state["completed_record_ids"]) == 6

    # With max_parallel=1, each cycle sees N-1 priors.
    for i, call in enumerate(cycle_calls):
        assert call["n_priors"] == i, (
            f"cycle {i + 1} should see {i} priors but got {call['n_priors']}"
        )


def test_signal_brief_regenerates_once_per_batch(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the signal expert is enabled, the brief is rebuilt at every batch start
    so batch N+1 sees the records produced by batches 1..N."""

    expert_invocations: List[int] = []

    class _StubProvider:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def fetch_context(self, _req: Any) -> Any:
            from investment_team.market_lab_data.models import MarketLabContext

            return MarketLabContext(fetched_at=lab_main._now(), degraded=False, sources_used=[])

        def close(self) -> None:
            pass

    class _StubExpert:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def produce_signal_brief(self, prior_records: Any, _market: Any) -> Any:
            expert_invocations.append(len(prior_records))
            from investment_team.signal_intelligence_models import SignalIntelligenceBriefV1

            return SignalIntelligenceBriefV1(
                brief_version=1,
                macro_themes=[],
                micro_themes=[],
                high_value_signal_hypotheses=[],
                trade_structures_benefiting=[],
                pairing_guidance="",
                evidence_from_priors="",
                evidence_from_market_data="",
                confidence="medium",
                unsupported_claims=[],
            )

    class _StubTracker:
        def snapshot(self) -> "_StubTracker":
            return _StubTracker()

        def record(self, *_a: Any, **_kw: Any) -> None:
            pass

    class _StubOrchestrator:
        _counter = 0

        def __init__(self, convergence_tracker: Any = None) -> None:
            self.convergence_tracker = _StubTracker()

        def run_cycle(
            self,
            prior_records: List[StrategyLabRecord],
            config: BacktestConfig,
            signal_brief: Any = None,
            on_phase: Any = None,
            exclude_asset_classes: Optional[List[str]] = None,
        ) -> StrategyLabRecord:
            type(self)._counter += 1
            return _make_record(type(self)._counter, config)

    monkeypatch.setattr(lab_main, "StrategyLabOrchestrator", _StubOrchestrator)
    monkeypatch.setattr(lab_main, "ConvergenceTracker", _StubTracker)
    monkeypatch.setattr(lab_main, "FreeTierMarketDataProvider", _StubProvider)
    monkeypatch.setattr(lab_main, "SignalIntelligenceExpert", _StubExpert)
    monkeypatch.setattr(lab_main, "_strategy_lab_signal_expert_enabled", lambda: True)
    monkeypatch.setattr(lab_main, "_persist_run_state", lambda *a, **kw: None)

    request = RunStrategyLabRequest(
        batch_size=2,
        batch_count=3,
        max_parallel=1,
        paper_trading_enabled=False,
    )
    run_id = "run-test-brief"
    _seed_run_state(run_id, request)

    _strategy_lab_worker(run_id, request)

    state = lab_main._active_runs[run_id]
    assert state["status"] == "completed", state
    assert len(lab_main._strategy_lab_records) == 6
    # Brief is generated exactly once per batch — 3 batches → 3 invocations.
    assert len(expert_invocations) == 3
    # Each new batch's brief sees the priors written by every previous batch.
    assert expert_invocations == [0, 2, 4]


def test_total_cycles_is_batch_size_times_batch_count(empty_lab_state: None) -> None:
    """Sanity check: the request validates and computes total work correctly."""
    request = RunStrategyLabRequest(batch_size=5, batch_count=4)
    assert request.batch_size * request.batch_count == 20

    # Field bounds remain enforced. batch_count upper bound is the operator-
    # tunable _MAX_BATCH_COUNT (default 100 via STRATEGY_LAB_MAX_BATCH_COUNT).
    with pytest.raises(Exception):
        RunStrategyLabRequest(batch_size=0)
    with pytest.raises(Exception):
        RunStrategyLabRequest(batch_count=0)
    with pytest.raises(Exception):
        RunStrategyLabRequest(batch_count=lab_main._MAX_BATCH_COUNT + 1)
