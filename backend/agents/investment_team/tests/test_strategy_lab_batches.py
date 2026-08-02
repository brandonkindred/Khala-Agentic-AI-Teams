"""Tests for sequential multi-batch execution in the Strategy Lab worker.

Covers the user-configurable ``batch_size`` and ``batch_count`` knobs added to
``RunStrategyLabRequest``: each batch must run after the previous one finishes,
each new strategy must see all prior strategies, and the signal-intelligence
brief must be regenerated once per batch (not once per run).
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Dict, List, Optional

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

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
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    Predicate,
    StopLossRule,
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
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
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
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[StopLossRule(pct=0.03)],
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
    from investment_team.strategy_lab import run_state as _run_state

    monkeypatch.setattr(lab_main, "_strategy_lab_records", {})
    monkeypatch.setattr(lab_main, "_strategies", {})
    monkeypatch.setattr(lab_main, "_backtests", {})
    # Patch both the ``api.main`` alias and the source-module attribute to the
    # *same* dict, so direct reads/writes (routes, ``_seed_run_state``) and
    # ``_get_run_state`` (which closes over ``run_state.active_runs``) share one
    # store. Rebinding only one name leaves resume/restart lookups missing the
    # seeded run.
    shared_runs: Dict[str, Any] = {}
    monkeypatch.setattr(lab_main, "_active_runs", shared_runs)
    monkeypatch.setattr(_run_state, "active_runs", shared_runs)


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

        def merge_from(self, *_a: Any, **_kw: Any) -> None:
            # Issue #269 — wave-completion loop now calls merge_from on the
            # primary for each cycle snapshot; stub is a no-op.
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

        def merge_from(self, *_a: Any, **_kw: Any) -> None:
            # Issue #269 — wave-completion loop now calls merge_from on the
            # primary for each cycle snapshot; stub is a no-op.
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
    with pytest.raises(ValidationError):
        RunStrategyLabRequest(batch_size=0)
    with pytest.raises(ValidationError):
        RunStrategyLabRequest(batch_count=0)
    with pytest.raises(ValidationError):
        RunStrategyLabRequest(batch_count=lab_main._MAX_BATCH_COUNT + 1)


def test_parallel_wave_merges_trial_counts_into_primary_tracker(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #269 — after a parallel wave completes, the primary tracker's
    trial_count must equal the sum of per-cycle refinement rounds, not just
    the primary's snapshot-time value (which would stay at 0)."""

    # Each cycle simulates this many refinement rounds on its own snapshot.
    trials_per_cycle = 7

    instances: List["_CapturingOrchestrator"] = []

    class _CapturingOrchestrator:
        """Passes ``convergence_tracker`` through unchanged so the real
        ``ConvergenceTracker.merge_from`` path is exercised end-to-end.
        The first instance receives the primary tracker; subsequent
        instances receive snapshots built at wave submission time."""

        _counter = 0

        def __init__(self, convergence_tracker: Any = None) -> None:
            self.convergence_tracker = convergence_tracker
            instances.append(self)

        def run_cycle(
            self,
            prior_records: List[StrategyLabRecord],
            config: BacktestConfig,
            signal_brief: Any = None,
            on_phase: Any = None,
            exclude_asset_classes: Any = None,
        ) -> StrategyLabRecord:
            # Simulate refinement-loop trial increments on the cycle snapshot.
            if self.convergence_tracker is not None:
                self.convergence_tracker.increment_trials(trials_per_cycle)
            type(self)._counter += 1
            return _make_record(type(self)._counter, config)

    monkeypatch.setattr(lab_main, "StrategyLabOrchestrator", _CapturingOrchestrator)
    # NB: intentionally do NOT monkeypatch ConvergenceTracker — we want the
    # real class so merge_from is exercised end-to-end.
    monkeypatch.setattr(lab_main, "_strategy_lab_signal_expert_enabled", lambda: False)
    monkeypatch.setattr(lab_main, "_persist_run_state", lambda *a, **kw: None)

    request = RunStrategyLabRequest(
        batch_size=3,
        batch_count=1,
        max_parallel=3,  # truly parallel wave
        paper_trading_enabled=False,
    )
    run_id = "run-test-parallel-merge"
    _seed_run_state(run_id, request)

    _strategy_lab_worker(run_id, request)

    state = lab_main._active_runs[run_id]
    assert state["status"] == "completed", state

    # The first constructed orchestrator holds the primary tracker
    # (constructed with a fresh ConvergenceTracker at worker entry).
    assert len(instances) == 1 + 3  # primary + one per cycle
    primary_tracker = instances[0].convergence_tracker
    assert primary_tracker is not None

    # Before the fix: primary_tracker.trial_count == 0 (snapshots incremented
    # their own copies and were discarded). After the fix: 3 cycles × 7 =
    # 21 trials merged back into the primary.
    assert primary_tracker.trial_count == 3 * trials_per_cycle

    # Sanity: each cycle's snapshot tracker also still shows its own count
    # (merge_from does not mutate the snapshot).
    for cycle_orch in instances[1:]:
        # Snapshot started at baseline 0, was incremented trials_per_cycle.
        assert cycle_orch.convergence_tracker.trial_count == trials_per_cycle


# ---------------------------------------------------------------------------
# Resilience: per-cycle exceptions no longer halt the whole run.
# ---------------------------------------------------------------------------


class _NoopTracker:
    """Minimal tracker stub shared by the resilience tests."""

    def snapshot(self) -> "_NoopTracker":
        return _NoopTracker()

    def record(self, *_a: Any, **_kw: Any) -> None:
        pass

    def merge_from(self, *_a: Any, **_kw: Any) -> None:
        pass


def _install_publish_captor(
    monkeypatch: pytest.MonkeyPatch,
    on_event: Optional[Callable[[Optional[str]], None]] = None,
) -> List[Dict[str, Any]]:
    """Patch the job event bus to capture every published SSE event.

    Preconditions:
        ``monkeypatch`` is the active fixture; ``on_event`` (optional) is invoked
        with each event's ``event_type`` after capture — e.g. to release a
        threading gate on a specific type.
    Postconditions:
        ``investment_team.api.job_event_bus.publish`` is patched to append
        ``{**event, "type": event_type}`` to the returned list (empty at call
        time); returns that list. Never raises.
    """
    published: List[Dict[str, Any]] = []

    def _capture(job_id: str, event: Dict[str, Any], *, event_type: Optional[str] = None) -> None:
        published.append({**event, "type": event_type})
        if on_event is not None:
            on_event(event_type)

    from investment_team.api import job_event_bus as _job_event_bus

    monkeypatch.setattr(_job_event_bus, "publish", _capture)
    return published


def _make_succeeding_orchestrator() -> type:
    """Return a fresh ``StrategyLabOrchestrator`` stub whose ``run_cycle`` always
    succeeds, producing a new record per call.

    Preconditions: none.
    Postconditions: returns a NEW class each call (its class-level ``_counter``
        starts at 0), so tests never share cycle-count state; each instance's
        ``convergence_tracker`` is a fresh ``_NoopTracker``.
    """

    class _SucceedingOrchestrator:
        _counter = 0

        def __init__(self, convergence_tracker: Any = None) -> None:
            self.convergence_tracker = _NoopTracker()

        def run_cycle(
            self,
            prior_records: List[StrategyLabRecord],
            config: BacktestConfig,
            signal_brief: Any = None,
            on_phase: Any = None,
            exclude_asset_classes: Any = None,
        ) -> StrategyLabRecord:
            type(self)._counter += 1
            return _make_record(type(self)._counter, config)

    return _SucceedingOrchestrator


def test_unexpected_cycle_exception_does_not_halt_run(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single cycle raising an unexpected error must be surfaced as an errored
    cycle and the remaining batches must still run to completion."""

    call_counter = {"n": 0}

    class _OneBadCycleOrchestrator:
        _counter = 0

        def __init__(self, convergence_tracker: Any = None) -> None:
            self.convergence_tracker = _NoopTracker()

        def run_cycle(
            self,
            prior_records: List[StrategyLabRecord],
            config: BacktestConfig,
            signal_brief: Any = None,
            on_phase: Any = None,
            exclude_asset_classes: Any = None,
        ) -> StrategyLabRecord:
            call_counter["n"] += 1
            # Fail cycle #2 with a totally unexpected exception.
            if call_counter["n"] == 2:
                raise RuntimeError("boom — downstream provider exploded")
            type(self)._counter += 1
            return _make_record(type(self)._counter, config)

    monkeypatch.setattr(lab_main, "StrategyLabOrchestrator", _OneBadCycleOrchestrator)
    monkeypatch.setattr(lab_main, "ConvergenceTracker", _NoopTracker)
    monkeypatch.setattr(lab_main, "_strategy_lab_signal_expert_enabled", lambda: False)
    monkeypatch.setattr(lab_main, "_persist_run_state", lambda *a, **kw: None)

    request = RunStrategyLabRequest(
        batch_size=2,
        batch_count=3,
        max_parallel=1,
        paper_trading_enabled=False,
    )
    run_id = "run-test-errored"
    _seed_run_state(run_id, request)

    _strategy_lab_worker(run_id, request)

    state = lab_main._active_runs[run_id]
    # Pre-fix regression: status would be "failed" and the loop would have
    # broken after batch 1 (~1 completed cycle). Post-fix: run continues.
    assert state["status"] == "completed_with_errors", state
    assert state["completed_cycles"] == 5  # 6 total - 1 errored
    assert state["errored_cycles"] == 1
    assert state["completed_batches"] == 3
    assert len(state["errored_details"]) == 1
    detail = state["errored_details"][0]
    assert detail["cycle_index"] == 2
    assert detail["batch_index"] == 1
    assert detail["exception_type"] == "RuntimeError"
    assert "boom" in detail["error"]


def test_concurrent_deep_failures_in_one_wave_publish_only_one_terminal_error(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: two concurrent cycles in one wave both deep-fail (non-502
    HTTPException). as_completed() yields the wave's futures one at a time to
    this single consumer, so only the FIRST deep failure publishes the run's
    terminal 'error' event (guarded on run_failed); the second is logged but
    must not publish a second terminal event."""

    class _AllCyclesFailOrchestrator:
        def __init__(self, convergence_tracker: Any = None) -> None:
            self.convergence_tracker = _NoopTracker()

        def run_cycle(
            self,
            prior_records: List[StrategyLabRecord],
            config: BacktestConfig,
            signal_brief: Any = None,
            on_phase: Any = None,
            exclude_asset_classes: Any = None,
        ) -> StrategyLabRecord:
            raise HTTPException(status_code=500, detail="downstream provider unavailable")

    published = _install_publish_captor(monkeypatch)
    monkeypatch.setattr(lab_main, "StrategyLabOrchestrator", _AllCyclesFailOrchestrator)
    monkeypatch.setattr(lab_main, "ConvergenceTracker", _NoopTracker)
    monkeypatch.setattr(lab_main, "_strategy_lab_signal_expert_enabled", lambda: False)
    monkeypatch.setattr(lab_main, "_persist_run_state", lambda *a, **kw: None)

    # A single wave of 2 concurrent cycles, both hitting the deep-failure path.
    request = RunStrategyLabRequest(
        batch_size=2,
        batch_count=1,
        max_parallel=2,
        paper_trading_enabled=False,
    )
    run_id = "run-test-concurrent-deep-failure"
    _seed_run_state(run_id, request)

    _strategy_lab_worker(run_id, request)

    state = lab_main._active_runs[run_id]
    assert state["status"] == "failed"

    error_events = [e for e in published if e["type"] == "error"]
    assert len(error_events) == 1, f"expected exactly one terminal 'error' event, got {error_events}"


def test_deep_failure_skips_post_wave_tracker_merge(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression covering three guarantees for a wave where one cycle succeeds
    and a sibling deep-fails (non-502 HTTPException):

    1. The post-wave tracker-merge loop is skipped once run_failed is set, so a
       merge failure can't publish a second, post-terminal ``cycle_errored``
       (reason ``tracker_merge_failed``) after the run's one terminal 'error'.
    2. The succeeding sibling is STILL counted (its record in
       ``completed_record_ids``) — the loop drains every future rather than
       breaking, so a resume of the failed run never re-runs and duplicates it.
    3. The terminal state carries no stale ``current_cycle`` (each drained
       cycle's handler clears it).
    """
    import threading

    ordering_lock = threading.Lock()
    call_order = {"n": 0}
    # Set by _capture_publish the instant the succeeding sibling's cycle_complete
    # is published, so the deep-failing sibling only raises AFTER the succeeding
    # one has been collected into wave_results — making the merge-skip assertion
    # deterministic (the sibling is guaranteed present at the guard).
    sibling_cycle_complete = threading.Event()

    class _RaisingMergeTracker(_NoopTracker):
        """Primary tracker whose merge_from always raises, so any post-wave
        merge attempt would loudly publish a tracker_merge_failed event."""

        def merge_from(self, *_a: Any, **_kw: Any) -> None:
            raise ValueError("merge boom — must never be reached after a deep failure")

    class _OneSucceedsOneDeepFailsOrchestrator:
        def __init__(self, convergence_tracker: Any = None) -> None:
            self.convergence_tracker = _RaisingMergeTracker()

        def run_cycle(
            self,
            prior_records: List[StrategyLabRecord],
            config: BacktestConfig,
            signal_brief: Any = None,
            on_phase: Any = None,
            exclude_asset_classes: Any = None,
        ) -> StrategyLabRecord:
            # Assign roles under a lock so the two concurrent cycles can't race:
            # the first to enter succeeds; the second blocks until the first has
            # been collected (its cycle_complete published) and only then hits
            # the deep-failure path — guaranteeing the succeeding sibling is in
            # wave_results when run_failed is set and the guard runs.
            with ordering_lock:
                call_order["n"] += 1
                me = call_order["n"]
            if me == 1:
                return _make_record(me, config)
            assert sibling_cycle_complete.wait(timeout=5), "sibling cycle_complete was never published"
            raise HTTPException(status_code=500, detail="downstream provider unavailable")

    published = _install_publish_captor(
        monkeypatch,
        on_event=lambda et: sibling_cycle_complete.set() if et == "cycle_complete" else None,
    )
    monkeypatch.setattr(lab_main, "StrategyLabOrchestrator", _OneSucceedsOneDeepFailsOrchestrator)
    monkeypatch.setattr(lab_main, "ConvergenceTracker", _NoopTracker)
    monkeypatch.setattr(lab_main, "_strategy_lab_signal_expert_enabled", lambda: False)
    monkeypatch.setattr(lab_main, "_persist_run_state", lambda *a, **kw: None)

    # A single wave of 2 concurrent cycles: one succeeds, one deep-fails.
    request = RunStrategyLabRequest(
        batch_size=2,
        batch_count=1,
        max_parallel=2,
        paper_trading_enabled=False,
    )
    run_id = "run-test-deep-failure-skips-merge"
    _seed_run_state(run_id, request)

    _strategy_lab_worker(run_id, request)

    state = lab_main._active_runs[run_id]
    assert state["status"] == "failed"

    # (1) Exactly one terminal 'error' event, and NO post-terminal tracker-merge
    # cycle_errored — the merge loop was skipped once run_failed was set.
    assert len([e for e in published if e["type"] == "error"]) == 1
    tracker_merge_events = [
        e for e in published if e["type"] == "cycle_errored" and e.get("reason") == "tracker_merge_failed"
    ]
    assert not tracker_merge_events, f"tracker merge published after terminal failure: {tracker_merge_events}"
    assert state.get("tracker_merge_error_count", 0) == 0

    # (2) The succeeding sibling is still counted despite the sibling's deep
    # failure — the loop drained its future rather than abandoning it, so a
    # resume would not re-run (and duplicate) it.
    assert len(state["completed_record_ids"]) == 1
    assert state["completed_cycles"] == 1
    cycle_complete_events = [e for e in published if e["type"] == "cycle_complete"]
    assert len(cycle_complete_events) == 1

    # (3) The terminal 'failed' state carries no stale in-progress cycle.
    assert state["current_cycle"] is None


def test_merge_from_failure_does_not_halt_run(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If primary_tracker.merge_from raises (e.g. negative trial delta), the run
    must still complete — only that cycle's merge is dropped."""

    class _MergeFailingTracker(_NoopTracker):
        calls = {"merge": 0}

        def merge_from(self, *_a: Any, **_kw: Any) -> None:
            _MergeFailingTracker.calls["merge"] += 1
            # Second merge blows up; the run must still keep going.
            if _MergeFailingTracker.calls["merge"] == 2:
                raise ValueError("synthetic negative delta")

    class _Orch:
        _counter = 0

        def __init__(self, convergence_tracker: Any = None) -> None:
            self.convergence_tracker = _MergeFailingTracker()

        def run_cycle(
            self,
            prior_records: List[StrategyLabRecord],
            config: BacktestConfig,
            signal_brief: Any = None,
            on_phase: Any = None,
            exclude_asset_classes: Any = None,
        ) -> StrategyLabRecord:
            type(self)._counter += 1
            return _make_record(type(self)._counter, config)

    monkeypatch.setattr(lab_main, "StrategyLabOrchestrator", _Orch)
    monkeypatch.setattr(lab_main, "ConvergenceTracker", _MergeFailingTracker)
    monkeypatch.setattr(lab_main, "_strategy_lab_signal_expert_enabled", lambda: False)
    monkeypatch.setattr(lab_main, "_persist_run_state", lambda *a, **kw: None)

    request = RunStrategyLabRequest(
        batch_size=3,
        batch_count=1,
        max_parallel=1,
        paper_trading_enabled=False,
    )
    run_id = "run-test-merge-fail"
    _seed_run_state(run_id, request)

    _strategy_lab_worker(run_id, request)

    state = lab_main._active_runs[run_id]
    # All 3 cycles produced records; one merge failed but that's non-fatal.
    assert state["status"] == "completed_with_errors", state
    assert state["completed_cycles"] == 3
    assert state["errored_cycles"] == 1
    assert state["completed_batches"] == 1
    assert state["errored_details"][0].get("reason") == "tracker_merge_failed"
    assert state["tracker_merge_error_count"] == 1


def test_cancelled_run_publishes_distinct_cancelled_event(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user cancellation publishes its own terminal `cancelled` SSE event
    type (not folded into `error`), mirroring the blogging team's own
    cancelled-job publish, so SSE consumers can tell a deliberate stop apart
    from a genuine failure by `type` alone."""

    _Orch = _make_succeeding_orchestrator()

    published = _install_publish_captor(monkeypatch)
    monkeypatch.setattr(lab_main, "StrategyLabOrchestrator", _Orch)
    monkeypatch.setattr(lab_main, "ConvergenceTracker", _NoopTracker)
    monkeypatch.setattr(lab_main, "_strategy_lab_signal_expert_enabled", lambda: False)
    monkeypatch.setattr(lab_main, "_persist_run_state", lambda *a, **kw: None)
    # Cancelled between waves, after the (only) wave in this single-cycle run.
    monkeypatch.setattr(lab_main, "_strategy_lab_external_terminal_status", lambda run_id: "cancelled")

    request = RunStrategyLabRequest(
        batch_size=1,
        batch_count=1,
        max_parallel=1,
        paper_trading_enabled=False,
    )
    run_id = "run-test-cancelled"
    _seed_run_state(run_id, request)

    _strategy_lab_worker(run_id, request)

    state = lab_main._active_runs[run_id]
    assert state["status"] == "cancelled"

    assert not [e for e in published if e["type"] == "error"]
    cancelled_events = [e for e in published if e["type"] == "cancelled"]
    assert len(cancelled_events) == 1
    assert cancelled_events[0]["detail"] == "Run cancelled by user"


@pytest.mark.parametrize("external_status", ["interrupted", "failed"])
def test_externally_interrupted_or_failed_run_is_not_mislabeled_cancelled(
    external_status: str, empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: _STRATEGY_LAB_CANCEL_STATUSES also includes "failed" and
    "interrupted" (e.g. a service-wide "mark all interrupted" reconciliation
    hitting a still-running job), but the between-wave check used to
    unconditionally overwrite the run's status with "cancelled" and publish
    a "Run cancelled by user" detail regardless of the true external cause —
    mislabeling both the live SSE event and the persisted record."""

    _Orch = _make_succeeding_orchestrator()

    published = _install_publish_captor(monkeypatch)
    monkeypatch.setattr(lab_main, "StrategyLabOrchestrator", _Orch)
    monkeypatch.setattr(lab_main, "ConvergenceTracker", _NoopTracker)
    monkeypatch.setattr(lab_main, "_strategy_lab_signal_expert_enabled", lambda: False)
    monkeypatch.setattr(lab_main, "_persist_run_state", lambda *a, **kw: None)
    monkeypatch.setattr(
        lab_main, "_strategy_lab_external_terminal_status", lambda run_id: external_status
    )

    request = RunStrategyLabRequest(
        batch_size=1,
        batch_count=1,
        max_parallel=1,
        paper_trading_enabled=False,
    )
    run_id = f"run-test-{external_status}"
    _seed_run_state(run_id, request)

    _strategy_lab_worker(run_id, request)

    state = lab_main._active_runs[run_id]
    # The true external status survives — not silently overwritten to "cancelled".
    assert state["status"] == external_status

    assert not [e for e in published if e["type"] == "cancelled"]
    error_events = [e for e in published if e["type"] == "error"]
    assert len(error_events) == 1
    assert "cancelled by user" not in error_events[0]["detail"].lower()
    assert external_status in error_events[0]["detail"]
    # The event carries the true terminal status structurally so live SSE
    # consumers can distinguish "interrupted" from "failed" without parsing
    # the free-text detail.
    assert error_events[0]["terminal_status"] == external_status


def test_restart_accepts_completed_with_errors(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """completed_with_errors is a terminal outcome of the same workflow as
    completed; restart must accept it (reviewer P2)."""
    from fastapi.testclient import TestClient

    # Seed a run that is already in the new terminal state.
    request = RunStrategyLabRequest(batch_size=1, batch_count=1)
    run_id = "run-test-restart-cwe"
    _seed_run_state(run_id, request)
    lab_main._active_runs[run_id]["status"] = "completed_with_errors"

    # Stub the Temporal dispatch so restart doesn't actually run the pipeline.
    started: List[str] = []
    monkeypatch.setattr(
        lab_main, "_dispatch_strategy_lab_run", lambda rid, req, **kw: started.append(rid)
    )
    monkeypatch.setattr(lab_main, "_persist_run_state", lambda *a, **kw: None)

    # restart_strategy_lab_run resolves any prior execution via
    # _require_temporal()/terminate_and_await_workflow_sync() before the
    # dispatch stub above is ever reached — stub those too.
    import shared.temporal

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(shared.temporal, "terminate_and_await_workflow_sync", lambda *a, **kw: None)

    client = TestClient(lab_main.app)
    resp = client.post(f"/strategy-lab/runs/{run_id}/restart")
    # Before the fix this returned 400 ("job not restartable").
    assert resp.status_code == 200, resp.text
    assert started, "restart should have dispatched the run"


def test_sse_stream_short_circuits_completed_with_errors(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSE endpoint must treat completed_with_errors as terminal so reconnecting
    clients get snapshot + done instead of hanging in 'running' (reviewer P1)."""
    from fastapi.testclient import TestClient

    request = RunStrategyLabRequest(batch_size=1, batch_count=1)
    run_id = "run-test-sse-cwe"
    _seed_run_state(run_id, request)
    lab_main._active_runs[run_id]["status"] = "completed_with_errors"
    lab_main._active_runs[run_id]["errored_cycles"] = 1

    monkeypatch.setattr(lab_main, "_persist_run_state", lambda *a, **kw: None)

    client = TestClient(lab_main.app)
    with client.stream("GET", f"/strategy-lab/runs/{run_id}/stream") as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes())
    text = body.decode()
    # Must contain both the snapshot and the terminal done event.
    assert '"type": "snapshot"' in text
    assert '"type": "done"' in text
