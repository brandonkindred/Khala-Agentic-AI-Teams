"""Pydantic models for the Signal Intelligence Expert JSON brief."""

from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field

ConfidenceLevel = Literal["low", "medium", "high"]


class SignalIntelligenceBriefV1(BaseModel):
    """Versioned structured brief from the Signal Intelligence Expert LLM."""

    brief_version: int = Field(default=1, ge=1)
    macro_themes: List[str] = Field(default_factory=list)
    micro_themes: List[str] = Field(default_factory=list)
    high_value_signal_hypotheses: List[str] = Field(default_factory=list)
    trade_structures_benefiting: List[str] = Field(default_factory=list)
    pairing_guidance: str = Field(default="", description="How to combine signals / asset classes for this window")
    evidence_from_priors: str = Field(
        default="",
        description="References to prior lab indices or 'none / first run'",
    )
    evidence_from_market_data: str = Field(
        default="",
        description="Symbols/series from MarketLabContext or 'none' if empty/degraded",
    )
    confidence: ConfidenceLevel = Field(default="medium")
    unsupported_claims: List[str] = Field(default_factory=list)
