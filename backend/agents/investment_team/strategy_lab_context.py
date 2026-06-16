"""
Shared helpers for Strategy Lab: prior-results formatting and asset-class mix hints.

Used by strategy ideation and signal intelligence to avoid circular imports.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from .models import StrategyLabRecord

_CANONICAL_ASSET_CLASSES: tuple[str, ...] = (
    "stocks",
    "crypto",
    "forex",
    "options",
    "futures",
    "commodities",
)

# Asset classes the LLM is allowed to choose for new strategies (#535).
# 'options' is canonical (so ``normalize_asset_class`` preserves it for the
# validator gate to reject) but not a valid ideation target, so it's
# excluded from prompt counts and underrepresented-class steering.
_PROMPT_ASSET_CLASSES: tuple[str, ...] = tuple(
    c for c in _CANONICAL_ASSET_CLASSES if c != "options"
)

# Public alias of the ideation-valid asset classes. Callers that need to
# validate user-supplied category selections or compute exclusion complements
# import this rather than the underscore-prefixed internal tuple.
PROMPT_ASSET_CLASSES: tuple[str, ...] = _PROMPT_ASSET_CLASSES

# Backtest statuses for cycles that short-circuited *before* running a backtest.
# Their persisted ``strategy.asset_class`` may be a coerced placeholder (an
# unsupported class like ``bonds`` is canonicalized to ``stocks`` for schema
# validity before the redesign route), so they must not feed the asset-class
# diversity steering. Distinct from executed-but-losing statuses (``failed`` /
# ``failed: max_refinement_rounds``), which ran a real backtest with a genuine
# canonical class and SHOULD count — hence an explicit set rather than a blanket
# ``startswith("failed")`` filter.
#
# This must enumerate EVERY status the orchestrator passes to
# ``_build_short_circuit_record`` (the pre-backtest exit path). Keep it in sync
# with the ``short_circuit_status`` values in ``strategy_lab/orchestrator.py``:
# spec_unimplementable, spec_validation, code_synthesis, design_not_ready,
# budget_exhausted. The in-memory ``ConvergenceTracker`` skips all of these via
# ``count_asset_class=False`` at the call site; this set is the persisted-record
# equivalent for ``prior_records`` rebuilt after a restart.
_NON_EXECUTED_BACKTEST_STATUSES: frozenset[str] = frozenset(
    {
        "failed: spec_unimplementable",
        "failed: spec_validation",
        "failed: code_synthesis",
        "failed: design_not_ready",
        "failed: budget_exhausted",
    }
)


def normalize_asset_class(ac: object) -> str:
    """Map any asset-class string variant to one of the canonical labels.

    Accepts ``object`` so callers can pass raw LLM output without casting.
    """
    x = str(ac or "stocks").lower().strip()
    if x in ("equities", "equity", "stock", "etf", "etfs"):
        return "stocks"
    if x in ("fx",):
        return "forex"
    if x in ("commodity", "metal", "energy"):
        return "commodities"
    if x in ("cryptocurrency", "cryptocurrencies"):
        return "crypto"
    if x in _CANONICAL_ASSET_CLASSES:
        return x
    return "stocks"


# Asset classes whose venues trade in whole units (shares / contracts); all
# others (forex, crypto) accept fractional quantities. Single source of truth
# shared by the readiness whole-lot gate and the runtime sizing dispatcher so
# the two never disagree on whether a class is fractional.
WHOLE_LOT_ASSET_CLASSES: frozenset[str] = frozenset({"stocks", "futures", "commodities"})


def is_fractional_asset_class(ac: object) -> bool:
    """True when the normalized asset class trades in fractional quantities.

    Preconditions: none — ``ac`` may be any value (``None``/unknown normalize to
    ``"stocks"`` → whole-lot → ``False``).
    Postconditions: returns ``True`` iff ``normalize_asset_class(ac)`` is not in
    ``WHOLE_LOT_ASSET_CLASSES``.
    """
    return normalize_asset_class(ac) not in WHOLE_LOT_ASSET_CLASSES


def normalize_asset_class_strict(ac: object) -> str:
    """Strict variant of :func:`normalize_asset_class`.

    Applies the same alias map (``equity``/``equities``/``stock``/``etf``/``etfs``
    → ``stocks``, ``fx`` → ``forex``, ``commodity``/``metal``/``energy`` →
    ``commodities``, ``cryptocurrency``/``cryptocurrencies`` → ``crypto``) so
    callers see the same canonical class the runtime fetch path does, but raises
    :class:`ValueError` for truly unknown classes instead of silently falling
    back to ``"stocks"``.

    Use this in gates and other fail-closed paths where a typo'd
    ``asset_class`` (``"bonds"``, ``"crpto"``) must surface as an error.
    Runtime paths that need defense-in-depth keep using
    :func:`normalize_asset_class`.
    """
    x = str(ac or "").lower().strip()
    if x in ("equities", "equity", "stock", "etf", "etfs"):
        return "stocks"
    if x in ("fx",):
        return "forex"
    if x in ("commodity", "metal", "energy"):
        return "commodities"
    if x in ("cryptocurrency", "cryptocurrencies"):
        return "crypto"
    if x in _CANONICAL_ASSET_CLASSES:
        return x
    raise ValueError(
        f"unknown asset_class {ac!r}; expected one of {sorted(_CANONICAL_ASSET_CLASSES)} "
        "or a known alias (equity/equities/stock/etf/etfs, fx, "
        "commodity/metal/energy, cryptocurrency/cryptocurrencies)"
    )


def normalize_allowed_asset_classes(raw: Optional[Iterable[object]]) -> Optional[List[str]]:
    """Normalize a user-supplied list of asset categories to canonical, ideation-valid classes.

    The selector on the Strategy Lab UI lets the user constrain which asset
    categories the design agent may generate strategies for. This maps that
    raw selection (canonical names or known aliases such as ``stock`` /
    ``equity`` / ``fx``) to the canonical, ideation-valid labels in
    :data:`PROMPT_ASSET_CLASSES`, deduplicated and returned in canonical order.

    Preconditions:
      - ``raw`` is ``None`` or an iterable of scalar values (each a class name
        or known alias). Items that do not resolve to a known canonical class
        (via :func:`normalize_asset_class_strict`) are silently dropped, as is
        ``options`` — it is canonical but never a valid ideation target, so it
        cannot be a generation category.

    Postconditions:
      - Returns ``None`` when ``raw`` is ``None`` (no constraint — the caller
        treats this as "all categories allowed").
      - Otherwise returns a list that is a subset of :data:`PROMPT_ASSET_CLASSES`
        in canonical order with no duplicates. The list MAY be empty when
        ``raw`` was non-empty but contained nothing valid; the API boundary
        rejects that case rather than running with zero categories.
    """
    if raw is None:
        return None
    seen: set[str] = set()
    for item in raw:
        try:
            canonical = normalize_asset_class_strict(item)
        except ValueError:
            # Unrecognized token (e.g. "bonds", a typo) — drop it rather than
            # coercing to stocks, so the user's intent is never silently widened.
            continue
        if canonical in PROMPT_ASSET_CLASSES:
            seen.add(canonical)
    return [c for c in PROMPT_ASSET_CLASSES if c in seen]


def excluded_for_allowed(allowed: Optional[Iterable[str]]) -> List[str]:
    """Complement of an allowed-category set within the ideation-valid classes.

    The design pipeline constrains generation via an *exclusion* list
    (``exclude_asset_classes``). A positive "allowed categories" selection is
    therefore expressed downstream as everything in :data:`PROMPT_ASSET_CLASSES`
    that the user did NOT allow.

    Preconditions:
      - ``allowed`` is ``None`` or an iterable of canonical class labels
        (typically the output of :func:`normalize_allowed_asset_classes`, which
        may itself return ``None`` for "no constraint").

    Postconditions:
      - Returns ``[]`` when ``allowed`` is ``None`` (no constraint → nothing
        excluded).
      - Otherwise returns the canonical-order list of
        :data:`PROMPT_ASSET_CLASSES` not present in ``allowed``. Empty when
        ``allowed`` covers every class (also no constraint).
    """
    if allowed is None:
        return []
    allowed_set = set(allowed)
    return [c for c in PROMPT_ASSET_CLASSES if c not in allowed_set]


def format_prior_results(records: List[StrategyLabRecord], *, max_records: int = 50) -> str:
    if not records:
        return "None yet — this is the first strategy."
    ordered = sorted(records, key=lambda x: x.created_at)
    if len(ordered) > max_records:
        ordered = ordered[-max_records:]
    lines = []
    for i, r in enumerate(ordered, start=1):
        label = "WINNING" if r.is_winning else "LOSING"
        hyp = r.strategy.hypothesis.replace("\n", " ").strip()
        if len(hyp) > 160:
            hyp = hyp[:157] + "..."
        analysis = (r.analysis_narrative or "").replace("\n", " ").strip()
        if len(analysis) > 420:
            analysis = analysis[:417] + "..."
        rationale = (r.strategy_rationale or "").replace("\n", " ").strip()
        if len(rationale) > 220:
            rationale = rationale[:217] + "..."
        res = r.backtest.result
        lines.append(
            f"{i}. [{label}] {r.strategy.asset_class} | {hyp}\n"
            f"   Metrics: annual {res.annualized_return_pct:.1f}%, Sharpe {res.sharpe_ratio:.2f}, "
            f"max DD {res.max_drawdown_pct:.1f}%, win rate {res.win_rate_pct:.1f}%\n"
            f"   Ideation rationale: {rationale}\n"
            f"   Post-backtest analysis: {analysis}"
        )
    return "\n\n".join(lines)


def _or_join(items: List[str]) -> str:
    """Render a list as an Oxford-style ``a, b, or c`` menu (single item → itself)."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + ", or " + items[-1]


def asset_class_mix_hint(
    records: List[StrategyLabRecord],
    *,
    tail: int = 24,
    exclude: Optional[List[str]] = None,
) -> str:
    """Steer the LLM toward a balanced mix of asset classes across lab runs.

    ``exclude`` (optional) names asset classes the design agent is forbidden to
    pick this run — the complement of a user's allowed-category selection. It is
    typed ``List[str]`` (not ``Iterable[str]``) deliberately: ``str`` is itself
    iterable, so an ``Iterable[str]`` annotation would silently accept a bare
    string and iterate its characters. When provided, the menu, recent-class
    counts, and underrepresented-class steering are all restricted to the
    still-allowed classes so the hint never nudges the model toward a class the
    run is not permitted to use. When ``exclude`` is ``None`` / empty the output
    is identical to the unconstrained hint.
    """
    allowed = [c for c in PROMPT_ASSET_CLASSES if c not in set(exclude or ())]
    if not allowed:
        # Defensive: an exclusion covering every class would leave nothing to
        # steer toward. The API boundary rejects an empty allowed set, so this
        # only guards against a misuse from internal callers.
        allowed = list(PROMPT_ASSET_CLASSES)
    menu = _or_join(allowed)
    # The anti-stocks-bias nudge only makes sense when stocks is still a valid
    # choice; drop it when stocks has been excluded so the hint never references
    # a class the run cannot use. Unconstrained runs keep stocks, so their
    # output is unchanged.
    stocks_nudge = "do **not** default to stocks; " if "stocks" in allowed else ""
    if not records:
        return (
            "No prior lab strategies. Choose **asset_class** from "
            f"{menu} with similar frequency over time — "
            f"{stocks_nudge}pick the class that best fits your multi-signal story."
        )

    ordered = sorted(records, key=lambda x: x.created_at)
    # Exclude only cycles that short-circuited *before* running a backtest
    # (``_NON_EXECUTED_BACKTEST_STATUSES``): their ``strategy.asset_class`` may
    # be a coerced placeholder (an unsupported class like ``bonds`` mapped to
    # ``stocks`` for schema validity before the redesign route), so counting
    # them would let a rejected, never-backtested design pollute the stock
    # history and skew the diversity steering. Executed-but-losing cycles
    # (status ``failed`` / ``failed: max_refinement_rounds``) DID run a backtest
    # with a genuine canonical class and must keep counting — otherwise
    # repeated failed futures/forex/etc. runs would be omitted from steering.
    # Records persisted before ``BacktestRecord.status`` existed default to
    # ``"completed"``, so legacy rows are unaffected.
    executed = [
        r
        for r in ordered
        if str(getattr(r.backtest, "status", "completed")) not in _NON_EXECUTED_BACKTEST_STATUSES
    ]
    sample = executed[-tail:] if len(executed) > tail else executed
    if not sample:
        return (
            "No executed lab backtests yet. Choose **asset_class** from "
            f"{menu} with similar frequency over time — "
            f"{stocks_nudge}pick the class that best fits your multi-signal story."
        )
    # #535: count only asset classes the LLM may still target. 'options' is
    # rejected by StrategySpecValidator, so leaving it in the count dict
    # would push it into ``underrep`` whenever no options strategies have
    # run and steer the LLM toward a guaranteed-failure choice. Excluded
    # classes are likewise dropped so the counts/steering only span the
    # categories this run is allowed to generate.
    counts = {c: 0 for c in allowed}
    for r in sample:
        k = normalize_asset_class(r.strategy.asset_class)
        if k in counts:
            counts[k] += 1
        elif k not in PROMPT_ASSET_CLASSES and "stocks" in counts:
            # Only a class outside the ideation-valid set (``options``, or an
            # unknown coerced by ``normalize_asset_class``) folds into stocks,
            # and only when stocks is still an allowed target. A class that is a
            # valid ideation target but *excluded* this run is deliberately
            # skipped — it is outside the steering window, so counting it (as
            # stocks or anything else) would fabricate the diversity picture.
            counts["stocks"] += 1

    n_sample = len(sample)
    stock_share = counts.get("stocks", 0) / n_sample if n_sample else 0.0
    min_n = min(counts.values())
    underrep = [c for c, n in counts.items() if n == min_n]

    parts: List[str] = [
        "Recent asset-class counts (last "
        f"{n_sample} strategies): " + ", ".join(f"{k}={v}" for k, v in counts.items()) + "."
    ]
    if "stocks" in counts and stock_share > 0.35 and n_sample >= 2:
        non_stock = [c for c in allowed if c != "stocks"]
        if non_stock:
            parts.append(
                "Equities are relatively heavy in this window — **strongly prefer** "
                f"{_or_join(non_stock)} for this run if you can state coherent rules."
            )
    parts.append(
        "Underrepresented line(s) to favor when ties: "
        f"{', '.join(underrep)} — use one of these **unless** your thesis clearly requires a different class."
    )
    return " ".join(parts)
