"""Strands Agent that audits whether executed backtest trades faithfully
implement a strategy specification, and proposes Python code fixes when not.

Used by :class:`StrategyLabOrchestrator` after the code-refinement loop has
produced a runnable backtest. The orchestrator drives a problem-solving loop
(up to ``MAX_ALIGNMENT_ROUNDS``) that re-executes the proposed code through
the sandbox until the trades align with the strategy spec.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from strands import Agent

from ...models import BacktestResult, StrategySpec, TradeRecord
from .model_factory import get_strands_model

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class AlignmentIssue(BaseModel):
    """A single way in which the executed trades diverged from the spec."""

    rule_type: str = Field(
        description=(
            "Which part of the spec the trade ledger violated: "
            "'entry_rules' | 'exit_rules' | 'sizing_rules' | 'risk_limits' | "
            "'universe' | 'direction'."
        )
    )
    description: str
    severity: Literal["info", "warning", "critical"] = "warning"
    affected_trades: List[int] = Field(default_factory=list)


class TradeAlignmentReport(BaseModel):
    """Verdict from one alignment audit round."""

    aligned: bool
    rationale: str = ""
    issues: List[AlignmentIssue] = Field(default_factory=list)
    proposed_code: Optional[str] = None
    predicted_aligned_after_fix: bool = False
    changes_made: str = ""


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


_ALIGNMENT_USER_TEMPLATE = """\
Audit whether the executed trades below faithfully implement the strategy
specification, and propose code improvements if they do not.

## Strategy Specification (source of truth)
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal definition: {signal_definition}
Entry rules: {entry_rules}
Exit rules: {exit_rules}
Sizing rules: {sizing_rules}
Risk limits: {risk_limits}

## Aggregate Backtest Metrics
Annualized: {annualized_return_pct:.1f}% | Total: {total_return_pct:.1f}% | Sharpe: {sharpe_ratio:.2f}
Max DD: {max_drawdown_pct:.1f}% | Win rate: {win_rate_pct:.1f}% | Profit factor: {profit_factor:.2f}
Volatility: {volatility_pct:.1f}%

## Executed Trades ({n_trades} trades)
{trades_section}

## Current Strategy Code
```python
{strategy_code}
```

## Prior Alignment-Fix Attempts ({n_prior_attempts} so far)
{prior_attempts_text}

## Instructions
1. Restate each rule as a concrete test, then probe the sample trades.
2. Identify every misalignment as an AlignmentIssue (cite trade_num when
   relevant).
3. If misaligned, rewrite the FULL Python code so the next backtest run
   produces aligned trades. Preserve the `run_strategy(data, config) -> list`
   contract and only use allowed imports.
4. Set ``predicted_aligned_after_fix`` to ``true`` only when you are highly
   confident the fixed code will produce aligned trades on the next run.
5. If trades already align, set ``aligned`` to ``true``, return an empty
   ``issues`` array, and ``proposed_code`` to null.

Return ONLY a JSON object with no markdown.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class TradeAlignmentAgent:
    """Audit executed trades against a strategy spec and propose code fixes."""

    def run(
        self,
        spec: StrategySpec,
        code: str,
        trades: List[TradeRecord],
        metrics: BacktestResult,
        prior_attempts: Optional[List[str]] = None,
    ) -> TradeAlignmentReport:
        """Run one alignment audit round.

        Returns a :class:`TradeAlignmentReport`. On parser failure the report
        falls back to ``aligned=True`` so the orchestrator does not infinite-
        loop on a malformed LLM response (the fallback rationale records the
        parse error).
        """
        system_prompt = (_PROMPT_DIR / "alignment_system.md").read_text(encoding="utf-8")

        prior_text = (
            "None yet."
            if not prior_attempts
            else "\n".join(f"  Round {i + 1}: {a}" for i, a in enumerate(prior_attempts))
        )

        user_prompt = _ALIGNMENT_USER_TEMPLATE.format(
            asset_class=spec.asset_class,
            hypothesis=spec.hypothesis,
            signal_definition=spec.signal_definition,
            entry_rules=", ".join(spec.entry_rules),
            exit_rules=", ".join(spec.exit_rules),
            sizing_rules=", ".join(spec.sizing_rules),
            risk_limits=spec.risk_limits.model_dump_json(),
            annualized_return_pct=metrics.annualized_return_pct,
            total_return_pct=metrics.total_return_pct,
            sharpe_ratio=metrics.sharpe_ratio,
            max_drawdown_pct=metrics.max_drawdown_pct,
            win_rate_pct=metrics.win_rate_pct,
            profit_factor=metrics.profit_factor,
            volatility_pct=metrics.volatility_pct,
            n_trades=len(trades),
            trades_section=_format_trades_section(trades),
            strategy_code=code,
            n_prior_attempts=len(prior_attempts) if prior_attempts else 0,
            prior_attempts_text=prior_text,
        )

        agent = Agent(
            model=get_strands_model("strategy_ideation"),
            system_prompt=system_prompt,
            tools=[],
        )

        try:
            result = agent(user_prompt)
            parsed = _extract_json(str(result))
        except Exception as exc:
            logger.exception("Alignment agent failed to produce parseable JSON")
            return TradeAlignmentReport(
                aligned=True,
                rationale=(
                    "Alignment audit skipped: LLM response could not be parsed "
                    f"({exc}). Treating trades as aligned to avoid infinite loop."
                ),
            )

        return _coerce_report(parsed, fallback_code=code)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_trades_section(trades: List[TradeRecord], max_sample_rows: int = 20) -> str:
    """Compact, decision-relevant view of the trade ledger.

    Shows aggregate stats plus a chronological mix of head/tail rows so the
    LLM has both early and late-period evidence without overwhelming its
    context window.
    """
    if not trades:
        return "No trades produced by this backtest."

    n = len(trades)
    wins = sum(1 for t in trades if t.outcome == "win")
    losses = n - wins
    holds = [t.hold_days for t in trades]
    rets = [t.return_pct for t in trades]
    avg_hold = sum(holds) / n
    sides = sorted({t.side for t in trades})
    symbols = sorted({t.symbol for t in trades})

    lines = [
        f"Aggregate: {n} trades | {wins} wins / {losses} losses ({100.0 * wins / n:.1f}%)",
        f"Sides: {sides} | Symbols: {symbols[:8]}{' …' if len(symbols) > 8 else ''}",
        f"Hold days: avg {avg_hold:.1f} (min {min(holds)}, max {max(holds)})",
        f"Return %: best {max(rets):.2f}, worst {min(rets):.2f}",
        "",
        "Sample trades (entry → exit, position_value = shares × entry_price):",
    ]

    if n <= max_sample_rows:
        indices = list(range(n))
    else:
        head = max_sample_rows // 2
        tail = max_sample_rows - head
        indices = list(range(head)) + list(range(n - tail, n))

    seen: set[int] = set()
    for i in indices:
        if i in seen:
            continue
        seen.add(i)
        t = trades[i]
        lines.append(
            f"  #{t.trade_num} {t.symbol} {t.side} {t.entry_date}->{t.exit_date} "
            f"hold={t.hold_days}d shares={t.shares} entry={t.entry_price} "
            f"exit={t.exit_price} pos_val={t.position_value} "
            f"net_pnl={t.net_pnl} ret={t.return_pct:.2f}% [{t.outcome}]"
        )
    if n > len(seen):
        lines.append(f"  ... ({n - len(seen)} additional trades not shown) ...")

    return "\n".join(lines)


def _coerce_report(parsed: Dict[str, Any], fallback_code: str) -> TradeAlignmentReport:
    """Convert raw LLM JSON into a :class:`TradeAlignmentReport`.

    Tolerates loose schemas (missing fields, snake_case vs camelCase issues)
    so a small format drift in the LLM does not abort the alignment loop.
    """
    aligned = bool(parsed.get("aligned", False))
    rationale = str(parsed.get("rationale", "")).strip()
    raw_issues = parsed.get("issues") or []

    issues: List[AlignmentIssue] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        try:
            issues.append(AlignmentIssue.model_validate(raw))
        except Exception:
            # Best-effort coercion — keep going on a single bad issue
            issues.append(
                AlignmentIssue(
                    rule_type=str(raw.get("rule_type", "entry_rules")),
                    description=str(raw.get("description", "(unparseable issue)")),
                    severity=str(raw.get("severity", "warning"))
                    if str(raw.get("severity", "warning")) in ("info", "warning", "critical")
                    else "warning",  # type: ignore[arg-type]
                    affected_trades=list(raw.get("affected_trades") or []),
                )
            )

    proposed_code_raw = parsed.get("proposed_code")
    proposed_code = (
        str(proposed_code_raw).strip()
        if isinstance(proposed_code_raw, str) and proposed_code_raw.strip()
        else None
    )

    predicted = bool(parsed.get("predicted_aligned_after_fix", False))
    changes = str(parsed.get("changes_made", "")).strip()

    if aligned:
        # Defensive: ignore proposed_code / changes when the agent says aligned
        proposed_code = None
        predicted = False
        changes = ""

    # If misaligned but no code was proposed, the loop has nothing to act on.
    # Mark prediction false so the orchestrator exits cleanly.
    if not aligned and proposed_code is None:
        predicted = False

    return TradeAlignmentReport(
        aligned=aligned,
        rationale=rationale,
        issues=issues,
        proposed_code=proposed_code,
        predicted_aligned_after_fix=predicted,
        changes_made=changes,
    )


def _extract_json(text: str) -> Dict[str, Any]:
    """Extract a JSON object from LLM output, handling markdown fences."""
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)

    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in LLM response")

    depth = 0
    end = start
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON from LLM response: {e}") from e
