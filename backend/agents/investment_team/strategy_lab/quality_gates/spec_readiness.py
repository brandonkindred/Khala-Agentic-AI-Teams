"""Deterministic implementability gate for `StrategySpec` (issue #540).

Runs in the design phase (before any code is written) and again as the first
gate of the synthesis phase as a sanity check that the spec wasn't mutated.
Each rule is deterministic, has a dedicated test, and fails closed.

The eight rules implemented here are the ones enumerated in issue #540.
Several of them overlap with :class:`StrategySpecValidator` — that is
intentional: this gate is self-contained and runs at a different phase, with
critical (rather than warning) severity on the items #540 deems blocking.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional

from ...models import BacktestConfig, StrategySpec
from ...symbols import COMMODITY_SYMBOLS, CRYPTO_SYMBOLS, OTHER_SYMBOLS, STOCK_SYMBOLS
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

# Whitelist regex matches any tradeable symbol the spec is allowed to name in
# its hypothesis. STOCK ∪ CRYPTO ∪ COMMODITY ∪ OTHER (broad ETFs). Word-bounded
# so "ETH" doesn't match "ETHEREUM" appearing as a prose adjective.
_SYMBOL_WHITELIST: frozenset[str] = frozenset(
    {*STOCK_SYMBOLS, *CRYPTO_SYMBOLS, *COMMODITY_SYMBOLS, *OTHER_SYMBOLS}
)
_SYMBOL_REGEX = re.compile(
    r"\b(" + "|".join(sorted(_SYMBOL_WHITELIST, key=len, reverse=True)) + r")\b"
)

# Asset classes that today's data-provider chain supports for every timeframe
# (stocks/crypto have minute-bar yfinance coverage). Other classes only fetch
# daily bars reliably — intraday timeframes on those classes are rejected.
_FULL_TIMEFRAME_ASSET_CLASSES: frozenset[str] = frozenset({"stocks", "crypto"})

# Authoritative set of DSL indicator names. Mirrors `IndicatorName` so a
# constructed IndicatorRef always satisfies this set; the gate enforces it
# again as defense-in-depth against a future refactor that bypasses Pydantic.
_KNOWN_INDICATOR_NAMES: frozenset[str] = frozenset(IndicatorName.__args__)

# Indicators whose `params` must include a specific key — derived from
# `_INDICATOR_PARAM_SPECS` in spec_dsl.py. `rsi` is intentionally absent here:
# the DSL ships a `period=14` default, so a bare `IndicatorRef(name="rsi")` is
# fully realisable.
_INDICATOR_REQUIRED_PARAMS: dict[str, frozenset[str]] = {
    "sma": frozenset({"period"}),
    "ema": frozenset({"period"}),
}

# Reuse the indicator vocabulary the existing validator uses for prose mentions.
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
    ac = asset_class.lower()
    if ac == "stocks":
        return list(STOCK_SYMBOLS)
    if ac == "crypto":
        return list(CRYPTO_SYMBOLS)
    if ac == "commodities":
        return list(COMMODITY_SYMBOLS)
    return list(OTHER_SYMBOLS)


def _default_market_sample_provider(symbol: str) -> float:
    """Static fallback price used when no real recent-bar feed is wired."""
    return 100.0


class SpecReadinessGate:
    """Deterministic implementability checks on a constructed ``StrategySpec``.

    Issue #540 — eight blocking rules that refuse a vague or unimplementable
    spec *before* code synthesis. The gate is self-contained; failures are
    persisted via ``StrategyLabRecord.quality_gate_results`` with the supplied
    ``phase`` tag so design-phase vs synthesis-phase invocations are
    distinguishable downstream.
    """

    def __init__(
        self,
        *,
        market_sample_provider: Optional[MarketSampleProvider] = None,
        backtest_config: Optional[BacktestConfig] = None,
    ) -> None:
        self._market_sample_provider: MarketSampleProvider = (
            market_sample_provider or _default_market_sample_provider
        )
        self._backtest_config = backtest_config

    def validate(
        self,
        spec: StrategySpec,
        *,
        phase: StrategyLabPhase = "design",
        backtest_config: Optional[BacktestConfig] = None,
    ) -> List[QualityGateResult]:
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

        return results

    # ------------------------------------------------------------------
    # Rule 1: Universe set
    # ------------------------------------------------------------------
    def _check_universe_set(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        named = {m.group(0).upper() for m in _SYMBOL_REGEX.finditer(spec.hypothesis or "")}
        targets = {s.upper() for s in spec.target_symbols}

        # Pass-through cases: no symbols named in hypothesis AND target_symbols
        # is also empty → asset-class default universe will be used (handled
        # by the fetch path). That's acceptable here; rule 1 only fires on a
        # mismatch between named and targeted symbols.
        if not named and not targets:
            return []

        if named and not targets:
            return [
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
            ]

        missing = named - targets
        if missing:
            return [
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
            ]

        return []

    # ------------------------------------------------------------------
    # Rule 2: Entry rules non-trivial
    # ------------------------------------------------------------------
    def _check_entry_rules_non_trivial(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        if not spec.entry_rules:
            return [
                QualityGateResult(
                    gate_name=GATE,
                    phase=phase,
                    passed=False,
                    severity="critical",
                    details="No entry rules — strategy cannot generate trades.",
                )
            ]

        for idx, rule in enumerate(spec.entry_rules):
            if not isinstance(rule, EntryRule):
                return [
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
                ]
            pred = rule.when
            if not isinstance(pred, Predicate):
                return [
                    QualityGateResult(
                        gate_name=GATE,
                        phase=phase,
                        passed=False,
                        severity="critical",
                        details=(
                            f"entry_rules[{idx}].when is not a Predicate "
                            f"(got {type(pred).__name__})."
                        ),
                    )
                ]

        return []

    # ------------------------------------------------------------------
    # Rule 3: Indicator validity
    # ------------------------------------------------------------------
    def _check_indicator_validity(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        for ref in self._iter_indicator_refs(spec):
            if ref.name not in _KNOWN_INDICATOR_NAMES:
                return [
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
                ]
            required = _INDICATOR_REQUIRED_PARAMS.get(ref.name, frozenset())
            missing = sorted(required - set(ref.params.keys()))
            if missing:
                return [
                    QualityGateResult(
                        gate_name=GATE,
                        phase=phase,
                        passed=False,
                        severity="critical",
                        details=(f"Indicator '{ref.name}' is missing required param(s) {missing}."),
                    )
                ]
        return []

    # ------------------------------------------------------------------
    # Rule 4: Exit completeness
    # ------------------------------------------------------------------
    def _check_exit_completeness(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        allowed_kinds = {"signal_exit", "stop_loss", "take_profit"}
        if not spec.exit_rules:
            return [
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
            ]
        if not any(getattr(r, "kind", None) in allowed_kinds for r in spec.exit_rules):
            return [
                QualityGateResult(
                    gate_name=GATE,
                    phase=phase,
                    passed=False,
                    severity="critical",
                    details=(f"exit_rules contains no rule of kind in {sorted(allowed_kinds)}."),
                )
            ]
        return []

    # ------------------------------------------------------------------
    # Rule 5: Sizing realisable
    # ------------------------------------------------------------------
    def _check_sizing_realisable(
        self,
        spec: StrategySpec,
        phase: StrategyLabPhase,
        config: Optional[BacktestConfig],
    ) -> List[QualityGateResult]:
        if config is None:
            return []  # no capital basis available; skip silently

        kind = getattr(spec.sizing, "kind", None)
        # Use target_symbols when set; otherwise pick a deterministic
        # representative from the asset-class default universe.
        symbols = spec.target_symbols or _default_universe_for(spec.asset_class)
        if not symbols:
            return []
        capital = config.initial_capital

        for sym in symbols:
            try:
                price = float(self._market_sample_provider(sym))
            except Exception:
                price = 0.0
            if price <= 0:
                return [
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
                ]
            if kind == "fixed_fraction":
                notional = capital * float(spec.sizing.fraction)
            elif kind == "fixed_notional":
                notional = float(spec.sizing.notional_usd)
            elif kind == "volatility_target":
                # Volatility-target sizing can't be evaluated without realised
                # vol; treat as realisable for this static check.
                continue
            else:
                continue
            qty = notional / price
            if qty < 1:
                return [
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
                ]
        return []

    # ------------------------------------------------------------------
    # Rule 6: Hypothesis–rule consistency
    # ------------------------------------------------------------------
    def _check_hypothesis_rule_consistency(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        hypothesis = spec.hypothesis or ""
        terms_in_hypothesis = {
            re.sub(r"\s+", " ", m.group(0).lower()) for m in _CONCEPT_TERMS.finditer(hypothesis)
        }
        referenced = {ref.name for ref in self._iter_indicator_refs(spec)}
        orphan = sorted(
            t
            for t in terms_in_hypothesis
            if (n := _CONCEPT_TO_INDICATOR_NAME.get(t)) is not None and n not in referenced
        )
        if orphan:
            return [
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
            ]
        return []

    # ------------------------------------------------------------------
    # Rule 7: Timeframe data availability
    # ------------------------------------------------------------------
    def _check_timeframe_availability(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        if spec.timeframe == "1d":
            return []
        if spec.asset_class.lower() not in _FULL_TIMEFRAME_ASSET_CLASSES:
            return [
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
            ]
        return []

    # ------------------------------------------------------------------
    # Rule 8: Risk-limit coherence
    # ------------------------------------------------------------------
    def _check_risk_limit_coherence(
        self, spec: StrategySpec, phase: StrategyLabPhase
    ) -> List[QualityGateResult]:
        out: List[QualityGateResult] = []

        stop_losses = [r for r in spec.exit_rules if isinstance(r, StopLossRule)]
        take_profits = [r for r in spec.exit_rules if isinstance(r, TakeProfitRule)]
        if stop_losses and take_profits:
            min_tp = min(r.pct for r in take_profits)
            max_sl = max(r.pct for r in stop_losses)
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

        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_indicator_refs(spec: StrategySpec):
        for rule in spec.entry_rules:
            yield from SpecReadinessGate._predicate_indicator_refs(rule.when)
        for rule in spec.exit_rules:
            if isinstance(rule, SignalExitRule):
                yield from SpecReadinessGate._predicate_indicator_refs(rule.when)

    @staticmethod
    def _predicate_indicator_refs(pred: Predicate):
        if isinstance(pred.lhs, IndicatorRef):
            yield pred.lhs
        if isinstance(pred.rhs, IndicatorRef):
            yield pred.rhs
