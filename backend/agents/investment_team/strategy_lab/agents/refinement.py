"""Strands Agent for refining strategy code after quality gate failures."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from strands import Agent

from ...models import BacktestResult, StrategySpec
from ..spec_dsl import format_rules_for_prompt, format_sizing_rule
from .model_factory import get_strands_model

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

_REFINEMENT_USER_TEMPLATE = """\
Fix the following trading strategy code that failed {failure_phase}.

## Current Strategy
Asset class: {asset_class}
Hypothesis: {hypothesis}
Entry rules: {entry_rules}
Exit rules: {exit_rules}
Sizing rules: {sizing_rules}
Risk limits: {risk_limits}

## Current Code
```python
{strategy_code}
```

## Failure Details
{failure_details}

{metrics_section}

## Prior Refinement Attempts ({n_prior_attempts} so far)
{prior_attempts_text}

## Instructions
1. Diagnose the root cause from the failure details.
2. Fix the code only. Do NOT alter the strategy spec (entry/exit/sizing
   rules, risk limits, hypothesis) — spec changes go through ideation,
   not refinement.
3. Ensure your fix doesn't re-introduce any previously fixed issues.

Return ONLY a JSON object with no markdown:
{{
  "strategy_code": "the complete fixed Python code",
  "changes_made": "1-2 sentence summary of what you changed and why"
}}
"""


class RefinementAgent:
    """Refine strategy code based on quality gate or execution failures."""

    def run(
        self,
        spec: StrategySpec,
        code: str,
        failure_phase: str,
        failure_details: str,
        metrics: Optional[BacktestResult] = None,
        prior_attempts: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Refine the strategy code.

        Returns:
            (updated_fields_dict, updated_code)
        """
        system_prompt = (_PROMPT_DIR / "refinement_system.md").read_text(encoding="utf-8")

        metrics_section = ""
        if metrics:
            metrics_section = (
                f"## Backtest Metrics (for context)\n"
                f"Annualized: {metrics.annualized_return_pct:.1f}% | "
                f"Total: {metrics.total_return_pct:.1f}% | "
                f"Sharpe: {metrics.sharpe_ratio:.2f} | "
                f"Max DD: {metrics.max_drawdown_pct:.1f}% | "
                f"Win rate: {metrics.win_rate_pct:.1f}% | "
                f"Profit factor: {metrics.profit_factor:.2f}"
            )

        prior_text = (
            "None yet."
            if not prior_attempts
            else "\n".join(f"  Round {i + 1}: {a}" for i, a in enumerate(prior_attempts))
        )

        user_prompt = _REFINEMENT_USER_TEMPLATE.format(
            failure_phase=failure_phase,
            asset_class=spec.asset_class,
            hypothesis=spec.hypothesis,
            entry_rules=format_rules_for_prompt(spec.entry_rules),
            exit_rules=format_rules_for_prompt(spec.exit_rules),
            sizing_rules=format_sizing_rule(spec.sizing),
            risk_limits=spec.risk_limits.model_dump_json(),
            strategy_code=code,
            failure_details=failure_details,
            metrics_section=metrics_section,
            n_prior_attempts=len(prior_attempts) if prior_attempts else 0,
            prior_attempts_text=prior_text,
        )

        agent = Agent(
            model=get_strands_model("strategy_ideation"),
            system_prompt=system_prompt,
            tools=[],
        )

        result = agent(user_prompt)
        parsed = _extract_json(str(result))

        updated_code = parsed.pop("strategy_code", code)
        return parsed, updated_code


def _extract_json(text: str) -> Dict[str, Any]:
    """Extract a JSON object from LLM output."""
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
