"""
Signal Intelligence Expert — synthesizes a structured brief from priors, mix hint, and market snapshot.
"""

from __future__ import annotations

import json
import re
from typing import List

from strands import Agent

from llm_service import get_strands_model

from .market_lab_data.models import MarketLabContext
from .models import StrategyLabRecord
from .signal_intelligence_models import SignalIntelligenceBriefV1
from .strategy_lab_context import asset_class_mix_hint, format_prior_results

_MAX_BRIEF_INJECT_CHARS = 12000

_SIGNAL_SYSTEM = (
    "You are a Signal Intelligence Expert for a **simulated** strategy research lab. "
    "You synthesize macro/micro hypotheses and trade-style guidance from (1) prior lab results, "
    "(2) asset-class diversity hints, and (3) a **market data snapshot** that may be partial or delayed. "
    "External data is not investment advice. Never claim real-time precision; use as-of language. "
    "Ground every hypothesis in the prior table or the snapshot when possible; state uncertainty clearly."
)

_SIGNAL_JSON_INSTRUCTIONS = """\
Return ONLY a JSON object with these keys (no markdown):
{{
  "brief_version": 1,
  "macro_themes": ["short bullet", "..."],
  "micro_themes": ["..."],
  "high_value_signal_hypotheses": ["testable hypotheses tied to priors and/or snapshot"],
  "trade_structures_benefiting": ["e.g. pairs, spreads, options overlays — conceptual"],
  "pairing_guidance": "how to blend signals / asset classes this batch",
  "evidence_from_priors": "which prior rows or patterns you rely on, or 'none / first run'",
  "evidence_from_market_data": "which snapshot lines (FX, macro, crypto) you use, or 'none' if degraded/empty",
  "confidence": "low" | "medium" | "high",
  "unsupported_claims": ["optional list of things you cannot verify from inputs"]
}}
"""


def sanitize_brief_for_injection(text: str, *, max_chars: int = _MAX_BRIEF_INJECT_CHARS) -> str:
    """Strip control characters and cap length to reduce nested-prompt abuse."""
    cleaned = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    if re.search(r"(?i)ignore (all )?(previous|prior) instructions", cleaned):
        cleaned = "[sanitized: disallowed instruction pattern removed]\n" + cleaned[:8000]
    if len(cleaned) > max_chars:
        return cleaned[: max_chars - 20] + "\n...[truncated]"
    return cleaned


def brief_to_prompt_block(brief: SignalIntelligenceBriefV1) -> str:
    """Human-readable block inside delimiters for ideation."""
    lines = [
        f"brief_version: {brief.brief_version}",
        f"confidence: {brief.confidence}",
        "macro_themes: " + "; ".join(brief.macro_themes),
        "micro_themes: " + "; ".join(brief.micro_themes),
        "hypotheses: " + " | ".join(brief.high_value_signal_hypotheses),
        "trade_structures: " + " | ".join(brief.trade_structures_benefiting),
        f"pairing_guidance: {brief.pairing_guidance}",
        f"evidence_from_priors: {brief.evidence_from_priors}",
        f"evidence_from_market_data: {brief.evidence_from_market_data}",
    ]
    if brief.unsupported_claims:
        lines.append("unsupported_claims: " + "; ".join(brief.unsupported_claims))
    return sanitize_brief_for_injection("\n".join(lines))


class SignalIntelligenceExpert:
    def __init__(self, llm_client=None) -> None:
        self._agent = (
            llm_client
            if llm_client is not None
            else Agent(
                model=get_strands_model("signal_intelligence"),
                system_prompt=_SIGNAL_SYSTEM,
            )
        )

    def produce_signal_brief(
        self,
        prior_results: List[StrategyLabRecord],
        market_context: MarketLabContext,
    ) -> SignalIntelligenceBriefV1:
        prior_text = format_prior_results(prior_results)
        mix_hint = asset_class_mix_hint(prior_results)
        market_block = market_context.as_prompt_text()

        prompt = f"""\
## Prior Strategy Results
{prior_text}

## Asset-class diversity hint
{mix_hint}

## Market data snapshot (may be partial; not investment advice)
{market_block}

{_SIGNAL_JSON_INSTRUCTIONS}
"""

        result = self._agent(prompt)
        raw_text = str(result).strip()
        raw = json.loads(raw_text)
        data = dict(raw) if isinstance(raw, dict) else {}
        data.setdefault("brief_version", 1)
        return SignalIntelligenceBriefV1.model_validate(data)
