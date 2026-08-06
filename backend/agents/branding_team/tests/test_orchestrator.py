"""Tests for the branding team orchestrator.

Since all agents are now LLM-backed strands.Agent instances running inside
Strands SDK Graph/Swarm orchestration, we patch
``branding_team.orchestrator.build_branding_graph`` so the returned graph's
``invoke_async`` yields a canned result, and verify the orchestrator
correctly assembles ``TeamOutput`` from it.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from branding_team import (
    BrandingTeamOrchestrator,
    BrandPhase,
    HumanReview,
    WorkflowStatus,
)
from branding_team.models import (
    AudienceMessageMapOutput,
    AudienceSegment,
    AudienceSegmentsOutput,
    Brand,
    BrandArchetypeOutput,
    BrandArchetypesOutput,
    BrandCheckRequest,
    BrandDiscoveryAuditOutput,
    BrandHealthKPI,
    BrandStatus,
    BrandStoryOutput,
    ChannelActivationOutput,
    ChannelGuideline,
    ColorEntry,
    CompetitiveSnapshot,
    CoreValue,
    CoreValuesOutput,
    CreativeRefinementDecision,
    DesignSystemDefinition,
    DifferentiationPillarOutput,
    DifferentiationPillarsOutput,
    ElevatorPitchOutput,
    GovernanceOutput,
    MessagingFrameworkOutput,
    MessagingPillarOutput,
    MoodBoardConcept,
    NarrativeMessagingOutput,
    PersonaProfileOutput,
    PersonaProfilesOutput,
    PositioningOutput,
    PurposeVisionOutput,
    StrategicCoreOutput,
    TaglineOutput,
    TypographySpec,
    VisualIdentityOutput,
    WikiEntry,
    WritingGuidelines,
    WritingGuidelinesBody,
    WritingGuidelinesOutput,
)
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


def test_extract_phase_output_uses_structured_output_when_present() -> None:
    """Agents built with ``structured_output=`` populate ``AgentResult.structured_output``
    rather than the message's text blocks; extraction must check that field before
    falling back to text or it silently discards the agent's real output.

    Calls the private ``_extract_phase_output`` helper directly: the public
    ``run``/``run_phase`` APIs always go through a full graph invoke, which
    cannot isolate the structured-output-vs-text branch without rebuilding the
    entire Strands result shape around this one field.
    """
    agent_result = MagicMock()
    agent_result.message = {"content": []}
    agent_result.structured_output = PositioningOutput(
        positioning_statement=(
            "For enterprise product leaders who need cohesive digital experiences, "
            "Northstar Labs is the hands-on partner that delivers clarity."
        ),
        brand_promise="Every customer touchpoint will feel cohesive, useful, and unmistakably aligned.",
    )

    node_result = MagicMock()
    node_result.get_agent_results.return_value = [agent_result]

    mock_result = MagicMock()
    mock_result.result = {"phase1_strategic_core": node_result}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase1_strategic_core", StrategicCoreOutput
    )

    assert degraded is False
    assert isinstance(output, StrategicCoreOutput)
    assert output.positioning_statement == agent_result.structured_output.positioning_statement
    assert output.brand_promise == agent_result.structured_output.brand_promise


def _phase1_leaf_node(structured_output) -> MagicMock:
    """A mock NodeResult for a single Phase 1 fan-out/fan-in leaf agent."""
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
    positioning_leaf = _phase1_leaf_node(
        PositioningOutput(
            positioning_statement="For enterprise leaders who need clarity, we deliver it.",
            brand_promise="Every touchpoint feels cohesive.",
        )
    )
    nested_results = {
        "discovery_auditor": _phase1_leaf_node(
            BrandDiscoveryAuditOutput(
                current_brand_perception="Seen as reliable but generic.",
                market_position="Mid-market challenger.",
                strengths=["Delivery speed"],
                weaknesses=["Low brand recall"],
                opportunities=["Category consolidating"],
                threats=["Bigger competitors out-spending"],
                stakeholder_insights=["Sales wants sharper differentiation"],
            )
        ),
        "purpose_vision_writer": _phase1_leaf_node(
            PurposeVisionOutput(
                brand_purpose="Why we exist.",
                mission_statement="What we do for customers.",
                vision_statement="Where the brand is headed.",
            )
        ),
        "values_articulator": _phase1_leaf_node(
            CoreValuesOutput(
                core_values=[
                    CoreValue(value="Clarity"),
                    CoreValue(value="Trust"),
                    CoreValue(value="Momentum"),
                ]
            )
        ),
        "audience_segmenter": _phase1_leaf_node(
            AudienceSegmentsOutput(
                target_audience_segments=[AudienceSegment(name="Enterprise product leaders")]
            )
        ),
        "differentiation_mapper": _phase1_leaf_node(
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
        "positioning_synthesizer": positioning_leaf,
    }

    inner_multi_result = MagicMock()
    inner_multi_result.results = nested_results

    node_result = MagicMock()
    node_result.result = inner_multi_result
    # Matches real Strands semantics: get_agent_results() on the outer node
    # flattens to every nested agent, in completion order (synthesizer last).
    node_result.get_agent_results.return_value = [
        node.get_agent_results.return_value[0] for node in nested_results.values()
    ]

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


def test_extract_phase_output_merges_every_phase2_fragment() -> None:
    """Phase 2 wraps six sequential Graph agents as one top-level node;
    get_agent_results()[-1] only ever sees VoicePrinciplesDrafter, so the five
    upstream agents' fragments must be merged in separately."""
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
    voice_leaf = _phase1_leaf_node(
        WritingGuidelinesOutput(
            **_story,
            brand_archetypes=_archetypes,
            tagline="Ship brand with the product",
            tagline_rationale="Ties cohesion to shipping speed.",
            elevator_pitches=_pitches,
            messaging_framework=_pillars,
            audience_message_maps=_maps,
            persona_profiles=_personas,
            writing_guidelines=WritingGuidelinesBody(
                voice_principles=["Confident", "Human", "Concrete"],
                style_dos=["Lead with outcome", "Use active voice", "Name the audience"],
                style_donts=["Empty superlatives", "Bury the offer", "Mix slang with claims"],
                editorial_quality_bar=["States who it's for", "Cites proof", "Matches tone"],
            ),
        )
    )
    nested_results = {
        "Storyteller": _phase1_leaf_node(BrandStoryOutput(**_story)),
        "ArchetypeAnalyst": _phase1_leaf_node(
            BrandArchetypesOutput(**_story, brand_archetypes=_archetypes)
        ),
        "TaglineWriter": _phase1_leaf_node(
            TaglineOutput(
                **_story,
                brand_archetypes=_archetypes,
                tagline="Ship brand with the product",
                tagline_rationale="Ties cohesion to shipping speed.",
                elevator_pitches=_pitches,
            )
        ),
        "MessageMapper": _phase1_leaf_node(
            MessagingFrameworkOutput(
                **_story,
                brand_archetypes=_archetypes,
                tagline="Ship brand with the product",
                tagline_rationale="Ties cohesion to shipping speed.",
                elevator_pitches=_pitches,
                messaging_framework=_pillars,
                audience_message_maps=_maps,
            )
        ),
        "PersonaBuilder": _phase1_leaf_node(
            PersonaProfilesOutput(
                **_story,
                brand_archetypes=_archetypes,
                tagline="Ship brand with the product",
                tagline_rationale="Ties cohesion to shipping speed.",
                elevator_pitches=_pitches,
                messaging_framework=_pillars,
                audience_message_maps=_maps,
                persona_profiles=_personas,
            )
        ),
        "VoicePrinciplesDrafter": voice_leaf,
    }

    inner_multi_result = MagicMock()
    inner_multi_result.results = nested_results

    node_result = MagicMock()
    node_result.result = inner_multi_result
    node_result.get_agent_results.return_value = [
        node.get_agent_results.return_value[0] for node in nested_results.values()
    ]

    mock_result = MagicMock()
    mock_result.result = {"phase2_narrative": node_result}

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


def test_extract_phase_output_phase2_prefers_upstream_owned_fields() -> None:
    """Later carry-forward dumps must not overwrite earlier specialists' fields.

    A real LLM may rewrite inherited ``brand_story`` when Voice emits its
    cumulative payload; merge keeps Storyteller's authoritative value.
    """
    _story = dict(
        brand_story="Authoritative origin from Storyteller.",
        hero_narrative="Authoritative hero from Storyteller.",
        boilerplate_variants=["short bio", "medium bio", "long bio"],
    )
    _rewritten = dict(
        brand_story="Rewritten by VoicePrinciplesDrafter.",
        hero_narrative="Rewritten hero.",
        boilerplate_variants=["v-short", "v-medium", "v-long"],
    )
    _archetypes = [
        BrandArchetypeOutput(
            archetype="The Creator",
            rationale="Inventive.",
            personality_traits=["Imaginative", "Original"],
        )
    ]
    _pitches = [
        ElevatorPitchOutput(tier="5-second", pitch="a"),
        ElevatorPitchOutput(tier="30-second", pitch="b"),
        ElevatorPitchOutput(tier="2-minute", pitch="c"),
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
    guidelines = WritingGuidelinesBody(
        voice_principles=["Confident", "Human", "Concrete"],
        style_dos=["Lead with outcome", "Use active voice", "Name the audience"],
        style_donts=["Empty superlatives", "Bury the offer", "Mix slang with claims"],
        editorial_quality_bar=["States who it's for", "Cites proof", "Matches tone"],
    )
    nested_results = {
        "Storyteller": _phase1_leaf_node(BrandStoryOutput(**_story)),
        "ArchetypeAnalyst": _phase1_leaf_node(
            BrandArchetypesOutput(**_rewritten, brand_archetypes=_archetypes)
        ),
        "TaglineWriter": _phase1_leaf_node(
            TaglineOutput(
                **_rewritten,
                brand_archetypes=_archetypes,
                tagline="Ship brand with the product",
                tagline_rationale="Ties cohesion to shipping speed.",
                elevator_pitches=_pitches,
            )
        ),
        "MessageMapper": _phase1_leaf_node(
            MessagingFrameworkOutput(
                **_rewritten,
                brand_archetypes=_archetypes,
                tagline="Ship brand with the product",
                tagline_rationale="Ties cohesion to shipping speed.",
                elevator_pitches=_pitches,
                messaging_framework=_pillars,
                audience_message_maps=_maps,
            )
        ),
        "PersonaBuilder": _phase1_leaf_node(
            PersonaProfilesOutput(
                **_rewritten,
                brand_archetypes=_archetypes,
                tagline="Ship brand with the product",
                tagline_rationale="Ties cohesion to shipping speed.",
                elevator_pitches=_pitches,
                messaging_framework=_pillars,
                audience_message_maps=_maps,
                persona_profiles=_personas,
            )
        ),
        "VoicePrinciplesDrafter": _phase1_leaf_node(
            WritingGuidelinesOutput(
                **_rewritten,
                brand_archetypes=_archetypes,
                tagline="Ship brand with the product",
                tagline_rationale="Ties cohesion to shipping speed.",
                elevator_pitches=_pitches,
                messaging_framework=_pillars,
                audience_message_maps=_maps,
                persona_profiles=_personas,
                writing_guidelines=guidelines,
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
    mock_result = MagicMock()
    mock_result.result = {"phase2_narrative": node_result}

    output, degraded = BrandingTeamOrchestrator._extract_phase_output(
        mock_result, "phase2_narrative", NarrativeMessagingOutput
    )

    assert degraded is False
    assert output.brand_story == "Authoritative origin from Storyteller."
    assert output.hero_narrative == "Authoritative hero from Storyteller."
    assert output.boilerplate_variants == ["short bio", "medium bio", "long bio"]
    assert [a.archetype for a in output.brand_archetypes] == ["The Creator"]
    assert output.tagline == "Ship brand with the product"
    assert output.writing_guidelines.voice_principles == ["Confident", "Human", "Concrete"]


def test_extract_phase_output_rejects_incomplete_phase2_fragments() -> None:
    """A Phase 2 run that only produced Storyteller must not validate as a
    complete NarrativeMessagingOutput via field defaults (the structured_output
    + Swarm failure mode where handoff never fired)."""
    nested_results = {
        "Storyteller": _phase1_leaf_node(
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


def test_extract_phase_output_falls_back_when_not_phase1_shaped() -> None:
    """A node whose nested result isn't Phase 1/2's known node-id set (e.g. every
    other phase) must fall through to the existing single-agent-result logic
    unchanged — fragment merges are additive, never a regression."""
    agent_result = MagicMock()
    agent_result.message = {"content": []}
    agent_result.structured_output = PositioningOutput(
        positioning_statement="Fallback statement.",
        brand_promise="Fallback promise.",
    )

    inner_multi_result = MagicMock()
    inner_multi_result.results = {"some_other_phase_node": MagicMock()}

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
    assert output.positioning_statement == "Fallback statement."
    assert output.brand_promise == "Fallback promise."


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
