"""
Shared helpers for Strategy Lab: prior-results formatting and asset-class mix hints.

Used by strategy ideation and signal intelligence to avoid circular imports.
"""

from __future__ import annotations

from typing import List

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

# Backtest statuses for cycles that short-circuited *before* running a backtest.
# Their persisted ``strategy.asset_class`` may be a coerced placeholder (an
# unsupported class like ``bonds`` is canonicalized to ``stocks`` for schema
# validity before the redesign route), so they must not feed the asset-class
# diversity steering. Distinct from executed-but-losing statuses (``failed`` /
# ``failed: max_refinement_rounds``), which ran a real backtest with a genuine
# canonical class and SHOULD count — hence an explicit set rather than a blanket
# ``startswith("failed")`` filter.
_NON_EXECUTED_BACKTEST_STATUSES: frozenset[str] = frozenset(
    {
        "failed: spec_unimplementable",
        "failed: spec_validation",
        "failed: code_synthesis",
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


def asset_class_mix_hint(records: List[StrategyLabRecord], *, tail: int = 24) -> str:
    """Steer the LLM toward a balanced mix of asset classes across lab runs."""
    if not records:
        return (
            "No prior lab strategies. Choose **asset_class** from "
            "stocks, crypto, forex, futures, or commodities with similar frequency over time — "
            "do **not** default to stocks; pick the class that best fits your multi-signal story."
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
            "stocks, crypto, forex, futures, or commodities with similar frequency over time — "
            "do **not** default to stocks; pick the class that best fits your multi-signal story."
        )
    # #535: count only asset classes the LLM may still target. 'options' is
    # rejected by StrategySpecValidator, so leaving it in the count dict
    # would push it into ``underrep`` whenever no options strategies have
    # run and steer the LLM toward a guaranteed-failure choice.
    counts = {c: 0 for c in _PROMPT_ASSET_CLASSES}
    for r in sample:
        k = normalize_asset_class(r.strategy.asset_class)
        if k in counts:
            counts[k] += 1
        else:
            counts["stocks"] += 1

    n_sample = len(sample)
    stock_share = counts["stocks"] / n_sample if n_sample else 0.0
    min_n = min(counts.values())
    underrep = [c for c, n in counts.items() if n == min_n]

    parts: List[str] = [
        "Recent asset-class counts (last "
        f"{n_sample} strategies): " + ", ".join(f"{k}={v}" for k, v in counts.items()) + "."
    ]
    if stock_share > 0.35 and n_sample >= 2:
        parts.append(
            "Equities are relatively heavy in this window — **strongly prefer** "
            "crypto, forex, futures, or commodities for this run if you can state coherent rules."
        )
    parts.append(
        "Underrepresented line(s) to favor when ties: "
        f"{', '.join(underrep)} — use one of these **unless** your thesis clearly requires a different class."
    )
    return " ".join(parts)
