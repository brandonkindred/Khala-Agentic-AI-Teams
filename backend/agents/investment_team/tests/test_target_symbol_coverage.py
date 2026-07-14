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
        timeframe="1d",
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


def test_check_fetch_warns_on_forex_bare_names() -> None:
    """6-char forex bare names (EURUSD/USDJPY) must still trigger the warning."""
    gate = TargetSymbolCoverageGate()
    spec = _spec(hypothesis="Long EURUSD when USDJPY breaks support.", target_symbols=[])

    results = gate.check_fetch(spec, requested_symbols=["AAPL"], fetched_symbols=["AAPL"])

    warnings = _warnings(results)
    assert len(warnings) == 1
    assert "EURUSD" in warnings[0].details
    assert "USDJPY" in warnings[0].details


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


# ── check_breadth ───────────────────────────────────────────────────────


def test_check_breadth_warns_when_multi_target_but_single_traded_symbol() -> None:
    """Spec asks for QQQ + SPY + IWM but the ledger only touches QQQ — the
    strategy isn't exploiting its intended universe, so emit a warning."""
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=["QQQ", "SPY", "IWM"])

    results = gate.check_breadth(
        spec, trades=[_trade("QQQ"), _trade("QQQ", trade_num=2), _trade("QQQ", trade_num=3)]
    )

    warnings = _warnings(results)
    assert len(warnings) == 1
    assert "QQQ" in warnings[0].details
    assert "SPY" in warnings[0].details
    assert warnings[0].gate_name == GATE
    assert warnings[0].phase == "verification"


def test_check_breadth_passes_when_multi_target_and_multi_traded_symbols() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=["QQQ", "SPY"])

    results = gate.check_breadth(
        spec, trades=[_trade("QQQ"), _trade("SPY", trade_num=2)]
    )

    assert _criticals(results) == []
    assert _warnings(results) == []
    assert any(r.passed for r in results)


def test_check_breadth_skipped_when_single_target_symbol() -> None:
    """One target ticker → breadth is irrelevant; emit a benign info."""
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=["QQQ"])

    results = gate.check_breadth(spec, trades=[_trade("QQQ")])

    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed and r.severity == "info" for r in results)


def test_check_breadth_skipped_when_target_symbols_empty() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=[])

    results = gate.check_breadth(spec, trades=[_trade("AAPL")])

    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed and r.severity == "info" for r in results)


def test_check_breadth_skipped_when_ledger_empty() -> None:
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=["QQQ", "SPY"])

    results = gate.check_breadth(spec, trades=[])

    assert _criticals(results) == []
    assert _warnings(results) == []
    assert all(r.passed and r.severity == "info" for r in results)


def test_check_breadth_case_insensitive() -> None:
    """Mixed-case symbols on either side must collapse to one entry."""
    gate = TargetSymbolCoverageGate()
    spec = _spec(target_symbols=["QQQ", "SPY"])

    results = gate.check_breadth(
        spec, trades=[_trade("qqq"), _trade("QQQ", trade_num=2)]
    )

    warnings = _warnings(results)
    assert len(warnings) == 1
    assert "QQQ" in warnings[0].details


# ── Shared ticker-extraction parity tests (issue consolidation) ─────────────
#
# The ``extract_known_tickers`` helper in ``spec_readiness`` is now the single
# canonical implementation used by BOTH ``spec_readiness._check_universe_set``
# (via ``_SYMBOL_REGEX``) and ``TargetSymbolCoverageGate._tickers_in_hypothesis``
# (via direct delegation). These tests assert:
#   1. The helper's output is consistent with ``spec_readiness``'s word-bounded
#      / case-insensitive / suffix-canonicalizing behavior on representative
#      fixtures.
#   2. ``TargetSymbolCoverageGate._tickers_in_hypothesis`` now matches the
#      helper — both return the same canonical bare symbols.
#   3. Suffix variants (``ES=F``, ``EURUSD=X``) that the old
#      ``target_symbol_coverage._TICKER_RE`` would have missed are now
#      correctly identified and canonicalized.


def test_extract_known_tickers_case_insensitive() -> None:
    """``extract_known_tickers`` matches lowercase ticker mentions in prose.

    Preconditions: ``text`` contains a lowercase version of a known ticker.
    Postconditions: the helper returns the upper-cased canonical form,
    demonstrating case-insensitivity that the old ``_TICKER_RE`` lacked.
    """
    from investment_team.strategy_lab.quality_gates.spec_readiness import extract_known_tickers

    # "qqq" is a known ticker (from STOCK_SYMBOLS, stored upper-cased).
    result = extract_known_tickers("trade qqq on oversold breakouts")
    assert "QQQ" in result, f"expected 'QQQ' in {result}"


def test_extract_known_tickers_word_bounded() -> None:
    """``extract_known_tickers`` uses word boundaries so a short ticker token
    that appears as a substring of a longer word is not matched.

    Preconditions: ``text`` contains a known ticker token embedded inside a
    longer word without word-boundary separation.
    Postconditions: the embedded form is NOT returned; the helper does not
    false-match substrings.
    """
    from investment_team.strategy_lab.quality_gates.spec_readiness import extract_known_tickers

    # "AAPL" should not match inside "AAPLXYZ" (no word boundary after 'L').
    result = extract_known_tickers("AAPLXYZ is not a ticker")
    assert "AAPL" not in result, f"unexpected match inside longer token: {result}"


def test_extract_known_tickers_suffix_canonicalization_futures() -> None:
    """``extract_known_tickers`` strips ``=F`` Yahoo futures suffixes and returns
    the bare canonical symbol.

    Preconditions: ``text`` contains a futures ticker in Yahoo suffix form.
    Postconditions: the canonical bare symbol (without ``=F``) is returned.
    This is new behavior relative to the old ``_TICKER_RE`` in
    ``target_symbol_coverage.py``, which required all-uppercase tokens and
    had no suffix stripping.
    """
    from investment_team.strategy_lab.quality_gates.spec_readiness import extract_known_tickers

    result = extract_known_tickers("long ES=F on breakouts above the Donchian upper band")
    assert "ES" in result, f"expected canonical 'ES' from 'ES=F', got {result}"
    assert "ES=F" not in result, f"raw suffix form must not appear in result: {result}"


def test_extract_known_tickers_suffix_canonicalization_forex() -> None:
    """``extract_known_tickers`` strips ``=X`` Yahoo forex suffixes.

    Preconditions: ``text`` contains a forex ticker in Yahoo suffix form.
    Postconditions: the canonical bare symbol (without ``=X``) is returned.
    """
    from investment_team.strategy_lab.quality_gates.spec_readiness import extract_known_tickers

    result = extract_known_tickers("EURUSD=X breaks support at 1.05")
    assert "EURUSD" in result, f"expected canonical 'EURUSD' from 'EURUSD=X', got {result}"
    assert "EURUSD=X" not in result, f"raw suffix form must not appear in result: {result}"


def test_target_symbol_coverage_tickers_in_hypothesis_agrees_with_extract_known_tickers() -> None:
    """``TargetSymbolCoverageGate._tickers_in_hypothesis`` now delegates to
    ``extract_known_tickers``, so both return identical canonical sets for the
    same input text.

    Preconditions: a representative hypothesis string with known tickers.
    Postconditions: the gate's static method and the helper return exactly
    the same set, proving the two gates cannot reach different conclusions
    about the same hypothesis text.
    """
    from investment_team.strategy_lab.quality_gates.spec_readiness import extract_known_tickers
    from investment_team.strategy_lab.quality_gates.target_symbol_coverage import (
        TargetSymbolCoverageGate,
    )

    hypothesis = "Trade QQQ on RSI oversold breakouts and BTC on volume spikes."
    assert TargetSymbolCoverageGate._tickers_in_hypothesis(hypothesis) == extract_known_tickers(
        hypothesis
    )


def test_target_symbol_coverage_gains_case_insensitivity_via_shared_helper() -> None:
    """After consolidation, ``_tickers_in_hypothesis`` correctly detects a
    lowercase ticker mention that the old ``_TICKER_RE`` (uppercase-only) would
    have missed.

    Preconditions: hypothesis contains a lowercase known ticker.
    Postconditions: the gate's static method returns the upper-cased canonical
    form, demonstrating the behavior change introduced by the consolidation.
    """
    from investment_team.strategy_lab.quality_gates.target_symbol_coverage import (
        TargetSymbolCoverageGate,
    )

    result = TargetSymbolCoverageGate._tickers_in_hypothesis("buy qqq when rsi dips below 30")
    assert "QQQ" in result, f"expected 'QQQ' (case-insensitive match), got {result}"


def test_target_symbol_coverage_gains_suffix_stripping_via_shared_helper() -> None:
    """After consolidation, ``_tickers_in_hypothesis`` canonicalizes ``ES=F``
    to the bare ``ES`` symbol, matching ``spec_readiness``'s behavior.

    Preconditions: hypothesis contains a futures ticker with a Yahoo ``=F`` suffix.
    Postconditions: the canonical bare symbol is returned, not the suffix form.
    """
    from investment_team.strategy_lab.quality_gates.target_symbol_coverage import (
        TargetSymbolCoverageGate,
    )

    result = TargetSymbolCoverageGate._tickers_in_hypothesis("long ES=F above prior day high")
    assert "ES" in result, f"expected canonical 'ES' from 'ES=F', got {result}"
