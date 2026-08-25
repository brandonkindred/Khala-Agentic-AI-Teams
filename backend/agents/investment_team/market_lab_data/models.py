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
    degraded: bool = Field(
        default=False, description="True if one or more sources failed or timed out"
    )
    degraded_reason: Optional[str] = Field(
        default=None, description="Human-readable reason when degraded"
    )
    sources_used: List[str] = Field(
        default_factory=list, description="Provider ids included in this snapshot"
    )

    fx_rates: dict[str, float] = Field(
        default_factory=dict,
        description="Sample FX vs USD (e.g. EUR, GBP, JPY keys with USD quote interpretation)",
    )
    macro_snippets: List[str] = Field(
        default_factory=list, description="Short macro lines, e.g. DGS10"
    )
    crypto_snapshot: Optional[str] = Field(
        default=None, description="Optional crypto headline price line"
    )
    social_sentiment: Optional[str] = Field(
        default=None,
        description="Optional social/sentiment line; often empty on free tier without dedicated API",
    )

    def scoped_to(self, asset_class: Optional[str]) -> "MarketLabContext":
        """Narrow the shared snapshot to what one asset category may see.

        ``fx_rates`` and ``crypto_snapshot`` are single asset-class-specific
        fields on an otherwise-shared per-batch snapshot; rendering them
        unconditionally into a category-pinned signal brief's prompt
        directly contradicts that brief's own "covers X and nothing else"
        scope instruction, leaving a path for cross-category evidence
        (explicit FX rates in a stocks-only brief, say) to reach the brief's
        narrative and, from there, the pinned design prompt it's injected
        into verbatim. ``macro_snippets`` (e.g. the 10-year yield) and
        ``social_sentiment`` are genuinely class-agnostic macro context and
        stay shared across every category.

        Preconditions:
            ``asset_class`` is a canonical asset-class label or ``None``
            (unscoped — returns ``self`` unchanged).
        Postconditions:
            Returns a new ``MarketLabContext`` with ``fx_rates`` cleared
            unless ``asset_class == "forex"`` and ``crypto_snapshot`` cleared
            unless ``asset_class == "crypto"``. Returns ``self`` verbatim
            (no copy) when ``asset_class`` is ``None``, when neither field
            is populated, or when both already match the given class.
        """
        if asset_class is None:
            return self
        updates: dict = {}
        if self.fx_rates and asset_class != "forex":
            updates["fx_rates"] = {}
        if self.crypto_snapshot and asset_class != "crypto":
            updates["crypto_snapshot"] = None
        if not updates:
            return self
        return self.model_copy(update=updates)

    def as_prompt_text(self) -> str:
        """Render a stable block for LLM consumption.

        Postconditions: returns the full rendered snapshot block; the content
        is never length-truncated so no decision-relevant line is dropped
        before reaching the prompt.
        """
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
        return "\n".join(lines)
