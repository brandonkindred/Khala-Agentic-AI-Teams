"""Provenance fields on ``StrategyLabRecord`` (#547 item 5, #533).

The Strategy Lab orchestrator now snapshots the ideation-time spec and
code on the persisted record so reviewers can see any drift introduced
by the refinement loop (#547). Issue #533 extends this with a
structured ``data_provenance`` block on ``BacktestRecord`` so reviewers
can spot "spec asked for QQQ → ledger traded TSLA" from data instead of
prose. These tests pin the model contract: the fields exist, default
empty/None for legacy rows, populate from the assembly path, and
round-trip through JSON serialization without loss.
"""

from __future__ import annotations

import json
import types

from investment_team.models import (
    BacktestConfig,
    BacktestRecord,
    DataProvenance,
    StrategyLabRecord,
    StrategySpec,
    TradeRecord,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
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
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70)
            )
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


# ──────────────────────────────────────────────────────────────────────────
# Issue #533 — data_provenance block on BacktestRecord.
# ──────────────────────────────────────────────────────────────────────────


def test_data_provenance_defaults_on_legacy_row() -> None:
    """Legacy rows persisted before #533 deserialize with an empty block.

    Real call site: every ``BacktestRecord`` constructor that omits
    ``data_provenance`` must still validate, and rehydrating an old JSON
    payload (no ``data_provenance`` key) must yield the default-empty
    block. Both flows guard against breaking persisted job-service rows.
    """
    spec = _spec("strat-legacy-prov")
    record = _backtest_record(spec)

    assert isinstance(record.data_provenance, DataProvenance)
    assert record.data_provenance.target_symbols == []
    assert record.data_provenance.fetched_symbols == []
    assert record.data_provenance.traded_symbols == []
    assert record.data_provenance.provider_used == {}
    assert record.data_provenance.as_of is None
    assert record.data_provenance.legacy_fingerprint is None

    payload = json.loads(record.model_dump_json())
    payload.pop("data_provenance", None)
    rebuilt = BacktestRecord.model_validate(payload)
    assert rebuilt.data_provenance == DataProvenance()


def test_data_provenance_populated_on_assembled_record() -> None:
    """``_assemble_record`` populates every provenance field from inputs."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    final_spec = _spec("strat-prov-assemble").model_copy(update={"target_symbols": ["QQQ"]})
    config = BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )
    metrics = compute_metrics([], config.initial_capital, config.start_date, config.end_date)
    metrics = metrics.model_copy(update={"dataset_fingerprint": "sha256:abc123"})
    trades = [
        TradeRecord(
            trade_num=1,
            entry_date="2023-06-01",
            exit_date="2023-06-05",
            symbol="TSLA",
            side="long",
            entry_price=100.0,
            exit_price=110.0,
            shares=10.0,
            position_value=1000.0,
            gross_pnl=100.0,
            net_pnl=100.0,
            return_pct=0.10,
            hold_days=4,
            outcome="win",
            cumulative_pnl=100.0,
        )
    ]

    # Drive the unbound method with a minimal stand-in for ``self``: the
    # assembler only needs ``convergence_tracker`` (for the record() call)
    # and ``build_orchestrator_gate`` (unused on this path). A
    # ``SimpleNamespace`` avoids the cost of instantiating the full
    # orchestrator (LLM clients, gate constructors, …) and keeps the test
    # focused on the provenance derivation.
    self_ = types.SimpleNamespace(
        convergence_tracker=types.SimpleNamespace(record=lambda *_a, **_kw: None),
    )

    record = StrategyLabOrchestrator._assemble_record(
        self_,  # type: ignore[arg-type]
        spec=final_spec,
        code="# code",
        config=config,
        metrics=metrics,
        trades=trades,
        narrative="n",
        original_spec=final_spec,
        original_code="# code",
        rationale="r",
        requested_symbols=["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"],
        fetched_symbols=["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"],
        provider_used={
            "AAPL": "yfinance",
            "MSFT": "yfinance",
            "NVDA": "yfinance",
            "TSLA": "yfinance",
            "AMZN": "yfinance",
        },
        max_rounds_exhausted=False,
        execution_succeeded=True,
        is_winning=True,
        trades_aligned=True,
        refinement_rounds=0,
        alignment_rounds=0,
        all_gate_results=[],
        emit=lambda *_a, **_kw: None,
    )

    prov = record.backtest.data_provenance
    assert prov.target_symbols == ["QQQ"]
    assert prov.fetched_symbols == ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"]
    assert prov.traded_symbols == ["TSLA"]
    assert prov.provider_used == {
        "AAPL": "yfinance",
        "MSFT": "yfinance",
        "NVDA": "yfinance",
        "TSLA": "yfinance",
        "AMZN": "yfinance",
    }
    assert prov.legacy_fingerprint == "sha256:abc123"


def test_data_provenance_round_trips_through_json() -> None:
    """``DataProvenance`` survives ``model_dump_json`` → ``model_validate``."""
    spec = _spec("strat-prov-json")
    record = _backtest_record(spec)
    record = record.model_copy(
        update={
            "data_provenance": DataProvenance(
                target_symbols=["QQQ"],
                fetched_symbols=["AAPL", "TSLA"],
                traded_symbols=["TSLA"],
                provider_used={"AAPL": "yfinance", "TSLA": "polygon"},
                as_of="2024-06-01",
                legacy_fingerprint="sha256:deadbeef",
            )
        }
    )

    payload = json.loads(record.model_dump_json())
    assert payload["data_provenance"]["target_symbols"] == ["QQQ"]
    assert payload["data_provenance"]["traded_symbols"] == ["TSLA"]
    assert payload["data_provenance"]["provider_used"] == {
        "AAPL": "yfinance",
        "TSLA": "polygon",
    }

    rebuilt = BacktestRecord.model_validate(payload)
    assert rebuilt.data_provenance.target_symbols == ["QQQ"]
    assert rebuilt.data_provenance.fetched_symbols == ["AAPL", "TSLA"]
    assert rebuilt.data_provenance.traded_symbols == ["TSLA"]
    assert rebuilt.data_provenance.provider_used == {
        "AAPL": "yfinance",
        "TSLA": "polygon",
    }
    assert rebuilt.data_provenance.as_of == "2024-06-01"
    assert rebuilt.data_provenance.legacy_fingerprint == "sha256:deadbeef"


def test_data_provenance_isolated_across_consecutive_fetches() -> None:
    """Regression: ``_fetch_market_data`` snapshots only the fresh subset.

    ``MarketDataService.provider_used`` is shared mutable state that
    accumulates across fetches. Without the snapshot-and-filter step in
    ``_fetch_market_data``, a later cycle's fetched symbols would leak
    earlier cycles' provider entries onto this row's
    ``data_provenance.provider_used``. This test pins the filter behavior
    so the leak can't silently regress.
    """
    from investment_team.strategy_lab._orchestrator_helpers import _MarketDataFetch
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    # Stand-in ``MarketDataService``: tracks the resolved + fetched symbols
    # and simulates a service whose ``provider_used`` has been polluted by a
    # previous cycle (the "STALE_FROM_PRIOR_CYCLE" entry).
    class _StubService:
        def __init__(self) -> None:
            self.provider_used: dict[str, str] = {
                "STALE_FROM_PRIOR_CYCLE": "polygon",
            }

        def resolve_strategy_symbols(self, spec):  # noqa: ARG002
            return ["AAPL", "MSFT"]

        def fetch_multi_symbol_range(self, *, symbols, **_kw):
            # The service mutates ``provider_used`` in place as it fetches.
            for sym in symbols:
                self.provider_used[sym] = "yfinance"
            from investment_team.market_data_service import OHLCVBar

            bar = OHLCVBar(
                date="2024-06-01",
                open=100.0,
                high=100.5,
                low=99.5,
                close=100.0,
                volume=1_000_000,
            )
            return {sym: [bar] for sym in symbols}

    service = _StubService()
    spec = _spec("strat-prov-isolation")
    config = BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )

    self_ = types.SimpleNamespace(market_data_service=service)
    fetch: _MarketDataFetch = StrategyLabOrchestrator._fetch_market_data(
        self_,
        spec,
        config,  # type: ignore[arg-type]
    )

    assert fetch.fetched_symbols == ["AAPL", "MSFT"]
    # The polluted entry from a "prior cycle" must NOT appear on this
    # fetch's snapshot, even though it still sits on the service's dict.
    assert "STALE_FROM_PRIOR_CYCLE" not in fetch.provider_used
    assert fetch.provider_used == {"AAPL": "yfinance", "MSFT": "yfinance"}
