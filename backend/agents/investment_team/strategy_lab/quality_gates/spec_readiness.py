"""Deterministic implementability gate for `StrategySpec`.

Runs in the design phase (before any code is written) and again as the first
gate of the synthesis phase to confirm the spec wasn't mutated. Eight
deterministic rules, each unit-testable and failing closed.

Several rules overlap with :class:`StrategySpecValidator`. The overlap is
intentional: this gate is self-contained, runs at a different phase, and
escalates a subset of the overlapping items to critical severity.
"""

from __future__ import annotations

import re
from typing import Callable, Iterator, List, Optional

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
from .models import QualityGateResult, StrategyLabPhase

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
    r"\b(" + "|".join(sorted(map(re.escape, _SYMBOL_WHITELIST), key=len, reverse=True)) + r")\b"
)

# Asset classes whose intraday timeframes the data-provider chain supports.
_FULL_TIMEFRAME_ASSET_CLASSES: frozenset[str] = frozenset({"stocks", "crypto"})

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

_VALID_PHASES: frozenset[str] = frozenset({"design", "design_review", "synthesis", "verification"})

# Indicator concept vocabulary for prose mentions in the hypothesis.
_CONCEPT_TERMS = re.compile(
    r"\b(rsi|macd|moving\s+average|ema|sma|bollinger|atr|stochastic|adx|vwap)\b",
    re.IGNORECASE,
)
_CONCEPT_TO_INDICATOR_NAME: dict[str, str] = {
    "rsi": "rsi",
    "macd": "macd",
    "moving average": "sma",
    "ema": "ema",
    "sma": "sma",
    "bollinger": "bollinger",
    "atr": "atr",
    "stochastic": "stochastic",
    "adx": "adx",
    "vwap": "vwap",
}


MarketSampleProvider = Callable[[str], float]


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
    else:
        out = list(OTHER_SYMBOLS)

    # Post: returns a non-empty list of upper-case ticker strings.
    assert out and all(isinstance(s, str) and s for s in out), "default universe must be non-empty"
    return out


def _default_market_sample_provider(symbol: str) -> float:
    # Pre: symbol is a non-empty string.
    assert isinstance(symbol, str) and symbol, "symbol must be a non-empty str"
    # Post: returns a strictly positive finite price.
    price = 100.0
    assert price > 0, "price must be strictly positive"
    return price


class SpecReadinessGate:
    """Deterministic implementability checks on a constructed ``StrategySpec``.

    Contract (class invariant): every call to :meth:`validate` returns a
    non-empty ``List[QualityGateResult]``. Every result in that list carries
    the ``phase`` argument the caller supplied and has ``gate_name == GATE``.
    """

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

        # Post: provider is callable, config slot is the supplied value or None.
        assert callable(self._market_sample_provider), "provider slot must be callable"

    def validate(
        self,
        spec: StrategySpec,
        *,
        phase: StrategyLabPhase = "design",
        backtest_config: Optional[BacktestConfig] = None,
    ) -> List[QualityGateResult]:
        # Pre: spec is a StrategySpec; phase is one of the four valid labels.
        assert isinstance(spec, StrategySpec), "spec must be a StrategySpec"
        assert phase in _VALID_PHASES, f"phase must be one of {sorted(_VALID_PHASES)}; got {phase!r}"
        assert backtest_config is None or isinstance(backtest_config, BacktestConfig), (
            "backtest_config override must be a BacktestConfig or None"
        )

        results: List[QualityGateResult] = []
        config = backtest_config or self._backtest_config

        results.extend(self._check_universe_set(spec, phase))
        results.extend(self._check_entry_rules_non_trivial(spec, phase))
        results.extend(self._check_indicator_validity(spec, phase))
        results.extend(self._check_exit_completeness(spec, phase))
        results.extend(self._check_sizing_realisable(spec, phase, config))
        results.extend(self._check_hypothesis_rule_consistency(spec, phase))
        results.extend(self._check_timeframe_availability(spec, phase))
        results.extend(self._check_risk_limit_coherence(spec, phase))

        if not results:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    phase=phase,
                    passed=True,
                    severity="info",
                    details="Strategy spec passed all readiness checks.",
                )
            )

        # Post: results non-empty; every result carries the caller's phase and GATE name.
        assert results, "validate must always return at least one result"
        assert all(r.phase == phase for r in results), "every result must carry the caller's phase"
        assert all(r.gate_name == GATE for r in results), "every result must carry GATE name"
        return results

    # ------------------------------------------------------------------
    # Rule 1: Universe set
    # ------------------------------------------------------------------
    def _check_universe_set(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        # Pre: spec is StrategySpec, phase is a valid phase literal.
        assert isinstance(spec, StrategySpec)
        assert phase in _VALID_PHASES

        named = {m.group(0).upper() for m in _SYMBOL_REGEX.finditer(spec.hypothesis or "")}
        targets = {s.upper() for s in spec.target_symbols}

        out: List[QualityGateResult] = []
        if not named and not targets:
            pass  # asset-class default universe will be used
        elif named and not targets:
            out.append(
                QualityGateResult(
                    gate_name=GATE,
                    phase=phase,
                    passed=False,
                    severity="critical",
                    details=(
                        f"Hypothesis names symbol(s) {sorted(named)} but "
                        "target_symbols is empty — backtest universe would not "
                        "include the symbols the strategy is about."
                    ),
                )
            )
        else:
            missing = named - targets
            if missing:
                out.append(
                    QualityGateResult(
                        gate_name=GATE,
                        phase=phase,
                        passed=False,
                        severity="critical",
                        details=(
                            f"Hypothesis names symbol(s) {sorted(missing)} not "
                            f"present in target_symbols {sorted(targets)}."
                        ),
                    )
                )

        # Post: 0 or 1 critical failures; never warnings or info.
        assert len(out) <= 1, "universe-set check emits at most one result"
        assert all(not r.passed and r.severity == "critical" for r in out)
        return out

    # ------------------------------------------------------------------
    # Rule 2: Entry rules non-trivial
    # ------------------------------------------------------------------
    def _check_entry_rules_non_trivial(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        # Pre: spec is StrategySpec, phase is a valid phase literal.
        assert isinstance(spec, StrategySpec)
        assert phase in _VALID_PHASES

        out: List[QualityGateResult] = []
        if not spec.entry_rules:
            out.append(
                QualityGateResult(
                    gate_name=GATE,
                    phase=phase,
                    passed=False,
                    severity="critical",
                    details="No entry rules — strategy cannot generate trades.",
                )
            )
        else:
            for idx, rule in enumerate(spec.entry_rules):
                if not isinstance(rule, EntryRule):
                    out.append(
                        QualityGateResult(
                            gate_name=GATE,
                            phase=phase,
                            passed=False,
                            severity="critical",
                            details=(
                                f"entry_rules[{idx}] is not a structured EntryRule "
                                f"(got {type(rule).__name__})."
                            ),
                        )
                    )
                    break
                if not isinstance(rule.when, Predicate):
                    out.append(
                        QualityGateResult(
                            gate_name=GATE,
                            phase=phase,
                            passed=False,
                            severity="critical",
                            details=(
                                f"entry_rules[{idx}].when is not a Predicate "
                                f"(got {type(rule.when).__name__})."
                            ),
                        )
                    )
                    break

        # Post: 0 or 1 critical failures.
        assert len(out) <= 1
        assert all(not r.passed and r.severity == "critical" for r in out)
        return out

    # ------------------------------------------------------------------
    # Rule 3: Indicator validity
    # ------------------------------------------------------------------
    def _check_indicator_validity(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        # Pre: spec is StrategySpec, phase is a valid phase literal.
        assert isinstance(spec, StrategySpec)
        assert phase in _VALID_PHASES

        out: List[QualityGateResult] = []
        for ref in self._iter_indicator_refs(spec):
            if ref.name not in _KNOWN_INDICATOR_NAMES:
                out.append(
                    QualityGateResult(
                        gate_name=GATE,
                        phase=phase,
                        passed=False,
                        severity="critical",
                        details=(
                            f"Indicator '{ref.name}' is not in the supported "
                            f"set {sorted(_KNOWN_INDICATOR_NAMES)}."
                        ),
                    )
                )
                break
            required = _INDICATOR_REQUIRED_PARAMS.get(ref.name, frozenset())
            missing = sorted(required - set(ref.params.keys()))
            if missing:
                out.append(
                    QualityGateResult(
                        gate_name=GATE,
                        phase=phase,
                        passed=False,
                        severity="critical",
                        details=(
                            f"Indicator '{ref.name}' is missing required "
                            f"param(s) {missing}."
                        ),
                    )
                )
                break

        # Post: 0 or 1 critical failures (short-circuits on first violation).
        assert len(out) <= 1
        assert all(not r.passed and r.severity == "critical" for r in out)
        return out

    # ------------------------------------------------------------------
    # Rule 4: Exit completeness
    # ------------------------------------------------------------------
    def _check_exit_completeness(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        # Pre: spec is StrategySpec, phase is a valid phase literal.
        assert isinstance(spec, StrategySpec)
        assert phase in _VALID_PHASES

        allowed_kinds = {"signal_exit", "stop_loss", "take_profit"}
        out: List[QualityGateResult] = []
        if not spec.exit_rules:
            out.append(
                QualityGateResult(
                    gate_name=GATE,
                    phase=phase,
                    passed=False,
                    severity="critical",
                    details=(
                        "No exit rules — positions would never close. Add at "
                        "least one of: signal_exit, stop_loss, take_profit."
                    ),
                )
            )
        elif not any(getattr(r, "kind", None) in allowed_kinds for r in spec.exit_rules):
            out.append(
                QualityGateResult(
                    gate_name=GATE,
                    phase=phase,
                    passed=False,
                    severity="critical",
                    details=(
                        f"exit_rules contains no rule of kind in {sorted(allowed_kinds)}."
                    ),
                )
            )

        # Post: 0 or 1 critical failures.
        assert len(out) <= 1
        assert all(not r.passed and r.severity == "critical" for r in out)
        return out

    # ------------------------------------------------------------------
    # Rule 5: Sizing realisable
    # ------------------------------------------------------------------
    def _check_sizing_realisable(
        self,
        spec: StrategySpec,
        phase: StrategyLabPhase,
        config: Optional[BacktestConfig],
    ) -> List[QualityGateResult]:
        # Pre: spec is StrategySpec, phase is valid, config is BacktestConfig or None.
        assert isinstance(spec, StrategySpec)
        assert phase in _VALID_PHASES
        assert config is None or isinstance(config, BacktestConfig)

        if config is None:
            return []

        kind = getattr(spec.sizing, "kind", None)
        symbols = spec.target_symbols or _default_universe_for(spec.asset_class)
        if not symbols:
            return []
        capital = config.initial_capital
        # Class invariant: BacktestConfig guarantees initial_capital > 0.
        assert capital > 0, "initial_capital must be strictly positive"

        out: List[QualityGateResult] = []
        for sym in symbols:
            try:
                price = float(self._market_sample_provider(sym))
            except Exception:
                price = 0.0
            if price <= 0:
                out.append(
                    QualityGateResult(
                        gate_name=GATE,
                        phase=phase,
                        passed=False,
                        severity="critical",
                        details=(
                            f"Sizing realisability: no positive price sample "
                            f"for '{sym}' (got {price!r})."
                        ),
                    )
                )
                break
            if kind == "fixed_fraction":
                notional = capital * float(spec.sizing.fraction)
            elif kind == "fixed_notional":
                notional = float(spec.sizing.notional_usd)
            elif kind == "volatility_target":
                # Cannot evaluate without realised vol; treat as realisable.
                continue
            else:
                continue
            qty = notional / price
            if qty < 1:
                out.append(
                    QualityGateResult(
                        gate_name=GATE,
                        phase=phase,
                        passed=False,
                        severity="critical",
                        details=(
                            f"Sizing yields qty={qty:.4f} < 1 for symbol "
                            f"'{sym}' at sample price ${price:.2f} with "
                            f"capital ${capital:.0f}."
                        ),
                    )
                )
                break

        # Post: 0 or 1 critical failures (short-circuits on first symbol that fails).
        assert len(out) <= 1
        assert all(not r.passed and r.severity == "critical" for r in out)
        return out

    # ------------------------------------------------------------------
    # Rule 6: Hypothesis–rule consistency
    # ------------------------------------------------------------------
    def _check_hypothesis_rule_consistency(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        # Pre: spec is StrategySpec, phase is a valid phase literal.
        assert isinstance(spec, StrategySpec)
        assert phase in _VALID_PHASES

        hypothesis = spec.hypothesis or ""
        terms_in_hypothesis = {
            re.sub(r"\s+", " ", m.group(0).lower())
            for m in _CONCEPT_TERMS.finditer(hypothesis)
        }
        referenced = {ref.name for ref in self._iter_indicator_refs(spec)}
        orphan = sorted(
            t
            for t in terms_in_hypothesis
            if (n := _CONCEPT_TO_INDICATOR_NAME.get(t)) is not None and n not in referenced
        )

        out: List[QualityGateResult] = []
        if orphan:
            out.append(
                QualityGateResult(
                    gate_name=GATE,
                    phase=phase,
                    passed=False,
                    severity="critical",
                    details=(
                        f"Hypothesis names indicator concept(s) {sorted(orphan)} "
                        "that no entry/exit rule references."
                    ),
                )
            )

        # Post: 0 or 1 critical failures.
        assert len(out) <= 1
        assert all(not r.passed and r.severity == "critical" for r in out)
        return out

    # ------------------------------------------------------------------
    # Rule 7: Timeframe data availability
    # ------------------------------------------------------------------
    def _check_timeframe_availability(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        # Pre: spec is StrategySpec, phase is a valid phase literal.
        assert isinstance(spec, StrategySpec)
        assert phase in _VALID_PHASES

        out: List[QualityGateResult] = []
        if spec.timeframe != "1d" and spec.asset_class.lower() not in _FULL_TIMEFRAME_ASSET_CLASSES:
            out.append(
                QualityGateResult(
                    gate_name=GATE,
                    phase=phase,
                    passed=False,
                    severity="critical",
                    details=(
                        f"Asset class '{spec.asset_class}' has no reliable "
                        f"intraday data for timeframe '{spec.timeframe}'; "
                        "use '1d' or pick stocks/crypto."
                    ),
                )
            )

        # Post: 0 or 1 critical failures.
        assert len(out) <= 1
        assert all(not r.passed and r.severity == "critical" for r in out)
        return out

    # ------------------------------------------------------------------
    # Rule 8: Risk-limit coherence
    # ------------------------------------------------------------------
    def _check_risk_limit_coherence(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        # Pre: spec is StrategySpec, phase is a valid phase literal.
        assert isinstance(spec, StrategySpec)
        assert phase in _VALID_PHASES

        out: List[QualityGateResult] = []

        stop_losses = [r for r in spec.exit_rules if isinstance(r, StopLossRule)]
        take_profits = [r for r in spec.exit_rules if isinstance(r, TakeProfitRule)]
        if stop_losses and take_profits:
            min_tp = min(r.pct for r in take_profits)
            max_sl = max(r.pct for r in stop_losses)
            # Class invariant: StopLossRule.pct and TakeProfitRule.pct are > 0.
            assert min_tp > 0 and max_sl > 0, "exit-rule pcts must be strictly positive"
            if max_sl >= min_tp:
                out.append(
                    QualityGateResult(
                        gate_name=GATE,
                        phase=phase,
                        passed=False,
                        severity="critical",
                        details=(
                            f"stop_loss.pct={max_sl} ≥ take_profit.pct={min_tp} — "
                            "stop would trigger before profit target."
                        ),
                    )
                )

        if spec.risk_limits.max_position_pct > 25:
            out.append(
                QualityGateResult(
                    gate_name=GATE,
                    phase=phase,
                    passed=False,
                    severity="critical",
                    details=(
                        f"max_position_pct={spec.risk_limits.max_position_pct}% "
                        "exceeds the 25% cap for a single-position risk budget."
                    ),
                )
            )

        # Post: 0, 1, or 2 critical failures (the two sub-checks are independent).
        assert len(out) <= 2
        assert all(not r.passed and r.severity == "critical" for r in out)
        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_indicator_refs(spec: StrategySpec) -> Iterator[IndicatorRef]:
        # Pre: spec is a StrategySpec.
        assert isinstance(spec, StrategySpec)
        for rule in spec.entry_rules:
            yield from SpecReadinessGate._predicate_indicator_refs(rule.when)
        for rule in spec.exit_rules:
            if isinstance(rule, SignalExitRule):
                yield from SpecReadinessGate._predicate_indicator_refs(rule.when)

    @staticmethod
    def _predicate_indicator_refs(pred: Predicate) -> Iterator[IndicatorRef]:
        # Pre: pred is a Predicate.
        assert isinstance(pred, Predicate)
        if isinstance(pred.lhs, IndicatorRef):
            yield pred.lhs
        if isinstance(pred.rhs, IndicatorRef):
            yield pred.rhs
