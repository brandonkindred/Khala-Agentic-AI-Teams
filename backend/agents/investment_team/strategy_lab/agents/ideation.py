"""Strands Agent for strategy ideation + Python code generation."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from strands import Agent

from ...models import StrategyLabRecord
from ...signal_intelligence_agent import brief_to_prompt_block
from ...signal_intelligence_models import SignalIntelligenceBriefV1
from ...strategy_lab_context import asset_class_mix_hint, format_prior_results
from .model_factory import get_strands_model

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

_IDEATION_USER_TEMPLATE = """\
Generate ONE novel swing-style strategy (typical holds ~2-14 days unless the asset class implies shorter).
Goal: exceed 8% annualized in principle, with explicit risk controls.

## Prior Strategy Results ({n_prior} tested so far, chronological)
{prior_results_text}

## Asset-class diversity (mandatory)
{asset_class_mix_hint}

{signal_section}

{convergence_directives}

## Instructions
Follow your decomposed reasoning process: ANALYZE → HYPOTHESIZE → DESIGN → STRESS-TEST → CODE → OUTPUT.

Each prior entry includes outcome, metrics, rationale, and post-backtest analysis. Generate a strategy that **differs** from prior ones and learns from their failures.

Return ONLY a JSON object with no markdown:
{{
  "asset_class": "stocks" | "crypto" | "forex" | "options" | "futures" | "commodities",
  "hypothesis": "1-3 sentence investment thesis tying multiple signals to edge",
  "signal_definition": "Describe the ensemble of signals and how they combine",
  "entry_rules": ["rule 1", "rule 2", "rule 3"],
  "exit_rules": ["exit rule 1", "exit rule 2"],
  "sizing_rules": ["sizing rule 1"],
  "risk_limits": {{"max_position_pct": 5, "stop_loss_pct": 3}},
  "speculative": false,
  "rationale": "Why this strategy and asset class now, given priors and the diversity hint",
  "strategy_code": "COMPLETE Python code implementing run_strategy(data, config) -> list"
}}
"""


class IdeationAgent:
    """Generate a novel trading strategy specification and executable Python code."""

    def run(
        self,
        prior_records: List[StrategyLabRecord],
        signal_brief: Optional[SignalIntelligenceBriefV1] = None,
        convergence_directives: Optional[List[str]] = None,
        exclude_asset_classes: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], str, str]:
        """Ideate a strategy with code.

        Returns:
            (strategy_dict, strategy_code, rationale)
        """
        system_prompt = (_PROMPT_DIR / "ideation_system.md").read_text(encoding="utf-8")

        # Build user prompt
        prior_text = format_prior_results(prior_records) if prior_records else "No prior strategies tested yet."
        mix_hint = asset_class_mix_hint(prior_records) if prior_records else "No history — choose freely."

        if exclude_asset_classes:
            mix_hint += f"\nMANDATORY EXCLUSION: Do NOT use these asset classes: {', '.join(exclude_asset_classes)}."

        signal_section = ""
        if signal_brief:
            block = brief_to_prompt_block(signal_brief)
            signal_section = f"## Signal Intelligence Brief\n{block}"

        directives_text = ""
        if convergence_directives:
            directives_text = "## Mandatory Directives\n" + "\n".join(convergence_directives)

        user_prompt = _IDEATION_USER_TEMPLATE.format(
            n_prior=len(prior_records),
            prior_results_text=prior_text,
            asset_class_mix_hint=mix_hint,
            signal_section=signal_section,
            convergence_directives=directives_text,
        )

        agent = Agent(
            model=get_strands_model("strategy_ideation"),
            system_prompt=system_prompt,
            tools=[],
        )

        result = agent(user_prompt)
        result_text = str(result)

        # Parse JSON from response
        parsed = _extract_json(result_text)

        strategy_code = parsed.pop("strategy_code", "")
        rationale = parsed.pop("rationale", "")

        return parsed, strategy_code, rationale


def _extract_json(text: str) -> Dict[str, Any]:
    """Extract a JSON object from LLM output, handling markdown fences."""
    # Try to find JSON in code fences first
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)

    # Find the outermost { ... }
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
