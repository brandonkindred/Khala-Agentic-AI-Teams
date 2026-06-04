"""Unit tests for ``SpecReadinessGate``.

Eight deterministic implementability rules plus the two end-to-end scenarios
that match the gate's acceptance contract. Every rule has at least one
dedicated test that exercises its failure path.
"""

from __future__ import annotations

from typing import List

import pytest
from pydantic import ValidationError

from investment_team.models import BacktestConfig, StrategySpec
from investment_team.strategy_lab.quality_gates.spec_readiness import (
    GATE,
    SpecReadinessGate,
    _canonicalize_ticker,
    _extract_prose_position_pct,
    _sizing_coherence_rel_tol,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    VolatilityTargetSizing,
)


def _spec(
    *,
    hypothesis: str = "RSI(14) below 30 on AAPL signals long entry.",
    entry: List | None = None,
    exit_: List | None = None,
    asset_class: str = "stocks",
    timeframe: str = "1d",
    target_symbols: List[str] | None = None,
    sizing=None,
    risk_limits: dict | None = None,
) -> StrategySpec:
    if entry is None:
        entry = [
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="<",
                    rhs=30.0,
                ),
            )
        ]
    if exit_ is None:
        exit_ = [
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70.0,
                )
            )
        ]
    if target_symbols is None:
        target_symbols = ["AAPL"]
    kwargs: dict = dict(
        strategy_id="strat-readiness-test",
        authored_by="test",
        asset_class=asset_class,
        hypothesis=hypothesis,
        signal_definition="sig",
        timeframe=timeframe,
        entry_rules=entry,
        exit_rules=exit_,
        target_symbols=target_symbols,
        risk_limits=risk_limits or {"max_position_pct": 5, "max_drawdown_pct": 10},
        speculative=False,
        strategy_code="from contract import Strategy\nclass S(Strategy):\n    def on_bar(self, ctx, bar):\n        pass\n",
    )
    if sizing is not None:
        kwargs["sizing"] = sizing
    return StrategySpec(**kwargs)


def _config() -> BacktestConfig:
    return BacktestConfig(start_date="2024-01-01", end_date="2024-06-01")


def _critical(results) -> list[str]:
    return [r.details for r in results if r.severity == "critical" and not r.passed]


# ---------------------------------------------------------------------------
# Rule 1: Universe set
# ---------------------------------------------------------------------------


def test_rule1_target_symbols_missing_when_hypothesis_names_ticker() -> None:
    # GLD is whitelisted (cross-asset ETF) but not in the stocks default
    # universe, so an empty target_symbols leaves the named ticker
    # unreachable and Rule 1 fires.
    spec = _spec(
        hypothesis="GLD tends to revert to its 50-day SMA after a 2-sigma move.",
        target_symbols=[],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    critical = _critical(results)
    assert any("GLD" in c and "target_symbols" in c for c in critical), critical


def test_rule1_hypothesis_symbol_not_in_target_symbols() -> None:
    spec = _spec(
        hypothesis="QQQ tends to revert to its 50-day SMA after a 2-sigma move.",
        target_symbols=["AAPL"],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    critical = _critical(results)
    assert any("QQQ" in c for c in critical), critical


def test_rule1_catches_futures_ticker_mismatch() -> None:
    """Bare futures names (`ES`, `NQ`) in the hypothesis must be caught."""
    spec = _spec(
        hypothesis="Trade ES on Monday-morning gaps.",
        target_symbols=["NQ=F"],
        asset_class="futures",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    critical = _critical(results)
    assert any("ES" in c for c in critical), critical


def test_rule1_catches_forex_ticker_mismatch() -> None:
    """Forex suffixed tickers (`EURUSD=X`) in the hypothesis must be caught."""
    spec = _spec(
        hypothesis="EURUSD=X tends to revert intraday.",
        target_symbols=["GBPUSD=X"],
        asset_class="forex",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    critical = _critical(results)
    assert any("EURUSD=X" in c for c in critical), critical


def test_rule1_canonicalizes_bare_futures_against_provider_suffix() -> None:
    """``ES`` in hypothesis matches ``ES=F`` in target_symbols — same symbol."""
    spec = _spec(
        hypothesis="Trade ES gaps on Monday mornings.",
        target_symbols=["ES=F"],
        asset_class="futures",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule1_failures = [
        c for c in _critical(results) if "target_symbols" in c or "Hypothesis names symbol" in c
    ]
    assert not rule1_failures, rule1_failures


def test_rule1_canonicalizes_bare_forex_against_provider_suffix() -> None:
    """``EURUSD`` in hypothesis matches ``EURUSD=X`` in target_symbols."""
    spec = _spec(
        hypothesis="EURUSD tends to revert intraday.",
        target_symbols=["EURUSD=X"],
        asset_class="forex",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule1_failures = [
        c for c in _critical(results) if "target_symbols" in c or "Hypothesis names symbol" in c
    ]
    assert not rule1_failures, rule1_failures


def test_rule1_canonicalizes_bare_crypto_against_usd_suffix() -> None:
    """``BTC`` in hypothesis matches ``BTC-USD`` in target_symbols.

    Crypto tickers use the yfinance ``-USD`` quote-suffix convention
    (``BTC-USD``, ``ETH-USD``, ...). The bare alias from the canonical
    symbol list must canonicalize equal to its provider-suffixed form so
    a hypothesis that names ``BTC`` does not false-critical against a
    spec whose ``target_symbols`` were correctly populated as ``BTC-USD``.
    """
    spec = _spec(
        hypothesis="BTC momentum after a volatility-contraction regime.",
        target_symbols=["BTC-USD"],
        asset_class="crypto",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule1_failures = [
        c for c in _critical(results) if "target_symbols" in c or "Hypothesis names symbol" in c
    ]
    assert not rule1_failures, rule1_failures


def test_rule1_canonicalizes_multiple_crypto_against_usd_suffix() -> None:
    """Mixed bare/provider-suffix crypto tickers all canonicalize correctly."""
    spec = _spec(
        hypothesis="Pair-trade BTC long vs ETH short on momentum divergence.",
        target_symbols=["BTC-USD", "ETH-USD"],
        asset_class="crypto",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule1_failures = [
        c for c in _critical(results) if "target_symbols" in c or "Hypothesis names symbol" in c
    ]
    assert not rule1_failures, rule1_failures


def test_canonicalize_ticker_single_suffixes() -> None:
    """Single provider suffixes strip to the bare, upper-cased symbol."""
    assert _canonicalize_ticker("ES=F") == "ES"
    assert _canonicalize_ticker("EURUSD=X") == "EURUSD"
    assert _canonicalize_ticker("BTC-USD") == "BTC"
    assert _canonicalize_ticker("aapl") == "AAPL"
    assert _canonicalize_ticker("AAPL") == "AAPL"


def test_canonicalize_ticker_compound_suffix_does_not_crash() -> None:
    """Compound suffixes strip iteratively instead of raising AssertionError.

    A double-quoted crypto ticker like ``BTC-USD-USD`` (LLM hallucination,
    double-normalization, or operator typo) must reduce to the bare symbol
    rather than leaving a residual suffix that trips the post-condition.
    """
    assert _canonicalize_ticker("BTC-USD-USD") == "BTC"
    assert _canonicalize_ticker("ETH-USD-USD") == "ETH"
    # Mixed compound suffixes also fully strip.
    assert _canonicalize_ticker("EURUSD=X=X") == "EURUSD"


def test_rule1_compound_suffix_target_symbol_does_not_crash() -> None:
    """A compound-suffix target symbol yields gate results, never a crash.

    Before the fix, ``_canonicalize_ticker`` raised an ``AssertionError`` for
    ``BTC-USD-USD`` that propagated out of ``validate()`` and crashed the entire
    gate run. The gate must instead produce ordinary results.
    """
    spec = _spec(
        hypothesis="BTC momentum after a volatility-contraction regime.",
        target_symbols=["BTC-USD-USD"],
        asset_class="crypto",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert results, "gate must return results rather than raising"


def test_rule1_passes_when_no_symbols_in_hypothesis_and_no_targets() -> None:
    spec = _spec(
        hypothesis="RSI(14) below 30 signals long entry.",
        target_symbols=[],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule1_failures = [
        c for c in _critical(results) if "target_symbols" in c or "Hypothesis names symbol" in c
    ]
    assert not rule1_failures, rule1_failures


# ---------------------------------------------------------------------------
# Rule 2: Entry rules non-trivial
# ---------------------------------------------------------------------------


def test_rule2_no_entry_rules_is_critical() -> None:
    spec = _spec(entry=[])
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("No entry rules" in c for c in _critical(results))


# ---------------------------------------------------------------------------
# Rule 3: Indicator validity
# ---------------------------------------------------------------------------


def test_rule3_sma_without_period_param_is_critical() -> None:
    # Bypass Pydantic validation by constructing a spec then mutating an
    # IndicatorRef's params dict — the gate must catch what slipped past.
    spec = _spec()
    ref = spec.entry_rules[0].when.lhs
    assert isinstance(ref, IndicatorRef)
    # Swap the well-formed rsi(period=14) for sma with no params dict entry
    spec.entry_rules[0].when.lhs = IndicatorRef.model_construct(
        name="sma", params={}, source="close"
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("sma" in c and "period" in c for c in _critical(results))


# ---------------------------------------------------------------------------
# Rule 4: Exit completeness
# ---------------------------------------------------------------------------


def test_rule4_no_exit_rules_is_critical() -> None:
    spec = _spec(exit_=[])
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("No exit rules" in c for c in _critical(results))


# ---------------------------------------------------------------------------
# Rule 5: Sizing realisable
# ---------------------------------------------------------------------------


def test_rule5_sizing_under_one_share_is_critical() -> None:
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=10.0),
        target_symbols=["AAPL"],
    )
    # Default provider returns $100/share; $10 notional / $100 = 0.1 share.
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("qty=" in c and "AAPL" in c for c in _critical(results))


def test_rule5_exactly_one_whole_lot_passes_for_stocks() -> None:
    """A notional spec yielding qty=1.0 on stocks is implementable."""
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=100.0),
        target_symbols=["AAPL"],
        asset_class="stocks",
    )
    # Default provider returns $100/share; 100 / 100 = 1.0 exactly.
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    sizing_failures = [c for c in _critical(results) if "qty=" in c]
    assert not sizing_failures, sizing_failures


def test_rule5_fixed_notional_exceeds_initial_capital_is_critical() -> None:
    """A fixed_notional spec whose notional exceeds initial_capital cannot
    be filled — fill engine rejects with ``insufficient_capital`` the
    moment ``portfolio.capital < notional``. The readiness gate must
    catch this before code synthesis."""
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=150_000.0),  # > default $100k
        target_symbols=["AAPL"],
        asset_class="stocks",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    matches = [
        c
        for c in _critical(results)
        if "exceeds initial_capital" in c and "insufficient_capital" in c
    ]
    assert matches, _critical(results)


def test_rule5_nan_price_fails_closed() -> None:
    """A provider returning NaN must trip Rule 5 — fail closed."""
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=1000.0),
        target_symbols=["AAPL"],
        asset_class="stocks",
    )
    gate = SpecReadinessGate(market_sample_provider=lambda sym, asset_class: float("nan"))
    results = gate.validate(spec, backtest_config=_config())
    assert any("no usable price sample" in c for c in _critical(results))


def test_default_universe_for_futures_and_forex() -> None:
    """`_default_universe_for` must return matching asset-class symbols."""
    from investment_team.strategy_lab.quality_gates.spec_readiness import _default_universe_for
    from investment_team.symbols import FOREX_SYMBOLS, FUTURES_SYMBOLS

    assert _default_universe_for("futures") == list(FUTURES_SYMBOLS)
    assert _default_universe_for("forex") == list(FOREX_SYMBOLS)


def test_default_universe_for_raises_on_unknown_asset_class() -> None:
    """Unknown asset classes must raise instead of silently returning ETF list."""
    import pytest

    from investment_team.strategy_lab.quality_gates.spec_readiness import _default_universe_for

    with pytest.raises(ValueError, match="unknown asset_class 'bonds'"):
        _default_universe_for("bonds")


def test_rule5_unknown_asset_class_is_critical() -> None:
    """A non-canonical asset_class reaching the gate with empty target_symbols
    emits a Rule 5 critical.

    The ``StrategySpec`` boundary now rejects an off-vocabulary class on live
    construction, so build a valid spec and mutate the field afterwards
    (assignment skips validation) to simulate a class arriving at the gate
    without going through construction."""
    spec = _spec(
        asset_class="stocks",
        target_symbols=[],
        timeframe="1d",
        hypothesis="generic mean reversion",
    )
    spec.asset_class = "bonds"
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("unknown asset_class" in c for c in _critical(results))


def test_rule5_unknown_asset_class_with_target_symbols_fails_closed() -> None:
    """An unknown class mutated onto a spec that ALSO has explicit
    ``target_symbols`` must still fail closed. With ``target_symbols`` set, Rule
    5 skips the ``_default_universe_for`` strict check, and on a ``1d`` timeframe
    Rule 7 returns early too — so without a dedicated guard a ``bonds`` spec
    would pass readiness as a stock-like whole-lot strategy. The strict
    normalization at the top of Rule 5 surfaces it as a critical."""
    spec = _spec(
        asset_class="stocks",
        target_symbols=["TLT"],
        timeframe="1d",
        hypothesis="generic mean reversion",
    )
    spec.asset_class = "bonds"
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("unknown asset_class" in c for c in _critical(results))


def test_rule5_volatility_target_emits_warning() -> None:
    """Vol-target sizing cannot be evaluated statically — surface as warning."""
    from investment_team.strategy_lab.spec_dsl import VolatilityTargetSizing

    spec = _spec(
        sizing=VolatilityTargetSizing(target_annual_vol=0.15),
        target_symbols=["AAPL"],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    assert any("volatility_target" in w and "0.15" in w for w in warnings), warnings
    # No critical from Rule 5 (the rule abstained, not failed).
    sizing_criticals = [c for c in _critical(results) if "Sizing" in c or "qty=" in c]
    assert not sizing_criticals, sizing_criticals


def test_check_sizing_realisable_directly_catches_unknown_asset_class_value_error() -> None:
    """Direct probe of the rule's ``ValueError`` catch path.

    The end-to-end ``test_rule5_unknown_asset_class_is_critical`` exercises
    this transitively via the rule-table iteration in ``validate``; this
    unit test pins the catch-and-convert behaviour at the rule boundary so
    a future refactor that moves the asset-class resolution out of Rule 5
    fails loudly here rather than silently dropping the critical.
    """
    from investment_team.strategy_lab.quality_gates.spec_readiness import (
        SpecReadinessCtx,
        SpecReadinessGate,
    )

    spec = _spec(
        asset_class="stocks",
        target_symbols=[],
        timeframe="1d",
        hypothesis="generic mean reversion",
    )
    spec.asset_class = "bonds"
    gate = SpecReadinessGate()
    ctx = SpecReadinessCtx(spec=spec, config=_config())
    with gate._using_phase("design"):
        results = list(gate._check_sizing_realisable(ctx))
    assert len(results) == 1
    assert results[0].severity == "critical"
    assert "unknown asset_class" in results[0].details


def test_check_sizing_realisable_directly_emits_volatility_target_warning() -> None:
    """Direct probe of the rule's volatility-target abstention path."""
    from investment_team.strategy_lab.quality_gates.spec_readiness import (
        SpecReadinessCtx,
        SpecReadinessGate,
    )
    from investment_team.strategy_lab.spec_dsl import VolatilityTargetSizing

    spec = _spec(
        sizing=VolatilityTargetSizing(target_annual_vol=0.001),
        target_symbols=["AAPL"],
    )
    gate = SpecReadinessGate()
    ctx = SpecReadinessCtx(spec=spec, config=_config())
    with gate._using_phase("design"):
        results = list(gate._check_sizing_realisable(ctx))
    assert len(results) == 1
    assert results[0].severity == "warning"
    assert "volatility_target" in results[0].details
    assert "0.001" in results[0].details


def test_rule5_accepts_fractional_qty_on_crypto() -> None:
    """Crypto specs accept fractional positions — 0.1 BTC is implementable."""
    spec = _spec(
        asset_class="crypto",
        target_symbols=["BTC"],
        sizing=FixedNotionalSizing(notional_usd=10.0),
        hypothesis="BTC mean-reversion on the 1d timeframe.",
        # Replace the default RSI entry with a self-consistent one to avoid
        # tripping Rule 6 (hypothesis mentions reversion but no rsi term).
    )
    # Default provider returns $100/BTC → 0.1 BTC. Crypto allows fractional, pass.
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    sizing_failures = [c for c in _critical(results) if "qty=" in c]
    assert not sizing_failures, sizing_failures


def test_rule5_accepts_fractional_qty_on_forex() -> None:
    """Forex specs accept fractional positions — sub-lot sizing is valid."""
    spec = _spec(
        asset_class="forex",
        target_symbols=["EURUSD=X"],
        sizing=FixedNotionalSizing(notional_usd=50.0),
        hypothesis="EURUSD=X mean-reverts intraday on RSI(14).",
        timeframe="1h",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    sizing_failures = [c for c in _critical(results) if "qty=" in c]
    assert not sizing_failures, sizing_failures


def test_rule5_passes_with_realistic_sizing() -> None:
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.02),
        target_symbols=["AAPL"],
    )
    # 0.02 * $100k = $2000 / $100 = 20 shares.
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    sizing_failures = [c for c in _critical(results) if "qty=" in c]
    assert not sizing_failures, sizing_failures


def test_rule5_warns_on_persistent_nan_for_fractional_forex() -> None:
    """Forex (fractional sizing) must NOT critical-fail on a missing price,
    but a provider returning NaN for *every* symbol is downgraded to a
    warning rather than skipped silently.

    Fractional classes accept sub-lot sizing, so a transient provider gap
    (weekend, holiday, yfinance miss) must not block the cycle — otherwise a
    forex spec on a Saturday dies on Rule 5 before producing a trade and the
    system loses its learning signal. But a persistently broken provider is
    worth surfacing, so we warn (non-blocking) instead of returning nothing.
    """
    spec = _spec(
        asset_class="forex",
        target_symbols=["USDJPY=X"],
        sizing=FixedFractionSizing(fraction=0.02),
        hypothesis="USDJPY=X mean reversion via RSI(14).",
        timeframe="1h",
    )
    gate = SpecReadinessGate(market_sample_provider=lambda sym, ac: float("nan"))
    results = gate.validate(spec, backtest_config=_config())
    sizing_failures = [c for c in _critical(results) if "Sizing realisability" in c]
    assert not sizing_failures, sizing_failures
    warnings = [
        r.details
        for r in results
        if r.severity == "warning" and not r.passed and "no usable price sample" in r.details
    ]
    assert warnings, "expected a non-blocking warning for persistent NaN on forex"


def test_rule5_warns_on_persistent_nan_for_fractional_crypto() -> None:
    """Crypto (fractional sizing) inherits the same NaN-tolerance contract:
    persistent NaN warns, never criticals."""
    spec = _spec(
        asset_class="crypto",
        target_symbols=["BTC-USD"],
        sizing=FixedFractionSizing(fraction=0.02),
        hypothesis="BTC-USD momentum via RSI(14).",
    )
    gate = SpecReadinessGate(market_sample_provider=lambda sym, ac: float("nan"))
    results = gate.validate(spec, backtest_config=_config())
    sizing_failures = [c for c in _critical(results) if "Sizing realisability" in c]
    assert not sizing_failures, sizing_failures
    warnings = [
        r.details
        for r in results
        if r.severity == "warning" and not r.passed and "no usable price sample" in r.details
    ]
    assert warnings, "expected a non-blocking warning for persistent NaN on crypto"


def test_rule5_persistent_zero_price_is_critical_for_forex() -> None:
    """A finite but non-positive price (e.g. 0.0 parsed from a rate-limit
    body) signals a broken provider, not a market gap — critical for every
    asset class, including fractional forex/crypto."""
    spec = _spec(
        asset_class="forex",
        target_symbols=["USDJPY=X"],
        sizing=FixedFractionSizing(fraction=0.02),
        hypothesis="USDJPY=X mean reversion via RSI(14).",
        timeframe="1h",
    )
    gate = SpecReadinessGate(market_sample_provider=lambda sym, ac: 0.0)
    results = gate.validate(spec, backtest_config=_config())
    criticals = [c for c in _critical(results) if "Sizing realisability" in c]
    assert criticals, "expected a critical for a non-positive (broken-provider) price"


def test_rule5_transient_nan_with_finite_samples_does_not_fail() -> None:
    """A NaN for one symbol alongside a finite sample for another is a
    transient gap on a fractional class — neither critical nor warning."""
    spec = _spec(
        asset_class="crypto",
        target_symbols=["BTC-USD", "ETH-USD"],
        sizing=FixedFractionSizing(fraction=0.02),
        hypothesis="crypto momentum via RSI(14).",
    )
    # First symbol resolves to a finite price; second is a transient gap.
    provider = lambda sym, ac: 100.0 if sym == "BTC-USD" else float("nan")  # noqa: E731
    gate = SpecReadinessGate(market_sample_provider=provider)
    results = gate.validate(spec, backtest_config=_config())
    sizing_failures = [c for c in _critical(results) if "Sizing realisability" in c]
    assert not sizing_failures, sizing_failures
    sizing_warnings = [
        r.details
        for r in results
        if r.severity == "warning" and not r.passed and "Sizing realisability" in r.details
    ]
    assert not sizing_warnings, sizing_warnings


def test_rule5_still_fails_closed_on_whole_lot_class_with_nan_price() -> None:
    """Stocks (whole-lot) must still fail closed on NaN — the per-symbol
    fetch genuinely matters there (qty < 1 is unfillable). Pin this so a
    future refactor that skips too aggressively gets caught."""
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=1000.0),
        target_symbols=["AAPL"],
        asset_class="stocks",
    )
    gate = SpecReadinessGate(market_sample_provider=lambda sym, ac: float("nan"))
    results = gate.validate(spec, backtest_config=_config())
    assert any("no usable price sample" in c for c in _critical(results))


# ---------------------------------------------------------------------------
# Rule 6: Hypothesis–rule consistency
# ---------------------------------------------------------------------------


def test_rule6_hypothesis_mentions_indicator_not_in_rules_is_warning() -> None:
    """An indicator named in the hypothesis but absent from every predicate
    is prose hygiene, not an implementability failure: surface it as a
    warning so the design ↔ review loop can act on it, but do not block
    the cycle. Blocking on this lost the system its learning signal for
    every otherwise-runnable spec whose prose drifted from its rules.
    """
    spec = _spec(
        hypothesis="MACD bullish crossovers on AAPL signal long entries.",
        # entry rule uses RSI, not MACD
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    assert any("macd" in w.lower() for w in warnings), warnings
    # And explicitly NOT critical — that's the behavioural contract change.
    assert not any("macd" in c.lower() for c in _critical(results))


def test_rule1_matches_lowercase_ticker_in_hypothesis() -> None:
    """Rule 1's regex is case-insensitive: `gld` in hypothesis is caught.

    Uses GLD (whitelisted via OTHER_SYMBOLS / COMMODITY_SYMBOLS but not in
    the stocks default) so the empty-targets path still trips Rule 1 and
    the case-insensitivity assertion remains observable.
    """
    spec = _spec(
        hypothesis="gld mean-reverts to its 50-day moving average.",
        target_symbols=[],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("GLD" in c for c in _critical(results))


def test_rule1_passes_when_named_ticker_is_in_asset_class_default() -> None:
    """When target_symbols is empty but the hypothesis-named ticker is
    reachable from the asset-class default universe, Rule 1 passes —
    ``resolve_strategy_symbols`` will include the symbol via the default."""
    spec = _spec(
        hypothesis="QQQ trend continuation on the daily timeframe.",
        target_symbols=[],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule1_failures = [
        c for c in _critical(results) if "target_symbols" in c or "Hypothesis names symbol" in c
    ]
    assert not rule1_failures, rule1_failures


def test_rule1_critical_message_cites_default_universe_when_unreachable() -> None:
    """When target_symbols is empty and the named ticker is not in the
    asset-class default either, the critical message names the default
    universe so the refinement prompt has enough context to fix it."""
    spec = _spec(
        hypothesis="SLV momentum on the daily timeframe.",  # SLV not in stocks default
        target_symbols=[],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    critical = _critical(results)
    assert any(
        "SLV" in c and "default universe" in c and "target_symbols" in c for c in critical
    ), critical


def test_rule1_respects_universe_cap_when_default_is_truncated(monkeypatch) -> None:
    """The reachability check must compare the named ticker against the
    *capped* default that ``resolve_strategy_symbols`` will actually
    request, not the raw declared list. With a low cap, a ticker that
    sits beyond the slice would otherwise produce a false pass.

    QQQ lives near the tail of the stocks default; setting
    ``STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS=2`` truncates the slice to the
    first two large-cap entries so QQQ is *not* reachable. The gate must
    surface that as a critical.
    """
    monkeypatch.setenv("STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS", "2")
    spec = _spec(
        hypothesis="QQQ trend continuation on the daily timeframe.",
        target_symbols=[],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    critical = _critical(results)
    assert any("QQQ" in c and "default universe" in c for c in critical), critical


def test_rule1_accepts_asset_class_aliases_via_strict_normalize() -> None:
    """The runtime fetch path accepts ``equity`` / ``fx`` / ``commodity`` and
    friends via ``normalize_asset_class``. Rule 1's reachability check must
    apply the same alias mapping so otherwise-tradeable specs don't get a
    false critical when the LLM emits an alias instead of the canonical
    label. ``equity`` resolves to ``stocks`` and ``QQQ`` is in the stocks
    default, so Rule 1 must pass."""
    spec = _spec(
        asset_class="equity",
        hypothesis="QQQ trend continuation on the daily timeframe.",
        target_symbols=[],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule1_failures = [
        c for c in _critical(results) if "target_symbols" in c or "Hypothesis names symbol" in c
    ]
    assert not rule1_failures, rule1_failures


# ---------------------------------------------------------------------------
# asset_class canonicalization + enforcement at the StrategySpec boundary
# ---------------------------------------------------------------------------


def test_strategy_spec_canonicalizes_asset_class_aliases() -> None:
    """Accepted aliases are canonicalized at construction so every downstream
    asset_class-keyed gate sees a canonical label without re-encoding aliases."""
    cases = {
        "equity": "stocks",
        "equities": "stocks",
        "stock": "stocks",
        "etf": "stocks",
        "etfs": "stocks",
        "fx": "forex",
        "commodity": "commodities",
        "cryptocurrency": "crypto",
        "CRYPTO ": "crypto",
    }
    for raw, canonical in cases.items():
        assert _spec(asset_class=raw).asset_class == canonical, raw


def test_strategy_spec_rejects_unknown_asset_class_on_live_construction() -> None:
    """A typo'd / off-vocabulary asset_class is rejected at the spec boundary so
    it surfaces as a defect rather than silently bypassing the gates."""
    with pytest.raises(ValidationError):
        _spec(asset_class="bonds")


def test_strategy_spec_legacy_context_tolerates_unknown_asset_class() -> None:
    """Legacy deserialization must never fail to load a persisted row with an
    off-vocabulary asset_class — it falls back to the permissive mapping."""
    payload = _spec(asset_class="stocks").model_dump()
    payload["asset_class"] = "bonds"
    loaded = StrategySpec.model_validate(payload, context={"legacy_spec": True})
    assert loaded.asset_class == "stocks"


def test_rule5_enforces_whole_lot_for_equity_alias_end_to_end() -> None:
    """The reported bug: a spec authored with the ``equity`` alias must get the
    same whole-lot enforcement as ``stocks``. The alias canonicalizes at
    construction, so Rule 5 fires the sub-share critical instead of silently
    passing an unfillable equity spec to backtest."""
    spec = _spec(
        asset_class="equity",
        target_symbols=["AAPL"],
        sizing=FixedNotionalSizing(notional_usd=10.0),
        hypothesis="AAPL momentum on the 1d timeframe.",
    )
    assert spec.asset_class == "stocks"
    gate = SpecReadinessGate(market_sample_provider=lambda sym, ac: 1000.0)
    results = gate.validate(spec, backtest_config=_config())
    assert [c for c in _critical(results) if "qty=" in c], "expected sub-share qty critical"


def test_rule5_respects_universe_cap_when_default_is_truncated(monkeypatch) -> None:
    """Rule 5 sizes against the same capped universe ``resolve_strategy_symbols``
    will request — a tail symbol with a missing price sample beyond the cap
    must not fail readiness, since the fetcher will never request it.

    Setup: cap at 2, empty ``target_symbols``, asset_class ``stocks``. The
    market sample provider returns a usable price for the first two head
    symbols and ``nan`` for everything else. Without the cap, Rule 5 walks
    the entire default and trips on the third symbol; with the cap, only
    the first two are sized and the rule passes.
    """
    monkeypatch.setenv("STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS", "2")
    from investment_team.symbols import STOCK_SYMBOLS

    head = set(STOCK_SYMBOLS[:2])

    def sample_provider(symbol: str, asset_class: str) -> float:
        return 100.0 if symbol in head else float("nan")

    spec = _spec(
        asset_class="stocks",
        hypothesis="generic large-cap momentum",
        target_symbols=[],
    )
    gate = SpecReadinessGate(market_sample_provider=sample_provider)
    results = gate.validate(spec, backtest_config=_config())
    sizing_failures = [
        c for c in _critical(results) if "Sizing realisability" in c or "Sizing yields" in c
    ]
    assert not sizing_failures, sizing_failures


def test_rule6_moving_average_is_satisfied_by_ema() -> None:
    """'Moving average' in the hypothesis is satisfied by either SMA or EMA."""
    spec = _spec(
        hypothesis="AAPL crosses its EMA moving average to signal long entry.",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="ema", params={"period": 20}),
                    op="cross_above",
                    rhs="bar.close",
                ),
            ),
        ],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule6_failures = [c for c in _critical(results) if "moving average" in c]
    assert not rule6_failures, rule6_failures


def test_rule6_passes_when_hypothesis_and_rules_match() -> None:
    spec = _spec(
        hypothesis="RSI(14) below 30 on AAPL signals oversold conditions.",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    rule6_failures = [c for c in _critical(results) if "Hypothesis names indicator" in c]
    assert not rule6_failures, rule6_failures


# ---------------------------------------------------------------------------
# Rule 7: Timeframe data availability
# ---------------------------------------------------------------------------


def test_rule7_intraday_timeframe_on_commodities_is_critical() -> None:
    spec = _spec(
        asset_class="commodities",
        timeframe="5m",
        target_symbols=["GLD"],
        hypothesis="GLD intraday momentum.",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("intraday" in c and "commodities" in c for c in _critical(results))


def test_rule7_daily_timeframe_on_commodities_passes() -> None:
    spec = _spec(
        asset_class="commodities",
        timeframe="1d",
        target_symbols=["GLD"],
        hypothesis="GLD daily mean reversion.",
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    tf_failures = [c for c in _critical(results) if "intraday" in c]
    assert not tf_failures, tf_failures


def test_rule7_unknown_asset_class_on_intraday_fails_closed() -> None:
    """An off-vocabulary asset_class reaching the gate (via post-construction
    mutation, since the StrategySpec boundary rejects it on construction) must
    fail closed on an intraday timeframe via the strict normalizer, not
    permissively map to ``stocks`` and pass as if intraday data existed."""
    spec = _spec(
        asset_class="stocks",
        timeframe="5m",
        target_symbols=["AAPL"],
        hypothesis="AAPL intraday momentum.",
    )
    spec.asset_class = "bonds"
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("intraday" in c and "bonds" in c for c in _critical(results))


# ---------------------------------------------------------------------------
# Rule 8: Risk-limit coherence
# ---------------------------------------------------------------------------


def test_rule8_stop_loss_geq_take_profit_is_warning_not_critical() -> None:
    """A wider stop than profit target is a valid risk/reward choice — warn, don't block."""
    spec = _spec(
        exit_=[
            StopLossRule(pct=0.10),
            TakeProfitRule(pct=0.05),
        ],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    # No criticals about the stop/profit ratio.
    assert not any("stop_loss.pct" in c for c in _critical(results))
    # Warning surfaced so the refinement prompt notices the unusual asymmetry.
    warnings = [r.details for r in results if r.severity == "warning" and not r.passed]
    assert any("stop_loss.pct" in w for w in warnings), warnings


def test_rule8_max_position_pct_above_25_is_critical() -> None:
    spec = _spec(risk_limits={"max_position_pct": 30, "max_drawdown_pct": 10})
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any("max_position_pct=30" in c for c in _critical(results))


# ---------------------------------------------------------------------------
# Acceptance contract: end-to-end vague + well-formed cases
# ---------------------------------------------------------------------------


def test_vague_spec_returns_multiple_criticals() -> None:
    """A spec that is structurally valid but vague trips multiple rules.

    Uses GLD (whitelisted but not in the stocks default) so Rule 1 still
    fires from the empty-targets path. QQQ would no longer trip Rule 1
    now that it sits in the stocks default universe.
    """
    spec = _spec(
        hypothesis="enter on bullish momentum on GLD — MACD watch",
        entry=[],  # Rule 2: no entries
        exit_=[],  # Rule 4: no exits
        target_symbols=[],  # Rule 1: GLD named, no targets, not in stocks default
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    critical = _critical(results)
    # Expect at least three independent criticals across the three rules.
    assert len(critical) >= 3, critical
    assert any("entry" in c.lower() for c in critical)
    assert any("exit" in c.lower() for c in critical)
    assert any("GLD" in c for c in critical)


def test_well_formed_spec_passes() -> None:
    """RSI(14) cross_above 30, SMA(50) > SMA(200) — clean entry/exit."""
    spec = _spec(
        hypothesis="On AAPL, RSI(14) crossing above 30 with SMA(50) > SMA(200) marks long entry.",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="cross_above",
                    rhs=30.0,
                ),
            ),
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="sma", params={"period": 50}),
                    op=">",
                    rhs=IndicatorRef(name="sma", params={"period": 200}),
                ),
            ),
        ],
        exit_=[
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70.0,
                )
            ),
            StopLossRule(pct=0.03),
            TakeProfitRule(pct=0.10),
        ],
        sizing=FixedFractionSizing(fraction=0.02),
        target_symbols=["AAPL"],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert not _critical(results), _critical(results)
    # Single passing summary result
    assert any(r.gate_name == GATE and r.passed and r.severity == "info" for r in results)


# ---------------------------------------------------------------------------
# Phase tagging
# ---------------------------------------------------------------------------


def test_phase_tag_propagates_to_results() -> None:
    spec = _spec()
    design = SpecReadinessGate().validate(spec, phase="design", backtest_config=_config())
    synth = SpecReadinessGate().validate(spec, phase="synthesis", backtest_config=_config())
    assert all(r.phase == "design" for r in design)
    assert all(r.phase == "synthesis" for r in synth)


def test_orchestrator_wires_readiness_price_provider() -> None:
    """``StrategyLabOrchestrator`` constructs the gate with a real-price provider.

    The provider invokes ``MarketDataService.fetch_ohlcv``; we verify the
    wiring by monkeypatching the service to return a sentinel close and
    confirming Rule 5 uses that price.
    """
    from investment_team.market_data_service import OHLCVBar
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    orch = StrategyLabOrchestrator()

    sentinel_bar = OHLCVBar(
        date="2024-06-01",
        open=950.0,
        high=960.0,
        low=940.0,
        close=950.0,
        volume=1_000_000,
    )
    orch.market_data_service.fetch_ohlcv = lambda symbol, asset_class, days=5: [sentinel_bar]

    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=500.0),
        target_symbols=["NVDA"],
        asset_class="stocks",
    )
    # $500 notional / $950 = 0.526 share — fails the whole-lot check.
    results = orch.spec_readiness_gate.validate(spec, backtest_config=_config())
    assert any("qty=" in c and "NVDA" in c for c in _critical(results))


def test_readiness_price_provider_fails_closed_on_data_failure() -> None:
    """Provider returns ``NaN`` when the data service raises, so Rule 5 fails closed."""
    import math

    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    orch = StrategyLabOrchestrator()

    def boom(*_a, **_kw):
        raise RuntimeError("network down")

    orch.market_data_service.fetch_ohlcv = boom
    price = orch._readiness_price_provider("AAPL", "stocks")
    assert math.isnan(price)


def test_readiness_price_provider_fails_closed_on_empty_bars() -> None:
    """Provider returns ``NaN`` when the data service returns no bars."""
    import math

    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    orch = StrategyLabOrchestrator()
    orch.market_data_service.fetch_ohlcv = lambda *_a, **_kw: []
    price = orch._readiness_price_provider("AAPL", "stocks")
    assert math.isnan(price)


def test_custom_market_sample_provider_is_used() -> None:
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=500.0),
        target_symbols=["AAPL"],
    )
    # Pretend AAPL is $1000 — $500 notional yields 0.5 share, should fail.
    gate = SpecReadinessGate(market_sample_provider=lambda sym, asset_class: 1000.0)
    results = gate.validate(spec, backtest_config=_config())
    assert any("qty=" in c for c in _critical(results))


# ---------------------------------------------------------------------------
# Rule 9: Position-sizing / risk-policy coherence
# ---------------------------------------------------------------------------


def _warning(results) -> list[str]:
    return [r.details for r in results if r.severity == "warning" and not r.passed]


def test_rule9_coherent_spec_is_silent() -> None:
    """fraction=0.05, stop=0.05, cap=5, no loss tolerance, no position prose.

    The corrected core case: a 5% position with a 5% stop and a 5% cap is
    fully consistent (per-trade loss is 0.25% of equity, well within the
    deployed 5%). Rule 9 must emit nothing.
    """
    spec = _spec(
        hypothesis="RSI(14) below 30 on AAPL signals long entry.",
        exit_=[
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70.0
                )
            ),
            StopLossRule(pct=0.05),
        ],
        sizing=FixedFractionSizing(fraction=0.05),
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert not any(
        (r.rule_id or "").startswith(("sizing:", "risk_limits:", "hypothesis:position"))
        for r in results
        if not r.passed
    ), [r.details for r in results if not r.passed]


def test_rule9_check_a_fraction_exceeds_cap_is_critical() -> None:
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.10),
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    crit = [
        r.details
        for r in results
        if r.severity == "critical" and r.rule_id == "sizing:position_cap"
    ]
    assert crit, _critical(results)
    assert "10.00%" in crit[0] and "max_position_pct=5.00%" in crit[0]


def test_rule9_check_a_fraction_equal_cap_is_silent() -> None:
    """Boundary: fraction == cap is consistent (not a violation)."""
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.05),
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert not any(r.rule_id == "sizing:position_cap" for r in results if not r.passed)


def test_rule9_check_a_fixed_notional_over_cap_is_critical() -> None:
    # Default capital is $100k; cap 5% → $5,000. $20k notional deploys 20%.
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=20_000.0),
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any(r.rule_id == "sizing:position_cap" for r in results if not r.passed)


def test_rule9_check_a_fixed_notional_under_cap_is_silent() -> None:
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=4_000.0),  # 4% of $100k <= 5% cap
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert not any(r.rule_id == "sizing:position_cap" for r in results if not r.passed)


def test_rule9_check_a_fixed_notional_skipped_without_config() -> None:
    """No config → notional cannot be expressed as a fraction → Check A skipped."""
    spec = _spec(
        sizing=FixedNotionalSizing(notional_usd=90_000.0),
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec)  # no backtest_config
    assert not any(r.rule_id == "sizing:position_cap" for r in results if not r.passed)


def test_rule9_volatility_target_sizing_is_silent() -> None:
    """Vol-target sizing has no static deployed fraction, so Checks A and C
    abstain. With no per-trade loss tolerance declared, Check B is also silent."""
    spec = _spec(
        sizing=VolatilityTargetSizing(target_annual_vol=0.15),
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert not any(
        (r.rule_id or "").startswith(("sizing:", "risk_limits:loss"))
        for r in results
        if not r.passed
    )


def test_rule9_check_b_runs_for_volatility_target_sizing() -> None:
    """Check B compares two static risk-limit fields, so it must fire even for
    volatility_target sizing (whose deployed fraction is dynamic/unknown)."""
    spec = _spec(
        sizing=VolatilityTargetSizing(target_annual_vol=0.15),
        risk_limits={"max_position_pct": 10, "max_loss_per_trade_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    crit = [
        r.details
        for r in results
        if r.severity == "critical" and r.rule_id == "risk_limits:loss_tolerance"
    ]
    assert crit, _critical(results)
    assert "max_position_pct=10.00%" in crit[0] and "max_loss_per_trade_pct=5.00%" in crit[0]


def test_rule9_check_b_cap_exceeds_loss_tolerance_is_critical() -> None:
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.05),
        risk_limits={"max_position_pct": 10, "max_loss_per_trade_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    crit = [
        r.details
        for r in results
        if r.severity == "critical" and r.rule_id == "risk_limits:loss_tolerance"
    ]
    assert crit, _critical(results)
    assert "max_position_pct=10.00%" in crit[0] and "max_loss_per_trade_pct=5.00%" in crit[0]


def test_rule9_check_a_small_overage_is_critical_not_tolerated() -> None:
    """The prose tolerance must not slacken the hard sizing cap: a fraction
    just over the cap (10.5% vs 10%) is a real breach and must be critical."""
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.105),
        risk_limits={"max_position_pct": 10, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any(r.severity == "critical" and r.rule_id == "sizing:position_cap" for r in results), (
        _critical(results)
    )


def test_rule9_check_a_exactly_at_cap_is_silent() -> None:
    """Deploying exactly the cap (10% == 10%) is allowed — float noise only."""
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.10),
        risk_limits={"max_position_pct": 10, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert not any(r.rule_id == "sizing:position_cap" for r in results if not r.passed)


def test_rule9_check_b_small_overage_is_critical_not_tolerated() -> None:
    """A cap just over the loss tolerance (5.2% vs 5%, within the old 5% band)
    is a real breach and must be critical now that A/B are strict."""
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.05),
        risk_limits={"max_position_pct": 5.2, "max_loss_per_trade_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert any(
        r.severity == "critical" and r.rule_id == "risk_limits:loss_tolerance" for r in results
    ), _critical(results)


def test_rule9_check_b_cap_within_loss_tolerance_is_silent() -> None:
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.05),
        risk_limits={"max_position_pct": 5, "max_loss_per_trade_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert not any(r.rule_id == "risk_limits:loss_tolerance" for r in results if not r.passed)


def test_rule9_check_b_skipped_when_no_loss_tolerance() -> None:
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.02),
        risk_limits={"max_position_pct": 20, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert not any(r.rule_id == "risk_limits:loss_tolerance" for r in results if not r.passed)


def test_rule9_check_c_prose_disagrees_is_warning() -> None:
    spec = _spec(
        hypothesis="Allocate 10% per trade to AAPL on RSI mean reversion.",
        sizing=FixedFractionSizing(fraction=0.02),
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    warns = [r.details for r in results if r.rule_id == "hypothesis:position_pct"]
    assert warns, _warning(results)
    assert "10.00%" in warns[0]
    # Prose-only mismatch must not be a critical.
    assert not any(
        r.rule_id == "hypothesis:position_pct" and r.severity == "critical" for r in results
    )


def test_rule9_check_c_prose_matches_actual_deployment_is_silent() -> None:
    """Prose agrees with the actual deployed fraction (sizing.fraction=5%),
    which also happens to equal the cap — no warning."""
    spec = _spec(
        hypothesis="Allocate 5% per trade to AAPL.",
        sizing=FixedFractionSizing(fraction=0.05),
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert not any(r.rule_id == "hypothesis:position_pct" for r in results if not r.passed)


def test_rule9_check_c_prose_matches_cap_not_fraction_warns() -> None:
    """Prose matching the cap must not suppress the warning when it disagrees
    with the ACTUAL deployed fraction — the engine deploys sizing.fraction, not
    the cap, so 'allocate 10%' with fraction=0.02 (2% deployed) and cap=10 is a
    real prose↔deployment contradiction."""
    spec = _spec(
        hypothesis="Allocate 10% per trade to AAPL on RSI mean reversion.",
        sizing=FixedFractionSizing(fraction=0.02),
        risk_limits={"max_position_pct": 10, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    warns = [r.details for r in results if r.rule_id == "hypothesis:position_pct"]
    assert warns, _warning(results)
    assert "10.00%" in warns[0] and "2.00%" in warns[0]
    assert not any(
        r.rule_id == "hypothesis:position_pct" and r.severity == "critical" for r in results
    )


def test_rule9_check_c_no_position_prose_is_silent() -> None:
    """A stop-loss percentage in prose must not be read as a deployment %."""
    spec = _spec(
        hypothesis="Exit AAPL on a 5% stop loss after an RSI(14) cross below 30.",
        sizing=FixedFractionSizing(fraction=0.02),
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert not any(r.rule_id == "hypothesis:position_pct" for r in results if not r.passed)


def test_rule9_details_are_deterministic() -> None:
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.10),
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
    )
    gate = SpecReadinessGate()
    first = [r.details for r in gate.validate(spec, backtest_config=_config())]
    second = [r.details for r in gate.validate(spec, backtest_config=_config())]
    assert first == second


def test_readiness_signature_changes_with_hypothesis() -> None:
    """Rule 9 reads ``hypothesis``, so the design-loop cache signature must
    change when only the prose changes — otherwise a prose-only revision reuses
    a stale ``hypothesis:position_pct`` verdict."""
    from investment_team.strategy_lab.orchestrator import _spec_readiness_signature

    base = _spec(hypothesis="Allocate 10% per trade to AAPL.")
    revised = _spec(hypothesis="Allocate 2% per trade to AAPL.")
    assert _spec_readiness_signature(base) != _spec_readiness_signature(revised)
    # And identical specs still produce identical signatures (cache hits).
    assert _spec_readiness_signature(base) == _spec_readiness_signature(
        _spec(hypothesis="Allocate 10% per trade to AAPL.")
    )


# --- _extract_prose_position_pct unit coverage ---


@pytest.mark.parametrize(
    "text,expected",
    [
        ("deploy 5% per trade", 5.0),
        ("Allocate 10% per trade to AAPL", 10.0),
        ("use up to 7.5% per trade", 7.5),
        ("risk 5% per trade", 5.0),
        ("commit ~3% of the account per trade", 3.0),
        ("invest 8% in each position", 8.0),
        ("12% per-trade allocation", 12.0),
        ("4% position size", 4.0),
        ("position sizing of 6%", 6.0),
        ("position size of 9%", 9.0),
    ],
)
def test_extract_prose_position_pct_positive(text, expected) -> None:
    assert _extract_prose_position_pct(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Exit on a 5% stop loss.",
        "Take profit at 10%.",
        "Cap the max drawdown at 25%.",
        "risks 0.25% of equity per trade",  # a LOSS statement, not deployment
        "RSI(14) below 30 signals entry.",
    ],
)
def test_extract_prose_position_pct_negative(text) -> None:
    assert _extract_prose_position_pct(text) is None


def test_sizing_coherence_rel_tol_default_and_overrides(monkeypatch) -> None:
    monkeypatch.delenv("STRATEGY_LAB_SIZING_COHERENCE_TOLERANCE", raising=False)
    assert _sizing_coherence_rel_tol() == 0.05
    monkeypatch.setenv("STRATEGY_LAB_SIZING_COHERENCE_TOLERANCE", "0.2")
    assert _sizing_coherence_rel_tol() == 0.2
    monkeypatch.setenv("STRATEGY_LAB_SIZING_COHERENCE_TOLERANCE", "garbage")
    assert _sizing_coherence_rel_tol() == 0.05
    monkeypatch.setenv("STRATEGY_LAB_SIZING_COHERENCE_TOLERANCE", "-1")
    assert _sizing_coherence_rel_tol() == 0.05
