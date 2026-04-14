"""Tests for the branding team orchestrator.

Since all agents are now LLM-backed strands.Agent instances running inside
Strands SDK Graph/Swarm orchestration, we mock ``graph.invoke_async`` to
return a canned result and verify the orchestrator correctly assembles
``TeamOutput`` from it.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from branding_team import (
    BrandingMission,
    BrandingTeamOrchestrator,
    BrandPhase,
    HumanReview,
    WorkflowStatus,
)
from branding_team.models import (
    BrandCheckRequest,
    BrandHealthKPI,
    ChannelActivationOutput,
    ChannelGuideline,
    ColorEntry,
    CompetitiveSnapshot,
    CoreValue,
    CreativeRefinementDecision,
    DesignSystemDefinition,
    GovernanceOutput,
    MoodBoardConcept,
    NarrativeMessagingOutput,
    StrategicCoreOutput,
    TypographySpec,
    VisualIdentityOutput,
    WikiEntry,
    WritingGuidelines,
)
from branding_team.tests._fake_postgres import install_fake_postgres


@pytest.fixture(autouse=False)
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def _mission() -> BrandingMission:
    return BrandingMission(
        company_name="Northstar Labs",
        company_description="A strategic studio helping product teams ship cohesive digital experiences",
        target_audience="enterprise product leaders",
        values=["clarity", "trust", "momentum"],
        differentiators=["hands-on partnership", "execution speed"],
    )


def _full_strategic_core() -> StrategicCoreOutput:
    return StrategicCoreOutput(
        brand_purpose="Northstar Labs exists to help enterprise product leaders achieve transformative outcomes.",
        mission_statement="To empower enterprise product leaders by turning strategy into consistent experiences.",
        vision_statement="A world where every interaction with Northstar Labs feels cohesive and intentional.",
        positioning_statement=(
            "For enterprise product leaders who need cohesive digital experiences, Northstar Labs is the "
            "hands-on partner that delivers clarity because execution speed sets us apart."
        ),
        brand_promise="Every customer touchpoint will feel cohesive, useful, and unmistakably aligned.",
        core_values=[
            CoreValue(
                value="clarity", behavioral_definition="We demonstrate clarity in every decision."
            ),
            CoreValue(value="trust", behavioral_definition="We build trust through transparency."),
            CoreValue(
                value="momentum",
                behavioral_definition="We maintain momentum through disciplined execution.",
            ),
        ],
    )


def _full_narrative() -> NarrativeMessagingOutput:
    return NarrativeMessagingOutput(
        brand_story="Northstar Labs was founded on the belief that brand is strategy made visible.",
        hero_narrative="We turn brand chaos into brand clarity.",
        tagline="Strategy made visible.",
        tagline_rationale="Captures our core promise in three words.",
        brand_archetypes=[],
        messaging_framework=[],
        audience_message_maps=[],
        elevator_pitches=[],
        boilerplate_variants=["Short bio.", "Medium bio.", "Long bio."],
        persona_profiles=[],
        writing_guidelines=WritingGuidelines(
            voice_principles=["Use a clear, confident voice"],
            style_dos=["Use active voice"],
            style_donts=["Do not overpromise"],
            editorial_quality_bar=["Every artifact must map to one narrative pillar"],
        ),
    )


def _full_visual_identity() -> VisualIdentityOutput:
    return VisualIdentityOutput(
        color_palette=[
            ColorEntry(
                name="Midnight",
                hex_value="#1a1a2e",
                usage="Primary background",
                psychological_rationale="Conveys depth and authority",
            ),
        ],
        typography_system=[
            TypographySpec(role="display", font_family="Inter", weight_range="600-800"),
        ],
        voice_tone_spectrum=[],
        language_dos=["Use plain language"],
        language_donts=["Avoid jargon"],
        mood_board_candidates=[
            MoodBoardConcept(
                title="Modern Confidence",
                visual_direction="Clean grids",
                color_story=["midnight blue"],
                typography_direction="Geometric sans",
            ),
        ],
        creative_refinement=CreativeRefinementDecision(
            winning_candidate_title="Modern Confidence",
            rationale="Best aligns with brand values.",
        ),
        design_system=DesignSystemDefinition(
            design_principles=["Clarity over decoration", "Consistency at scale"],
            foundation_tokens=["Color tokens: primary/secondary"],
            component_standards=["Buttons: size variants"],
        ),
    )


def _full_channel_activation() -> ChannelActivationOutput:
    return ChannelActivationOutput(
        brand_experience_principles=["Consistency", "Intentionality"],
        signature_moments=["First visit", "Onboarding"],
        channel_guidelines=[
            ChannelGuideline(channel="website", strategy="Lead with value proposition"),
            ChannelGuideline(channel="social", strategy="Build community"),
            ChannelGuideline(channel="email", strategy="Personalise by segment"),
            ChannelGuideline(channel="events", strategy="Showcase expertise"),
        ],
        brand_architecture=[],
        naming_conventions=["Use title case"],
        terminology_glossary={"Brand": "The brand system"},
        brand_in_action=[],
    )


def _full_governance() -> GovernanceOutput:
    return GovernanceOutput(
        ownership_model="Brand Director owns the system.",
        decision_authority={"logo_changes": "Brand Director"},
        approval_workflows=[],
        agency_briefing_protocols=["Always include brand book"],
        asset_management_guidance=["Store in central DAM"],
        training_onboarding_plan=["Brand 101 for new hires"],
        brand_health_kpis=[
            BrandHealthKPI(
                metric="NPS",
                measurement_method="Survey",
                target=">50",
                review_frequency="quarterly",
            ),
        ],
        tracking_methodology="Quarterly brand health surveys.",
        review_trigger_points=["Major campaign launch"],
        evolution_framework="Annual brand refresh cycle.",
        version_control_cadence="Bi-annual version bumps.",
        brand_guidelines=[
            "Positioning: use the approved positioning statement.",
            "Promise: lead with the brand promise.",
            "Identity: follow logo spacing rules.",
            "Messaging: promise -> pillar -> proof -> CTA.",
            "Governance: route major campaigns through brand review.",
        ],
        wiki_backlog=[
            WikiEntry(
                title="Brand North Star",
                summary="Source of truth for positioning.",
                owners=["Brand Strategy"],
                update_cadence="quarterly",
            ),
        ],
    )


def _mock_graph_result(phases_to_include: list[str]):
    """Build a mock graph result that returns the given phase outputs."""
    outputs = {
        "phase1_strategic_core": _full_strategic_core(),
        "phase2_narrative": _full_narrative(),
        "phase3_visual": _full_visual_identity(),
        "phase4_channel": _full_channel_activation(),
        "phase5_governance": _full_governance(),
    }

    mock_result = MagicMock()
    mock_result.result = {}

    for phase_key in phases_to_include:
        output_model = outputs[phase_key]
        json_str = output_model.model_dump_json()

        agent_result = MagicMock()
        agent_result.message = {"content": [{"text": json_str}]}

        node_result = MagicMock()
        node_result.get_agent_results.return_value = [agent_result]

        mock_result.result[phase_key] = node_result

    return mock_result


def _patch_graph_invoke(phases_to_include: list[str]):
    """Return a context manager that patches graph.invoke_async."""
    mock_result = _mock_graph_result(phases_to_include)

    async def mock_invoke_async(task, **kwargs):
        return mock_result

    return patch(
        "branding_team.orchestrator.build_branding_graph",
        return_value=MagicMock(invoke_async=AsyncMock(side_effect=mock_invoke_async)),
    )


ALL_PHASES = [
    "phase1_strategic_core",
    "phase2_narrative",
    "phase3_visual",
    "phase4_channel",
    "phase5_governance",
]


def test_full_run_approved() -> None:
    with _patch_graph_invoke(ALL_PHASES):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(mission=_mission(), human_review=HumanReview(approved=True))

    assert result.status == WorkflowStatus.READY_FOR_ROLLOUT
    assert result.current_phase == BrandPhase.COMPLETE
    assert result.strategic_core is not None
    assert result.strategic_core.positioning_statement
    assert result.strategic_core.core_values
    assert result.narrative_messaging is not None
    assert result.narrative_messaging.tagline
    assert result.narrative_messaging.writing_guidelines.voice_principles
    assert result.visual_identity is not None
    assert result.visual_identity.color_palette
    assert result.visual_identity.mood_board_candidates
    assert result.visual_identity.creative_refinement.winning_candidate_title
    assert result.visual_identity.design_system.foundation_tokens
    assert result.channel_activation is not None
    assert result.channel_activation.channel_guidelines
    assert result.governance is not None
    assert result.governance.brand_health_kpis
    assert result.governance.brand_guidelines
    assert result.governance.wiki_backlog
    assert result.brand_book is not None
    assert result.brand_book.content
    assert "Brand Purpose" in result.brand_book.content


def test_requires_human_approval() -> None:
    with _patch_graph_invoke(ALL_PHASES):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=_mission(),
            human_review=HumanReview(approved=False, feedback="Need legal review."),
        )

    assert result.status == WorkflowStatus.NEEDS_HUMAN_DECISION
    assert result.human_feedback == "Need legal review."
    assert result.strategic_core is not None
    assert result.narrative_messaging is not None
    assert result.visual_identity is not None
    assert result.channel_activation is not None
    assert result.governance is not None
    assert result.phase_gates


def test_brand_checks() -> None:
    with _patch_graph_invoke(ALL_PHASES):
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
        result = orchestrator.run(
            mission=_mission(), human_review=HumanReview(approved=True), brand_checks=checks
        )

    assert len(result.brand_checks) == 2
    assert any(not item.is_on_brand for item in result.brand_checks)


def test_market_research_integration() -> None:
    with (
        _patch_graph_invoke(ALL_PHASES),
        patch("branding_team.adapters.market_research.request_market_research") as mock_mr,
    ):
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


def test_design_assets_integration() -> None:
    with _patch_graph_invoke(ALL_PHASES):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=_mission(),
            human_review=HumanReview(approved=True),
            include_design_assets=True,
        )
    assert result.design_asset_result is not None
    assert result.design_asset_result.request_id.startswith("design_")
    assert result.design_asset_result.status == "pending"


def test_run_with_store_appends_version(fake_pg) -> None:
    from branding_team.store import BrandingStore

    store = BrandingStore()
    client = store.create_client("Test Client")
    mission = _mission()
    brand = store.create_brand(client.id, mission)
    assert brand is not None
    assert brand.version == 0

    with _patch_graph_invoke(ALL_PHASES):
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


def test_run_phase_stops_at_strategic_core() -> None:
    with _patch_graph_invoke(["phase1_strategic_core"]):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run_phase(
            mission=_mission(),
            phase=BrandPhase.STRATEGIC_CORE,
            human_review=HumanReview(approved=True),
        )
    assert result.strategic_core is not None
    assert result.narrative_messaging is None
    assert result.visual_identity is None
    assert result.channel_activation is None
    assert result.governance is None
    assert result.current_phase == BrandPhase.STRATEGIC_CORE
    assert result.status == WorkflowStatus.NEEDS_HUMAN_DECISION


def test_approved_partial_run_is_not_rollout_ready() -> None:
    """Approved intermediate phases must not be marked READY_FOR_ROLLOUT."""
    phase_sets = {
        BrandPhase.STRATEGIC_CORE: ["phase1_strategic_core"],
        BrandPhase.NARRATIVE_MESSAGING: ["phase1_strategic_core", "phase2_narrative"],
        BrandPhase.VISUAL_IDENTITY: ["phase1_strategic_core", "phase2_narrative", "phase3_visual"],
        BrandPhase.CHANNEL_ACTIVATION: [
            "phase1_strategic_core",
            "phase2_narrative",
            "phase3_visual",
            "phase4_channel",
        ],
    }
    for phase, phases in phase_sets.items():
        with _patch_graph_invoke(phases):
            orchestrator = BrandingTeamOrchestrator()
            result = orchestrator.run_phase(
                mission=_mission(),
                phase=phase,
                human_review=HumanReview(approved=True),
            )
        assert result.status == WorkflowStatus.NEEDS_HUMAN_DECISION, (
            f"Phase {phase.value} with approved=True should not be READY_FOR_ROLLOUT"
        )
        assert result.current_phase != BrandPhase.COMPLETE


def test_phase_gates_are_populated() -> None:
    with _patch_graph_invoke(ALL_PHASES):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(mission=_mission(), human_review=HumanReview(approved=True))
    assert len(result.phase_gates) == 5
    for gate in result.phase_gates:
        assert gate.status.value in ("approved", "not_started", "pending_review", "in_progress")


def test_phase_absorbed_fields_populated() -> None:
    """Verify sub-team outputs are accessible via their phase-output homes."""
    with _patch_graph_invoke(ALL_PHASES):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(mission=_mission(), human_review=HumanReview(approved=True))

    # Writing guidelines absorbed into narrative_messaging
    assert result.narrative_messaging.writing_guidelines.voice_principles

    # Mood boards absorbed into visual_identity
    assert result.visual_identity.mood_board_candidates
    assert result.visual_identity.mood_board_candidates[0].title

    # Creative refinement absorbed into visual_identity
    assert result.visual_identity.creative_refinement.winning_candidate_title

    # Design system absorbed into visual_identity
    assert result.visual_identity.design_system.design_principles
    assert result.visual_identity.design_system.foundation_tokens

    # Brand guidelines absorbed into governance
    assert len(result.governance.brand_guidelines) >= 4

    # Wiki backlog absorbed into governance
    assert result.governance.wiki_backlog
    assert result.governance.wiki_backlog[0].title
