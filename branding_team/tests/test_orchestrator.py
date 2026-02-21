from branding_team import BrandingMission, BrandingTeamOrchestrator, HumanReview, WorkflowStatus
from branding_team.models import BrandCheckRequest


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
