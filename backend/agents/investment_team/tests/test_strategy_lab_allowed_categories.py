"""Tests for user-selectable asset categories in the Strategy Lab.

The Strategy Lab UI lets the user constrain which asset categories the design
agent may generate strategies for. The selection rides on
``RunStrategyLabRequest.allowed_asset_classes`` and is translated into the
design pipeline's existing ``exclude_asset_classes`` constraint at the worker
boundary. These tests cover:

  * normalization of the raw selection to canonical, ideation-valid labels,
  * the allowed → excluded complement,
  * request-model validation (including the empty-after-normalization reject),
  * mix-hint steering restricted to the allowed classes, and
  * the worker threading the computed exclusion into every cycle.
"""

from __future__ import annotations

import uuid
from typing import Any, List, Optional

import pytest
from pydantic import ValidationError

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
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, StopLossRule
from investment_team.strategy_lab_context import (
    PROMPT_ASSET_CLASSES,
    asset_class_mix_hint,
    excluded_for_allowed,
    normalize_allowed_asset_classes,
)

# ---------------------------------------------------------------------------
# normalize_allowed_asset_classes
# ---------------------------------------------------------------------------


def test_normalize_none_returns_none() -> None:
    """``None`` means "no constraint" and must propagate as ``None``."""
    assert normalize_allowed_asset_classes(None) is None


def test_normalize_maps_aliases_to_canonical_labels() -> None:
    out = normalize_allowed_asset_classes(["stock", "fx", "equity", "cryptocurrency"])
    # stock/equity → stocks (deduped), fx → forex, cryptocurrency → crypto.
    assert out == ["stocks", "crypto", "forex"]


def test_normalize_preserves_canonical_order_and_dedupes() -> None:
    out = normalize_allowed_asset_classes(["futures", "crypto", "crypto", "stocks"])
    assert out == ["stocks", "crypto", "futures"]


def test_normalize_drops_options_and_unknown_tokens() -> None:
    # options is canonical but not an ideation target; "bonds"/"" are unknown.
    out = normalize_allowed_asset_classes(["forex", "options", "bonds", ""])
    assert out == ["forex"]


def test_normalize_all_invalid_returns_empty_list() -> None:
    # A non-empty selection that resolves to nothing valid → empty list (the
    # request validator rejects this, but the helper itself stays total).
    assert normalize_allowed_asset_classes(["options", "bonds"]) == []


# ---------------------------------------------------------------------------
# excluded_for_allowed
# ---------------------------------------------------------------------------


def test_excluded_is_complement_within_prompt_classes() -> None:
    assert excluded_for_allowed(["forex"]) == ["stocks", "crypto", "futures", "commodities"]


def test_excluded_empty_when_all_classes_allowed() -> None:
    assert excluded_for_allowed(list(PROMPT_ASSET_CLASSES)) == []


# ---------------------------------------------------------------------------
# RunStrategyLabRequest.allowed_asset_classes validation
# ---------------------------------------------------------------------------


def test_request_default_is_none() -> None:
    assert RunStrategyLabRequest().allowed_asset_classes is None


def test_request_normalizes_selection() -> None:
    req = RunStrategyLabRequest(allowed_asset_classes=["stock", "fx"])
    assert req.allowed_asset_classes == ["stocks", "forex"]


def test_request_accepts_full_selection() -> None:
    req = RunStrategyLabRequest(allowed_asset_classes=list(PROMPT_ASSET_CLASSES))
    assert req.allowed_asset_classes == list(PROMPT_ASSET_CLASSES)


def test_request_rejects_selection_with_no_valid_category() -> None:
    # Pydantic surfaces the field validator's ValueError as a ValidationError
    # at model construction; catch that specific type rather than bare Exception
    # so an unexpected error (e.g. a validator bug) fails the test loudly.
    with pytest.raises(ValidationError):
        RunStrategyLabRequest(allowed_asset_classes=["options"])
    with pytest.raises(ValidationError):
        RunStrategyLabRequest(allowed_asset_classes=["bonds", "nonsense"])


# ---------------------------------------------------------------------------
# asset_class_mix_hint(..., exclude=...)
# ---------------------------------------------------------------------------


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


def _record(asset_class: str) -> StrategyLabRecord:
    suffix = uuid.uuid4().hex[:6]
    strategy = StrategySpec(
        strategy_id=f"s-{suffix}",
        authored_by="test",
        asset_class=asset_class,
        hypothesis="h",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[StopLossRule(pct=0.03)],
        risk_limits={},
        speculative=False,
    )
    now = lab_main._now()
    backtest = BacktestRecord(
        backtest_id=f"bt-{suffix}",
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        config=BacktestConfig(start_date="2024-01-01", end_date="2024-12-31"),
        submitted_by="test",
        submitted_at=now,
        completed_at=now,
        status="completed",
        result=_stub_backtest_result(),
        notes=[],
        trades=[],
    )
    return StrategyLabRecord(
        lab_record_id=f"lab-{suffix}",
        strategy=strategy,
        backtest=backtest,
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="ok",
        created_at=now,
        quality_gate_results=[],
    )


def test_mix_hint_exclude_restricts_menu_when_no_records() -> None:
    out = asset_class_mix_hint([], exclude=["stocks", "crypto", "futures", "commodities"])
    # Only forex remains; excluded classes must not be offered as a choice.
    assert "forex" in out
    for excluded in ("stocks=", "crypto=", "futures=", "commodities="):
        assert excluded not in out
    assert "crypto" not in out.split("do **not**")[0]


def test_mix_hint_exclude_drops_excluded_classes_from_counts() -> None:
    records = [_record("forex") for _ in range(3)] + [_record("crypto") for _ in range(2)]
    out = asset_class_mix_hint(records, exclude=["stocks", "futures", "commodities"])
    # Allowed classes are counted; excluded classes never appear in the counts.
    assert "forex=3" in out
    assert "crypto=2" in out
    assert "stocks=" not in out
    assert "futures=" not in out
    assert "commodities=" not in out


def test_mix_hint_no_exclude_matches_unconstrained() -> None:
    """An empty exclusion must reproduce the unconstrained hint verbatim."""
    records = [_record("stocks") for _ in range(4)]
    assert asset_class_mix_hint(records, exclude=[]) == asset_class_mix_hint(records)
    assert asset_class_mix_hint([], exclude=None) == asset_class_mix_hint([])


# ---------------------------------------------------------------------------
# Worker threading: allowed_asset_classes → exclude_asset_classes per cycle
# ---------------------------------------------------------------------------


def _make_cycle_record(idx: int, config: BacktestConfig) -> StrategyLabRecord:
    suffix = uuid.uuid4().hex[:6]
    strategy = StrategySpec(
        strategy_id=f"strat-{idx}-{suffix}",
        authored_by="test",
        asset_class="forex",
        hypothesis="h",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[StopLossRule(pct=0.03)],
        risk_limits={},
        speculative=False,
    )
    now = lab_main._now()
    backtest = BacktestRecord(
        backtest_id=f"bt-{idx}-{suffix}",
        strategy_id=strategy.strategy_id,
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
        lab_record_id=f"lab-{idx}-{suffix}",
        strategy=strategy,
        backtest=backtest,
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="ok",
        created_at=now,
        quality_gate_results=[],
    )


@pytest.fixture
def empty_lab_state(monkeypatch: pytest.MonkeyPatch) -> None:
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


class _StubTracker:
    def snapshot(self) -> "_StubTracker":
        return _StubTracker()

    def record(self, *_a: Any, **_kw: Any) -> None:
        pass

    def merge_from(self, *_a: Any, **_kw: Any) -> None:
        pass


def _run_worker_capturing_exclusions(
    monkeypatch: pytest.MonkeyPatch, request: RunStrategyLabRequest
) -> List[Optional[List[str]]]:
    """Run the worker with stubs and return the ``exclude_asset_classes`` each cycle saw."""
    seen: List[Optional[List[str]]] = []

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
            seen.append(exclude_asset_classes)
            return _make_cycle_record(type(self)._counter, config)

    monkeypatch.setattr(lab_main, "StrategyLabOrchestrator", _StubOrchestrator)
    monkeypatch.setattr(lab_main, "ConvergenceTracker", _StubTracker)
    monkeypatch.setattr(lab_main, "_strategy_lab_signal_expert_enabled", lambda: False)
    monkeypatch.setattr(lab_main, "_persist_run_state", lambda *a, **kw: None)

    run_id = f"run-{uuid.uuid4().hex[:6]}"
    _seed_run_state(run_id, request)
    _strategy_lab_worker(run_id, request)
    assert lab_main._active_runs[run_id]["status"] == "completed"
    return seen


def test_worker_threads_complement_exclusion_into_every_cycle(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = RunStrategyLabRequest(
        batch_size=2,
        batch_count=1,
        max_parallel=1,
        paper_trading_enabled=False,
        allowed_asset_classes=["forex"],
    )
    seen = _run_worker_capturing_exclusions(monkeypatch, request)
    assert len(seen) == 2
    expected = ["stocks", "crypto", "futures", "commodities"]
    assert all(exc == expected for exc in seen)


def test_worker_passes_no_exclusion_when_all_categories_allowed(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = RunStrategyLabRequest(
        batch_size=1,
        batch_count=1,
        max_parallel=1,
        paper_trading_enabled=False,
        allowed_asset_classes=list(PROMPT_ASSET_CLASSES),
    )
    seen = _run_worker_capturing_exclusions(monkeypatch, request)
    assert seen == [None]


def test_worker_passes_no_exclusion_when_unset(
    empty_lab_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = RunStrategyLabRequest(
        batch_size=1,
        batch_count=1,
        max_parallel=1,
        paper_trading_enabled=False,
    )
    seen = _run_worker_capturing_exclusions(monkeypatch, request)
    assert seen == [None]
