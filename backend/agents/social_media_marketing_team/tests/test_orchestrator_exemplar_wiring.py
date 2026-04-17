"""Tests for orchestrator-level Winning Posts Bank wiring."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from social_media_marketing_team import orchestrator as orch_mod
from social_media_marketing_team.models import BrandGoals, CampaignProposal, Platform
from social_media_marketing_team.tests.conftest import make_goals


def test_extract_bank_keywords_filters_short_tokens():
    goals = make_goals(
        target_audience="B2B SaaS founders",
        goals=["engagement", "followers"],
        messaging_pillars=["Pricing strategies"],
        brand_objectives="Drive qualified demo requests",
    )
    keywords = orch_mod._extract_bank_keywords(goals)
    assert "founders" in keywords
    assert "saas" in keywords
    assert "engagement" in keywords
    assert "pricing" in keywords
    assert "strategies" in keywords
    # "b2b" is 3 chars after token-split → excluded by ≥4 filter.
    assert "b2b" not in keywords


def test_extract_bank_keywords_dedupes_case_insensitively():
    goals = make_goals(
        target_audience="Founders founders FOUNDERS",
        goals=["growth"],
    )
    keywords = orch_mod._extract_bank_keywords(goals)
    assert keywords.count("founders") == 1


def test_retrieve_exemplars_returns_empty_when_no_keywords(monkeypatch):
    goals = BrandGoals(
        brand_name="N",
        target_audience="a",  # ≤3 chars, filtered
        goals=["x"],
        messaging_pillars=[],
        brand_objectives="",
    )
    proposal = CampaignProposal(
        campaign_name="c",
        objective="x",
        audience_hypothesis="h",
    )

    called = {"yes": False}

    def _fake_find(*args, **kwargs):
        called["yes"] = True
        return [{"id": "x"}]

    monkeypatch.setattr(
        "social_media_marketing_team.shared.find_relevant_winning_posts",
        _fake_find,
    )
    result = orch_mod._retrieve_exemplars(proposal, goals, llm_client=None)
    assert result == []
    assert called["yes"] is False


def test_retrieve_exemplars_passes_platforms_and_context(monkeypatch):
    goals = make_goals(target_audience="founders", goals=["growth"])
    proposal = CampaignProposal(
        campaign_name="c",
        objective="drive qualified pipeline",
        audience_hypothesis="h",
        channel_mix_strategy={Platform.LINKEDIN: "lead-gen", Platform.X: "awareness"},
    )

    captured: Dict[str, Any] = {}

    def _fake_find(**kwargs):
        captured.update(kwargs)
        return [{"id": "e1", "platform": "linkedin"}]

    monkeypatch.setattr(
        "social_media_marketing_team.shared.find_relevant_winning_posts",
        _fake_find,
    )
    result = orch_mod._retrieve_exemplars(proposal, goals, llm_client="LLM")
    assert result == [{"id": "e1", "platform": "linkedin"}]
    assert set(captured["platforms"]) == {"linkedin", "x"}
    assert captured["rerank_context"] == "drive qualified pipeline"
    assert captured["llm_client"] == "LLM"
    assert "founders" in captured["query_keywords"]


def test_retrieve_exemplars_swallows_failure(monkeypatch, caplog):
    goals = make_goals(target_audience="founders", goals=["growth"])
    proposal = CampaignProposal(
        campaign_name="c",
        objective="drive",
        audience_hypothesis="h",
    )

    def _boom(**kwargs):
        raise RuntimeError("pg disabled")

    monkeypatch.setattr(
        "social_media_marketing_team.shared.find_relevant_winning_posts",
        _boom,
    )
    with caplog.at_level("WARNING"):
        result = orch_mod._retrieve_exemplars(proposal, goals, llm_client=None)
    assert result == []
    assert any("Winning posts bank retrieval failed" in rec.message for rec in caplog.records)


def test_plan_content_passes_exemplars_to_every_concept_agent(monkeypatch):
    """Orchestrator must forward retrieved exemplars to each concept agent."""
    from social_media_marketing_team.orchestrator import SocialMediaMarketingOrchestrator

    orch = SocialMediaMarketingOrchestrator()

    canned: List[Dict[str, Any]] = [
        {"id": "e1", "platform": "linkedin", "engagement_score": 0.9, "title": "t", "body": "b"}
    ]
    monkeypatch.setattr(orch_mod, "_retrieve_exemplars", lambda *a, **k: canned)

    seen: List[List[Dict[str, Any]]] = []

    class _Spy:
        def generate_candidates(self, proposal, goals, exemplars=None):
            seen.append(exemplars or [])
            return []

    orch.concept_team = [_Spy(), _Spy()]

    goals = make_goals()
    proposal = CampaignProposal(
        campaign_name="c",
        objective="drive",
        audience_hypothesis="h",
    )
    orch._plan_content(proposal, goals)

    assert len(seen) == 2
    for s in seen:
        assert s == canned


@pytest.mark.parametrize("exemplars", [[], None])
def test_plan_content_tolerates_missing_exemplars(monkeypatch, exemplars):
    from social_media_marketing_team.orchestrator import SocialMediaMarketingOrchestrator

    orch = SocialMediaMarketingOrchestrator()
    monkeypatch.setattr(orch_mod, "_retrieve_exemplars", lambda *a, **k: exemplars or [])

    goals = make_goals()
    proposal = CampaignProposal(
        campaign_name="c",
        objective="drive",
        audience_hypothesis="h",
    )
    plan = orch._plan_content(proposal, goals)
    # Should produce a valid (possibly empty-approved) content plan.
    assert plan.campaign_name == "c"
