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
from typing import Any, Callable, ClassVar, Iterable, Iterator, List, Optional

from ...market_data_service import _max_universe_symbols
from ...models import BacktestConfig, StrategySpec
from ...strategy_lab_context import (
    PROMPT_ASSET_CLASSES,
    WHOLE_LOT_ASSET_CLASSES,
    normalize_asset_class,
    normalize_asset_class_strict,
)
from ...symbols import (
    COMMODITY_SYMBOLS,
    CRYPTO_SYMBOLS,
    FOREX_SYMBOLS,
    FOREX_SYMBOLS_BARE,
    FUTURES_SYMBOLS,
    FUTURES_SYMBOLS_BARE,
    OTHER_SYMBOLS,
    STOCK_SYMBOLS,
    classify_symbol,
)
from ..executor.predicate_evaluator import compare
from ..spec_dsl import (
    INDICATOR_OUTPUT_RANGES,
    AllOf,
    AnyOf,
    EntryRule,
    IndicatorName,
    IndicatorRef,
    OcoBracketRule,
    Predicate,
    ScaledTakeProfitRule,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    is_full_position_exit,
    iter_tree_indicator_refs,
    ladder_closes_full_position,
    stop_caps_side,
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

# Negligible relative epsilon for the *hard* sizing/cap comparisons (Checks A
# and B of Rule 9). These are structural limit breaches, not prose rounding, so
# they must be strict — the prose tolerance above must not let a spec deploy
# more than its declared cap. This epsilon only absorbs float-representation
# noise (e.g. 0.10 * 100 == 10.000000000000002), never a real overage.
_HARD_LIMIT_REL_EPS: float = 1e-9


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
# sizing. Under the realised-loss risk model these patterns match only the
# capital *deployed* into a position (a fraction of the account). "risk" is
# overloaded: the codebase's own ``format_sizing_rule()`` renders fixed sizing
# as "risk X% per trade" (DEPLOYMENT), so that bare form is matched below — but
# the loss-budget form "risk/lose X% OF equity/capital/account per trade"
# denotes the realised per-trade loss (``sizing.fraction × stop_loss.pct``), NOT
# the deployed amount, and is excluded so it cannot falsely flag a coherent spec
# (e.g. "risk 0.25% of equity per trade" with fraction=0.05 × stop=0.05). Each
# pattern captures the percentage in group ``pct`` and requires an explicit
# per-trade / position framing so the rule never fires on a stop-loss,
# take-profit, or drawdown percentage elsewhere in the prose.
_PROSE_POSITION_PCT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "deploy/allocate/use/commit/invest (up to|about|~) X% per trade|position"
    re.compile(
        r"\b(?:deploy|allocate|use|commit|invest)(?:ing)?\s+"
        r"(?:up\s+to\s+|about\s+|around\s+|approximately\s+|~\s*)?"
        r"(?P<pct>\d+(?:\.\d+)?)\s*%\s+(?:of\s+(?:the\s+)?(?:account|equity|capital|portfolio)\s+)?"
        r"(?:per[\s-]+trade|per[\s-]+position|in\s+(?:each|a)\s+(?:trade|position))",
        re.IGNORECASE,
    ),
    # "risk (up to|about|~) X% per trade|position" — DEPLOYMENT, matching
    # ``format_sizing_rule()``'s fixed-fraction rendering. The negative
    # lookahead drops the loss-budget form "risk X% OF equity/capital/account/
    # portfolio …", which is a realised-loss claim, not a deployed amount.
    re.compile(
        r"\brisk(?:ing)?\s+"
        r"(?:up\s+to\s+|about\s+|around\s+|approximately\s+|~\s*)?"
        r"(?P<pct>\d+(?:\.\d+)?)\s*%\s+"
        r"(?!of\s+(?:the\s+)?(?:account|equity|capital|portfolio))"
        r"(?:per[\s-]+trade|per[\s-]+position|in\s+(?:each|a)\s+(?:trade|position))",
        re.IGNORECASE,
    ),
    # "X% per-trade|per-position allocation/sizing/position"
    re.compile(
        r"\b(?P<pct>\d+(?:\.\d+)?)\s*%\s+per[\s-]+(?:trade|position)\s+"
        r"(?:allocation|sizing|position|stake)",
        re.IGNORECASE,
    ),
    # "X% position size/sizing" / "position size of X%". Requires the explicit
    # ``size``/``sizing`` qualifier so a bare "X% position" — which appears in
    # non-sizing prose like "take profit after a 10% position gain" — is NOT
    # read as a deployment claim and fed back as a spurious warning.
    re.compile(
        r"\b(?P<pct>\d+(?:\.\d+)?)\s*%\s+position\s+siz(?:e|ing)\b",
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


# Whole-lot asset classes are sourced from ``strategy_lab_context`` (single
# source of truth shared with the runtime sizing dispatcher) so the readiness
# whole-lot gate and the engine's fractional-sizing path never disagree. Crypto
# and forex accept fractional quantities, so Rule 5's whole-lot check is skipped
# for them — the runtime contract takes ``qty: float = Field(gt=0)``.

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
# Word-boundary anchored so substrings like "thematic" don't accidentally
# match "ema". This vocabulary covers every DSL indicator name and its
# common prose aliases. It is shared with ``audit_recent_runs.py`` (which
# keeps a byte-for-byte replica) so both the narrative-fidelity gate and
# the audit tool operate on the same indicator term set and cannot diverge.
# ``williams_r`` precedes ``williams`` so the regex engine's first-
# alternative-wins matching captures the full DSL token rather than the
# bare prose alias.
_CONCEPT_TERMS = re.compile(
    r"\b(rsi|macd|moving\s+average|ema|sma|bollinger|atr|stochastic|adx|vwap|"
    r"donchian|keltner|obv|on[\s-]balance\s+volume|mfi|money\s+flow|roc|"
    r"rate\s+of\s+change|cci|williams_r|williams)\b",
    re.IGNORECASE,
)

# Broader concept vocabulary used by ``strategy_validator.py``'s
# hypothesis-vs-rules consistency gate. Extends ``_CONCEPT_TERMS`` with
# pure strategy-concept words (breakout, mean reversion, momentum,
# volatility, volume) that have no DSL indicator mapping but are still
# relevant to consistency checking. These extra terms are NOT included in
# ``_CONCEPT_TERMS`` (and therefore not in ``audit_recent_runs.py``'s
# replica) because the audit's phantom-detection logic requires every
# recognised term to have a ``_CONCEPT_TO_INDICATOR_NAMES`` entry.
# ``strategy_validator.py`` imports this constant so both files share one
# definition and cannot silently diverge.
_CONCEPT_TERMS_BROAD = re.compile(
    r"\b(rsi|macd|moving\s+average|ema|sma|bollinger|atr|breakout|"
    r"mean\s+reversion|momentum|volatility|volume|vwap|stochastic|adx|obv|"
    r"on[\s-]balance\s+volume|donchian|keltner|mfi|money\s+flow|roc|"
    r"rate\s+of\s+change|cci|williams_r|williams)\b",
    re.IGNORECASE,
)


# Map each prose concept to the set of DSL indicator names that satisfy it.
# A concept is "orphan" iff *none* of its allowed indicators appears in the
# spec's predicates — so "moving average" is satisfied by either SMA or EMA.
def extract_known_tickers(text: str) -> set[str]:
    """Extract every known ticker mentioned in ``text``.

    Uses word-bounded, case-insensitive matching against the full symbol
    whitelist and strips Yahoo-provider suffixes (``=F``, ``=X``, ``-USD``)
    before returning so callers receive canonical bare symbols.

    Preconditions:
        ``text`` is a string (empty allowed).
    Postconditions:
        Returns a set of upper-cased canonical ticker strings (no provider
        suffixes) that were found in ``text`` and belong to the known symbol
        whitelist. The empty set is returned when ``text`` is empty or no
        known ticker appears.
    Invariants:
        Pure function; no I/O, no mutation of module state.
    """
    assert isinstance(text, str), "text must be a str"
    return {_canonicalize_ticker(m.group(0)) for m in _SYMBOL_REGEX.finditer(text or "")}


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
    "donchian": frozenset({"donchian"}),
    "keltner": frozenset({"keltner"}),
    "obv": frozenset({"obv"}),
    "on balance volume": frozenset({"obv"}),
    "on-balance volume": frozenset({"obv"}),
    "mfi": frozenset({"mfi"}),
    "money flow": frozenset({"mfi"}),
    "roc": frozenset({"roc"}),
    "rate of change": frozenset({"roc"}),
    "cci": frozenset({"cci"}),
    "williams_r": frozenset({"williams_r"}),
    "williams": frozenset({"williams_r"}),
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
    # The single asset category this design attempt is pinned to, or ``None``
    # when no pin applies (synthesis phase, refinement, ad-hoc validation).
    # Set by the design loop from the user's ``allowed_asset_classes``
    # selection; Rule 11 enforces it.
    pinned_asset_class: Optional[str] = None


# Comparison ops whose truth is monotone in the indicator value, so the
# satisfying set is a contiguous half-line ∩ [lo, hi] and endpoint evaluation
# decides always-true / always-false. ``==`` and the ``cross_*`` ops are
# handled separately (they are not monotone half-lines).
_MONOTONE_COMPARISON_OPS: frozenset[str] = frozenset({"<", "<=", ">", ">="})


def _ref_identity(side: object) -> Optional[str]:
    """Stable identity key for a predicate side that is a *reference*.

    Pre: ``side`` is a predicate ``lhs``/``rhs`` value (``IndicatorRef``,
    ``PriceRefLiteral`` string, or ``float``).
    Post: returns a string key for indicator / price-ref sides that compares
    equal iff two sides reference the same value; returns ``None`` for a float
    constant (not a reference).
    """
    if isinstance(side, IndicatorRef):
        return f"ind:{side.sig_id}"
    if isinstance(side, str):  # PriceRefLiteral, e.g. "bar.close"
        return f"price:{side}"
    return None


def _identical_ref_verdict(pred: Predicate) -> Optional[tuple[str, str]]:
    """Classify a predicate whose two sides reference the *same* value.

    Post: ``("false", reason)`` for a contradiction (``x < x`` / ``x > x`` /
    identical-ref cross), ``("true", reason)`` for a tautology (``x <= x`` /
    ``x >= x`` / ``x == x``), or ``None`` when the sides are not identical refs.
    """
    lhs_id = _ref_identity(pred.lhs)
    rhs_id = _ref_identity(pred.rhs)
    if lhs_id is None or rhs_id is None or lhs_id != rhs_id:
        return None
    ref = lhs_id.split(":", 1)[1]
    if pred.op in ("<", ">"):
        return (
            "false",
            f"both sides reference {ref} with op {pred.op!r} (a value is never strictly {'below' if pred.op == '<' else 'above'} itself)",
        )
    if pred.op in ("<=", ">=", "=="):
        return (
            "true",
            f"both sides reference {ref} with op {pred.op!r} (a value always equals itself)",
        )
    if pred.op in ("cross_above", "cross_below"):
        return (
            "false",
            f"both sides reference {ref} with op {pred.op!r} (identical series move together and never cross)",
        )
    return None


def _bounded_indicator_verdict(pred: Predicate) -> Optional[tuple[str, str]]:
    """Classify a predicate comparing a bounded indicator against a constant.

    Only the shape the DSL can express is considered: a bounded ``IndicatorRef``
    on the ``lhs`` against a float constant on the ``rhs``. (``lhs`` is never a
    bare float, and indicator-vs-indicator / indicator-vs-price-ref comparisons
    are data-dependent.)

    Post: ``("false", reason)`` when no in-range indicator value can satisfy the
    predicate, ``("true", reason)`` when every in-range value satisfies it, or
    ``None`` when the shape does not match or the predicate is reachable.
    """
    lhs, rhs, op = pred.lhs, pred.rhs, pred.op
    if not (isinstance(lhs, IndicatorRef) and lhs.name in INDICATOR_OUTPUT_RANGES):
        return None
    if not isinstance(rhs, (int, float)):
        return None
    const = float(rhs)
    lo, hi = INDICATOR_OUTPUT_RANGES[lhs.name]
    label = f"{lhs.name} ∈ [{lo:.0f}, {hi:.0f}] compared against {const:g}"

    if op in ("cross_above", "cross_below"):
        # A crossing requires the indicator to pass through ``const``; a level
        # strictly outside the indicator's range can never be crossed. A level
        # inside the range is reachable → undecidable here.
        if const < lo or const > hi:
            return "false", f"{label} (a crossing through an out-of-range level is impossible)"
        return None

    if op == "==":
        # Equality is reachable iff the constant lies within the range.
        if const < lo or const > hi:
            return "false", f"{label} (equality to an out-of-range value is impossible)"
        return None

    if op not in _MONOTONE_COMPARISON_OPS:
        return None

    # Monotone comparison: the satisfying set is a contiguous half-line ∩
    # [lo, hi], so the predicate is always-true iff it holds at both endpoints
    # and always-false iff it holds at neither.
    t_lo = compare(op, lo, const)
    t_hi = compare(op, hi, const)
    if not t_lo and not t_hi:
        return "false", f"{label} with op {op!r} (no in-range value satisfies it)"
    if t_lo and t_hi:
        return "true", f"{label} with op {op!r} (every in-range value satisfies it)"
    return None


def _classify_predicate(pred: Predicate) -> Optional[tuple[str, str]]:
    """Closed-form classification of a predicate as always-false / always-true.

    Pre: ``pred`` is a ``Predicate``.
    Post: ``("false", reason)`` / ``("true", reason)`` when the predicate's truth
    is decidable from the DSL alone (identical-reference tautology/contradiction,
    or a bounded indicator vs an out-of-range constant), else ``None``.
    """
    assert isinstance(pred, Predicate)
    ident = _identical_ref_verdict(pred)
    if ident is not None:
        return ident
    return _bounded_indicator_verdict(pred)


def _classify_tree(node: Any) -> Optional[tuple[str, str]]:
    """Compose :func:`_classify_predicate` over an ``all_of`` / ``any_of`` tree.

    Pre: ``node`` is a ``Predicate`` / ``AllOf`` / ``AnyOf``.
    Post: ``("false", reason)`` when the tree is provably always-false,
    ``("true", reason)`` when provably always-true, else ``None`` (undecidable —
    the gate abstains rather than risk a false reject). Composition:

      * ``all_of`` — always-false if ANY child is (the conjunction can never
        hold); always-true only if EVERY child is.
      * ``any_of`` — always-true if ANY child is; always-false only if EVERY
        child is.

    A single undecidable child collapses the parent to ``None`` unless a
    short-circuit verdict (a false conjunct / a true disjunct) already applies.
    """
    if isinstance(node, Predicate):
        return _classify_predicate(node)
    child_verdicts = [_classify_tree(child) for child in node.of]
    if isinstance(node, AllOf):
        for v in child_verdicts:
            if v is not None and v[0] == "false":
                return ("false", f"a conjunct is always false ({v[1]})")
        if all(v is not None and v[0] == "true" for v in child_verdicts):
            return ("true", "every conjunct is always true")
        return None
    # AnyOf
    for v in child_verdicts:
        if v is not None and v[0] == "true":
            return ("true", f"a disjunct is always true ({v[1]})")
    if all(v is not None and v[0] == "false" for v in child_verdicts):
        return ("false", "every disjunct is always false")
    return None


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
        pinned_asset_class: Optional[str] = None,
    ) -> List[QualityGateResult]:
        """Run every readiness rule and return one result list.

        Pre: ``spec`` is a StrategySpec; ``phase`` is a valid phase literal;
        ``pinned_asset_class``, when given, is a canonical asset-class label
        (a member of ``PROMPT_ASSET_CLASSES``) naming the single category the
        caller's design attempt is restricted to.
        Post: result list is non-empty; every entry carries the caller's
        ``phase`` and ``gate_name == GATE``. When ``pinned_asset_class`` is
        given and the spec violates the pin, at least one *critical* result is
        present — which is what makes the pin deterministic: the design loop
        never marks a readiness-critical spec ready, so an off-category spec
        can never reach code synthesis.
        """
        assert isinstance(spec, StrategySpec), "spec must be a StrategySpec"
        assert backtest_config is None or isinstance(backtest_config, BacktestConfig), (
            "backtest_config override must be a BacktestConfig or None"
        )
        assert pinned_asset_class is None or pinned_asset_class in PROMPT_ASSET_CLASSES, (
            "pinned_asset_class must be a canonical PROMPT_ASSET_CLASSES member or None"
        )
        ctx = SpecReadinessCtx(
            spec=spec,
            config=backtest_config or self._backtest_config,
            pinned_asset_class=pinned_asset_class,
        )
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
            if not isinstance(rule.when, (Predicate, AllOf, AnyOf)):
                return (
                    self._critical(
                        f"entry_rules[{idx}].when is not a Predicate or "
                        f"all_of/any_of combinator (got {type(rule.when).__name__})."
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
        """Verify the spec's exit rules can fully close every position.

        Preconditions: ``ctx.spec`` is a :class:`StrategySpec` (its ``exit_rules``
        is the authored exit list).
        Postconditions: returns an iterable (concretely a tuple) of
        :class:`QualityGateResult` — a single
        critical when there are no exit rules, when none is of an engine-closable
        kind (``signal_exit`` / ``stop_loss`` / ``take_profit`` /
        ``scaled_take_profit`` / ``oco_bracket``), or when the only exits are
        scaled ladders summing to < 1.0 with no full-position exit to close the
        residual; otherwise empty.
        """
        assert isinstance(ctx.spec, StrategySpec)
        # An ``oco_bracket`` only functions on the engine-managed entry path: its
        # legs attach to engine-EMITTED entry orders. With
        # ``requires_custom_code=True`` the runtime passes ``entry_rules=None`` to
        # the engine, the entry dispatcher never attaches the bracket, and the exit
        # evaluator skips it — so the bracket is inert and closes nothing. The
        # ``StrategySpec`` validator rejects this at construction, but the
        # orchestrator can flip ``requires_custom_code`` to True *after*
        # construction (trial-compile / synthesis fallback) without re-validation,
        # then re-run readiness — so enforce the same invariant here on the final
        # spec. A bracket-bearing custom-code spec is therefore NOT exit-complete.
        if ctx.spec.requires_custom_code and any(
            isinstance(r, OcoBracketRule) for r in ctx.spec.exit_rules
        ):
            return (
                self._critical(
                    "oco_bracket is not usable with requires_custom_code=True: the bracket "
                    "attaches only to engine-managed entries, so on the custom-code path it is "
                    "inert and cannot close positions. Replace it with a stop_loss / "
                    "take_profit / signal_exit, or set requires_custom_code=False.",
                    rule_id="exit_completeness:bracket_requires_engine_entries",
                ),
            )
        # Classify by type, not a duck-typed ``kind`` attribute, so a malformed
        # rule can't slip past the check; ``allowed_kinds`` mirrors these types
        # purely for the failure message. ``oco_bracket`` is engine-closable: its
        # stop / target legs attach to the entry order and the engine materializes
        # them into a resting OCO group, so a bracket-only spec closes positions.
        allowed_rule_types = (
            SignalExitRule,
            StopLossRule,
            TakeProfitRule,
            ScaledTakeProfitRule,
            OcoBracketRule,
        )
        allowed_kinds = {
            "signal_exit",
            "stop_loss",
            "take_profit",
            "scaled_take_profit",
            "oco_bracket",
        }
        if not ctx.spec.exit_rules:
            return (
                self._critical(
                    "No exit rules — positions would never close. Add at "
                    "least one of: signal_exit, stop_loss, take_profit, "
                    "scaled_take_profit, oco_bracket."
                ),
            )
        if not any(isinstance(r, allowed_rule_types) for r in ctx.spec.exit_rules):
            return (
                self._critical(f"exit_rules contains no rule of kind in {sorted(allowed_kinds)}."),
            )
        # A laddered take-profit whose rung fractions sum to < 1.0 closes only
        # those tranches and leaves the residual position open indefinitely. That
        # is exit-complete ONLY if a ladder itself sums to a full close, OR some
        # full-position exit closes the runner for EVERY side the spec enters — a
        # side-restricted trailing stop (``trailing_high`` caps only longs,
        # ``trailing_low`` only shorts) does NOT cover a residual on the opposite
        # side. A partial ladder whose residual nothing closes would finish the
        # backtest with an unclosed position — the "positions never close" failure
        # this rule guards against.
        ladders = [r for r in ctx.spec.exit_rules if isinstance(r, ScaledTakeProfitRule)]
        if ladders:
            covered = any(
                ladder_closes_full_position(lad) for lad in ladders
            ) or self._full_exit_covers_all_entry_sides(ctx.spec)
            if not covered:
                return (
                    self._critical(
                        "scaled_take_profit ladder(s) close only a fraction of the "
                        "position (rung qty_fraction sums to < 1.0) and no other "
                        "full-position exit (stop_loss / take_profit / signal_exit) "
                        "closes the residual — the remainder would never close. Add "
                        "a full-position exit or make a ladder's fractions sum to 1.0.",
                        rule_id="exit_completeness:partial_ladder_residual",
                    ),
                )
        return ()

    @staticmethod
    def _full_exit_covers_all_entry_sides(spec: StrategySpec) -> bool:
        """Whether a FULL-position exit can close a partial ladder's residual for
        every side the spec enters.

        A take-profit / signal-exit closes either side; a stop-loss closes only the
        side(s) its basis can fire for (:func:`stop_caps_side` — ``entry_price``
        both, ``trailing_high`` long only, ``trailing_low`` short only); a
        ``ScaledTakeProfitRule`` covers the residual only when its rungs sum to a
        full close (:func:`is_full_position_exit`), in which case it closes either
        side, and otherwise is a partial scale-out that leaves a residual.

        Preconditions: ``spec`` is a :class:`StrategySpec`.
        Postconditions: ``True`` iff for every distinct ``entry_rules`` side there is
        a full-position exit rule that can fire for that side. Vacuously ``True`` when
        the spec has no entry rules (Rule 2 separately flags missing entries).
        """

        def covers(rule: object, side: str) -> bool:
            # Stop-losses are the only side-conditional full exit; every other
            # full-position exit (TP / signal-exit / full-close ladder) closes
            # either side, and a partial ladder closes neither.
            if isinstance(rule, StopLossRule):
                return stop_caps_side(rule.basis, side)
            return is_full_position_exit(rule)

        entry_sides = {e.side for e in spec.entry_rules}
        return all(any(covers(r, side) for r in spec.exit_rules) for side in entry_sides)

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
        enforce_whole_lot = normalize_asset_class(ctx.spec.asset_class) in WHOLE_LOT_ASSET_CLASSES
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
        # A laddered take-profit's FIRST rung is its effective profit target for
        # the risk/reward ratio — that is the level the position starts realising
        # gains at. Fold each ladder's first rung pct into the take-profit pool.
        # ``levels[0]`` IS the lowest pct (the DSL enforces strictly-increasing pct).
        scaled_tp_first_rungs = [
            r.levels[0].pct for r in ctx.spec.exit_rules if isinstance(r, ScaledTakeProfitRule)
        ]
        tp_pcts = [r.pct for r in take_profits] + scaled_tp_first_rungs
        if stop_losses and tp_pcts:
            min_tp = min(tp_pcts)
            max_sl = max(r.pct for r in stop_losses)
            # Pydantic already enforces positivity upstream; re-check with an
            # explicit raise (not ``assert``) so the gate holds under ``python -O``.
            if min_tp <= 0 or max_sl <= 0:
                raise ValueError("exit-rule pcts must be strictly positive")
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
                        "choice but unusual — confirm the asymmetry is intentional.",
                        rule_id="risk_reward:stop_geq_tp",
                    )
                )

        if ctx.spec.risk_limits.max_position_pct == 0:
            # ``max_position_pct`` is the deployed-capital cap (and, since the
            # consolidation, the per-trade loss cap). A 0 cap sizes every entry to
            # zero at runtime (``_cap_qty_to_position`` → ``equity*0/100/close``),
            # so the strategy can never open a position. The schema permits 0
            # (``ge=0``), and Check A is skipped for dynamic sizing
            # (``volatility_target`` / unconfigured ``fixed_notional``), so catch
            # the degenerate cap here regardless of sizing kind.
            out.append(
                self._critical(
                    "max_position_pct=0 caps deployed position size to 0% of the "
                    "account, so the strategy can never open a position. Set a "
                    "positive max_position_pct.",
                    rule_id="risk_limits:zero_position_cap",
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
    # Risk model (do NOT re-introduce the "position = risk / stop" inversion,
    # a runtime "loss budget", or a separate per-trade-loss field):
    #   * ``sizing.fraction`` / ``max_position_pct`` = capital DEPLOYED per
    #     position as a fraction of the account. This deployed size IS the most
    #     a single trade can lose, because an entered position can lose up to
    #     ~100% of what was deployed — so the position cap is also the per-trade
    #     loss cap. (The retired ``max_loss_per_trade_pct`` was a duplicate of
    #     ``max_position_pct``; do NOT bring it back.)
    #   * ``stop_loss.pct`` is a price move off ENTRY measured against the
    #     trade — an independent, OPTIONAL safeguard that limits a position's
    #     realised loss BELOW a full wipeout. It is decoupled from sizing and is
    #     never multiplied into the cap.
    # A SHORT can lose more than 100% of deployed capital (price can more than
    # double), so the runtime auto-injects a 100%-adverse-move stop for any
    # short that lacks an effective stop (see ``TradingService.__init__``),
    # bounding a short's modeled worst-case loss at the deployed amount too.
    # That runtime contract is why an uncovered short needs no special gate
    # critical here.
    # Two deterministic checks, both using correct algebra:
    #   A. Deployed capital must not exceed the position cap
    #      (``sizing.fraction`` ≤ ``max_position_pct``) — critical. Only when
    #      the deployed fraction is known (skipped for ``volatility_target`` and
    #      unconfigured ``fixed_notional``).
    #   C. A prose-stated per-trade deployment % must agree with the ACTUAL
    #      deployed fraction (``sizing.fraction``) when it is known — warning
    #      (prose hygiene). The cap is an upper bound, not the deployed amount,
    #      so matching it must not by itself satisfy the claim;
    #      ``volatility_target`` deployment is dynamic so the check abstains.
    # ------------------------------------------------------------------
    def _check_risk_math_reconciliation(self, ctx: SpecReadinessCtx) -> Iterable[QualityGateResult]:
        assert isinstance(ctx.spec, StrategySpec)
        out: List[QualityGateResult] = []
        rel_tol = _sizing_coherence_rel_tol()

        kind = getattr(ctx.spec.sizing, "kind", None)
        max_position_pct = float(ctx.spec.risk_limits.max_position_pct)

        # Resolve the deployed fraction (of the account) for the supported
        # static sizing kinds. ``fixed_notional`` needs initial_capital to
        # express the notional as a fraction, so it is skipped without config.
        # ``volatility_target`` is dynamic (depends on realised vol), so the
        # deployed fraction is unknown at design time and stays ``None`` —
        # consistent with Rule 5 abstaining on the same kind.
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
        # Because the deployed size is also the most a single trade can lose,
        # this same cap is the per-trade loss bound — there is no separate loss
        # field to reconcile. Skipped when the deployed fraction is unknown
        # (volatility_target / unconfigured fixed_notional). This is a HARD cap,
        # so the comparison is strict (``_HARD_LIMIT_REL_EPS`` only absorbs float
        # noise) — the prose tolerance must not let the spec deploy more than the
        # declared limit.
        if pos_fraction is not None:
            pos_pct = pos_fraction * 100.0
            if pos_pct > max_position_pct * (1.0 + _HARD_LIMIT_REL_EPS):
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

        # Check C — prose claim vs ACTUAL deployment. "risk/deploy X% per trade"
        # denotes capital deployed, which the engine computes from the sizing
        # rule (``sizing.fraction``), NOT from ``max_position_pct`` (an upper
        # bound, not the deployed amount). When the deployed fraction is known,
        # reconcile the prose against it alone — matching the cap must not on
        # its own satisfy the claim, or a thesis can promise 10%/trade while the
        # backtest deploys 2%. ``volatility_target`` deployment is dynamic and
        # unknown at design time, so a prose deployment % cannot be reconciled
        # and the check abstains; unconfigured ``fixed_notional`` falls back to
        # the cap as the only available static figure.
        if kind != "volatility_target":
            prose_pct = _extract_prose_position_pct(ctx.spec.hypothesis or "")
            if prose_pct is not None:
                if pos_fraction is not None:
                    actual_pct = pos_fraction * 100.0
                    if abs(prose_pct - actual_pct) > max(
                        rel_tol * max(actual_pct, prose_pct), 1e-9
                    ):
                        out.append(
                            self._warning(
                                f"Hypothesis states ~{prose_pct:.2f}% deployed per trade but the "
                                f"spec deploys {actual_pct:.2f}% (sizing.fraction; "
                                f"max_position_pct={max_position_pct:.2f}% is only an upper bound, "
                                "not the deployed amount). Reconcile the prose with the actual sizing.",
                                rule_id="hypothesis:position_pct",
                            )
                        )
                elif abs(prose_pct - max_position_pct) > max(
                    rel_tol * max(max_position_pct, prose_pct), 1e-9
                ):
                    out.append(
                        self._warning(
                            f"Hypothesis states ~{prose_pct:.2f}% deployed per trade but "
                            f"max_position_pct={max_position_pct:.2f}% (no static deployed "
                            "fraction is available to compare against). Reconcile the prose with "
                            "the sizing/limit (per-trade % is capital deployed, not a loss budget).",
                            rule_id="hypothesis:position_pct",
                        )
                    )

        return out

    # ------------------------------------------------------------------
    # Rule 10: Predicate reachability / coherence — closed-form. Entry and
    # signal-exit predicates are pure boolean conditions, so two defect
    # classes are decidable WITHOUT market data and are caught here rather
    # than discovered at runtime:
    #   (a) a bounded indicator (RSI/ADX/Stochastic, all 0–100) compared
    #       against an out-of-range constant — ``rsi > 100`` (never true →
    #       a dead rule) or ``rsi < 101`` (always true → adds no signal);
    #   (b) a predicate whose two sides are the *same* reference —
    #       ``bar.close < bar.close`` (contradiction), ``rsi >= rsi``
    #       (tautology), or an identical-ref ``cross_above`` (never crosses).
    # Routing mirrors Rule 2 ("strategy cannot generate trades"): an
    # always-false ENTRY predicate is critical (no position can ever open);
    # an always-false SIGNAL-EXIT predicate is a warning (that exit leg never
    # fires, but stop/take-profit can still close positions); a vacuous
    # (always-true) predicate is a warning for both. Reachable predicates and
    # shapes this analysis cannot decide (unbounded indicators,
    # indicator-vs-indicator, mixed bar-field comparisons) emit nothing.
    # ------------------------------------------------------------------
    def _check_predicate_reachability(self, ctx: SpecReadinessCtx) -> Iterable[QualityGateResult]:
        assert isinstance(ctx.spec, StrategySpec)
        out: List[QualityGateResult] = []
        for when, kind, label in self._iter_rule_whens(ctx.spec):
            verdict = _classify_tree(when)
            if verdict is None:
                continue
            always, reason = verdict
            if always == "true":
                result = self._warning(
                    f"{label}.when is vacuous: {reason}. The predicate is always true, so "
                    "it adds no signal to the rule.",
                    rule_id="predicate:vacuous",
                )
            elif kind == "entry":
                result = self._critical(
                    f"{label}.when is unreachable: {reason}. An entry predicate that "
                    "can never be true means the strategy can never open a position.",
                    rule_id="predicate:unreachable",
                )
            else:
                result = self._warning(
                    f"{label}.when is unreachable: {reason}. This signal-exit leg can "
                    "never fire; positions would only close via stop-loss / take-profit "
                    "or other exit rules.",
                    rule_id="predicate:unreachable",
                )
            out.append(result)
        return out

    # ------------------------------------------------------------------
    # Rule 11: Asset-category pin — when the design attempt is pinned to one
    # category (the user selected a subset of asset classes at run start, and
    # the design loop picked one of them for this attempt), the spec must
    # declare that category and must not name symbols belonging to a
    # different one.
    #
    # This lives in the readiness gate rather than in a bespoke post-hoc
    # correction so the fix rides the design loop's own machinery: a critical
    # here becomes a synthetic critique, which the round's existing
    # ``DesignAgent.revise`` call answers — no extra LLM call, and the
    # critique ledger sees the issue like any other, so regression and stall
    # detection cover it. Because the loop only marks a spec ready when no
    # readiness critical remains, a spec outside the pinned category can never
    # reach code synthesis: the restriction is enforced, not merely requested.
    #
    # The symbol half is deliberately separated from the class half, and is
    # checked here even though ``mechanical_repair.repair_spec`` ALSO strips
    # off-category symbols — the two are not redundant, they cover disjoint
    # cases. A *minority* mismatch (most symbols on-category, a stray one or
    # two aren't) is the mechanical case repair_spec handles deterministically
    # before the reviewer runs, the same way it handles Rules 7 and 8. A
    # *wholesale* mismatch (every named symbol is off-category) is not a
    # stray ticker but evidence the whole named universe contradicts the
    # declared class; repair_spec deliberately leaves it untouched (stripping
    # to an empty list would fall back to the pinned class's full default
    # universe, silently laundering the mismatch instead of surfacing it).
    # This check is what catches that wholesale case: it fires unconditionally
    # on any off-category symbol, so if repair_spec already handled a minority
    # mismatch this never sees one; if it didn't (wholesale), this is the only
    # thing standing between the mismatch and a readiness-clean spec.
    # ------------------------------------------------------------------
    def _check_asset_category_pin(self, ctx: SpecReadinessCtx) -> Iterable[QualityGateResult]:
        pinned = ctx.pinned_asset_class
        if pinned is None:
            return ()
        out: List[QualityGateResult] = []
        actual = normalize_asset_class(ctx.spec.asset_class)
        if actual != pinned:
            out.append(
                self._critical(
                    f"asset_class is {actual!r} but this design attempt is pinned to "
                    f"{pinned!r}. Rebuild the ENTIRE strategy for {pinned!r} — hypothesis, "
                    f"entry/exit rules, indicators, sizing, and target_symbols must all "
                    f"describe a {pinned!r} strategy. Relabelling the asset class without "
                    f"redesigning the logic is not acceptable: each asset category has its "
                    f"own microstructure, session hours, and volatility regime.",
                    rule_id="asset_category:pin",
                )
            )
            # The symbol check below compares against the *declared* class,
            # which is already wrong — reporting it too would be noise.
            return tuple(out)
        offcategory = sorted(
            {
                sym
                for sym in ctx.spec.target_symbols
                # ``classify_symbol`` returns None for cross-asset ETFs (GLD,
                # QQQ, ...) that legitimately trade in more than one category —
                # only an unambiguous mismatch is a violation.
                if (cls := classify_symbol(sym)) is not None and cls != pinned
            }
        )
        if offcategory:
            out.append(
                self._critical(
                    f"target_symbols contains {offcategory} which belong to an asset class "
                    f"other than the pinned {pinned!r}. Every target symbol must be a "
                    f"{pinned!r} instrument.",
                    rule_id="asset_category:symbols",
                )
            )
        return tuple(out)

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
        _check_predicate_reachability,
        _check_asset_category_pin,
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_rule_whens(spec: StrategySpec) -> Iterator[tuple[Any, str, str]]:
        """Yield ``(when, kind, label)`` for every entry/signal-exit rule in ``spec``.

        ``when`` is the rule's predicate position — a single ``Predicate`` or an
        ``all_of`` / ``any_of`` tree. ``kind`` is ``"entry"`` or
        ``"signal_exit"``; ``label`` is a stable human-readable locator (e.g.
        ``"entry_rules[0]"``). Malformed rules (non-``EntryRule`` /
        non-``SignalExitRule``) and malformed ``when`` values (not a
        ``Predicate`` / ``AllOf`` / ``AnyOf`` — reachable via ``model_construct``,
        legacy loading, or post-construction mutation) are skipped: Rule 2
        already flags those as critical, and yielding them would crash the
        downstream tree walkers (``.of`` on a non-tree) instead of returning
        gate results.
        """
        assert isinstance(spec, StrategySpec)
        for idx, rule in enumerate(spec.entry_rules):
            if isinstance(rule, EntryRule) and isinstance(rule.when, (Predicate, AllOf, AnyOf)):
                yield rule.when, "entry", f"entry_rules[{idx}]"
        for idx, rule in enumerate(spec.exit_rules):
            if isinstance(rule, SignalExitRule) and isinstance(
                rule.when, (Predicate, AllOf, AnyOf)
            ):
                yield rule.when, "signal_exit", f"exit_rules[{idx}]"

    @staticmethod
    def _iter_indicator_refs(spec: StrategySpec) -> Iterator[IndicatorRef]:
        assert isinstance(spec, StrategySpec)
        # Reuses ``_iter_rule_whens`` so the entry/signal-exit rule traversal
        # lives in exactly one place; ``iter_tree_indicator_refs`` then projects
        # each ``when`` (leaf or tree) down to its indicator references.
        for when, _kind, _label in SpecReadinessGate._iter_rule_whens(spec):
            yield from iter_tree_indicator_refs(when)
