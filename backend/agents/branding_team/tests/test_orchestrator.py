from unittest.mock import patch

from branding_team import BrandingMission, BrandingTeamOrchestrator, HumanReview, WorkflowStatus
from branding_team.models import BrandCheckRequest, CompetitiveSnapshot, DesignAssetRequestResult


def _mission() -> BrandingMission:
    return BrandingMission(
        company_name="Northstar Labs",
        company_description="A strategic studio helping product teams ship cohesive digital experiences",
        target_audience="enterprise product leaders",
        values=["clarity", "trust", "momentum"],
        differentiators=["hands-on partnership", "execution speed"],
    )


def test_requires_human_approval() -> None:
    orchestrator = BrandingTeamOrchestrator()
    result = orchestrator.run(mission=_mission(), human_review=HumanReview(approved=False, feedback="Need legal review."))

    assert result.status == WorkflowStatus.NEEDS_HUMAN_DECISION
    assert result.human_feedback == "Need legal review."
    assert result.mood_boards
    assert result.wiki_backlog


def test_ready_for_rollout_with_brand_checks() -> None:
    orchestrator = BrandingTeamOrchestrator()
    checks = [
        BrandCheckRequest(
            asset_name="Homepage refresh",
            asset_description="Clear messaging for enterprise product leaders with trust-building proof points",
        ),
        BrandCheckRequest(
            asset_name="Flashy ad",
            asset_description="Guaranteed viral growth for everyone overnight",
        ),
    ]

    result = orchestrator.run(mission=_mission(), human_review=HumanReview(approved=True), brand_checks=checks)

    assert result.status == WorkflowStatus.READY_FOR_ROLLOUT
    assert len(result.brand_guidelines) >= 4
    assert result.design_system.foundation_tokens
    assert len(result.brand_checks) == 2
    assert any(not item.is_on_brand for item in result.brand_checks)


def test_run_with_include_market_research_adds_competitive_snapshot() -> None:
    with patch("branding_team.adapters.market_research.request_market_research") as mock_mr:
        mock_mr.return_value = CompetitiveSnapshot(
            summary="Competitive summary",
            similar_brands=["A", "B"],
            insights=["insight1"],
            source="market_research_team",
        )
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=_mission(),
            human_review=HumanReview(approved=True),
            include_market_research=True,
        )
        assert result.competitive_snapshot is not None
        assert result.competitive_snapshot.summary == "Competitive summary"
        assert result.competitive_snapshot.source == "market_research_team"


def test_run_with_include_design_assets_adds_design_asset_result() -> None:
    orchestrator = BrandingTeamOrchestrator()
    result = orchestrator.run(
        mission=_mission(),
        human_review=HumanReview(approved=True),
        include_design_assets=True,
    )
    assert result.design_asset_result is not None
    assert result.design_asset_result.request_id.startswith("design_")
    assert result.design_asset_result.status == "pending"


def test_run_with_brand_id_and_store_appends_version() -> None:
    from branding_team.store import BrandingStore

    store = BrandingStore()
    client = store.create_client("Test Client")
    mission = BrandingMission(
        company_name="Northstar Labs",
        company_description="A strategic studio helping product teams ship cohesive digital experiences",
        target_audience="enterprise product leaders",
        values=["clarity", "trust"],
        differentiators=["speed"],
    )
    brand = store.create_brand(client.id, mission)
    assert brand is not None
    assert brand.version == 0
    orchestrator = BrandingTeamOrchestrator()
    orchestrator.run(
        mission=mission,
        human_review=HumanReview(approved=True),
        store=store,
        client_id=client.id,
        brand_id=brand.id,
    )
    updated = store.get_brand(client.id, brand.id)
    assert updated is not None
    assert updated.version == 1
    assert updated.latest_output is not None
    assert len(updated.history) == 1
