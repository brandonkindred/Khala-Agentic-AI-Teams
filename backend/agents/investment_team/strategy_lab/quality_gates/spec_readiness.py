"""Deterministic implementability gate for `StrategySpec`.

Runs in the design phase (before any code is written) and again as the first
gate of the synthesis phase to confirm the spec wasn't mutated. Nine
deterministic rules, each unit-testable and failing closed.

Several rules overlap with :class:`StrategySpecValidator`. The overlap is
intentional: this gate is self-contained, runs at a different phase, and
escalates a subset of the overlapping items to critical severity.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Callable, ClassVar, Iterable, Iterator, List, Optional

from ...market_data_service import _max_universe_symbols
from ...models import BacktestConfig, StrategySpec
from ...strategy_lab_context import normalize_asset_class, normalize_asset_class_strict
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

logger = logging.getLogger(__name__)

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

# Single-position risk-budget ceiling enforced by Rule 8. Exposed as a module
# constant so the deterministic mechanical-repair pre-flight clamps to exactly
# the same threshold the gate rejects above — the rule and its repair cannot
# drift apart.
MAX_POSITION_PCT_CEILING: float = 25.0

# Relative tolerance for Rule 9's prose-vs-spec position-size comparison. A
# prose-stated deployment percentage within this band of the spec's actual
# sizing is treated as agreement (no warning) so rounding / loose wording
# ("~5%") doesn't churn the design loop. Overridable via
# ``STRATEGY_LAB_SIZING_COHERENCE_TOLERANCE``; garbage / negative values fall
# back to the default.
_DEFAULT_SIZING_COHERENCE_REL_TOL: float = 0.05


def _sizing_coherence_rel_tol() -> float:
    """Resolve ``STRATEGY_LAB_SIZING_COHERENCE_TOLERANCE``.

    Preconditions: env value, when set, parses as a finite non-negative float.
    Postconditions: returns a finite, non-negative relative tolerance. Unset,
    non-numeric, non-finite, or negative values fall back to
    ``_DEFAULT_SIZING_COHERENCE_REL_TOL`` (a WARN is logged for malformed input).
    """
    raw = os.environ.get("STRATEGY_LAB_SIZING_COHERENCE_TOLERANCE")
    if not raw:
        return _DEFAULT_SIZING_COHERENCE_REL_TOL
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid STRATEGY_LAB_SIZING_COHERENCE_TOLERANCE=%r; using default %.4f",
            raw,
            _DEFAULT_SIZING_COHERENCE_REL_TOL,
        )
        return _DEFAULT_SIZING_COHERENCE_REL_TOL
    if not math.isfinite(value) or value < 0:
        logger.warning(
            "STRATEGY_LAB_SIZING_COHERENCE_TOLERANCE=%r is out of range; using default %.4f",
            raw,
            _DEFAULT_SIZING_COHERENCE_REL_TOL,
        )
        return _DEFAULT_SIZING_COHERENCE_REL_TOL
    return value


# Capital-deployment phrasings Rule 9 reconciles against the spec's actual
# sizing. Per the corrected risk model, "risk X% per trade" denotes the capital
# *deployed* into a position (a fraction of the account), NOT a loss budget —
# the stop-loss only bounds the realised loss *below* that deployed amount. Each
# pattern captures the percentage in group ``pct``. The patterns require an
# explicit per-trade / position framing so the rule never fires on a stop-loss,
# take-profit, or drawdown percentage elsewhere in the prose.
_PROSE_POSITION_PCT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "deploy/allocate/use/risk/commit (up to|about|~) X% per trade|position"
    re.compile(
        r"\b(?:deploy|allocate|use|risk|commit|invest)(?:ing)?\s+"
        r"(?:up\s+to\s+|about\s+|around\s+|approximately\s+|~\s*)?"
        r"(?P<pct>\d+(?:\.\d+)?)\s*%\s+(?:of\s+(?:the\s+)?(?:account|equity|capital|portfolio)\s+)?"
        r"(?:per[\s-]+trade|per[\s-]+position|in\s+(?:each|a)\s+(?:trade|position))",
        re.IGNORECASE,
    ),
    # "X% per-trade|per-position allocation/sizing/position"
    re.compile(
        r"\b(?P<pct>\d+(?:\.\d+)?)\s*%\s+per[\s-]+(?:trade|position)\s+"
        r"(?:allocation|sizing|position|stake)",
        re.IGNORECASE,
    ),
    # "X% position size" / "position size of X%" / "X% position per trade"
    re.compile(
        r"\b(?P<pct>\d+(?:\.\d+)?)\s*%\s+position(?:\s+siz(?:e|ing))?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bposition\s+siz(?:e|ing)\s+of\s+(?P<pct>\d+(?:\.\d+)?)\s*%",
        re.IGNORECASE,
    ),
)


def _extract_prose_position_pct(text: str) -> Optional[float]:
    """Extract a prose-stated per-trade capital-deployment percentage.

    Preconditions: ``text`` is a string (may be empty).
    Postconditions: returns the first matched percentage as a float in percent
    units (e.g. ``5.0`` for "deploy 5% per trade"), or ``None`` when no
    capital-deployment phrasing is present. Loss-budget / stop / take-profit /
    drawdown phrasings are intentionally NOT matched — only capital deployed.
    """
    assert isinstance(text, str), "text must be a str"
    if not text:
        return None
    for pattern in _PROSE_POSITION_PCT_PATTERNS:
        m = pattern.search(text)
        if m is not None:
            try:
                return float(m.group("pct"))
            except (TypeError, ValueError):  # pragma: no cover - regex guarantees numeric
                continue
    return None


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


_KNOWN_ASSET_CLASSES: frozenset[str] = frozenset(
    {"stocks", "crypto", "commodities", "forex", "futures"}
)


def _default_universe_for(asset_class: str) -> List[str]:
    """Return the canonical default universe for ``asset_class``.

    Pre: ``asset_class`` is a non-empty string.
    Post: returns a non-empty list of upper-case ticker strings.

    Aliases the runtime fetch path accepts via ``normalize_asset_class``
    — ``equity`` / ``equities`` / ``stock`` for stocks, ``fx`` for forex,
    ``commodity`` / ``metal`` / ``energy`` for commodities — are mapped
    to the canonical label before dispatch so the gate doesn't false-
    critical otherwise-tradeable specs.

    Raises ``ValueError`` for asset classes the strict normalizer can't
    resolve (typos like ``"bonds"`` / ``"crpto"``) and for canonical
    classes that have no default universe in the gate's scope (today
    just ``"options"`` — ``StrategySpecValidator`` rejects that upstream;
    raising here is defense-in-depth). The old strict-only path silently
    fell back to ``OTHER_SYMBOLS`` for typos — exactly the false-
    confidence Codex flagged for forex/futures earlier.
    """
    assert isinstance(asset_class, str) and asset_class, "asset_class must be a non-empty str"

    canonical = normalize_asset_class_strict(asset_class)
    if canonical == "stocks":
        out = list(STOCK_SYMBOLS)
    elif canonical == "crypto":
        out = list(CRYPTO_SYMBOLS)
    elif canonical == "commodities":
        out = list(COMMODITY_SYMBOLS)
    elif canonical == "forex":
        out = list(FOREX_SYMBOLS)
    elif canonical == "futures":
        out = list(FUTURES_SYMBOLS)
    else:
        # ``canonical`` is in ``_CANONICAL_ASSET_CLASSES`` (the strict
        # normalizer guarantees that) but not in the gate's universe map
        # — today only ``"options"`` lands here.
        raise ValueError(
            f"asset_class {asset_class!r} normalizes to {canonical!r} which has no "
            f"default universe in the gate; expected one of {sorted(_KNOWN_ASSET_CLASSES)}"
        )

    assert out and all(isinstance(s, str) and s for s in out), "default universe must be non-empty"
    return out


def _canonicalize_ticker(symbol: str) -> str:
    # Strip Yahoo provider suffixes so bare aliases compare equal to their
    # provider-form counterparts: ``=F`` for futures (``ES=F`` → ``ES``),
    # ``=X`` for forex (``EURUSD=X`` → ``EURUSD``), and ``-USD`` for the
    # crypto quote-suffix convention (``BTC-USD`` → ``BTC``). Without the
    # ``-USD`` strip, a hypothesis that names ``BTC`` would false-critical
    # against a correctly populated ``target_symbols=["BTC-USD"]`` in
    # Rule 1's set-membership check.
    #
    # Suffixes are stripped iteratively so a compound input like ``BTC-USD-USD``
    # (an LLM hallucination, double-normalization, or operator typo) reduces to
    # ``BTC`` rather than leaving a residual suffix. The loop is bounded by the
    # string length and terminates because every iteration shortens ``s``. This
    # keeps the post-condition a true invariant — and the canonical form correct
    # even when assertions are stripped under ``python -O``.
    # Post: returns an upper-cased string with no `=F` / `=X` / `-USD` suffix.
    assert isinstance(symbol, str), "symbol must be a str"
    s = symbol.upper()
    suffixes = ("=F", "=X", "-USD")
    while s.endswith(suffixes):
        for suffix in suffixes:
            if s.endswith(suffix):
                s = s[: -len(suffix)]
                break
    assert not s.endswith(suffixes), "canonical form must not retain a provider suffix"
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


@dataclass(frozen=True)
class SpecReadinessCtx:
    """Per-``validate`` context handed to every rule in ``SpecReadinessGate._RULES``.

    Built once at the top of ``validate``. Threading the ctx explicitly
    through each rule replaces the previous ``ctx.config`` slot that
    had to be reset in a ``finally`` block.
    """

    spec: StrategySpec
    config: Optional[BacktestConfig]


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

        # Post: provider is callable.
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
        ctx = SpecReadinessCtx(spec=spec, config=backtest_config or self._backtest_config)
        with self._using_phase(phase):
            results: List[QualityGateResult] = [r for rule in self._RULES for r in rule(self, ctx)]
            if not results:
                results.append(self._info("Strategy spec passed all readiness checks."))
        # Post: every result carries the caller's phase and GATE name.
        assert all(r.phase == phase for r in results), "every result must carry the caller's phase"
        assert all(r.gate_name == GATE for r in results), "every result must carry GATE name"
        return results

    # ------------------------------------------------------------------
    # Rule 1: Universe set — every whitelisted ticker named in the
    # hypothesis must be reachable in the backtest universe. A ticker is
    # reachable when it appears in ``target_symbols`` (explicit operator
    # intent) OR — when ``target_symbols`` is empty — in the asset-class
    # default universe that ``MarketDataService.resolve_strategy_symbols``
    # would fall back to. Yahoo provider suffixes are stripped before
    # comparison so bare aliases compare equal to their suffix forms.
    # ------------------------------------------------------------------
    def _check_universe_set(self, ctx: SpecReadinessCtx) -> Iterable[QualityGateResult]:
        assert isinstance(ctx.spec, StrategySpec)
        named_raw = {m.group(0).upper() for m in _SYMBOL_REGEX.finditer(ctx.spec.hypothesis or "")}
        targets_raw = {s.upper() for s in ctx.spec.target_symbols}
        named = {_canonicalize_ticker(s) for s in named_raw}
        targets = {_canonicalize_ticker(s) for s in targets_raw}
        if not named or named <= targets:
            return ()
        if not targets:
            # Empty target_symbols ⇒ the fetcher falls back to the
            # asset-class default, truncated to the universe-size cap. A
            # hypothesis-named ticker is reachable iff it lands in that
            # *capped* slice, not the full raw default — otherwise a low
            # ``STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS`` could produce a false
            # pass for a ticker at the tail of the declared list that
            # the fetcher will never actually request. Delegates to the
            # same strict helper Rule 5 uses, so an unknown asset_class
            # surfaces there as a sharper critical instead of being
            # silently smoothed over here.
            try:
                raw_default = _default_universe_for(ctx.spec.asset_class)
            except ValueError:
                # Unknown asset_class — Rule 5 emits its own critical with
                # a sharper message. Treat the default as empty here so
                # Rule 1 still flags the unreachable named tickers.
                default_canon: set[str] = set()
            else:
                cap = _max_universe_symbols()
                capped_default = raw_default[:cap] if len(raw_default) > cap else raw_default
                default_canon = {_canonicalize_ticker(s) for s in capped_default}
            if named <= default_canon:
                return ()
            unreachable_canon = named - default_canon
            unreachable_raw = sorted(
                s for s in named_raw if _canonicalize_ticker(s) in unreachable_canon
            )
            return (
                self._critical(
                    f"Hypothesis names symbol(s) {unreachable_raw} that are not reachable "
                    f"via the (capped) {ctx.spec.asset_class} default universe and "
                    "target_symbols is empty — backtest universe would not include them. "
                    "Set spec.target_symbols explicitly or raise "
                    "STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS if the ticker is in the declared "
                    "default beyond the current cap."
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
    def _check_entry_rules_non_trivial(self, ctx: SpecReadinessCtx) -> Iterable[QualityGateResult]:
        assert isinstance(ctx.spec, StrategySpec)
        if not ctx.spec.entry_rules:
            return (self._critical("No entry rules — strategy cannot generate trades."),)
        for idx, rule in enumerate(ctx.spec.entry_rules):
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
    def _check_indicator_validity(self, ctx: SpecReadinessCtx) -> Iterable[QualityGateResult]:
        assert isinstance(ctx.spec, StrategySpec)
        for ref in self._iter_indicator_refs(ctx.spec):
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
    def _check_exit_completeness(self, ctx: SpecReadinessCtx) -> Iterable[QualityGateResult]:
        assert isinstance(ctx.spec, StrategySpec)
        allowed_kinds = {"signal_exit", "stop_loss", "take_profit"}
        if not ctx.spec.exit_rules:
            return (
                self._critical(
                    "No exit rules — positions would never close. Add at "
                    "least one of: signal_exit, stop_loss, take_profit."
                ),
            )
        if not any(getattr(r, "kind", None) in allowed_kinds for r in ctx.spec.exit_rules):
            return (
                self._critical(f"exit_rules contains no rule of kind in {sorted(allowed_kinds)}."),
            )
        return ()

    # ------------------------------------------------------------------
    # Rule 5: Sizing realisable.
    # ------------------------------------------------------------------
    def _check_sizing_realisable(self, ctx: SpecReadinessCtx) -> Iterable[QualityGateResult]:
        assert isinstance(ctx.spec, StrategySpec)
        config = ctx.config
        if config is None:
            return ()

        # Fail closed on an unsupported asset_class before any sizing logic.
        # ``StrategySpec`` rejects off-vocabulary classes at construction, but a
        # spec can reach this gate without that validation (post-construction
        # assignment / model-copy mutation, which the tests exercise). With
        # explicit ``target_symbols`` the ``_default_universe_for`` strict check
        # below is skipped, and the permissive ``normalize_asset_class`` used
        # for the whole-lot decision would map e.g. ``bonds`` → ``stocks`` and
        # let it pass as a stock-like whole-lot strategy. Resolve strictly here
        # so an unknown class surfaces as a critical regardless of the
        # target_symbols / timeframe path.
        try:
            normalize_asset_class_strict(ctx.spec.asset_class)
        except ValueError as exc:
            return (
                self._critical(
                    f"Sizing realisability: {exc}. Pick a supported asset_class "
                    "(stocks/crypto/forex/futures/commodities)."
                ),
            )

        kind = getattr(ctx.spec.sizing, "kind", None)

        # Volatility-target sizing depends on realised volatility, which we
        # cannot estimate at design time. Emit a warning so the operator
        # notices that Rule 5 abstained — a silent skip would let an
        # implausibly low ``target_annual_vol`` (e.g. 0.001) bypass the
        # implementability check entirely.
        if kind == "volatility_target":
            tav = getattr(ctx.spec.sizing, "target_annual_vol", None)
            return (
                self._warning(
                    "Sizing realisability: volatility_target sizing requires "
                    "realised vol and cannot be evaluated at readiness time. "
                    f"Confirm target_annual_vol={tav!r} is sensible "
                    "(typical range: 0.05–0.30)."
                ),
            )

        # Resolve the universe to size against. ``_default_universe_for`` now
        # raises on unknown asset classes (previously it silently fell back to
        # ``OTHER_SYMBOLS``); surface that as a critical so the operator sees
        # the misclassification instead of a sizing pass against an unrelated
        # universe. When falling back to the default, apply the same cap
        # ``resolve_strategy_symbols`` would — otherwise a missing price for
        # a tail symbol beyond the cap (which the fetcher will never request)
        # would fail-close the strategy at readiness time.
        if ctx.spec.target_symbols:
            symbols = list(ctx.spec.target_symbols)
        else:
            try:
                raw_default = _default_universe_for(ctx.spec.asset_class)
            except ValueError as exc:
                return (
                    self._critical(
                        f"Sizing realisability: {exc}. Set spec.target_symbols "
                        "explicitly or pick a supported asset_class."
                    ),
                )
            cap = _max_universe_symbols()
            symbols = raw_default[:cap] if len(raw_default) > cap else raw_default
        if not symbols:
            return ()
        capital = config.initial_capital
        assert capital > 0, "initial_capital must be strictly positive"
        enforce_whole_lot = normalize_asset_class(ctx.spec.asset_class) in _WHOLE_LOT_ASSET_CLASSES
        threshold = 1.0 if enforce_whole_lot else 0.0

        # Notional is symbol-independent for both supported kinds, so resolve
        # it once. fixed_notional with notional_usd > initial_capital can
        # never produce a fillable order — the fill engine rejects with
        # ``insufficient_capital`` the moment ``portfolio.capital < notional``
        # (see ``fill_simulator.py``). fixed_fraction is bounded by
        # ``fraction <= 1.0`` in the DSL so it cannot trip this branch.
        if kind == "fixed_fraction":
            notional = capital * float(ctx.spec.sizing.fraction)
        elif kind == "fixed_notional":
            notional = float(ctx.spec.sizing.notional_usd)
            if notional > capital:
                return (
                    self._critical(
                        f"Sizing realisability: fixed_notional ${notional:.0f} "
                        f"exceeds initial_capital ${capital:.0f}; the first "
                        "order would be rejected with insufficient_capital."
                    ),
                )
        else:
            # Unknown sizing kind — covered by spec_dsl validation, but be
            # defensive: nothing further to evaluate.
            return ()

        # Per-symbol price defense. The qty>=1 lot-size check below only
        # matters for whole-lot classes (stocks/futures/commodities); forex
        # and crypto accept fractional quantities, so for them threshold==0
        # and the qty check never fires. But the price *sample* still carries
        # two signals that apply to every asset class, so the loop runs for
        # all of them rather than short-circuiting fractional classes:
        #   * a finite price <= 0 means a broken provider (a 0.0 parsed from a
        #     rate-limit body, a negative sentinel), not a market gap — fail
        #     closed regardless of asset class;
        #   * a non-finite (NaN/inf) price is unfillable for whole-lot classes
        #     (qty<1) → critical, but for fractional classes it is treated as a
        #     possibly-transient gap: tolerated when any symbol still has a
        #     finite sample, and downgraded to a warning (never a hard fail)
        #     when it affects every symbol.
        saw_finite_price = False
        nan_symbols: list[str] = []
        for sym in symbols:
            try:
                price = float(self._market_sample_provider(sym, ctx.spec.asset_class))
            except Exception:
                price = float("nan")

            if math.isfinite(price):
                if price <= 0:
                    return (
                        self._critical(
                            f"Sizing realisability: non-positive price sample for '{sym}' "
                            f"(got {price!r}); this signals a broken market-data provider, "
                            "not a market gap."
                        ),
                    )
                saw_finite_price = True
                qty = notional / price
                if qty < threshold:
                    return (
                        self._critical(
                            f"Sizing yields qty={qty:.4f} (threshold {threshold}) "
                            f"for symbol '{sym}' at sample price ${price:.2f} "
                            f"with capital ${capital:.0f}."
                        ),
                    )
            elif enforce_whole_lot:
                # Whole-lot classes genuinely need a price to size a fillable
                # order; a missing sample is unfillable → fail closed.
                return (
                    self._critical(
                        f"Sizing realisability: no usable price sample for '{sym}' (got {price!r})."
                    ),
                )
            else:
                # Fractional class with a non-finite sample — defer the verdict
                # until we know whether any symbol resolved to a finite price.
                nan_symbols.append(sym)

        # Only fractional asset classes reach here with unresolved NaN samples.
        # A NaN that affected *every* symbol (no finite sample anywhere) is a
        # persistently broken provider, but fractional sizing stays
        # implementable once data returns, so warn rather than fail closed. A
        # NaN alongside a finite sample is a transient gap and is ignored.
        if nan_symbols and not saw_finite_price:
            return (
                self._warning(
                    f"Sizing realisability: no usable price sample for any of {nan_symbols} "
                    f"({ctx.spec.asset_class}); market-data provider may be down. Proceeding "
                    "since fractional sizing stays implementable once data returns."
                ),
            )
        return ()

    # ------------------------------------------------------------------
    # Rule 6: Hypothesis–rule consistency.
    # ------------------------------------------------------------------
    def _check_hypothesis_rule_consistency(
        self, ctx: SpecReadinessCtx
    ) -> Iterable[QualityGateResult]:
        assert isinstance(ctx.spec, StrategySpec)
        terms = {
            re.sub(r"\s+", " ", m.group(0).lower())
            for m in _CONCEPT_TERMS.finditer(ctx.spec.hypothesis or "")
        }
        referenced = {ref.name for ref in self._iter_indicator_refs(ctx.spec)}
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
        # Demoted from critical to warning: a hypothesis that names an
        # indicator the predicates don't use is prose hygiene, not an
        # implementability failure. The design ↔ review loop sees the
        # warning row and can push DesignAgent.revise() to either add the
        # indicator reference or trim the prose; the cycle still produces
        # a backtest record so the orchestrator has a learning signal.
        return (
            self._warning(
                f"Hypothesis names indicator concept(s) {orphan} "
                "that no entry/exit rule references."
            ),
        )

    # ------------------------------------------------------------------
    # Rule 7: Timeframe data availability.
    # ------------------------------------------------------------------
    def _check_timeframe_availability(self, ctx: SpecReadinessCtx) -> Iterable[QualityGateResult]:
        assert isinstance(ctx.spec, StrategySpec)
        if ctx.spec.timeframe == "1d":
            return ()
        # Resolve via the strict normalizer so an off-vocabulary class that
        # reached the gate without going through ``StrategySpec`` construction
        # (e.g. post-construction assignment / model-copy mutation) fails
        # closed on an intraday timeframe instead of permissively mapping to
        # ``stocks`` and passing as if reliable intraday data existed.
        try:
            canonical = normalize_asset_class_strict(ctx.spec.asset_class)
        except ValueError:
            canonical = None
        if canonical in _FULL_TIMEFRAME_ASSET_CLASSES:
            return ()
        return (
            self._critical(
                f"Asset class '{ctx.spec.asset_class}' has no reliable "
                f"intraday data for timeframe '{ctx.spec.timeframe}'; "
                "use '1d' or pick stocks/crypto."
            ),
        )

    # ------------------------------------------------------------------
    # Rule 8: Risk-limit coherence — independent stop/profit and position
    # caps; both sub-checks can fire on the same spec.
    # ------------------------------------------------------------------
    def _check_risk_limit_coherence(self, ctx: SpecReadinessCtx) -> Iterable[QualityGateResult]:
        assert isinstance(ctx.spec, StrategySpec)
        out: List[QualityGateResult] = []

        stop_losses = [r for r in ctx.spec.exit_rules if isinstance(r, StopLossRule)]
        take_profits = [r for r in ctx.spec.exit_rules if isinstance(r, TakeProfitRule)]
        if stop_losses and take_profits:
            min_tp = min(r.pct for r in take_profits)
            max_sl = max(r.pct for r in stop_losses)
            assert min_tp > 0 and max_sl > 0, "exit-rule pcts must be strictly positive"
            # Wider stops than profit targets are a deliberate risk/reward
            # choice for trend-following strategies (let losers run a bit
            # further before bailing, take winners quicker). The two legs
            # don't "race" each other — they trigger on opposite price
            # directions — so this isn't an implementability failure. Warn
            # so the refinement prompt notices the unusual ratio, but don't
            # block synthesis.
            if max_sl >= min_tp:
                out.append(
                    self._warning(
                        f"stop_loss.pct={max_sl} ≥ take_profit.pct={min_tp}; "
                        "wider stop than profit target is a valid risk/reward "
                        "choice but unusual — confirm the asymmetry is intentional."
                    )
                )

        if ctx.spec.risk_limits.max_position_pct > MAX_POSITION_PCT_CEILING:
            out.append(
                self._critical(
                    f"max_position_pct={ctx.spec.risk_limits.max_position_pct}% "
                    f"exceeds the {MAX_POSITION_PCT_CEILING:g}% cap for a single-position "
                    "risk budget."
                )
            )
        return out

    # ------------------------------------------------------------------
    # Rule 9: Position-sizing / risk-policy coherence.
    #
    # Corrected risk model (do NOT re-introduce the "position = risk / stop"
    # inversion or a runtime "loss budget"):
    #   * ``sizing.fraction`` / ``max_position_pct`` = capital DEPLOYED per
    #     position as a fraction of the account. This is the worst-case loss
    #     (a trade can lose up to 100% of what was deployed).
    #   * ``stop_loss.pct`` is a price move off ENTRY measured against the
    #     trade, so the realised per-trade loss = deployed × stop and can
    #     NEVER exceed the deployed amount — there is no runtime budget to
    #     breach.
    # Three deterministic checks, all using correct algebra:
    #   A. Deployed capital must not exceed the position cap
    #      (``sizing.fraction`` ≤ ``max_position_pct``) — critical.
    #   B. The position cap must not exceed the declared per-trade loss
    #      tolerance (``max_position_pct`` ≤ ``max_loss_per_trade_pct`` when
    #      set), keeping the declared policy self-consistent — critical.
    #   C. A prose-stated per-trade deployment % must agree with the spec's
    #      sizing / cap — warning (prose hygiene).
    # ------------------------------------------------------------------
    def _check_risk_math_reconciliation(self, ctx: SpecReadinessCtx) -> Iterable[QualityGateResult]:
        assert isinstance(ctx.spec, StrategySpec)
        out: List[QualityGateResult] = []
        rel_tol = _sizing_coherence_rel_tol()

        kind = getattr(ctx.spec.sizing, "kind", None)
        # Volatility-target sizing is dynamic (depends on realised vol); the
        # deployed fraction is unknown at design time, so there is nothing to
        # reconcile — consistent with Rule 5 abstaining on the same kind.
        if kind == "volatility_target":
            return ()

        max_position_pct = float(ctx.spec.risk_limits.max_position_pct)

        # Resolve the deployed fraction (of the account) for the supported
        # static sizing kinds. ``fixed_notional`` needs initial_capital to
        # express the notional as a fraction, so it is skipped without config.
        pos_fraction: Optional[float] = None
        if kind == "fixed_fraction":
            pos_fraction = float(ctx.spec.sizing.fraction)
        elif kind == "fixed_notional":
            if ctx.config is not None:
                capital = float(ctx.config.initial_capital)
                assert capital > 0, "initial_capital must be strictly positive"
                pos_fraction = float(ctx.spec.sizing.notional_usd) / capital

        # Check A — deployed capital must not exceed the position cap. Both
        # sides are account-capital fractions, so this is the one genuine
        # structural contradiction (e.g. fraction=0.10 with max_position_pct=5).
        if pos_fraction is not None:
            pos_pct = pos_fraction * 100.0
            if pos_pct > max_position_pct * (1.0 + rel_tol):
                fix_hint = (
                    f"lowering the fraction to <={max_position_pct / 100.0:.4g} "
                    if kind == "fixed_fraction"
                    else f"lowering notional_usd to <=initial_capital×{max_position_pct:g}% "
                )
                out.append(
                    self._critical(
                        f"sizing deploys {pos_pct:.2f}% of equity per position but "
                        f"max_position_pct={max_position_pct:.2f}% — the sizing rule "
                        f"commits more capital than the risk limit allows. Resolve by "
                        f"EITHER {fix_hint}OR raising max_position_pct to "
                        f">={pos_pct:.2f} (max {MAX_POSITION_PCT_CEILING:g}%).",
                        rule_id="sizing:position_cap",
                    )
                )

        # Check B — the position cap may not put more capital at risk per trade
        # than the declared per-trade loss tolerance. Worst-case per-trade loss
        # = capital deployed (a trade can lose up to 100% of its value), so
        # ``max_position_pct`` must not exceed ``max_loss_per_trade_pct`` when
        # the latter is declared. (The realised loss is reduced further by the
        # stop and can never *exceed* the deployed amount — there is no runtime
        # budget to breach; this keeps the declared policy self-consistent.)
        max_loss_pct = ctx.spec.risk_limits.max_loss_per_trade_pct
        if max_loss_pct is not None:
            max_loss_pct = float(max_loss_pct)
            if max_position_pct > max_loss_pct * (1.0 + rel_tol):
                out.append(
                    self._critical(
                        f"max_position_pct={max_position_pct:.2f}% exceeds "
                        f"max_loss_per_trade_pct={max_loss_pct:.2f}% — the position cap "
                        "lets a single trade put more capital at risk than the declared "
                        "per-trade loss tolerance (worst case is a 100% loss of the "
                        f"deployed amount). Resolve by lowering max_position_pct to "
                        f"<={max_loss_pct:.2f} OR raising max_loss_per_trade_pct to "
                        f">={max_position_pct:.2f}.",
                        rule_id="risk_limits:loss_tolerance",
                    )
                )

        # Check C — prose claim vs actual deployment. "risk/deploy X% per
        # trade" denotes capital deployed; flag when it disagrees with both the
        # sizing fraction and the position cap beyond tolerance.
        prose_pct = _extract_prose_position_pct(ctx.spec.hypothesis or "")
        if prose_pct is not None:
            spec_targets: List[tuple[str, float]] = [("max_position_pct", max_position_pct)]
            if pos_fraction is not None:
                spec_targets.append(("sizing.fraction", pos_fraction * 100.0))
            disagrees_all = all(
                abs(prose_pct - value) > max(rel_tol * max(value, prose_pct), 1e-9)
                for _, value in spec_targets
            )
            if disagrees_all:
                detail = "; ".join(f"{name}={value:.2f}%" for name, value in spec_targets)
                out.append(
                    self._warning(
                        f"Hypothesis states ~{prose_pct:.2f}% deployed per trade but the "
                        f"spec sets {detail}. Reconcile the prose with the sizing/limit "
                        "(per-trade % is capital deployed, not a loss budget).",
                        rule_id="hypothesis:position_pct",
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
        _check_risk_math_reconciliation,
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
