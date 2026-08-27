"""
Signal Intelligence Expert — synthesizes a structured brief from priors, mix hint, and market snapshot.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional

from strands import Agent

from llm_service import get_strands_model

from .market_lab_data.models import MarketLabContext
from .models import StrategyLabRecord
from .signal_intelligence_models import SignalIntelligenceBriefV1
from .strategy_lab_context import (
    PROMPT_ASSET_CLASSES,
    asset_class_mix_hint,
    excluded_for_allowed,
    format_prior_results,
)

_SIGNAL_SYSTEM = (
    "You are a Signal Intelligence Expert for a **simulated** strategy research lab. "
    "You synthesize macro/micro hypotheses and trade-style guidance from (1) prior lab results, "
    "(2) asset-class diversity hints, and (3) a **market data snapshot** that may be partial or delayed. "
    "External data is not investment advice. Never claim real-time precision; use as-of language. "
    "Ground every hypothesis in the prior table or the snapshot when possible; state uncertainty clearly."
)


def _signal_json_instructions(asset_class: Optional[str]) -> str:
    """JSON-shape instructions for the signal brief, scoped when pinned to one category.

    Preconditions: none.
    Postconditions: returns the schema instructions with the same keys every
    time; the ``pairing_guidance`` field's description asks the model to
    blend across asset classes only when ``asset_class`` is ``None`` (a
    run-wide, unscoped brief). With a category pinned, that phrasing would
    directly contradict the ``## Scope`` block's "Do NOT reference ... any
    other asset category" instruction a few lines above it in the same
    prompt — so a pinned call gets an intra-category pairing description
    instead.
    """
    pairing_guidance = (
        f"how to pair/combine signals within {asset_class} only (e.g. correlated "
        "symbols, complementary timeframes) — never a different asset class"
        if asset_class is not None
        else "how to blend signals / asset classes this batch"
    )
    # Mirrors pairing_guidance's own conditional: "options overlays" is a
    # reasonable example structure for an unscoped, run-wide brief, but a
    # pinned brief's ## Scope block already forbids referencing any other
    # asset category — an unconditional "options overlays" example risks the
    # model suggesting an off-category instrument anyway, so a pinned call
    # gets a same-category-only example instead.
    trade_structures_hint = (
        f"e.g. pairs, spreads, or other structures within {asset_class} only — "
        "never a different asset class"
        if asset_class is not None
        else "e.g. pairs, spreads, options overlays — conceptual"
    )
    # json.dumps rather than raw f-string interpolation so these hints can
    # never corrupt the surrounding JSON example even if a future edit adds a
    # quote, newline, or backslash to either branch above.
    pairing_guidance_json = json.dumps(pairing_guidance)
    trade_structures_hint_json = json.dumps(trade_structures_hint)
    return f"""\
Return ONLY a JSON object with these keys (no markdown):
{{
  "brief_version": 1,
  "macro_themes": ["short bullet", "..."],
  "micro_themes": ["..."],
  "high_value_signal_hypotheses": ["testable hypotheses tied to priors and/or snapshot"],
  "trade_structures_benefiting": [{trade_structures_hint_json}],
  "pairing_guidance": {pairing_guidance_json},
  "evidence_from_priors": "which prior rows or patterns you rely on, or 'none / first run'",
  "evidence_from_market_data": "which snapshot lines (FX, macro, crypto) you use, or 'none' if degraded/empty",
  "confidence": "low" | "medium" | "high",
  "unsupported_claims": ["optional list of things you cannot verify from inputs"]
}}
"""


def sanitize_brief_for_injection(text: str) -> str:
    """Strip control characters and flag injection patterns to reduce nested-prompt abuse.

    Preconditions: ``text`` is a string (may be empty).
    Postconditions: returns the full sanitized brief — control characters
    (other than newline/tab) are stripped and a disallowed
    "ignore previous instructions" pattern, when present, is prefixed with a
    sanitization notice. The content itself is never length-truncated; the
    whole brief reaches the prompt so no decision-relevant tail is lost.
    """
    cleaned = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    if re.search(r"(?i)ignore (all )?(previous|prior) instructions", cleaned):
        cleaned = "[sanitized: disallowed instruction pattern removed]\n" + cleaned
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
        *,
        asset_class: Optional[str] = None,
    ) -> SignalIntelligenceBriefV1:
        """Synthesize the per-batch signal brief from prior lab results.

        Preconditions:
            * ``prior_results`` are the records the brief may reason over. When
              ``asset_class`` is given the caller must have already filtered
              them to that class — this method does not re-filter.
            * ``asset_class``, when given, is a canonical asset-class label
              naming the single category this brief is scoped to.

        Postconditions:
            * Returns a validated :class:`SignalIntelligenceBriefV1`.
            * When ``asset_class`` is given, the prompt names that category as
              the brief's sole subject and the diversity hint is narrowed to
              it, guiding the model to confine every theme and hypothesis to
              the scoped category.
            * When ``asset_class`` is ``None``, the brief remains
              cross-category and the diversity hint enumerates the full
              menu. A brief is injected verbatim into the design prompt, so
              this unscoped path must never be used for an attempt pinned to
              a single category — only for callers that intentionally want a
              broad, run-wide synthesis.
        """
        if asset_class is not None:
            assert asset_class in PROMPT_ASSET_CLASSES, (
                f"asset_class must be a canonical PROMPT_ASSET_CLASSES member, got {asset_class!r}"
            )
        prior_text = format_prior_results(prior_results)
        # ``exclude`` narrows the hint's menu to the scoped class; without it
        # the hint enumerates all five categories and actively nudges away
        # from whichever one is "heavy" — which, on a single-category brief,
        # is always the scoped class itself.
        mix_hint = asset_class_mix_hint(
            prior_results,
            exclude=excluded_for_allowed([asset_class] if asset_class is not None else None),
        )
        market_block = market_context.as_prompt_text()
        scope_block = (
            f"""## Scope — SINGLE ASSET CATEGORY
This brief covers **{asset_class}** and nothing else. Every prior result below is a
{asset_class} strategy. Asset categories are not interchangeable: their microstructure,
session hours, liquidity, and volatility regimes differ, so evidence drawn from one
does not transfer to another. Do NOT reference, compare against, or recommend any
other asset category — confine every theme, hypothesis, trade structure, and piece of
prior evidence to {asset_class}.

"""
            if asset_class is not None
            else ""
        )

        prompt = f"""\
{scope_block}## Prior Strategy Results
{prior_text}

## Asset-class diversity hint
{mix_hint}

## Market data snapshot (may be partial; not investment advice)
{market_block}

{_signal_json_instructions(asset_class)}
"""

        result = self._agent(prompt)
        raw_text = str(result).strip()
        raw = json.loads(raw_text)
        data = dict(raw) if isinstance(raw, dict) else {}
        data.setdefault("brief_version", 1)
        return SignalIntelligenceBriefV1.model_validate(data)
