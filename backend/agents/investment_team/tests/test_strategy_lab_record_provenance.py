"""Provenance fields on ``StrategyLabRecord`` (#547 item 5).

The Strategy Lab orchestrator now snapshots the ideation-time spec and
code on the persisted record so reviewers can see any drift introduced
by the refinement loop. These tests pin the model contract: the fields
exist, default to ``None`` for legacy rows, and round-trip through
JSON serialization without loss.
"""

from __future__ import annotations

import json

from investment_team.models import (
    BacktestConfig,
    BacktestRecord,
    StrategyLabRecord,
    StrategySpec,
)
from investment_team.strategy_lab.spec_dsl import (
    ConstRef,
    EntryRule,
    Predicate,
    RSIRef,
    SignalExitRule,
)
from investment_team.trade_simulator import compute_metrics


def _spec(strategy_id: str, *, hypothesis: str = "RSI mean reversion") -> StrategySpec:
    return StrategySpec(
        strategy_id=strategy_id,
        authored_by="test",
        asset_class="stocks",
        hypothesis=hypothesis,
        signal_definition="sig",
        entry_rules=[
            EntryRule(
                side="long", when=Predicate(lhs=RSIRef(period=14), op="lt", rhs=ConstRef(value=30))
            )
        ],
        exit_rules=[
            SignalExitRule(when=Predicate(lhs=RSIRef(period=14), op="gt", rhs=ConstRef(value=70)))
        ],
        risk_limits={"max_position_pct": 5},
        speculative=False,
        strategy_code="# code",
    )


def _backtest_record(spec: StrategySpec) -> BacktestRecord:
    config = BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )
    return BacktestRecord(
        backtest_id="bt-test",
        strategy_id=spec.strategy_id,
        strategy=spec,
        config=config,
        submitted_by="test",
        submitted_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:00+00:00",
        status="completed",
        result=compute_metrics([], config.initial_capital, config.start_date, config.end_date),
        trades=[],
    )


def test_strategy_lab_record_original_fields_default_to_none() -> None:
    """Legacy rows (pre-#547) and any caller that omits the fields stay valid."""
    spec = _spec("strat-legacy")
    record = StrategyLabRecord(
        lab_record_id="lab-legacy",
        strategy=spec,
        backtest=_backtest_record(spec),
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2026-01-01T00:00:00+00:00",
    )

    assert record.original_spec is None
    assert record.original_code is None


def test_strategy_lab_record_persists_ideation_snapshot() -> None:
    """When refinement mutates the spec/code, the original snapshot is retained."""
    ideation_spec = _spec("strat-prov", hypothesis="ideation hypothesis")
    ideation_code = "# ideation code"

    # Simulate post-refinement state: spec hypothesis and code both changed.
    final_spec = ideation_spec.model_copy(
        update={"hypothesis": "refined hypothesis", "strategy_code": "# refined code"}
    )

    record = StrategyLabRecord(
        lab_record_id="lab-prov",
        strategy=final_spec,
        backtest=_backtest_record(final_spec),
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2026-01-01T00:00:00+00:00",
        strategy_code="# refined code",
        original_spec=ideation_spec,
        original_code=ideation_code,
    )

    assert record.original_code == "# ideation code"
    assert record.original_spec is not None
    assert record.original_spec.hypothesis == "ideation hypothesis"
    assert record.strategy.hypothesis == "refined hypothesis"
    assert record.original_code != record.strategy_code


def test_strategy_lab_record_provenance_round_trips_through_json() -> None:
    """The fields survive ``model_dump_json`` → ``model_validate_json``."""
    ideation_spec = _spec("strat-json")
    record = StrategyLabRecord(
        lab_record_id="lab-json",
        strategy=ideation_spec,
        backtest=_backtest_record(ideation_spec),
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2026-01-01T00:00:00+00:00",
        strategy_code="# code",
        original_spec=ideation_spec,
        original_code="# code",
    )

    payload = json.loads(record.model_dump_json())
    assert payload["original_code"] == "# code"
    assert payload["original_spec"]["strategy_id"] == "strat-json"

    rebuilt = StrategyLabRecord.model_validate(payload)
    assert rebuilt.original_spec == ideation_spec
    assert rebuilt.original_code == "# code"
