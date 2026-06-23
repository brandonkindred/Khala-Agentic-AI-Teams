"""Unit tests for BrandComplianceAgent (pure Python, no LLM)."""

from __future__ import annotations

from branding_team.agents import BrandComplianceAgent
from branding_team.models import BrandCheckRequest, BrandingMission


def _mission() -> BrandingMission:
    return BrandingMission(
        company_name="Northstar Labs",
        company_description="A studio for product teams",
        target_audience="enterprise product leaders",
        values=["clarity", "trust", "tech"],
        differentiators=["hands-on partnership", "execution speed"],
    )


def test_word_boundary_avoids_substring_false_positives() -> None:
    """The value 'tech' must not match 'fintech'/'logistics'; only whole words
    count toward the on-brand score."""
    agent = BrandComplianceAgent()
    checks = [
        BrandCheckRequest(
            asset_name="Fintech logistics ad",
            asset_description="A fintech logistics platform for cathedrals",
        )
    ]
    (result,) = agent.evaluate(checks, _mission())
    # No whole-word brand cue present -> not on brand, no false 'tech' match.
    assert result.is_on_brand is False
    assert "tech" not in result.rationale[1]
    assert result.revision_suggestions  # populated for off-brand assets


def test_on_brand_when_multiple_whole_words_match() -> None:
    agent = BrandComplianceAgent()
    checks = [
        BrandCheckRequest(
            asset_name="Homepage",
            asset_description=(
                "Clear messaging for enterprise product leaders with trust-building proof"
            ),
        )
    ]
    (result,) = agent.evaluate(checks, _mission())
    # 'trust' and the 'enterprise product leaders' phrase both match.
    assert result.is_on_brand is True
    assert result.revision_suggestions == []


def test_empty_checks_returns_empty() -> None:
    agent = BrandComplianceAgent()
    assert agent.evaluate([], _mission()) == []
