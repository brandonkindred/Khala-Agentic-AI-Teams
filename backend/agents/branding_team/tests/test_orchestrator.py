"""Tests for the branding team orchestrator.

Since all agents are now LLM-backed strands.Agent instances running inside
Strands SDK Graph/Swarm orchestration, we patch
``branding_team.orchestrator.build_branding_graph`` so the returned graph's
``invoke_async`` yields a canned result, and verify the orchestrator
correctly assembles ``TeamOutput`` from it.
"""

import asyncio
import functools
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from branding_team import (
    BrandingTeamOrchestrator,
    BrandPhase,
    HumanReview,
    WorkflowStatus,
)
from branding_team.graphs.shared import PHASE_ORDER
from branding_team.models import (
    ApprovalWorkflowOutput,
    ApprovalWorkflowsOutput,
    AssetWikiOutput,
    AudienceMessageMapOutput,
    AudienceSegmentOutput,
    AudienceSegmentsOutput,
    Brand,
    BrandArchetypeOutput,
    BrandArchetypesOutput,
    BrandArchitectureOutput,
    BrandArchitectureRuleOutput,
    BrandCheckRequest,
    BrandDiscoveryAudit,
    BrandExperiencePrinciplesOutput,
    BrandGuidelinesOutput,
    BrandHealthKPI,
    BrandHealthKPIOutput,
    BrandHealthKPIsOutput,
    BrandInActionExampleOutput,
    BrandInActionOutput,
    BrandStatus,
    BrandStoryOutput,
    ChannelActivationOutput,
    ChannelGuideline,
    ChannelGuidelineOutput,
    ColorEntry,
    ColorEntryOutput,
    ColorPaletteSystemOutput,
    CompetitiveSnapshot,
    CoreValue,
    CoreValueOutput,
    CoreValuesOutput,
    CreativeRefinementDecision,
    DesignSystemDefinition,
    DifferentiationPillarOutput,
    DifferentiationPillarsOutput,
    ElevatorPitchOutput,
    EvolutionFrameworkOutput,
    GovernanceOutput,
    IconographyOutput,
    LogoSuiteOutput,
    LogoUsageRuleOutput,
    MessagingFrameworkOutput,
    MessagingPillarOutput,
    MoodBoardConcept,
    NarrativeMessagingOutput,
    OwnershipOutput,
    PersonaProfileOutput,
    PersonaProfilesOutput,
    PhotographyVideoOutput,
    PositioningOutput,
    PurposeVisionOutput,
    StrategicCoreOutput,
    TaglineOutput,
    TrainingOnboardingOutput,
    TypographySpec,
    TypographySpecOutput,
    TypographySystemOutput,
    VisualIdentityOutput,
    VoiceToneEntryOutput,
    VoiceToneOutput,
    WikiEntry,
    WikiEntryOutput,
    WritingGuidelines,
    WritingGuidelinesBody,
    WritingGuidelinesOutput,
)
from branding_team.orchestrator import (
    _PHASE1_NODE_MERGE,
    _PHASE2_NODE_MERGE,
    _PHASE3_NODE_MERGE,
    _PHASE4_NODE_MERGE,
    _PHASE5_NODE_MERGE,
    _PHASE_SPEC,
    _merge_named_fragments,
)
from branding_team.shared.memoization import phase_input_hash
from branding_team.shared.phase_output_cache import PhaseOutputCache
from branding_team.store import BrandVersionAppendConflict
from branding_team.tests.conftest import make_mission


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
    """Return a context manager that patches ``branding_team.orchestrator.build_branding_graph`` so the returned graph's ``invoke_async`` yields a canned result."""
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
        result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
        )

    assert result.status == WorkflowStatus.READY_FOR_ROLLOUT
    assert result.current_phase == BrandPhase.COMPLETE
    assert result.degraded_phases == []
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
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=False, feedback="Need legal review."),
        )

    assert result.status == WorkflowStatus.NEEDS_HUMAN_DECISION
    assert result.human_feedback == "Need legal review."
    assert "Governance" in result.mission_summary
    assert result.degraded_phases == []
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
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
            brand_checks=checks,
        )

    assert len(result.brand_checks) == 2
    assert any(not item.is_on_brand for item in result.brand_checks)
    assert result.degraded_phases == []


def test_market_research_integration() -> None:
    # The orchestrator runs integrations concurrently via the async adapter
    # variant, so patch that (not the sync wrapper).
    with (
        _patch_graph_invoke(ALL_PHASES),
        patch(
            "branding_team.adapters.market_research.request_market_research_async",
            new_callable=AsyncMock,
        ) as mock_mr,
    ):
        mock_mr.return_value = CompetitiveSnapshot(
            summary="Competitive summary",
            similar_brands=["A", "B"],
            insights=["insight1"],
            source="market_research_team",
        )
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
            include_market_research=True,
        )
    assert result.competitive_snapshot is not None
    assert result.competitive_snapshot.summary == "Competitive summary"
    assert result.degraded_phases == []


def test_design_assets_integration() -> None:
    with _patch_graph_invoke(ALL_PHASES):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
            include_design_assets=True,
        )
    assert result.design_asset_result is not None
    assert result.design_asset_result.request_id.startswith("design_")
    assert result.design_asset_result.status == "pending"
    assert result.degraded_phases == []


def test_run_with_store_appends_version() -> None:
    mission = make_mission(
        company_description="A strategic studio helping product teams ship cohesive digital experiences",
        values=["clarity", "trust", "momentum"],
    )
    brand = Brand(
        id="brand_1",
        client_id="client_1",
        name="Northstar Labs",
        mission=mission,
        status=BrandStatus.draft,
        version=0,
    )
    updated = brand.model_copy(update={"version": 1})

    store = MagicMock()
    store.get_brand.return_value = brand
    store.append_brand_version.return_value = updated

    with _patch_graph_invoke(ALL_PHASES):
        orchestrator = BrandingTeamOrchestrator()
        orchestrator.run(
            mission=mission,
            human_review=HumanReview(approved=True),
            store=store,
            client_id=brand.client_id,
            brand_id=brand.id,
        )

    store.append_brand_version.assert_called_once()
    args, kwargs = store.append_brand_version.call_args
    assert kwargs == {}
    assert args[0] == brand.client_id
    assert args[1] == brand.id
    assert args[2].status == WorkflowStatus.READY_FOR_ROLLOUT
    assert args[2].current_phase == BrandPhase.COMPLETE
    assert args[2].brand_book is not None


def test_run_with_store_append_brand_version_none_raises() -> None:
    """A brand-version append that returns None must never be ignored.

    When ``append_brand_version`` returns ``None`` (brand deleted between
    resolve and append), the orchestrator must raise so the run can be
    marked failed instead of reporting success without persistence.
    """
    mission = make_mission(
        company_description="A strategic studio helping product teams ship cohesive digital experiences",
        values=["clarity", "trust", "momentum"],
    )
    brand = Brand(
        id="brand_deleted",
        client_id="client_1",
        name="Deleted Brand",
        mission=mission,
        status=BrandStatus.draft,
    )

    store = MagicMock()
    store.get_brand.return_value = brand
    store.append_brand_version.return_value = None

    with _patch_graph_invoke(ALL_PHASES):
        orchestrator = BrandingTeamOrchestrator()
        with pytest.raises(
            BrandVersionAppendConflict,
            match="Brand row disappeared while appending brand version",
        ):
            orchestrator.run(
                mission=mission,
                human_review=HumanReview(approved=True),
                store=store,
                client_id=brand.client_id,
                brand_id=brand.id,
            )


def test_run_branding_team_route_maps_append_conflict_to_409() -> None:
    """Sync ``POST /run`` must map ``BrandVersionAppendConflict`` to HTTP 409.

    Matches the background path's failed-job handling rather than leaking an
    unhandled 500 when the brand row disappears mid-run. Unrelated
    ``RuntimeError`` values must not be remapped to 409.
    """
    from fastapi import HTTPException

    from branding_team.api.models import RunBrandingTeamRequest
    from branding_team.api.routes import sessions as sessions_mod
    from branding_team.store import BrandVersionAppendConflict

    payload = RunBrandingTeamRequest(
        company_name="Northstar Labs",
        company_description="A strategic studio helping product teams ship cohesive digital experiences",
        target_audience="enterprise product leaders",
        human_approved=True,
        client_id="c1",
        brand_id="b1",
    )
    with patch(
        "branding_team.api.main.orchestrator.run",
        side_effect=BrandVersionAppendConflict(
            "Brand row disappeared while appending brand version (client_id=c1, brand_id=b1)"
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            sessions_mod.run_branding_team(payload)

    assert exc_info.value.status_code == 409
    assert "Brand row disappeared" in str(exc_info.value.detail)


def test_run_branding_team_route_does_not_remap_unrelated_runtime_error() -> None:
    """LLM/provider ``RuntimeError`` must not become HTTP 409."""
    from branding_team.api.models import RunBrandingTeamRequest
    from branding_team.api.routes import sessions as sessions_mod

    payload = RunBrandingTeamRequest(
        company_name="Northstar Labs",
        company_description="A strategic studio helping product teams ship cohesive digital experiences",
        target_audience="enterprise product leaders",
        human_approved=True,
        client_id="c1",
        brand_id="b1",
    )
    with patch(
        "branding_team.api.main.orchestrator.run",
        side_effect=RuntimeError("LLM provider unavailable"),
    ):
        with pytest.raises(RuntimeError, match="LLM provider unavailable"):
            sessions_mod.run_branding_team(payload)


def test_run_phase_stops_at_strategic_core() -> None:
    with _patch_graph_invoke(["phase1_strategic_core"]):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run_phase(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
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


def test_run_phase_forwards_phase_cache_to_run() -> None:
    """``run_phase`` must forward ``phase_cache`` to ``run`` so a cache hit
    is honored instead of always invoking the phase (#6671)."""
    mission = make_mission()
    cached_output = _full_strategic_core()
    cache = PhaseOutputCache()
    cache.put(
        BrandPhase.STRATEGIC_CORE,
        phase_input_hash(BrandPhase.STRATEGIC_CORE, mission, {}),
        cached_output,
    )
    orchestrator = BrandingTeamOrchestrator()

    with patch.object(orchestrator, "run_single_phase") as mock_run_single_phase:
        result = orchestrator.run_phase(
            mission=mission,
            phase=BrandPhase.STRATEGIC_CORE,
            human_review=HumanReview(approved=False),
            phase_cache=cache,
        )

    mock_run_single_phase.assert_not_called()
    assert result.strategic_core == cached_output


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
                mission=make_mission(
                    company_description="A strategic studio helping product teams ship cohesive digital experiences",
                    values=["clarity", "trust", "momentum"],
                ),
                phase=phase,
                human_review=HumanReview(approved=True),
            )
        assert result.status == WorkflowStatus.NEEDS_HUMAN_DECISION, (
            f"Phase {phase.value} with approved=True should not be READY_FOR_ROLLOUT"
        )
        assert result.current_phase != BrandPhase.COMPLETE


def test_unapproved_partial_run_labels_current_phase_not_target() -> None:
    """The unapproved-run summary must name the phase actually reached
    (current_phase), never a separately-derived target-phase index — the two
    are coupled today only by an invariant in the extraction gating, and
    that coupling must not silently break in the future (see #3438)."""
    phase_sets = {
        BrandPhase.STRATEGIC_CORE: (["phase1_strategic_core"], "Strategic Core"),
        BrandPhase.NARRATIVE_MESSAGING: (
            ["phase1_strategic_core", "phase2_narrative"],
            "Narrative Messaging",
        ),
        BrandPhase.VISUAL_IDENTITY: (
            ["phase1_strategic_core", "phase2_narrative", "phase3_visual"],
            "Visual Identity",
        ),
        BrandPhase.CHANNEL_ACTIVATION: (
            ["phase1_strategic_core", "phase2_narrative", "phase3_visual", "phase4_channel"],
            "Channel Activation",
        ),
    }
    for phase, (phases, expected_label) in phase_sets.items():
        with _patch_graph_invoke(phases):
            orchestrator = BrandingTeamOrchestrator()
            result = orchestrator.run_phase(
                mission=make_mission(
                    company_description="A strategic studio helping product teams ship cohesive digital experiences",
                    values=["clarity", "trust", "momentum"],
                ),
                phase=phase,
                human_review=HumanReview(approved=False),
            )
        assert expected_label in result.mission_summary, (
            f"Phase {phase.value}: expected {expected_label!r} in {result.mission_summary!r}"
        )


def test_default_human_feedback_message_survives_when_feedback_omitted() -> None:
    """Locks in the current (deliberately unchanged) fallback behavior for
    #3435: HumanReview.feedback is typed str = "", so omitted feedback must
    still produce the status-appropriate default message."""
    with _patch_graph_invoke(ALL_PHASES):
        orchestrator = BrandingTeamOrchestrator()
        approved_result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
        )
    assert approved_result.human_feedback == "Approved for rollout."

    with _patch_graph_invoke(ALL_PHASES):
        orchestrator = BrandingTeamOrchestrator()
        unapproved_result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=False),
        )
    assert unapproved_result.human_feedback == "Awaiting approval from brand leadership."


def test_build_status_summary_uses_current_phase_for_unapproved_label() -> None:
    """Direct unit test of the (now 2-arg) static method — also mechanically
    locks the signature change: calling with the old 3-required-arg form
    would raise TypeError."""
    status, summary = BrandingTeamOrchestrator._build_status_summary(
        HumanReview(approved=False), BrandPhase.VISUAL_IDENTITY
    )
    assert status == WorkflowStatus.NEEDS_HUMAN_DECISION
    assert "Visual Identity" in summary


def test_determine_current_phase_treats_degraded_default_as_reached() -> None:
    """A default-constructed (degraded) phase output is still non-None, so it
    still counts as "reached" for phase-advancement purposes — the invariant
    #3157's docstring now documents explicitly."""
    phase = BrandingTeamOrchestrator._determine_current_phase(
        narrative=NarrativeMessagingOutput(),
        visual_identity=None,
        channel_activation=None,
        governance=None,
        approved=False,
    )
    assert phase == BrandPhase.NARRATIVE_MESSAGING


def test_phase_gates_are_populated() -> None:
    with _patch_graph_invoke(ALL_PHASES):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
        )
    assert len(result.phase_gates) == 5
    for gate in result.phase_gates:
        assert gate.status.value in ("approved", "not_started", "pending_review", "in_progress")


def test_phase_absorbed_fields_populated() -> None:
    """Verify sub-team outputs are accessible via their phase-output homes."""
    with _patch_graph_invoke(ALL_PHASES):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
        )

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


def test_unparseable_phase_output_marks_phase_degraded(caplog) -> None:
    """A phase whose agent text isn't valid JSON gets a default output and is
    recorded in ``TeamOutput.degraded_phases`` instead of failing silently."""
    mock_result = _mock_graph_result(ALL_PHASES)
    agent_result = MagicMock()
    agent_result.message = {"content": [{"text": "not valid json at all"}]}
    mock_result.result["phase2_narrative"].get_agent_results.return_value = [agent_result]

    async def mock_invoke_async(task, **kwargs):
        return mock_result

    with (
        patch(
            "branding_team.orchestrator.build_branding_graph",
            return_value=MagicMock(invoke_async=AsyncMock(side_effect=mock_invoke_async)),
        ),
        caplog.at_level("WARNING", logger="branding_team.orchestrator"),
    ):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
        )

    assert result.degraded_phases == [BrandPhase.NARRATIVE_MESSAGING]
    assert any("phase2_narrative" in r.message and r.levelname == "WARNING" for r in caplog.records)
    assert result.narrative_messaging is not None
    assert result.narrative_messaging.tagline == ""
    # Unaffected phases still parse normally and are not marked degraded.
    assert result.strategic_core is not None
    assert result.strategic_core.positioning_statement


def test_trailing_unrelated_json_object_does_not_win_over_real_payload() -> None:
    """A reply carrying the real phase payload followed by an unrelated JSON
    object (e.g. a usage/metadata echo) must still recover the real payload —
    not silently accept the trailing object just because it parses too, which
    would validate against defaults and wrongly report degraded=False."""
    mock_result = _mock_graph_result(ALL_PHASES)
    real_payload = _full_narrative()
    agent_result = MagicMock()
    agent_result.message = {
        "content": [
            {
                "text": real_payload.model_dump_json()
                + "\n"
                + json.dumps({"usage": {"tokens": 42}, "model": "some-model"})
            }
        ]
    }
    mock_result.result["phase2_narrative"].get_agent_results.return_value = [agent_result]

    async def mock_invoke_async(task, **kwargs):
        return mock_result

    with patch(
        "branding_team.orchestrator.build_branding_graph",
        return_value=MagicMock(invoke_async=AsyncMock(side_effect=mock_invoke_async)),
    ):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
        )

    assert result.degraded_phases == []
    assert result.narrative_messaging is not None
    assert result.narrative_messaging.tagline == real_payload.tagline


def test_phase_spec_every_entry_declares_merge_fn() -> None:
    """All five phases now merge their fan-out fragments in Python (Phases 1-2
    always did; Phases 3-5 dropped their LLM compositors in stories 1a/1b/1c).
    No entry may silently fall back to the single-agent extraction default —
    guard the invariant so a future phase addition can't regress it."""
    assert set(_PHASE_SPEC) == set(PHASE_ORDER)
    for phase, spec in _PHASE_SPEC.items():
        assert spec.merge_fn is not None, f"{phase} has no merge_fn"


def _leaf_node_result(structured_output) -> MagicMock:
    """A mock NodeResult for a single fan-out/fan-in leaf agent.

    Shared generic helper reused across every phase's tests below.
    """
    agent_result = MagicMock()
    agent_result.message = {"content": []}
    agent_result.structured_output = structured_output
    node = MagicMock()
    node.get_agent_results.return_value = [agent_result]
    return node


def test_extract_phase_output_merges_every_phase1_fragment() -> None:
    """Phase 1 wraps six agents as one top-level node; get_agent_results()[-1] only
    ever sees the synthesizer, so the five specialists' fragments must be merged
    in separately or their data is silently discarded (see PR review discussion)."""
    node_result = _phase1_nested_node_result()

    mock_result = MagicMock()
    mock_result.result = {"phase1_strategic_core": node_result}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase1_strategic_core", StrategicCoreOutput
    )

    assert degraded is False
    assert isinstance(output, StrategicCoreOutput)
    assert output.brand_discovery.current_brand_perception == "Seen as reliable but generic."
    assert output.brand_discovery.stakeholder_insights == ["Sales wants sharper differentiation"]
    assert output.brand_purpose == "Why we exist."
    assert output.mission_statement == "What we do for customers."
    assert output.vision_statement == "Where the brand is headed."
    assert [v.value for v in output.core_values] == ["Clarity", "Trust", "Momentum"]
    assert [s.name for s in output.target_audience_segments] == ["Enterprise product leaders"]
    assert [p.pillar for p in output.differentiation_pillars] == [
        "Execution speed",
        "Hands-on partnership",
    ]
    assert output.positioning_statement == "For enterprise leaders who need clarity, we deliver it."
    assert output.brand_promise == "Every touchpoint feels cohesive."


def _phase1_nested_node_result() -> MagicMock:
    """A mock NodeResult wrapping all six Phase-1 specialists' fragments,
    shaped like the real nested MultiAgentResult.results Strands returns."""
    nested_results = {
        "discovery_auditor": _leaf_node_result(
            BrandDiscoveryAudit(
                current_brand_perception="Seen as reliable but generic.",
                market_position="Mid-market challenger.",
                strengths=["Delivery speed"],
                weaknesses=["Low brand recall"],
                opportunities=["Category consolidating"],
                threats=["Bigger competitors out-spending"],
                stakeholder_insights=["Sales wants sharper differentiation"],
            )
        ),
        "purpose_vision_writer": _leaf_node_result(
            PurposeVisionOutput(
                brand_purpose="Why we exist.",
                mission_statement="What we do for customers.",
                vision_statement="Where the brand is headed.",
            )
        ),
        "values_articulator": _leaf_node_result(
            CoreValuesOutput(
                core_values=[
                    CoreValueOutput(
                        value="Clarity",
                        behavioral_definition="We demonstrate clarity in every decision.",
                        observable_behaviors=["Plain-language docs"],
                    ),
                    CoreValueOutput(
                        value="Trust",
                        behavioral_definition="We build trust through transparency.",
                        observable_behaviors=["Public roadmap"],
                    ),
                    CoreValueOutput(
                        value="Momentum",
                        behavioral_definition="We maintain momentum through disciplined execution.",
                        observable_behaviors=["Weekly release cadence"],
                    ),
                ]
            )
        ),
        "audience_segmenter": _leaf_node_result(
            AudienceSegmentsOutput(
                target_audience_segments=[
                    AudienceSegmentOutput(
                        name="Enterprise product leaders",
                        description="VP/Director-level buyers at mid-market SaaS companies.",
                        pain_points=["Inconsistent brand touchpoints"],
                        goals=["Ship cohesive experiences"],
                        decision_drivers=["Proven execution speed"],
                    )
                ]
            )
        ),
        "differentiation_mapper": _leaf_node_result(
            DifferentiationPillarsOutput(
                differentiation_pillars=[
                    DifferentiationPillarOutput(
                        pillar="Execution speed",
                        proof_points=["Ship weekly release cadence"],
                        competitive_context="Competitors ship quarterly.",
                    ),
                    DifferentiationPillarOutput(
                        pillar="Hands-on partnership",
                        proof_points=["Dedicated strategist per account"],
                        competitive_context="Competitors use ticket-based support.",
                    ),
                ]
            )
        ),
        "positioning_synthesizer": _leaf_node_result(
            PositioningOutput(
                positioning_statement="For enterprise leaders who need clarity, we deliver it.",
                brand_promise="Every touchpoint feels cohesive.",
            )
        ),
    }

    inner_multi_result = MagicMock()
    inner_multi_result.results = nested_results

    node_result = MagicMock()
    node_result.result = inner_multi_result
    node_result.get_agent_results.return_value = [
        node.get_agent_results.return_value[0] for node in nested_results.values()
    ]
    return node_result


def _phase2_nested_node_result() -> MagicMock:
    """A mock NodeResult wrapping all six Phase-2 specialists' own-field
    fragments, shaped like the real nested MultiAgentResult.results Strands
    returns."""
    _story = dict(
        brand_story="Origin story about shipping on-brand experiences.",
        hero_narrative="Brand that ships with the product.",
        boilerplate_variants=["short bio", "medium bio", "long bio"],
    )
    _archetypes = [
        BrandArchetypeOutput(
            archetype="The Creator",
            rationale="Inventive.",
            personality_traits=["Imaginative", "Original"],
        )
    ]
    _pitches = [
        ElevatorPitchOutput(tier="5-second", pitch="On-brand, shipped weekly."),
        ElevatorPitchOutput(tier="30-second", pitch="Keep every touchpoint intentional."),
        ElevatorPitchOutput(tier="2-minute", pitch="Turn strategy into a workable system."),
    ]
    _pillars = [
        MessagingPillarOutput(
            pillar="Cohesion", key_message="One voice everywhere.", proof_points=["Style guide"]
        ),
        MessagingPillarOutput(
            pillar="Speed", key_message="Ship weekly.", proof_points=["Release cadence"]
        ),
        MessagingPillarOutput(
            pillar="Clarity", key_message="Say it simply.", proof_points=["Plain-language copy"]
        ),
    ]
    _maps = [
        AudienceMessageMapOutput(
            audience_segment="Enterprise product leaders",
            primary_message="Ship on-brand, faster.",
            supporting_messages=["Consistent across every touchpoint"],
            tone_adjustments="Confident, outcome-focused",
        )
    ]
    _personas = [
        PersonaProfileOutput(
            name="Alex Rivera",
            role="VP of Product",
            demographics="35-44, urban, enterprise SaaS",
            psychographics="Outcome-driven, skeptical of hype",
            goals=["Ship cohesive experiences"],
            frustrations=["Inconsistent brand touchpoints"],
            media_habits=["Industry newsletters"],
            jobs_to_be_done=["Align teams on brand voice"],
        ),
        PersonaProfileOutput(
            name="Jordan Lee",
            role="Head of Marketing",
            demographics="28-34, remote, mid-market",
            psychographics="Data-driven, values clarity",
            goals=["Grow brand recall"],
            frustrations=["Fragmented messaging"],
            media_habits=["Design newsletters"],
            jobs_to_be_done=["Brief agencies quickly"],
        ),
    ]
    voice_leaf = _leaf_node_result(
        WritingGuidelinesOutput(
            writing_guidelines=WritingGuidelinesBody(
                voice_principles=["Confident", "Human", "Concrete"],
                style_dos=["Lead with outcome", "Use active voice", "Name the audience"],
                style_donts=["Empty superlatives", "Bury the offer", "Mix slang with claims"],
                editorial_quality_bar=["States who it's for", "Cites proof", "Matches tone"],
            ),
        )
    )
    nested_results = {
        "Storyteller": _leaf_node_result(BrandStoryOutput(**_story)),
        "ArchetypeAnalyst": _leaf_node_result(BrandArchetypesOutput(brand_archetypes=_archetypes)),
        "TaglineWriter": _leaf_node_result(
            TaglineOutput(
                tagline="Ship brand with the product",
                tagline_rationale="Ties cohesion to shipping speed.",
                elevator_pitches=_pitches,
            )
        ),
        "MessageMapper": _leaf_node_result(
            MessagingFrameworkOutput(
                messaging_framework=_pillars,
                audience_message_maps=_maps,
            )
        ),
        "PersonaBuilder": _leaf_node_result(PersonaProfilesOutput(persona_profiles=_personas)),
        "VoicePrinciplesDrafter": voice_leaf,
    }

    inner_multi_result = MagicMock()
    inner_multi_result.results = nested_results

    node_result = MagicMock()
    node_result.result = inner_multi_result
    node_result.get_agent_results.return_value = [
        node.get_agent_results.return_value[0] for node in nested_results.values()
    ]
    return node_result


def test_extract_phase_output_merges_every_phase2_fragment() -> None:
    """Phase 2 wraps six parallel fan-out Graph agents as one top-level node;
    get_agent_results()[-1] only ever sees VoicePrinciplesDrafter, so the five
    upstream agents' fragments must be merged in separately."""
    mock_result = MagicMock()
    mock_result.result = {"phase2_narrative": _phase2_nested_node_result()}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase2_narrative", NarrativeMessagingOutput
    )

    assert degraded is False
    assert isinstance(output, NarrativeMessagingOutput)
    assert output.brand_story == "Origin story about shipping on-brand experiences."
    assert output.hero_narrative == "Brand that ships with the product."
    assert output.boilerplate_variants == ["short bio", "medium bio", "long bio"]
    assert [a.archetype for a in output.brand_archetypes] == ["The Creator"]
    assert output.tagline == "Ship brand with the product"
    assert output.tagline_rationale == "Ties cohesion to shipping speed."
    assert [p.tier for p in output.elevator_pitches] == ["5-second", "30-second", "2-minute"]
    assert [p.pillar for p in output.messaging_framework] == ["Cohesion", "Speed", "Clarity"]
    assert [m.audience_segment for m in output.audience_message_maps] == [
        "Enterprise product leaders"
    ]
    assert [p.name for p in output.persona_profiles] == ["Alex Rivera", "Jordan Lee"]
    assert output.writing_guidelines.voice_principles == ["Confident", "Human", "Concrete"]
    assert output.writing_guidelines.editorial_quality_bar == [
        "States who it's for",
        "Cites proof",
        "Matches tone",
    ]


def test_phase2_fragments_collectively_populate_every_output_field() -> None:
    """Schema-coverage guard: the six Phase-2 specialists' fragments must
    collectively populate every field on NarrativeMessagingOutput, checked
    generically against the model's own field list (not a hardcoded field
    enumeration) -- mirrors test_phase3/4/5_fragments_collectively_populate_every_output_field."""
    mock_result = MagicMock()
    mock_result.result = {"phase2_narrative": _phase2_nested_node_result()}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase2_narrative", NarrativeMessagingOutput
    )

    assert degraded is False
    assert isinstance(output, NarrativeMessagingOutput)
    _assert_every_field_populated(output)


# NOTE (Story 5b Step 1): test_extract_phase_output_phase2_prefers_upstream_owned_fields
# was removed here. Its premise -- a downstream specialist's cumulative payload
# re-emitting (and potentially rewriting) an earlier specialist's field, with
# prefer_first ensuring the earlier value wins -- is no longer representable now
# that each Phase 2 specialist's structured_output model contains only its own
# fields (e.g. BrandArchetypesOutput can no longer carry a brand_story key at
# all). Story 5b Step 3 subsequently removed the prefer_first mechanic itself
# from _apply_fragment / _merge_named_fragments, since Phase 2 was its only
# caller and the six fragments' fields can never collide.


def test_extract_phase_output_rejects_incomplete_phase2_fragments() -> None:
    """A Phase 2 run that only produced Storyteller must not validate as a
    complete NarrativeMessagingOutput via field defaults (the structured_output
    + Swarm failure mode where handoff never fired)."""
    nested_results = {
        "Storyteller": _leaf_node_result(
            BrandStoryOutput(
                brand_story="Origin story.",
                hero_narrative="Hero.",
                boilerplate_variants=["short", "medium", "long"],
            )
        )
    }
    inner_multi_result = MagicMock()
    inner_multi_result.results = nested_results

    node_result = MagicMock()
    node_result.result = inner_multi_result
    node_result.get_agent_results.return_value = [
        nested_results["Storyteller"].get_agent_results.return_value[0]
    ]

    mock_result = MagicMock()
    mock_result.result = {"phase2_narrative": node_result}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase2_narrative", NarrativeMessagingOutput
    )

    assert degraded is True
    assert output == NarrativeMessagingOutput()
    assert output.brand_story == ""
    assert output.tagline == ""


def test_full_run_phase2_not_degraded_with_six_fragments() -> None:
    """Phase 2's real runtime shape is six separate specialist fragments, not
    the single flat block _mock_graph_result's default gives every phase
    (which never actually exercises _merge_phase2_fragments, since it bails
    out unless node_result.result.results is a dict). Wire that real shape
    through orchestrator.run() end-to-end and confirm the Python merge keeps
    Phase 2 out of degraded_phases, fully populates its output, and -- with
    the prefer_first rewrite guard gone -- that every specialist's
    authoritative field value survives unrewritten all the way through
    _assemble_team_output, not just the direct _extract_phase_output call
    test_extract_phase_output_merges_every_phase2_fragment already covers."""
    mock_result = _mock_graph_result(ALL_PHASES)
    mock_result.result["phase2_narrative"] = _phase2_nested_node_result()

    async def mock_invoke_async(task, **kwargs):
        return mock_result

    with patch(
        "branding_team.orchestrator.build_branding_graph",
        return_value=MagicMock(invoke_async=AsyncMock(side_effect=mock_invoke_async)),
    ):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
        )

    assert result.degraded_phases == []
    assert isinstance(result.narrative_messaging, NarrativeMessagingOutput)
    _assert_every_field_populated(result.narrative_messaging)

    narrative = result.narrative_messaging
    assert narrative.brand_story == "Origin story about shipping on-brand experiences."
    assert narrative.hero_narrative == "Brand that ships with the product."
    assert narrative.boilerplate_variants == ["short bio", "medium bio", "long bio"]
    assert [a.archetype for a in narrative.brand_archetypes] == ["The Creator"]
    assert narrative.tagline == "Ship brand with the product"
    assert narrative.tagline_rationale == "Ties cohesion to shipping speed."
    assert [p.tier for p in narrative.elevator_pitches] == ["5-second", "30-second", "2-minute"]
    assert [p.pillar for p in narrative.messaging_framework] == ["Cohesion", "Speed", "Clarity"]
    assert [m.audience_segment for m in narrative.audience_message_maps] == [
        "Enterprise product leaders"
    ]
    assert [p.name for p in narrative.persona_profiles] == ["Alex Rivera", "Jordan Lee"]
    assert narrative.writing_guidelines.voice_principles == ["Confident", "Human", "Concrete"]
    assert narrative.writing_guidelines.editorial_quality_bar == [
        "States who it's for",
        "Cites proof",
        "Matches tone",
    ]



def test_phase1_and_phase2_merge_parity_through_shared_helper() -> None:
    """Phase 1 and Phase 2 both merge their fragments through the same shared
    ``_merge_named_fragments`` helper (bound as functools.partial in
    _PHASE_SPEC). This test confirms the shared path produces a valid,
    non-degraded output for both phases -- asserting parity of the merge
    mechanism, not just of the individual phase fixtures.

    By calling ``_merge_named_fragments`` directly with each phase's node_merge
    table against its own nested node result, we prove both phases exercise
    the same code path and that neither silently falls through to None.
    """
    import functools

    # Confirm both merge_fns are functools.partial wrapping _merge_named_fragments.
    phase1_spec = _PHASE_SPEC[BrandPhase.STRATEGIC_CORE]
    phase2_spec = _PHASE_SPEC[BrandPhase.NARRATIVE_MESSAGING]
    assert isinstance(phase1_spec.merge_fn, functools.partial)
    assert isinstance(phase2_spec.merge_fn, functools.partial)
    assert phase1_spec.merge_fn.func is _merge_named_fragments
    assert phase2_spec.merge_fn.func is _merge_named_fragments

    # Phase 1: call the shared helper directly with Phase 1's node_merge table.
    phase1_node = _phase1_nested_node_result()
    phase1_output = _merge_named_fragments(
        phase1_node, StrategicCoreOutput, _PHASE1_NODE_MERGE
    )
    assert phase1_output is not None
    assert isinstance(phase1_output, StrategicCoreOutput)
    assert phase1_output.positioning_statement != ""

    # Phase 2: call the shared helper directly with Phase 2's node_merge table.
    phase2_node = _phase2_nested_node_result()
    phase2_output = _merge_named_fragments(
        phase2_node, NarrativeMessagingOutput, _PHASE2_NODE_MERGE, require_all=True
    )
    assert phase2_output is not None
    assert isinstance(phase2_output, NarrativeMessagingOutput)
    assert phase2_output.tagline != ""

    # Both phases produce fully-populated outputs from the same helper.
    _assert_every_field_populated(phase1_output)
    _assert_every_field_populated(phase2_output)


def _channel_guide_output(channel: str) -> ChannelGuidelineOutput:
    return ChannelGuidelineOutput(
        channel=channel,
        strategy=f"{channel} strategy.",
        dos=[f"{channel} do 1", f"{channel} do 2", f"{channel} do 3"],
        donts=[f"{channel} don't 1", f"{channel} don't 2", f"{channel} don't 3"],
        content_types=[f"{channel} content 1", f"{channel} content 2", f"{channel} content 3"],
        frequency_guidance=f"{channel} cadence.",
    )


_PHASE4_CHANNELS = ["website", "social", "email", "events", "partnerships", "internal"]


def _phase4_nested_node_result() -> MagicMock:
    """A mock NodeResult wrapping all nine Phase-4 specialists' fragments,
    shaped like the real nested MultiAgentResult.results Strands returns."""
    channels = _PHASE4_CHANNELS
    nested_results = {
        "brand_experience_principler": _leaf_node_result(
            BrandExperiencePrinciplesOutput(
                brand_experience_principles=["Consistent", "Human", "Confident"],
                signature_moments=["Onboarding email", "First dashboard load", "Renewal call"],
                sensory_elements=["Signature blue", "Rounded corners"],
            )
        ),
        **{
            f"{channel}_guide": _leaf_node_result(_channel_guide_output(channel))
            for channel in channels
        },
        "brand_architecture_builder": _leaf_node_result(
            BrandArchitectureOutput(
                brand_architecture=[
                    BrandArchitectureRuleOutput(
                        entity="Parent brand",
                        relationship="Master brand",
                        naming_convention="Northstar [Product]",
                        visual_treatment="Primary logo lockup",
                    )
                ],
                naming_conventions=[
                    "Title Case product names",
                    "No internal codenames",
                    "ASCII only",
                ],
                terminology_glossary={
                    "Brand experience": "How the brand feels across touchpoints",
                    "Signature moment": "A high-impact touchpoint",
                    "Channel guideline": "Per-channel execution rules",
                    "Brand architecture": "How entities relate under the brand",
                    "Terminology glossary": "Shared vocabulary for the brand",
                },
            )
        ),
        "brand_in_action_illustrator": _leaf_node_result(
            BrandInActionOutput(
                brand_in_action=[
                    BrandInActionExampleOutput(
                        context="Website hero",
                        correct_example="On-brand hero copy.",
                        incorrect_example="Off-brand jargon-heavy copy.",
                        rationale="Keeps the promise consistent.",
                    ),
                    BrandInActionExampleOutput(
                        context="Support email",
                        correct_example="Warm, direct reply.",
                        incorrect_example="Cold, templated reply.",
                        rationale="Matches the brand's human tone.",
                    ),
                    BrandInActionExampleOutput(
                        context="Sales deck",
                        correct_example="Outcome-led narrative.",
                        incorrect_example="Feature-dump narrative.",
                        rationale="Reinforces the positioning.",
                    ),
                ]
            )
        ),
    }

    inner_multi_result = MagicMock()
    inner_multi_result.results = nested_results

    node_result = MagicMock()
    node_result.result = inner_multi_result
    node_result.get_agent_results.return_value = [
        node.get_agent_results.return_value[0] for node in nested_results.values()
    ]
    return node_result


def _assert_every_field_populated(model: BaseModel) -> None:
    """Fail with the offending field names if any field on ``model`` was left
    at an empty/falsy value — used to prove a set of merged fragments
    collectively covers every field on the target schema, so a future field
    added without a producing specialist is caught automatically."""
    empty = [name for name in type(model).model_fields if not getattr(model, name)]
    assert not empty, f"{type(model).__name__} fields left empty: {empty}"


def test_extract_phase_output_merges_every_phase4_fragment() -> None:
    """Phase 4 wraps nine fan-out agents as one top-level node; the six
    *_guide specialists each emit a single ChannelGuidelineOutput that must
    all survive as separate channel_guidelines list elements, not overwrite
    one another the way a plain nest_under assignment would."""
    channels = _PHASE4_CHANNELS
    node_result = _phase4_nested_node_result()

    mock_result = MagicMock()
    mock_result.result = {"phase4_channel": node_result}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase4_channel", ChannelActivationOutput
    )

    assert degraded is False
    assert isinstance(output, ChannelActivationOutput)
    assert output.brand_experience_principles == ["Consistent", "Human", "Confident"]
    assert output.signature_moments == [
        "Onboarding email",
        "First dashboard load",
        "Renewal call",
    ]
    assert output.sensory_elements == ["Signature blue", "Rounded corners"]
    assert [g.channel for g in output.channel_guidelines] == channels
    assert output.channel_guidelines[0].strategy == "website strategy."
    assert output.channel_guidelines[-1].strategy == "internal strategy."
    assert [r.entity for r in output.brand_architecture] == ["Parent brand"]
    assert output.naming_conventions == [
        "Title Case product names",
        "No internal codenames",
        "ASCII only",
    ]
    assert output.terminology_glossary["Signature moment"] == "A high-impact touchpoint"
    assert [e.context for e in output.brand_in_action] == [
        "Website hero",
        "Support email",
        "Sales deck",
    ]


def test_phase4_fragments_collectively_populate_every_output_field() -> None:
    """Schema-coverage guard: the nine Phase-4 specialists' fragments must
    collectively populate every field on ChannelActivationOutput, checked
    generically against the model's own field list (not a hardcoded field
    enumeration) so a field added later without a producing specialist fails
    this test instead of silently shipping empty."""
    mock_result = MagicMock()
    mock_result.result = {"phase4_channel": _phase4_nested_node_result()}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase4_channel", ChannelActivationOutput
    )

    assert degraded is False
    assert isinstance(output, ChannelActivationOutput)
    _assert_every_field_populated(output)


def test_full_run_phase4_not_degraded_with_nine_fragments() -> None:
    """Phase 4's real runtime shape is nine separate specialist fragments,
    not the single flat block _mock_graph_result's default gives every
    phase (which never actually exercises the Phase 4 merge_fn partial, since it
    bails out unless node_result.result.results is a dict). Wire that real
    shape through orchestrator.run() end-to-end and confirm the Python merge
    keeps Phase 4 out of degraded_phases and fully populates its output."""
    mock_result = _mock_graph_result(ALL_PHASES)
    mock_result.result["phase4_channel"] = _phase4_nested_node_result()

    async def mock_invoke_async(task, **kwargs):
        return mock_result

    with patch(
        "branding_team.orchestrator.build_branding_graph",
        return_value=MagicMock(invoke_async=AsyncMock(side_effect=mock_invoke_async)),
    ):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
        )

    assert result.degraded_phases == []
    assert isinstance(result.channel_activation, ChannelActivationOutput)
    _assert_every_field_populated(result.channel_activation)


def test_merge_named_fragments_phase4_rejects_incomplete_specialist_set() -> None:
    """A Phase 4 run missing one of the nine specialists (e.g. events_guide
    never completed) must not validate as a complete ChannelActivationOutput
    via field defaults — every field on it defaults to empty/absent, so a
    partial merge would otherwise pass validation silently.

    Tested directly against ``_merge_named_fragments`` bound with ``_PHASE4_NODE_MERGE`` (require_all=True) as
    a focused unit test of the merge function in isolation. See
    ``test_extract_phase_output_rejects_incomplete_phase4_fragments`` for the
    end-to-end path through ``_extract_phase_output``.
    """
    channels = ["website", "social", "email", "partnerships", "internal"]  # events_guide omitted
    nested_results = {
        "brand_experience_principler": _leaf_node_result(
            BrandExperiencePrinciplesOutput(
                brand_experience_principles=["Consistent", "Human", "Confident"],
                signature_moments=["Onboarding email", "First dashboard load", "Renewal call"],
                sensory_elements=["Signature blue", "Rounded corners"],
            )
        ),
        **{
            f"{channel}_guide": _leaf_node_result(_channel_guide_output(channel))
            for channel in channels
        },
    }

    inner_multi_result = MagicMock()
    inner_multi_result.results = nested_results

    node_result = MagicMock()
    node_result.result = inner_multi_result

    merged = functools.partial(
        _merge_named_fragments, node_merge=_PHASE4_NODE_MERGE, require_all=True
    )(node_result, ChannelActivationOutput)

    assert merged is None



def _phase5_specialist_fragments() -> dict[str, BaseModel]:
    """The seven Phase-5 specialists' typed fragments, keyed by node id.

    Every fragment's fields map 1:1 onto a disjoint set of GovernanceOutput
    field names (see models.py), so — unlike Phase 4's six *_guide
    specialists — none of these need to share a nest-under key.
    """
    return {
        "ownership_definer": OwnershipOutput(
            ownership_model="Brand Director owns the system end to end.",
            decision_authority={
                "logo_changes": "Brand Director",
                "campaign_messaging": "Marketing Lead",
            },
        ),
        "approval_workflow_designer": ApprovalWorkflowsOutput(
            approval_workflows=[
                ApprovalWorkflowOutput(
                    asset_type="campaign creative",
                    approvers=["Brand Director"],
                    sla="2 business days",
                    escalation_path="VP Marketing",
                ),
                ApprovalWorkflowOutput(
                    asset_type="website copy",
                    approvers=["Brand Director", "Legal"],
                    sla="3 business days",
                    escalation_path="VP Marketing",
                ),
                ApprovalWorkflowOutput(
                    asset_type="paid media",
                    approvers=["Marketing Lead"],
                    sla="1 business day",
                    escalation_path="Brand Director",
                ),
            ],
            agency_briefing_protocols=[
                "Always include the brand book",
                "Share the current campaign brief",
                "Confirm approvers before kickoff",
            ],
        ),
        "asset_wiki_planner": AssetWikiOutput(
            asset_management_guidance=[
                "Store all assets in the central DAM",
                "Tag assets with campaign and channel",
                "Archive superseded assets quarterly",
            ],
            wiki_backlog=[
                WikiEntryOutput(
                    title="Brand North Star",
                    summary="Source of truth for positioning.",
                    owners=["Brand Strategy"],
                    update_cadence="quarterly",
                ),
                WikiEntryOutput(
                    title="Voice Playbook",
                    summary="Tone and language guidance.",
                    owners=["Brand Strategy"],
                    update_cadence="quarterly",
                ),
                WikiEntryOutput(
                    title="Design System",
                    summary="Component and token reference.",
                    owners=["Design"],
                    update_cadence="monthly",
                ),
                WikiEntryOutput(
                    title="Governance Charter",
                    summary="Ownership and approval rules.",
                    owners=["Brand Strategy"],
                    update_cadence="annually",
                ),
            ],
        ),
        "training_planner": TrainingOnboardingOutput(
            training_onboarding_plan=[
                "Brand 101 for new hires",
                "Quarterly brand refresher workshop",
                "Self-serve brand portal walkthrough",
                "Manager-led brand values session",
            ],
        ),
        "kpi_designer": BrandHealthKPIsOutput(
            brand_health_kpis=[
                BrandHealthKPIOutput(
                    metric="NPS",
                    measurement_method="Quarterly survey",
                    target=">50",
                    review_frequency="quarterly",
                ),
                BrandHealthKPIOutput(
                    metric="Brand recall",
                    measurement_method="Annual brand study",
                    target=">40%",
                    review_frequency="annually",
                ),
                BrandHealthKPIOutput(
                    metric="Message consistency",
                    measurement_method="Channel audit",
                    target=">90%",
                    review_frequency="quarterly",
                ),
                BrandHealthKPIOutput(
                    metric="Employee brand literacy",
                    measurement_method="Internal survey",
                    target=">80%",
                    review_frequency="annually",
                ),
            ],
            tracking_methodology=(
                "Quarterly brand health surveys triangulated with channel audits."
            ),
            review_trigger_points=[
                "Major campaign launch",
                "Leadership change",
                "Category repositioning",
            ],
        ),
        "evolution_framer": EvolutionFrameworkOutput(
            evolution_framework=(
                "The brand evolves through an annual refresh cycle informed by health KPIs."
            ),
            version_control_cadence="Bi-annual version bumps, reviewed by the Brand Director.",
        ),
        "brand_rules_codifier": BrandGuidelinesOutput(
            brand_guidelines=[
                "Positioning: use the approved positioning statement.",
                "Promise: lead with the brand promise.",
                "Identity: follow logo spacing rules.",
                "Messaging: promise -> pillar -> proof -> CTA.",
                "Governance: route major campaigns through brand review.",
            ],
        ),
    }


def _phase5_nested_node_result(*, omit: str = "") -> MagicMock:
    """A mock NodeResult wrapping the Phase-5 specialists' fragments, shaped
    like the real nested MultiAgentResult.results Strands returns.

    ``omit`` drops one node id from the set (used to build an incomplete
    specialist set for the rejection tests below).
    """
    nested_results = {
        node_id: _leaf_node_result(fragment)
        for node_id, fragment in _phase5_specialist_fragments().items()
        if node_id != omit
    }

    inner_multi_result = MagicMock()
    inner_multi_result.results = nested_results

    node_result = MagicMock()
    node_result.result = inner_multi_result
    node_result.get_agent_results.return_value = [
        node.get_agent_results.return_value[0] for node in nested_results.values()
    ]
    return node_result


def test_extract_phase_output_merges_every_phase5_fragment() -> None:
    """Phase 5 wraps seven fan-out agents as one top-level node; each
    specialist's fragment maps 1:1 onto disjoint GovernanceOutput fields, so
    all seven must survive the merge with none overwriting another."""
    node_result = _phase5_nested_node_result()

    mock_result = MagicMock()
    mock_result.result = {"phase5_governance": node_result}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase5_governance", GovernanceOutput
    )

    assert degraded is False
    assert isinstance(output, GovernanceOutput)
    assert output.ownership_model == "Brand Director owns the system end to end."
    assert output.decision_authority["logo_changes"] == "Brand Director"
    assert len(output.approval_workflows) == 3
    assert output.agency_briefing_protocols[0] == "Always include the brand book"
    assert output.asset_management_guidance[0] == "Store all assets in the central DAM"
    assert [w.title for w in output.wiki_backlog] == [
        "Brand North Star",
        "Voice Playbook",
        "Design System",
        "Governance Charter",
    ]
    assert output.training_onboarding_plan[0] == "Brand 101 for new hires"
    assert [k.metric for k in output.brand_health_kpis] == [
        "NPS",
        "Brand recall",
        "Message consistency",
        "Employee brand literacy",
    ]
    assert (
        output.tracking_methodology
        == "Quarterly brand health surveys triangulated with channel audits."
    )
    assert output.review_trigger_points[0] == "Major campaign launch"
    assert output.evolution_framework == (
        "The brand evolves through an annual refresh cycle informed by health KPIs."
    )
    assert output.version_control_cadence == (
        "Bi-annual version bumps, reviewed by the Brand Director."
    )
    assert output.brand_guidelines[0] == "Positioning: use the approved positioning statement."


def test_phase5_fragments_collectively_populate_every_output_field() -> None:
    """Schema-coverage guard: the seven Phase-5 specialists' fragments must
    collectively populate every field on GovernanceOutput, checked
    generically against the model's own field list (not a hardcoded field
    enumeration) so a field added later without a producing specialist fails
    this test instead of silently shipping empty."""
    mock_result = MagicMock()
    mock_result.result = {"phase5_governance": _phase5_nested_node_result()}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase5_governance", GovernanceOutput
    )

    assert degraded is False
    assert isinstance(output, GovernanceOutput)
    _assert_every_field_populated(output)


def test_full_run_phase5_not_degraded_with_seven_fragments() -> None:
    """Phase 5's real runtime shape is seven separate specialist fragments,
    not the single flat block _mock_graph_result's default gives every
    phase (which never actually exercises the Phase 5 merge_fn partial, since it
    bails out unless node_result.result.results is a dict). Wire that real
    shape through orchestrator.run() end-to-end and confirm the Python merge
    keeps Phase 5 out of degraded_phases and fully populates its output."""
    mock_result = _mock_graph_result(ALL_PHASES)
    mock_result.result["phase5_governance"] = _phase5_nested_node_result()

    async def mock_invoke_async(task, **kwargs):
        return mock_result

    with patch(
        "branding_team.orchestrator.build_branding_graph",
        return_value=MagicMock(invoke_async=AsyncMock(side_effect=mock_invoke_async)),
    ):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
        )

    assert result.degraded_phases == []
    assert isinstance(result.governance, GovernanceOutput)
    _assert_every_field_populated(result.governance)


def test_merge_named_fragments_phase5_rejects_incomplete_specialist_set() -> None:
    """A Phase 5 run missing one of the seven specialists (e.g.
    evolution_framer never completed) must not validate as a complete
    GovernanceOutput via field defaults — every field on it defaults to
    empty/absent, so a partial merge would otherwise pass validation
    silently.

    Tested directly against ``_merge_named_fragments`` bound with ``_PHASE5_NODE_MERGE`` (require_all=True) as
    a focused unit test of the merge function in isolation. See
    ``test_extract_phase_output_rejects_incomplete_phase5_fragments`` for the
    end-to-end path through ``_extract_phase_output``.
    """
    node_result = _phase5_nested_node_result(omit="evolution_framer")

    merged = functools.partial(
        _merge_named_fragments, node_merge=_PHASE5_NODE_MERGE, require_all=True
    )(node_result, GovernanceOutput)

    assert merged is None



def _phase3_specialist_fragments() -> dict[str, BaseModel]:
    """The eleven Phase-3 node ids -> the structured_output fragment each emits.

    Three moodboard conceptualists, converge_decider, and seven post-converge
    specialists -- the full set ``_PHASE3_NODE_MERGE`` requires. Every string
    field is non-empty and every bounded list meets its schema's min/max, since
    these are the strict agent-facing "Output" twins (see models.py module
    docstring) which reject blanks.
    """
    return {
        "MoodBoardConceptualist_Editorial": MoodBoardConcept(
            title="Modern Confidence",
            visual_direction="Clean grids with generous whitespace.",
            color_story=["midnight blue", "warm white"],
            typography_direction="Geometric sans, confident weights.",
            image_style=["studio product shots", "candid team photography"],
        ),
        "MoodBoardConceptualist_Minimalist": MoodBoardConcept(
            title="Quiet Precision",
            visual_direction="Monochrome, restrained, plenty of negative space.",
            color_story=["off-white", "graphite"],
            typography_direction="Single grotesk family, tight tracking.",
            image_style=["macro texture shots", "architectural details"],
        ),
        "MoodBoardConceptualist_Bold": MoodBoardConcept(
            title="Kinetic Signal",
            visual_direction="High-contrast, energetic, motion-forward.",
            color_story=["electric blue", "signal orange"],
            typography_direction="Variable weight display type.",
            image_style=["dynamic action shots", "layered graphic overlays"],
        ),
        "converge_decider": CreativeRefinementDecision(
            winning_candidate_title="Modern Confidence",
            scoring_criteria=["Audience resonance", "Distinctiveness", "Feasibility"],
            scores_by_candidate={
                "Modern Confidence": 8.5,
                "Quiet Precision": 7.0,
                "Kinetic Signal": 6.5,
            },
            rationale="Best aligns with brand values and cross-channel consistency.",
            workshop_prompts=[
                "Does this direction feel distinct from competitors?",
                "Can this scale across every channel?",
                "Does it hold up in monochrome?",
            ],
            decision_criteria=["Audience resonance", "Distinctiveness", "Feasibility"],
        ),
        "logo_specifier": LogoSuiteOutput(
            logo_suite=[
                LogoUsageRuleOutput(
                    variant="primary",
                    usage_context="Default usage on light backgrounds.",
                    minimum_size="24px",
                    clear_space="1x logo height on all sides.",
                ),
                LogoUsageRuleOutput(
                    variant="monochrome",
                    usage_context="Single-color print or low-contrast contexts.",
                    minimum_size="24px",
                    clear_space="1x logo height on all sides.",
                ),
                LogoUsageRuleOutput(
                    variant="icon-only",
                    usage_context="App icons and favicons.",
                    minimum_size="16px",
                    clear_space="0.5x icon height on all sides.",
                ),
                LogoUsageRuleOutput(
                    variant="reversed",
                    usage_context="Dark backgrounds and photography overlays.",
                    minimum_size="24px",
                    clear_space="1x logo height on all sides.",
                ),
            ],
        ),
        "color_system_builder": ColorPaletteSystemOutput(
            color_palette=[
                ColorEntryOutput(
                    name=name,
                    hex_value=hex_value,
                    usage=usage,
                    psychological_rationale=rationale,
                )
                for name, hex_value, usage, rationale in [
                    ("Midnight", "#1a1a2e", "Primary background", "Conveys depth and authority"),
                    (
                        "Signal Orange",
                        "#ff6b35",
                        "Primary accent/CTA",
                        "Draws the eye, conveys energy",
                    ),
                    (
                        "Warm White",
                        "#f7f5f2",
                        "Primary text on dark",
                        "Softens contrast, feels approachable",
                    ),
                    (
                        "Graphite",
                        "#3a3a4a",
                        "Secondary text",
                        "Grounds the palette without competing",
                    ),
                    ("Sky Tint", "#c9d6e3", "Supporting/backgrounds", "Adds air and openness"),
                ]
            ],
        ),
        "typography_builder": TypographySystemOutput(
            typography_system=[
                TypographySpecOutput(
                    role=role,
                    font_family=font,
                    weight_range=weights,
                    usage_notes=notes,
                )
                for role, font, weights, notes in [
                    ("display", "Inter", "600-800", "Headlines and hero statements."),
                    ("body", "Inter", "400-500", "Paragraph copy and UI labels."),
                    ("caption", "Inter", "400", "Fine print and metadata."),
                ]
            ],
        ),
        "iconography_director": IconographyOutput(
            iconography_style="1.5px line weight, 4px corner radius, no fill.",
            illustration_style="Flat, geometric, two-tone with the accent color.",
        ),
        "photography_video_director": PhotographyVideoOutput(
            photography_direction="Natural light, candid moments, warm color grading.",
            video_direction="Confident pacing, minimal cuts, ambient sound.",
            motion_principles=[
                "Ease in, never linear",
                "Motion always has purpose",
                "Respect reduced-motion preferences",
            ],
        ),
        "voice_tone_builder": VoiceToneOutput(
            voice_tone_spectrum=[
                VoiceToneEntryOutput(
                    context="marketing",
                    tone="Confident and warm",
                    examples=["We built this for teams who move fast."],
                ),
            ],
            language_dos=[
                "Use plain language",
                "Lead with the customer's outcome",
                "Use active voice",
                "Be specific with numbers",
            ],
            language_donts=[
                "Avoid jargon",
                "Avoid hyperbole",
                "Avoid passive voice",
                "Avoid unverifiable superlatives",
            ],
        ),
        "design_system_codifier": DesignSystemDefinition(
            design_principles=["Clarity over decoration", "Consistency at scale"],
            foundation_tokens=["Color tokens: primary/secondary", "Spacing scale: 4px base"],
            component_standards=["Buttons: size variants", "Cards: consistent padding"],
        ),
    }


def _phase3_nested_node_result(*, omit: str = "") -> MagicMock:
    """A mock NodeResult wrapping the Phase-3 nodes' fragments, shaped like
    the real nested MultiAgentResult.results Strands returns.

    ``omit`` drops one node id from the set (used to build an incomplete
    node set for the rejection tests below).
    """
    nested_results = {
        node_id: _leaf_node_result(fragment)
        for node_id, fragment in _phase3_specialist_fragments().items()
        if node_id != omit
    }

    inner_multi_result = MagicMock()
    inner_multi_result.results = nested_results

    node_result = MagicMock()
    node_result.result = inner_multi_result
    node_result.get_agent_results.return_value = [
        node.get_agent_results.return_value[0] for node in nested_results.values()
    ]
    return node_result


def test_extract_phase_output_merges_every_phase3_fragment() -> None:
    """Phase 3 wraps eleven nodes (three conceptualists, converge_decider, and
    seven specialists) as one top-level node; each contributes a fragment that
    must survive the merge -- the three conceptualists' MoodBoardConcept
    fragments append into mood_board_candidates, converge_decider's decision
    nests under creative_refinement, and design_system_codifier's definition
    nests under design_system."""
    node_result = _phase3_nested_node_result()

    mock_result = MagicMock()
    mock_result.result = {"phase3_visual": node_result}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase3_visual", VisualIdentityOutput
    )

    assert degraded is False
    assert isinstance(output, VisualIdentityOutput)
    assert {c.title for c in output.mood_board_candidates} == {
        "Modern Confidence",
        "Quiet Precision",
        "Kinetic Signal",
    }
    assert output.creative_refinement.winning_candidate_title == "Modern Confidence"
    assert len(output.logo_suite) == 4
    assert len(output.color_palette) == 5
    assert len(output.typography_system) == 3
    assert output.iconography_style == "1.5px line weight, 4px corner radius, no fill."
    assert output.illustration_style == "Flat, geometric, two-tone with the accent color."
    assert output.photography_direction == "Natural light, candid moments, warm color grading."
    assert output.video_direction == "Confident pacing, minimal cuts, ambient sound."
    assert len(output.motion_principles) == 3
    assert len(output.voice_tone_spectrum) == 1
    assert len(output.language_dos) == 4
    assert len(output.language_donts) == 4
    assert output.design_system.design_principles == [
        "Clarity over decoration",
        "Consistency at scale",
    ]


def test_phase3_fragments_collectively_populate_every_output_field() -> None:
    """Schema-coverage guard: the three moodboard conceptualists,
    converge_decider, and the seven post-converge specialists' fragments must
    collectively populate every VisualIdentityOutput field they can produce,
    checked generically against the model's own field list (not a hardcoded
    enumeration) so a field added later without a producing node fails this
    test instead of silently shipping empty."""
    mock_result = MagicMock()
    mock_result.result = {"phase3_visual": _phase3_nested_node_result()}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase3_visual", VisualIdentityOutput
    )

    assert degraded is False
    assert isinstance(output, VisualIdentityOutput)
    _assert_every_field_populated(output)


def test_full_run_phase3_not_degraded_with_eleven_fragments() -> None:
    """Phase 3's real runtime shape is eleven separate node fragments, not the
    single flat block _mock_graph_result's default gives every phase (which
    never actually exercises the Phase 3 merge_fn partial, since it bails out
    unless node_result.result.results is a dict). Wire that real shape
    through orchestrator.run() end-to-end and confirm the Python merge keeps
    Phase 3 out of degraded_phases."""
    mock_result = _mock_graph_result(ALL_PHASES)
    mock_result.result["phase3_visual"] = _phase3_nested_node_result()

    async def mock_invoke_async(task, **kwargs):
        return mock_result

    with patch(
        "branding_team.orchestrator.build_branding_graph",
        return_value=MagicMock(invoke_async=AsyncMock(side_effect=mock_invoke_async)),
    ):
        orchestrator = BrandingTeamOrchestrator()
        result = orchestrator.run(
            mission=make_mission(
                company_description="A strategic studio helping product teams ship cohesive digital experiences",
                values=["clarity", "trust", "momentum"],
            ),
            human_review=HumanReview(approved=True),
        )

    assert result.degraded_phases == []
    assert isinstance(result.visual_identity, VisualIdentityOutput)
    assert result.visual_identity.creative_refinement.winning_candidate_title == "Modern Confidence"
    _assert_every_field_populated(result.visual_identity)


def test_merge_named_fragments_phase3_rejects_incomplete_specialist_set() -> None:
    """A Phase 3 run missing one node (e.g. design_system_codifier never
    completed) must not validate as a complete VisualIdentityOutput via field
    defaults -- every field on it defaults to empty/absent, so a partial
    merge would otherwise pass validation silently.

    Tested directly against ``_merge_named_fragments`` bound with ``_PHASE3_NODE_MERGE`` (require_all=True) as
    a focused unit test of the merge function in isolation. See
    ``test_extract_phase_output_rejects_incomplete_phase3_fragments`` for the
    end-to-end path through ``_extract_phase_output``.
    """
    node_result = _phase3_nested_node_result(omit="design_system_codifier")

    merged = functools.partial(
        _merge_named_fragments, node_merge=_PHASE3_NODE_MERGE, require_all=True
    )(node_result, VisualIdentityOutput)

    assert merged is None



def _text_node_result(text: str) -> MagicMock:
    """A mock NodeResult whose single agent result carries raw ``text`` and
    no ``structured_output``, forcing extraction down the
    ``_parse_model_from_text`` path exercised by the tests below."""
    agent_result = MagicMock()
    agent_result.message = {"content": [{"text": text}]}
    agent_result.structured_output = None
    node_result = MagicMock()
    node_result.get_agent_results.return_value = [agent_result]
    return node_result


def test_parse_model_from_text_truncated_json_returns_none_and_extract_degrades() -> None:
    """JSON cut off mid-object (e.g. a truncated LLM response) must not
    validate — _parse_model_from_text returns None, and _extract_phase_output
    must surface that as degraded=True rather than silently defaulting."""
    from branding_team.orchestrator import _parse_model_from_text

    full_json = _full_strategic_core().model_dump_json()
    truncated = full_json[: len(full_json) // 2]

    assert _parse_model_from_text(truncated, StrategicCoreOutput) is None

    mock_result = MagicMock()
    mock_result.result = {"phase1_strategic_core": _text_node_result(truncated)}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase1_strategic_core", StrategicCoreOutput
    )

    assert degraded is True
    assert output == StrategicCoreOutput()


def test_parse_model_from_text_prose_wrapped_json_recovers() -> None:
    """Real prose before and after the JSON block (a common LLM reply shape)
    must still be recovered — proving the degradation signal isn't
    spuriously set for payloads that are genuinely recoverable."""
    from branding_team.orchestrator import _parse_model_from_text

    real_payload = _full_strategic_core()
    text = (
        "Here is the strategic core output:\n\n"
        + real_payload.model_dump_json()
        + "\n\nLet me know if any revisions are needed."
    )

    parsed = _parse_model_from_text(text, StrategicCoreOutput)
    assert parsed == real_payload

    mock_result = MagicMock()
    mock_result.result = {"phase1_strategic_core": _text_node_result(text)}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase1_strategic_core", StrategicCoreOutput
    )

    assert degraded is False
    assert output.positioning_statement == real_payload.positioning_statement
    assert output.brand_promise == real_payload.brand_promise


def test_parse_model_from_text_schema_mismatch_returns_none_and_extract_degrades() -> None:
    """Syntactically valid JSON that fails schema validation (a required
    nested field missing) must not validate — _parse_model_from_text returns
    None, and _extract_phase_output must surface that as degraded=True."""
    from branding_team.orchestrator import _parse_model_from_text

    text = json.dumps(
        {
            "positioning_statement": "For enterprise leaders who need clarity, we deliver it.",
            "core_values": [{"behavioral_definition": "Missing the required 'value' field."}],
        }
    )

    assert _parse_model_from_text(text, StrategicCoreOutput) is None

    mock_result = MagicMock()
    mock_result.result = {"phase1_strategic_core": _text_node_result(text)}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase1_strategic_core", StrategicCoreOutput
    )

    assert degraded is True
    assert output == StrategicCoreOutput()


def test_last_resort_text_parse_recovers_malformed_specialist_reply() -> None:
    """When a specialist emits garbled nested results that merge_fn cannot
    assemble (e.g. unexpected child ids), but the last agent's text reply
    contains a valid JSON dump of the full phase output, the last-resort text
    parse in ``_extract_from_single_agent`` recovers it without degrading.

    This is the single remaining fallback path after the compositor-only
    branches were pruned: merge_fn fires and returns None → text parse of
    the last agent's message succeeds → output is returned non-degraded.
    """
    real_payload = _full_strategic_core()
    # Simulate a malformed specialist reply: the nested MultiAgentResult has
    # unexpected/garbled child ids so merge_fn finds none of its known ids.
    garbled_nested = {"unknown_specialist_xyz": MagicMock(), "another_garbled": MagicMock()}
    inner_multi_result = MagicMock()
    inner_multi_result.results = garbled_nested

    # But the last agent wrote the correct payload as prose-wrapped text.
    agent_result = MagicMock()
    agent_result.message = {
        "content": [
            {
                "text": (
                    "I've synthesized the strategic core:\n\n"
                    + real_payload.model_dump_json()
                    + "\n\nReady for review."
                )
            }
        ]
    }
    agent_result.structured_output = None

    node_result = MagicMock()
    node_result.result = inner_multi_result
    node_result.get_agent_results.return_value = [agent_result]

    mock_result = MagicMock()
    mock_result.result = {"phase1_strategic_core": node_result}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase1_strategic_core", StrategicCoreOutput
    )

    assert degraded is False
    assert isinstance(output, StrategicCoreOutput)
    assert output.positioning_statement == real_payload.positioning_statement
    assert output.brand_promise == real_payload.brand_promise
    assert [v.value for v in output.core_values] == [v.value for v in real_payload.core_values]


def test_gather_integrations_market_research_failure_returns_none() -> None:
    """A failing market-research call is swallowed to None; disabled design → None."""
    from branding_team.orchestrator import _gather_integrations

    async def _boom(_mission):
        raise RuntimeError("market research down")

    with patch("branding_team.adapters.market_research.request_market_research_async", _boom):
        snapshot, design = asyncio.run(
            _gather_integrations(
                make_mission(
                    company_description="A strategic studio helping product teams ship cohesive digital experiences",
                    values=["clarity", "trust", "momentum"],
                ),
                None,
                True,
                False,
            )
        )
    assert snapshot is None
    assert design is None


# ---------------------------------------------------------------------------
# Extracted helper unit tests: _locate_node_result / _extract_from_single_agent
# / _child_structured_output / _apply_fragment. These pin the small named
# pieces that _extract_phase_output and _merge_named_fragments were split into,
# so a regression localizes to the helper rather than the whole extractor.
# ---------------------------------------------------------------------------


def test_locate_node_result_returns_node_when_present() -> None:
    """A well-formed result with the node id present yields that node result."""
    from branding_team.orchestrator import _locate_node_result

    node_result = MagicMock()
    node_result.result = MagicMock()
    mock_result = MagicMock()
    mock_result.result = {"phase1_strategic_core": node_result}

    assert _locate_node_result(mock_result, "phase1_strategic_core") is node_result


def test_locate_node_result_missing_node_returns_none() -> None:
    """A result mapping without the node id degrades to None, not KeyError."""
    from branding_team.orchestrator import _locate_node_result

    mock_result = MagicMock()
    mock_result.result = {"other_node": MagicMock()}

    assert _locate_node_result(mock_result, "phase1_strategic_core") is None


def test_locate_node_result_non_mapping_result_returns_none() -> None:
    """A top-level result that isn't a ``.get``-able mapping returns None."""
    from branding_team.orchestrator import _locate_node_result

    mock_result = MagicMock()
    mock_result.result = object()  # no ``.get``

    assert _locate_node_result(mock_result, "phase1_strategic_core") is None


def test_locate_node_result_node_without_result_attr_returns_none() -> None:
    """A node value lacking a ``.result`` wrapper returns None."""
    from branding_team.orchestrator import _locate_node_result

    class _NoResult:
        pass

    mock_result = MagicMock()
    mock_result.result = {"phase1_strategic_core": _NoResult()}

    assert _locate_node_result(mock_result, "phase1_strategic_core") is None


def test_extract_from_single_agent_falls_back_to_text() -> None:
    """With no merge_fn match, the last agent's last text block is parsed."""
    from branding_team.orchestrator import _extract_from_single_agent

    core = _full_strategic_core()
    agent_result = MagicMock()
    agent_result.structured_output = None
    agent_result.message = {"content": [{"text": core.model_dump_json()}]}
    node = MagicMock()
    node.get_agent_results.return_value = [agent_result]

    parsed = _extract_from_single_agent(node, StrategicCoreOutput)

    assert isinstance(parsed, StrategicCoreOutput)
    assert parsed.brand_promise == core.brand_promise


def test_extract_from_single_agent_no_agent_results_returns_none() -> None:
    """An empty ``get_agent_results()`` yields None (caller then degrades)."""
    from branding_team.orchestrator import _extract_from_single_agent

    node = MagicMock()
    node.get_agent_results.return_value = []

    assert _extract_from_single_agent(node, StrategicCoreOutput) is None


def test_child_structured_output_valid_child() -> None:
    """A child with a BaseModel structured_output returns that model."""
    from branding_team.orchestrator import _child_structured_output

    core = _full_strategic_core()
    child = _leaf_node_result(core)

    assert _child_structured_output(child) is core


def test_child_structured_output_none_child_returns_none() -> None:
    """A missing child id (None) is skipped, not dereferenced."""
    from branding_team.orchestrator import _child_structured_output

    assert _child_structured_output(None) is None


def test_child_structured_output_without_get_agent_results_returns_none() -> None:
    """A child lacking ``get_agent_results`` is skipped."""
    from branding_team.orchestrator import _child_structured_output

    class _Bare:
        pass

    assert _child_structured_output(_Bare()) is None


def test_child_structured_output_empty_results_returns_none() -> None:
    """A child whose ``get_agent_results()`` is empty is skipped."""
    from branding_team.orchestrator import _child_structured_output

    child = MagicMock()
    child.get_agent_results.return_value = []

    assert _child_structured_output(child) is None


def test_child_structured_output_non_basemodel_returns_none() -> None:
    """A child whose structured_output isn't a BaseModel is skipped."""
    from branding_team.orchestrator import _child_structured_output

    child = _leaf_node_result({"not": "a model"})

    assert _child_structured_output(child) is None


def test_apply_fragment_flat_last_writer_wins() -> None:
    """Flat merge (no nest_under) overwrites existing keys."""
    from branding_team.orchestrator import _apply_fragment

    merged = {"a": 1, "b": 2}
    _apply_fragment(merged, {"b": 20, "c": 3}, None)

    assert merged == {"a": 1, "b": 20, "c": 3}


def test_apply_fragment_nest_under_places_data() -> None:
    """A nest_under key places the whole fragment under that key."""
    from branding_team.orchestrator import _apply_fragment

    merged: dict = {}
    _apply_fragment(merged, {"x": 1}, "brand_discovery")

    assert merged == {"brand_discovery": {"x": 1}}


def test_apply_fragment_list_field_appends_each_fragment() -> None:
    """A nest_under key naming a list field appends each fragment as one element
    (Phase 4's channel_guidelines) rather than overwriting."""
    from branding_team.orchestrator import _apply_fragment

    list_fields = frozenset({"channel_guidelines"})
    merged: dict = {}
    _apply_fragment(
        merged,
        {"channel": "web"},
        "channel_guidelines",
        list_fields=list_fields,
    )
    _apply_fragment(
        merged,
        {"channel": "social"},
        "channel_guidelines",
        list_fields=list_fields,
    )

    assert merged == {"channel_guidelines": [{"channel": "web"}, {"channel": "social"}]}


# ---------------------------------------------------------------------------
# Story 2b Step 1 & Step 2: run(phase_cache=...) -- per-phase hit/miss check,
# and recomputation from the earliest changed phase cascading to every
# downstream phase (each phase's input hash folds in the full mission and
# every upstream output, so a change anywhere upstream -- or in the mission
# itself -- necessarily changes the hash, and therefore forces a miss, for
# that phase and every phase after it in the same call).
# ---------------------------------------------------------------------------

_PHASE_FIXTURES: dict[BrandPhase, Any] = {
    BrandPhase.STRATEGIC_CORE: _full_strategic_core,
    BrandPhase.NARRATIVE_MESSAGING: _full_narrative,
    BrandPhase.VISUAL_IDENTITY: _full_visual_identity,
    BrandPhase.CHANNEL_ACTIVATION: _full_channel_activation,
    BrandPhase.GOVERNANCE: _full_governance,
}


def _run_single_phase_fixture_side_effect(mission, phase, prior_outputs=None):
    """Stand-in for ``run_single_phase``: return the canned fixture for ``phase``."""
    return _PHASE_FIXTURES[phase](), False


def test_run_without_phase_cache_never_calls_run_single_phase() -> None:
    """Omitting ``phase_cache`` (the default) preserves the monolithic-graph path."""
    orchestrator = BrandingTeamOrchestrator()
    with (
        _patch_graph_invoke(ALL_PHASES),
        patch.object(orchestrator, "run_single_phase") as mock_run_single_phase,
    ):
        result = orchestrator.run(
            mission=make_mission(),
            human_review=HumanReview(approved=True),
        )

    mock_run_single_phase.assert_not_called()
    assert result.status == WorkflowStatus.READY_FOR_ROLLOUT
    assert result.strategic_core is not None


def test_run_with_phase_cache_hit_reuses_output_without_invoking_phase() -> None:
    """A cache entry whose hash matches is reused; the phase is never invoked."""
    mission = make_mission()
    cached_output = _full_strategic_core()
    cache = PhaseOutputCache()
    cache.put(
        BrandPhase.STRATEGIC_CORE,
        phase_input_hash(BrandPhase.STRATEGIC_CORE, mission, {}),
        cached_output,
    )
    orchestrator = BrandingTeamOrchestrator()

    with patch.object(orchestrator, "run_single_phase") as mock_run_single_phase:
        result = orchestrator.run(
            mission=mission,
            human_review=HumanReview(approved=False),
            target_phase=BrandPhase.STRATEGIC_CORE,
            phase_cache=cache,
        )

    mock_run_single_phase.assert_not_called()
    assert result.strategic_core == cached_output
    assert result.narrative_messaging is None


def test_run_with_phase_cache_miss_falls_through_and_populates_cache() -> None:
    """An empty cache falls through to normal per-phase execution and gets populated."""
    mission = make_mission()
    cache = PhaseOutputCache()
    orchestrator = BrandingTeamOrchestrator()

    with patch.object(
        orchestrator, "run_single_phase", side_effect=_run_single_phase_fixture_side_effect
    ) as mock_run_single_phase:
        result = orchestrator.run(
            mission=mission,
            human_review=HumanReview(approved=True),
            phase_cache=cache,
        )

    assert mock_run_single_phase.call_count == len(PHASE_ORDER)
    assert result.status == WorkflowStatus.READY_FOR_ROLLOUT

    upstream: dict[BrandPhase, Any] = {}
    for phase in PHASE_ORDER:
        expected_hash = phase_input_hash(phase, mission, upstream)
        cached = cache.get(phase, expected_hash)
        assert cached is not None
        upstream[phase] = cached


def test_run_with_phase_cache_recomputes_downstream_phase_when_upstream_changes() -> None:
    """A stale cache entry for a downstream phase (keyed by outdated upstream) misses."""
    mission = make_mission()
    cache = PhaseOutputCache()
    stale_upstream = {BrandPhase.STRATEGIC_CORE: StrategicCoreOutput(brand_purpose="stale")}
    stale_narrative = _full_narrative()
    cache.put(
        BrandPhase.NARRATIVE_MESSAGING,
        phase_input_hash(BrandPhase.NARRATIVE_MESSAGING, mission, stale_upstream),
        stale_narrative,
    )
    orchestrator = BrandingTeamOrchestrator()

    with patch.object(
        orchestrator, "run_single_phase", side_effect=_run_single_phase_fixture_side_effect
    ) as mock_run_single_phase:
        orchestrator.run(
            mission=mission,
            human_review=HumanReview(approved=False),
            target_phase=BrandPhase.NARRATIVE_MESSAGING,
            phase_cache=cache,
        )

    called_phases = [call.args[1] for call in mock_run_single_phase.call_args_list]
    assert called_phases == [BrandPhase.STRATEGIC_CORE, BrandPhase.NARRATIVE_MESSAGING]


def test_run_with_phase_cache_never_caches_a_degraded_output() -> None:
    """A degraded phase output is returned but never stored in the cache."""
    mission = make_mission()
    cache = PhaseOutputCache()
    degraded_output = StrategicCoreOutput()
    orchestrator = BrandingTeamOrchestrator()

    with patch.object(orchestrator, "run_single_phase", return_value=(degraded_output, True)):
        result = orchestrator.run(
            mission=mission,
            human_review=HumanReview(approved=False),
            target_phase=BrandPhase.STRATEGIC_CORE,
            phase_cache=cache,
        )

    assert result.degraded_phases == [BrandPhase.STRATEGIC_CORE]
    expected_hash = phase_input_hash(BrandPhase.STRATEGIC_CORE, mission, {})
    assert cache.get(BrandPhase.STRATEGIC_CORE, expected_hash) is None


def test_run_with_phase_cache_reuses_multiple_cached_upstream_phases() -> None:
    """Two already-cached, still-valid upstream phases are both reused -- neither
    is re-invoked -- when the run is extended to a new, not-yet-cached
    downstream phase (a genuine "downstream-only" extension, not just a
    single-phase cache hit in isolation)."""
    mission = make_mission()
    cache = PhaseOutputCache()

    cached_strategic_core = _full_strategic_core()
    cache.put(
        BrandPhase.STRATEGIC_CORE,
        phase_input_hash(BrandPhase.STRATEGIC_CORE, mission, {}),
        cached_strategic_core,
    )
    cached_narrative = _full_narrative()
    cache.put(
        BrandPhase.NARRATIVE_MESSAGING,
        phase_input_hash(
            BrandPhase.NARRATIVE_MESSAGING,
            mission,
            {BrandPhase.STRATEGIC_CORE: cached_strategic_core},
        ),
        cached_narrative,
    )
    orchestrator = BrandingTeamOrchestrator()

    with patch.object(
        orchestrator, "run_single_phase", side_effect=_run_single_phase_fixture_side_effect
    ) as mock_run_single_phase:
        result = orchestrator.run(
            mission=mission,
            human_review=HumanReview(approved=False),
            target_phase=BrandPhase.VISUAL_IDENTITY,
            phase_cache=cache,
        )

    called_phases = [call.args[1] for call in mock_run_single_phase.call_args_list]
    assert called_phases == [BrandPhase.VISUAL_IDENTITY]
    assert result.strategic_core == cached_strategic_core
    assert result.narrative_messaging == cached_narrative
    assert result.visual_identity is not None
    assert result.channel_activation is None


def test_run_with_phase_cache_cascades_recompute_through_three_phase_chain() -> None:
    """A changed earliest-phase input (a mission field, which every phase's hash
    includes) invalidates the STRATEGIC_CORE cache entry and cascades a miss
    through every downstream phase in a three-phase chain; each recomputed
    phase's fresh output is written back to the cache for later reuse."""
    stale_mission = make_mission(company_name="Stale Co")
    mission = make_mission(company_name="Fresh Co")
    cache = PhaseOutputCache()

    # Pre-populate the cache as if a prior run completed for `stale_mission`,
    # forming a fully self-consistent hash chain across three phases.
    stale_upstream: dict[BrandPhase, Any] = {}
    for phase in (
        BrandPhase.STRATEGIC_CORE,
        BrandPhase.NARRATIVE_MESSAGING,
        BrandPhase.VISUAL_IDENTITY,
    ):
        stale_output = _PHASE_FIXTURES[phase]()
        cache.put(phase, phase_input_hash(phase, stale_mission, stale_upstream), stale_output)
        stale_upstream[phase] = stale_output

    orchestrator = BrandingTeamOrchestrator()

    with patch.object(
        orchestrator, "run_single_phase", side_effect=_run_single_phase_fixture_side_effect
    ) as mock_run_single_phase:
        result = orchestrator.run(
            mission=mission,
            human_review=HumanReview(approved=False),
            target_phase=BrandPhase.VISUAL_IDENTITY,
            phase_cache=cache,
        )

    called_phases = [call.args[1] for call in mock_run_single_phase.call_args_list]
    assert called_phases == [
        BrandPhase.STRATEGIC_CORE,
        BrandPhase.NARRATIVE_MESSAGING,
        BrandPhase.VISUAL_IDENTITY,
    ]

    # The cache now holds the freshly recomputed chain -- for `mission`, not
    # `stale_mission` -- ready for reuse by a later caller.
    fresh_upstream: dict[BrandPhase, Any] = {}
    for phase in (
        BrandPhase.STRATEGIC_CORE,
        BrandPhase.NARRATIVE_MESSAGING,
        BrandPhase.VISUAL_IDENTITY,
    ):
        expected_hash = phase_input_hash(phase, mission, fresh_upstream)
        cached = cache.get(phase, expected_hash)
        assert cached is not None
        fresh_upstream[phase] = cached

    assert result.strategic_core == fresh_upstream[BrandPhase.STRATEGIC_CORE]
    assert result.narrative_messaging == fresh_upstream[BrandPhase.NARRATIVE_MESSAGING]
    assert result.visual_identity == fresh_upstream[BrandPhase.VISUAL_IDENTITY]


# ---------------------------------------------------------------------------
# Story 2b Step 3: thread/Temporal confinement + cached-vs-cold output parity.
# The Temporal path (temporal/activities.py) never calls `run` -- it calls
# `run_single_phase` directly, a method with no `phase_cache` parameter -- so
# there is no code path by which a cache could reach it; that structural
# guarantee is exercised from the Temporal side in test_temporal_unit.py. This
# test instead nails down the thread-path half of the contract: a cache-driven
# run (whether populating an empty cache or fully reusing a warm one) must
# produce a `TeamOutput` equal to a cold monolithic-graph run over the same
# mission -- the cache may change *how much work* a run does, never *what it
# produces*.
# ---------------------------------------------------------------------------


def test_run_with_phase_cache_matches_cold_monolithic_run() -> None:
    """A cache miss run and a fully-cached hit run both equal a cold run's TeamOutput."""
    mission = make_mission()
    human_review = HumanReview(approved=True)

    with _patch_graph_invoke(ALL_PHASES):
        cold_orchestrator = BrandingTeamOrchestrator()
        cold_result = cold_orchestrator.run(mission=mission, human_review=human_review)

    cache = PhaseOutputCache()
    miss_orchestrator = BrandingTeamOrchestrator()
    with patch.object(
        miss_orchestrator, "run_single_phase", side_effect=_run_single_phase_fixture_side_effect
    ):
        miss_result = miss_orchestrator.run(
            mission=mission, human_review=human_review, phase_cache=cache
        )

    assert miss_result == cold_result

    hit_orchestrator = BrandingTeamOrchestrator()
    with patch.object(hit_orchestrator, "run_single_phase") as mock_run_single_phase:
        hit_result = hit_orchestrator.run(
            mission=mission, human_review=human_review, phase_cache=cache
        )

    mock_run_single_phase.assert_not_called()
    assert hit_result == cold_result
