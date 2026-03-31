"""Pydantic models for market lab data snapshots."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class StrategyLabDataRequest(BaseModel):
    """Optional scoping for a fetch (free tier may ignore symbol lists)."""

    benchmark_symbol: str = Field(default="SPY", description="Equity benchmark hint for context")


class MarketLabContext(BaseModel):
    """
    Compact, prompt-friendly snapshot from free-tier APIs.

    Not investment advice; may be delayed or incomplete when degraded=True.
    """

    fetched_at: str = Field(..., description="ISO UTC when snapshot was assembled")
    degraded: bool = Field(default=False, description="True if one or more sources failed or timed out")
    degraded_reason: Optional[str] = Field(default=None, description="Human-readable reason when degraded")
    sources_used: List[str] = Field(default_factory=list, description="Provider ids included in this snapshot")

    fx_rates: dict[str, float] = Field(
        default_factory=dict,
        description="Sample FX vs USD (e.g. EUR, GBP, JPY keys with USD quote interpretation)",
    )
    macro_snippets: List[str] = Field(default_factory=list, description="Short macro lines, e.g. DGS10")
    crypto_snapshot: Optional[str] = Field(default=None, description="Optional crypto headline price line")
    social_sentiment: Optional[str] = Field(
        default=None,
        description="Optional social/sentiment line; often empty on free tier without dedicated API",
    )

    def as_prompt_text(self, *, max_chars: int = 6000) -> str:
        """Render a stable block for LLM consumption."""
        lines: List[str] = [
            f"Data as-of: {self.fetched_at}",
            f"Sources: {', '.join(self.sources_used) if self.sources_used else 'none'}",
        ]
        if self.degraded:
            lines.append(f"WARNING: degraded snapshot — {self.degraded_reason or 'partial data'}")
        if self.fx_rates:
            fx = ", ".join(f"{k}={v:.4f}" for k, v in sorted(self.fx_rates.items())[:12])
            lines.append(f"FX (sample vs USD): {fx}")
        for s in self.macro_snippets:
            lines.append(f"Macro: {s}")
        if self.crypto_snapshot:
            lines.append(f"Crypto: {self.crypto_snapshot}")
        if self.social_sentiment:
            lines.append(f"Social/sentiment: {self.social_sentiment}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            return text[: max_chars - 3] + "..."
        return text
