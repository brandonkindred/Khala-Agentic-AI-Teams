"""Deterministic implementability gate for `StrategySpec`.

Runs in the design phase (before any code is written) and again as the first
gate of the synthesis phase to confirm the spec wasn't mutated. Eight
deterministic rules, each unit-testable and failing closed.

Several rules overlap with :class:`StrategySpecValidator`. The overlap is
intentional: this gate is self-contained, runs at a different phase, and
escalates a subset of the overlapping items to critical severity.
"""

from __future__ import annotations

import math
import re
from typing import Callable, ClassVar, Iterable, Iterator, List, Optional

from ...models import BacktestConfig, StrategySpec
from ...symbols import (
    COMMODITY_SYMBOLS,
    CRYPTO_SYMBOLS,
    FOREX_SYMBOLS,
    FOREX_SYMBOLS_BARE,
    FUTURES_SYMBOLS,
    FUTURES_SYMBOLS_BARE,
    OTHER_SYMBOLS,
    STOCK_SYMBOLS,
)
from ..spec_dsl import (
    EntryRule,
    IndicatorName,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
)
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "spec_readiness"

# Whitelist of every tradeable symbol the spec is allowed to name in its
# hypothesis. Covers all five asset classes plus the broad ETFs; word-bounded
# so "ETH" doesn't match "ETHEREUM" and "ES" doesn't match arbitrary prose.
_SYMBOL_WHITELIST: frozenset[str] = frozenset(
    {
        *STOCK_SYMBOLS,
        *CRYPTO_SYMBOLS,
        *COMMODITY_SYMBOLS,
        *FOREX_SYMBOLS,
        *FOREX_SYMBOLS_BARE,
        *FUTURES_SYMBOLS,
        *FUTURES_SYMBOLS_BARE,
        *OTHER_SYMBOLS,
    }
)
# `=X` / `=F` suffixes are non-word characters, so `\b` after the literal `F`
# / `X` still sits at a word/non-word boundary. Longest-first alternation
# ensures `ES=F` is preferred over the bare `ES` when both could match.
_SYMBOL_REGEX = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, _SYMBOL_WHITELIST), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# Asset classes whose intraday timeframes the data-provider chain supports.
_FULL_TIMEFRAME_ASSET_CLASSES: frozenset[str] = frozenset({"stocks", "crypto"})

# Asset classes that trade in whole units (shares / contracts). Crypto and
# forex accept fractional quantities, so Rule 5's whole-lot check is skipped
# for them — the runtime contract takes ``qty: float = Field(gt=0)``.
_WHOLE_LOT_ASSET_CLASSES: frozenset[str] = frozenset({"stocks", "futures", "commodities"})

# Authoritative set of DSL indicator names. A constructed IndicatorRef always
# satisfies this set; the gate enforces it again as defense-in-depth against
# a future refactor that bypasses Pydantic.
_KNOWN_INDICATOR_NAMES: frozenset[str] = frozenset(IndicatorName.__args__)

# Indicators whose `params` must include a specific key. `rsi` is intentionally
# absent: the DSL ships a `period=14` default, so a bare `IndicatorRef(name="rsi")`
# is fully realisable.
_INDICATOR_REQUIRED_PARAMS: dict[str, frozenset[str]] = {
    "sma": frozenset({"period"}),
    "ema": frozenset({"period"}),
}

# Indicator concept vocabulary for prose mentions in the hypothesis.
_CONCEPT_TERMS = re.compile(
    r"\b(rsi|macd|moving\s+average|ema|sma|bollinger|atr|stochastic|adx|vwap)\b",
    re.IGNORECASE,
)
# Map each prose concept to the set of DSL indicator names that satisfy it.
# A concept is "orphan" iff *none* of its allowed indicators appears in the
# spec's predicates — so "moving average" is satisfied by either SMA or EMA.
_CONCEPT_TO_INDICATOR_NAMES: dict[str, frozenset[str]] = {
    "rsi": frozenset({"rsi"}),
    "macd": frozenset({"macd"}),
    "moving average": frozenset({"sma", "ema"}),
    "ema": frozenset({"ema"}),
    "sma": frozenset({"sma"}),
    "bollinger": frozenset({"bollinger"}),
    "atr": frozenset({"atr"}),
    "stochastic": frozenset({"stochastic"}),
    "adx": frozenset({"adx"}),
    "vwap": frozenset({"vwap"}),
}


MarketSampleProvider = Callable[[str, str], float]
"""(symbol, asset_class) → recent close price in USD.

Implementations must return a strictly positive finite float. NaN / inf /
non-positive values are interpreted as a missing price and fail Rule 5 closed.
"""


def _default_universe_for(asset_class: str) -> List[str]:
    # Pre: asset_class is a non-empty string.
    assert isinstance(asset_class, str) and asset_class, "asset_class must be a non-empty str"

    ac = asset_class.lower()
    if ac == "stocks":
        out = list(STOCK_SYMBOLS)
    elif ac == "crypto":
        out = list(CRYPTO_SYMBOLS)
    elif ac == "commodities":
        out = list(COMMODITY_SYMBOLS)
    elif ac == "forex":
        out = list(FOREX_SYMBOLS)
    elif ac == "futures":
        out = list(FUTURES_SYMBOLS)
    else:
        out = list(OTHER_SYMBOLS)

    # Post: returns a non-empty list of upper-case ticker strings.
    assert out and all(isinstance(s, str) and s for s in out), "default universe must be non-empty"
    return out


def _canonicalize_ticker(symbol: str) -> str:
    # Strip Yahoo provider suffixes so bare aliases compare equal to their
    # provider-form counterparts. Post: returns an upper-cased string with no
    # `=F` / `=X` suffix.
    assert isinstance(symbol, str), "symbol must be a str"
    s = symbol.upper()
    for suffix in ("=F", "=X"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    assert not s.endswith(("=F", "=X")), "canonical form must not retain a provider suffix"
    return s


def _default_market_sample_provider(symbol: str, asset_class: str) -> float:
    # Pre: symbol and asset_class are non-empty strings.
    assert isinstance(symbol, str) and symbol, "symbol must be a non-empty str"
    assert isinstance(asset_class, str) and asset_class, "asset_class must be a non-empty str"
    # Post: returns a strictly positive finite price. The static fallback is
    # intentionally arbitrary — real callers inject a `MarketDataService`-backed
    # provider so high-priced symbols are sized against a real recent close.
    price = 100.0
    assert price > 0, "price must be strictly positive"
    return price


class SpecReadinessGate(GateResultsMixin):
    """Deterministic implementability checks on a constructed ``StrategySpec``.

    Contract (class invariant): every call to :meth:`validate` returns a
    non-empty ``List[QualityGateResult]``. Every result in that list carries
    the ``phase`` argument the caller supplied and has ``gate_name == GATE``.
    """

    GATE: ClassVar[str] = GATE

    def __init__(
        self,
        *,
        market_sample_provider: Optional[MarketSampleProvider] = None,
        backtest_config: Optional[BacktestConfig] = None,
    ) -> None:
        # Pre: market_sample_provider, if supplied, must be callable.
        assert market_sample_provider is None or callable(market_sample_provider), (
            "market_sample_provider must be callable or None"
        )
        # Pre: backtest_config, if supplied, must be a BacktestConfig.
        assert backtest_config is None or isinstance(backtest_config, BacktestConfig), (
            "backtest_config must be a BacktestConfig or None"
        )

        self._market_sample_provider: MarketSampleProvider = (
            market_sample_provider or _default_market_sample_provider
        )
        self._backtest_config = backtest_config
        # Per-call config slot — set by ``validate()`` before any rule runs so
        # individual ``_check_*`` methods can read it without an extra
        # parameter. Reset back to ``None`` on exit so the gate can be safely
        # re-used across cycles.
        self._call_config: Optional[BacktestConfig] = None

        # Post: provider is callable, config slot is the supplied value or None.
        assert callable(self._market_sample_provider), "provider slot must be callable"

    def validate(
        self,
        spec: StrategySpec,
        *,
        phase: StrategyLabPhase = "design",
        backtest_config: Optional[BacktestConfig] = None,
    ) -> List[QualityGateResult]:
        """Run every readiness rule and return one result list.

        Pre: ``spec`` is a StrategySpec; ``phase`` is a valid phase literal.
        Post: result list is non-empty; every entry carries the caller's
        ``phase`` and ``gate_name == GATE``.
        """
        assert isinstance(spec, StrategySpec), "spec must be a StrategySpec"
        assert backtest_config is None or isinstance(backtest_config, BacktestConfig), (
            "backtest_config override must be a BacktestConfig or None"
        )
        self._set_phase(phase)
        self._call_config = backtest_config or self._backtest_config
        try:
            results: List[QualityGateResult] = [
                r for rule in self._RULES for r in rule(self, spec)
            ]
            if not results:
                results.append(self._info("Strategy spec passed all readiness checks."))
            # Post: every result carries the caller's phase and GATE name.
            assert all(r.phase == phase for r in results), (
                "every result must carry the caller's phase"
            )
            assert all(r.gate_name == GATE for r in results), (
                "every result must carry GATE name"
            )
            return results
        finally:
            self._call_config = None

    # ------------------------------------------------------------------
    # Rule 1: Universe set — every whitelisted ticker named in the
    # hypothesis must also appear in ``target_symbols`` (after stripping
    # Yahoo provider suffixes).
    # ------------------------------------------------------------------
    def _check_universe_set(self, spec: StrategySpec) -> Iterable[QualityGateResult]:
        assert isinstance(spec, StrategySpec)
        named_raw = {m.group(0).upper() for m in _SYMBOL_REGEX.finditer(spec.hypothesis or "")}
        targets_raw = {s.upper() for s in spec.target_symbols}
        named = {_canonicalize_ticker(s) for s in named_raw}
        targets = {_canonicalize_ticker(s) for s in targets_raw}
        if not named or named <= targets:
            return ()
        if not targets:
            return (
                self._critical(
                    f"Hypothesis names symbol(s) {sorted(named_raw)} but "
                    "target_symbols is empty — backtest universe would not "
                    "include the symbols the strategy is about."
                ),
            )
        missing_canon = named - targets
        missing_raw = sorted(s for s in named_raw if _canonicalize_ticker(s) in missing_canon)
        return (
            self._critical(
                f"Hypothesis names symbol(s) {missing_raw} not "
                f"present in target_symbols {sorted(targets_raw)}."
            ),
        )

    # ------------------------------------------------------------------
    # Rule 2: Entry rules non-trivial.
    # ------------------------------------------------------------------
    def _check_entry_rules_non_trivial(self, spec: StrategySpec) -> Iterable[QualityGateResult]:
        assert isinstance(spec, StrategySpec)
        if not spec.entry_rules:
            return (self._critical("No entry rules — strategy cannot generate trades."),)
        for idx, rule in enumerate(spec.entry_rules):
            if not isinstance(rule, EntryRule):
                return (
                    self._critical(
                        f"entry_rules[{idx}] is not a structured EntryRule "
                        f"(got {type(rule).__name__})."
                    ),
                )
            if not isinstance(rule.when, Predicate):
                return (
                    self._critical(
                        f"entry_rules[{idx}].when is not a Predicate "
                        f"(got {type(rule.when).__name__})."
                    ),
                )
        return ()

    # ------------------------------------------------------------------
    # Rule 3: Indicator validity.
    # ------------------------------------------------------------------
    def _check_indicator_validity(self, spec: StrategySpec) -> Iterable[QualityGateResult]:
        assert isinstance(spec, StrategySpec)
        for ref in self._iter_indicator_refs(spec):
            if ref.name not in _KNOWN_INDICATOR_NAMES:
                return (
                    self._critical(
                        f"Indicator '{ref.name}' is not in the supported "
                        f"set {sorted(_KNOWN_INDICATOR_NAMES)}."
                    ),
                )
            required = _INDICATOR_REQUIRED_PARAMS.get(ref.name, frozenset())
            missing = sorted(required - set(ref.params.keys()))
            if missing:
                return (
                    self._critical(
                        f"Indicator '{ref.name}' is missing required param(s) {missing}."
                    ),
                )
        return ()

    # ------------------------------------------------------------------
    # Rule 4: Exit completeness.
    # ------------------------------------------------------------------
    def _check_exit_completeness(self, spec: StrategySpec) -> Iterable[QualityGateResult]:
        assert isinstance(spec, StrategySpec)
        allowed_kinds = {"signal_exit", "stop_loss", "take_profit"}
        if not spec.exit_rules:
            return (
                self._critical(
                    "No exit rules — positions would never close. Add at "
                    "least one of: signal_exit, stop_loss, take_profit."
                ),
            )
        if not any(getattr(r, "kind", None) in allowed_kinds for r in spec.exit_rules):
            return (
                self._critical(
                    f"exit_rules contains no rule of kind in {sorted(allowed_kinds)}."
                ),
            )
        return ()

    # ------------------------------------------------------------------
    # Rule 5: Sizing realisable.
    # ------------------------------------------------------------------
    def _check_sizing_realisable(self, spec: StrategySpec) -> Iterable[QualityGateResult]:
        assert isinstance(spec, StrategySpec)
        config = self._call_config
        if config is None:
            return ()

        kind = getattr(spec.sizing, "kind", None)
        symbols = spec.target_symbols or _default_universe_for(spec.asset_class)
        if not symbols:
            return ()
        capital = config.initial_capital
        assert capital > 0, "initial_capital must be strictly positive"
        enforce_whole_lot = spec.asset_class.lower() in _WHOLE_LOT_ASSET_CLASSES
        threshold = 1.0 if enforce_whole_lot else 0.0

        for sym in symbols:
            try:
                price = float(self._market_sample_provider(sym, spec.asset_class))
            except Exception:
                price = float("nan")
            if not math.isfinite(price) or price <= 0:
                return (
                    self._critical(
                        f"Sizing realisability: no usable price sample for '{sym}' (got {price!r})."
                    ),
                )
            if kind == "fixed_fraction":
                notional = capital * float(spec.sizing.fraction)
            elif kind == "fixed_notional":
                notional = float(spec.sizing.notional_usd)
            else:
                # volatility_target needs realised vol — cannot evaluate here.
                continue
            qty = notional / price
            if qty < threshold or (not enforce_whole_lot and qty <= threshold):
                return (
                    self._critical(
                        f"Sizing yields qty={qty:.4f} (threshold {threshold}) "
                        f"for symbol '{sym}' at sample price ${price:.2f} "
                        f"with capital ${capital:.0f}."
                    ),
                )
        return ()

    # ------------------------------------------------------------------
    # Rule 6: Hypothesis–rule consistency.
    # ------------------------------------------------------------------
    def _check_hypothesis_rule_consistency(
        self, spec: StrategySpec
    ) -> Iterable[QualityGateResult]:
        assert isinstance(spec, StrategySpec)
        terms = {
            re.sub(r"\s+", " ", m.group(0).lower())
            for m in _CONCEPT_TERMS.finditer(spec.hypothesis or "")
        }
        referenced = {ref.name for ref in self._iter_indicator_refs(spec)}
        # A concept fires only when *none* of its allowed indicators is referenced,
        # so "moving average" is satisfied by either SMA or EMA.
        orphan = sorted(
            t
            for t in terms
            if (names := _CONCEPT_TO_INDICATOR_NAMES.get(t)) is not None
            and not (names & referenced)
        )
        if not orphan:
            return ()
        return (
            self._critical(
                f"Hypothesis names indicator concept(s) {orphan} "
                "that no entry/exit rule references."
            ),
        )

    # ------------------------------------------------------------------
    # Rule 7: Timeframe data availability.
    # ------------------------------------------------------------------
    def _check_timeframe_availability(self, spec: StrategySpec) -> Iterable[QualityGateResult]:
        assert isinstance(spec, StrategySpec)
        if spec.timeframe == "1d" or spec.asset_class.lower() in _FULL_TIMEFRAME_ASSET_CLASSES:
            return ()
        return (
            self._critical(
                f"Asset class '{spec.asset_class}' has no reliable "
                f"intraday data for timeframe '{spec.timeframe}'; "
                "use '1d' or pick stocks/crypto."
            ),
        )

    # ------------------------------------------------------------------
    # Rule 8: Risk-limit coherence — independent stop/profit and position
    # caps; both sub-checks can fire on the same spec.
    # ------------------------------------------------------------------
    def _check_risk_limit_coherence(self, spec: StrategySpec) -> Iterable[QualityGateResult]:
        assert isinstance(spec, StrategySpec)
        out: List[QualityGateResult] = []

        stop_losses = [r for r in spec.exit_rules if isinstance(r, StopLossRule)]
        take_profits = [r for r in spec.exit_rules if isinstance(r, TakeProfitRule)]
        if stop_losses and take_profits:
            min_tp = min(r.pct for r in take_profits)
            max_sl = max(r.pct for r in stop_losses)
            assert min_tp > 0 and max_sl > 0, "exit-rule pcts must be strictly positive"
            if max_sl >= min_tp:
                out.append(
                    self._critical(
                        f"stop_loss.pct={max_sl} ≥ take_profit.pct={min_tp} — "
                        "stop would trigger before profit target."
                    )
                )

        if spec.risk_limits.max_position_pct > 25:
            out.append(
                self._critical(
                    f"max_position_pct={spec.risk_limits.max_position_pct}% "
                    "exceeds the 25% cap for a single-position risk budget."
                )
            )
        return out

    # ------------------------------------------------------------------
    # Rule registry — declarative list iterated by ``validate``. Order is
    # preserved so error messages remain stable across runs.
    # ------------------------------------------------------------------
    _RULES: ClassVar[tuple] = (
        _check_universe_set,
        _check_entry_rules_non_trivial,
        _check_indicator_validity,
        _check_exit_completeness,
        _check_sizing_realisable,
        _check_hypothesis_rule_consistency,
        _check_timeframe_availability,
        _check_risk_limit_coherence,
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_indicator_refs(spec: StrategySpec) -> Iterator[IndicatorRef]:
        assert isinstance(spec, StrategySpec)
        for rule in spec.entry_rules:
            yield from SpecReadinessGate._predicate_indicator_refs(rule.when)
        for rule in spec.exit_rules:
            if isinstance(rule, SignalExitRule):
                yield from SpecReadinessGate._predicate_indicator_refs(rule.when)

    @staticmethod
    def _predicate_indicator_refs(pred: Predicate) -> Iterator[IndicatorRef]:
        assert isinstance(pred, Predicate)
        if isinstance(pred.lhs, IndicatorRef):
            yield pred.lhs
        if isinstance(pred.rhs, IndicatorRef):
            yield pred.rhs
