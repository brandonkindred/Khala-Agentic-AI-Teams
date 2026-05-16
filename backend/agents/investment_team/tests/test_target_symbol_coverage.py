"""Issue #526 — TargetSymbolCoverageGate unit tests."""

from __future__ import annotations

from typing import List

from investment_team.models import StrategySpec, TradeRecord
from investment_team.strategy_lab.quality_gates.target_symbol_coverage import (
    GATE,
    TargetSymbolCoverageGate,
)


def _spec(
    *, hypothesis: str = "Catch short-term momentum.", target_symbols: List[str] | None = None
) -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-coverage-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis=hypothesis,
        signal_definition="sig",
        entry_rules=[],
        exit_rules=[],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
        speculative=False,
        strategy_code="from contract import Strategy\nclass S(Strategy):\n    def on_bar(self, ctx, bar):\n        pass\n",
        target_symbols=target_symbols or [],
    )


def _trade(symbol: str, trade_num: int = 1) -> TradeRecord:
    return TradeRecord(
        trade_num=trade_num,
        entry_date="2024-01-02",
        exit_date="2024-01-05",
        symbol=symbol,
        side="long",
        entry_price=100.0,
        exit_price=105.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=50.0,
        net_pnl=49.0,
        return_pct=5.0,
        hold_days=3,
        outcome="win",
        cumulative_pnl=49.0,
    )


def _criticals(results):
    return [r for r in results if not r.passed and r.severity == "critical"]


def _warnings(results):
    return [r for r in results if not r.passed and r.severity == "warning"]


# ── check_fetch ──────────────────────────────────────────────────────────


def test_check_fetch_passes_when_target_symbols_subset_of_fetched() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=["QQQ", "SPY"])

    results = gate.check_fetch(
        spec, requested_symbols=["QQQ", "SPY"], fetched_symbols=["QQQ", "SPY", "IWM"]
    )

    assert _criticals(results) == []
    assert any(r.passed and r.gate_name == GATE for r in results)


def test_check_fetch_critical_when_target_symbol_missing_from_fetched() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=["QQQ", "SPY"])

    results = gate.check_fetch(spec, requested_symbols=["QQQ", "SPY"], fetched_symbols=["QQQ"])

    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "SPY" in criticals[0].details
    assert criticals[0].gate_name == GATE


def test_check_fetch_passes_when_target_symbols_empty_and_no_ticker_in_hypothesis() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(hypothesis="Catch short-term momentum across the market.", target_symbols=[])

    results = gate.check_fetch(spec, requested_symbols=["AAPL"], fetched_symbols=["AAPL"])

    assert _criticals(results) == []
    assert _warnings(results) == []
    assert any(r.passed for r in results)


def test_check_fetch_warns_when_hypothesis_mentions_ticker_but_target_symbols_empty() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(hypothesis="Trade QQQ on RSI oversold breakouts.", target_symbols=[])

    results = gate.check_fetch(
        spec, requested_symbols=["AAPL", "TSLA"], fetched_symbols=["AAPL", "TSLA"]
    )

    warnings = _warnings(results)
    assert len(warnings) == 1
    assert "QQQ" in warnings[0].details
    assert "target_symbols" in warnings[0].details


def test_check_fetch_warns_on_crypto_and_commodity_tickers_too() -> None:
    gate = TargetSymbolCoverageGate()
    for ticker in ("BTC", "GLD"):
        spec = _spec(hypothesis=f"Long {ticker} on volume spikes.", target_symbols=[])
        results = gate.check_fetch(spec, requested_symbols=["AAPL"], fetched_symbols=["AAPL"])
        warnings = _warnings(results)
        assert len(warnings) == 1, f"expected warning for {ticker}, got {warnings}"
        assert ticker in warnings[0].details


def test_check_fetch_does_not_warn_when_target_symbols_set_even_if_hypothesis_mentions_ticker() -> (
    None
):
    gate = TargetSymbolCoverageGate()
    spec = _spec(hypothesis="Trade QQQ on RSI oversold breakouts.", target_symbols=["QQQ"])

    results = gate.check_fetch(spec, requested_symbols=["QQQ"], fetched_symbols=["QQQ"])

    assert _warnings(results) == []
    assert _criticals(results) == []


def test_check_fetch_case_insensitive_match() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=["QQQ"])

    results = gate.check_fetch(spec, requested_symbols=["qqq"], fetched_symbols=["qqq"])

    assert _criticals(results) == []


# ── check_trades ────────────────────────────────────────────────────────


def test_check_trades_passes_when_every_trade_symbol_in_target_symbols() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=["QQQ", "SPY"])

    results = gate.check_trades(spec, trades=[_trade("QQQ"), _trade("SPY", trade_num=2)])

    assert _criticals(results) == []
    assert any(r.passed for r in results)


def test_check_trades_critical_when_trade_symbol_outside_target_symbols() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=["QQQ"])

    results = gate.check_trades(spec, trades=[_trade("QQQ"), _trade("AAPL", trade_num=2)])

    criticals = _criticals(results)
    assert len(criticals) == 1
    assert "AAPL" in criticals[0].details
    assert criticals[0].gate_name == GATE


def test_check_trades_info_when_target_symbols_empty() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=[])

    results = gate.check_trades(spec, trades=[_trade("AAPL")])

    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed and r.severity == "info" for r in results)


def test_check_trades_case_insensitive_match() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=["qqq"])  # normaliser uppercases this, but be defensive

    results = gate.check_trades(spec, trades=[_trade("QQQ")])

    assert _criticals(results) == []


def test_check_trades_empty_ledger_passes() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=["QQQ"])

    results = gate.check_trades(spec, trades=[])

    assert _criticals(results) == []
    assert any(r.passed for r in results)
