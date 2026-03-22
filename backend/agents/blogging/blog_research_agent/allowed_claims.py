"""
Allowed claims schema and utilities for the blogging pipeline.

Research Librarian produces allowed_claims.json; Draft Writer uses only these
claims and tags them as [CLAIM:id] in the draft.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

RiskLevel = Literal["low", "medium", "high"]

EXTRACT_CLAIMS_PROMPT = """You are an expert research analyst extracting evidence-backed factual claims from research material for a blog post.

Given the research document and source references below, extract 5-15 factual claims that are:
- Explicitly supported by the sources (with citations)
- Suitable for use in a blog post (statistics, findings, definitions, verifiable facts)
- NOT opinions, recommendations, or vague assertions

For each claim, provide:
- id: short unique ID (e.g. "1", "2", "c1")
- text: the claim as a clear, standalone sentence
- citations: list of source identifiers (e.g. URL, title, or "Source 1")
- risk_level: "low" (well-established), "medium" (needs context), or "high" (contested or sensitive)

Output JSON only, in this exact format:
{"claims": [{"id": "1", "text": "...", "citations": ["..."], "risk_level": "low"}, ...]}

RESEARCH DOCUMENT:
---
{compiled_document}
---

SOURCES (for citations):
---
{sources_text}
---

JSON output:"""


class ClaimEntry(BaseModel):
    """A single evidence-backed factual claim."""

    id: str = Field(..., description="Unique claim ID (e.g. '123', 'c1').")
    text: str = Field(..., description="The factual claim text.")
    citations: List[str] = Field(default_factory=list, description="Source references.")
    risk_level: RiskLevel = Field(default="low", description="Risk level for this claim.")


class AllowedClaims(BaseModel):
    """Schema for allowed_claims.json."""

    topic: str = Field(default="", description="Topic or brief this claims set applies to.")
    claims: List[ClaimEntry] = Field(default_factory=list, description="List of allowed factual claims.")

    def to_dict(self) -> Dict[str, Any]:
        """Export for JSON serialization."""
        return {"topic": self.topic, "claims": [c.model_dump() for c in self.claims]}


def extract_allowed_claims(
    llm_client: Any,
    compiled_document: str,
    references: List[Any],
    topic: str = "",
) -> AllowedClaims:
    """
    Extract evidence-backed factual claims from research output using the LLM.

    Args:
        llm_client: LLM client with complete_json method.
        compiled_document: Research compiled document text.
        references: List of ResearchReference or similar (with title, url, summary).
        topic: Optional topic/brief for the claims set.

    Returns:
        AllowedClaims with extracted claims.
    """
    sources_parts = []
    for i, ref in enumerate(references[:15], 1):
        title = getattr(ref, "title", str(ref))[:80]
        url = getattr(ref, "url", "")
        sources_parts.append(f"Source {i}: {title} | {url}")
    sources_text = "\n".join(sources_parts) if sources_parts else "No sources"

    prompt = EXTRACT_CLAIMS_PROMPT.format(
        compiled_document=compiled_document[:8000],
        sources_text=sources_text,
    )

    try:
        data = llm_client.complete_json(prompt, temperature=0.2)
    except Exception as e:
        logger.warning("Claims extraction failed: %s; returning empty claims", e)
        return AllowedClaims(topic=topic, claims=[])

    if not isinstance(data, dict):
        logger.warning("Claims extraction: LLM returned non-dict (type=%s); returning empty claims", type(data).__name__)
        return AllowedClaims(topic=topic, claims=[])

    try:
        raw_claims = data.get("claims") if isinstance(data.get("claims"), list) else []
    except (KeyError, AttributeError, TypeError) as e:
        logger.warning("Claims extraction: could not read 'claims' from response: %s; returning empty claims", e)
        return AllowedClaims(topic=topic, claims=[])

    claims = []
    for c in raw_claims:
        if not isinstance(c, dict):
            continue
        try:
            cid = str(c.get("id", len(claims) + 1))
            text = (c.get("text") or "").strip()
            if not text:
                continue
            citations = c.get("citations") or []
            if isinstance(citations, str):
                citations = [citations]
            risk = (c.get("risk_level") or "low").lower()
            if risk not in ("low", "medium", "high"):
                risk = "low"
            claims.append(
                ClaimEntry(id=cid, text=text, citations=list(citations), risk_level=risk)
            )
        except (KeyError, TypeError, ValueError, Exception) as _e:
            logger.debug("Skipping invalid claim entry: %s", _e)
            continue

    return AllowedClaims(topic=topic, claims=claims)
