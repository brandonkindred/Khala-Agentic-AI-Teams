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


def normalize_asset_class(ac: str) -> str:
    x = (ac or "").lower().strip()
    if x in ("equities", "equity", "stock"):
        return "stocks"
    if x in ("fx",):
        return "forex"
    if x in ("commodity", "metal", "energy"):
        return "commodities"
    if x in _CANONICAL_ASSET_CLASSES:
        return x
    return "stocks"


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
            "stocks, crypto, forex, options, futures, or commodities with similar frequency over time — "
            "do **not** default to stocks; pick the class that best fits your multi-signal story."
        )

    ordered = sorted(records, key=lambda x: x.created_at)
    sample = ordered[-tail:] if len(ordered) > tail else ordered
    counts = {c: 0 for c in _CANONICAL_ASSET_CLASSES}
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
        f"{n_sample} strategies): "
        + ", ".join(f"{k}={v}" for k, v in counts.items())
        + "."
    ]
    if stock_share > 0.35 and n_sample >= 2:
        parts.append(
            "Equities are relatively heavy in this window — **strongly prefer** "
            "crypto, forex, options, futures, or commodities for this run if you can state coherent rules."
        )
    parts.append(
        "Underrepresented line(s) to favor when ties: "
        f"{', '.join(underrep)} — use one of these **unless** your thesis clearly requires a different class."
    )
    return " ".join(parts)
