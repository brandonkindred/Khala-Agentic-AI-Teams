"""Orchestrator for the 5-phase branding strategy team.

Phase gate logic:
  Phase 1 → 2: Strategy is validated with stakeholders
  Phase 2 → 3: Messaging is approved and stable
  Phase 3 → 4: Identity system is locked
  Phase 4 → 5: At least one full channel is live

The orchestrator enforces dependency order — nothing in a later phase
should be definable without what came before it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from .agents import (
    BrandCodificationAgent,
    BrandComplianceAgent,
    BrandGuidelinesAgent,
    BrandWikiAgent,
    ChannelActivationAgent,
    CreativeRefinementAgent,
    GovernanceAgent,
    MoodBoardIdeationAgent,
    NarrativeMessagingAgent,
    StrategicCoreAgent,
    VisualIdentityAgent,
)
from .models import (
    BrandBook,
    BrandCheckRequest,
    BrandingMission,
    BrandPhase,
    ChannelActivationOutput,
    GovernanceOutput,
    HumanReview,
    NarrativeMessagingOutput,
    PhaseGate,
    PhaseGateStatus,
    StrategicCoreOutput,
    TeamOutput,
    VisualIdentityOutput,
    WorkflowStatus,
)

if TYPE_CHECKING:
    from .store import BrandingStore


# Phase execution order
_PHASE_ORDER = [
    BrandPhase.STRATEGIC_CORE,
    BrandPhase.NARRATIVE_MESSAGING,
    BrandPhase.VISUAL_IDENTITY,
    BrandPhase.CHANNEL_ACTIVATION,
    BrandPhase.GOVERNANCE,
]


def _phase_index(phase: BrandPhase) -> int:
    """Return the 0-based position of a phase in the pipeline."""
    try:
        return _PHASE_ORDER.index(phase)
    except ValueError:
        return len(_PHASE_ORDER)


def _build_phase_gates(up_to_phase: BrandPhase, approved: bool) -> List[PhaseGate]:
    """Build gate statuses for all phases up to and including the target phase."""
    gates: List[PhaseGate] = []
    target_idx = _phase_index(up_to_phase)
    for i, phase in enumerate(_PHASE_ORDER):
        if i < target_idx:
            gates.append(PhaseGate(phase=phase, status=PhaseGateStatus.APPROVED))
        elif i == target_idx:
            status = PhaseGateStatus.APPROVED if approved else PhaseGateStatus.PENDING_REVIEW
            gates.append(PhaseGate(phase=phase, status=status))
        else:
            gates.append(PhaseGate(phase=phase, status=PhaseGateStatus.NOT_STARTED))
    return gates


# Phase execution order
_PHASE_ORDER = [
    BrandPhase.STRATEGIC_CORE,
    BrandPhase.NARRATIVE_MESSAGING,
    BrandPhase.VISUAL_IDENTITY,
    BrandPhase.CHANNEL_ACTIVATION,
    BrandPhase.GOVERNANCE,
]


def _phase_index(phase: BrandPhase) -> int:
    """Return the 0-based position of a phase in the pipeline."""
    try:
        return _PHASE_ORDER.index(phase)
    except ValueError:
        return len(_PHASE_ORDER)


def _build_phase_gates(up_to_phase: BrandPhase, approved: bool) -> List[PhaseGate]:
    """Build gate statuses for all phases up to and including the target phase."""
    gates: List[PhaseGate] = []
    target_idx = _phase_index(up_to_phase)
    for i, phase in enumerate(_PHASE_ORDER):
        if i < target_idx:
            gates.append(PhaseGate(phase=phase, status=PhaseGateStatus.APPROVED))
        elif i == target_idx:
            status = PhaseGateStatus.APPROVED if approved else PhaseGateStatus.PENDING_REVIEW
            gates.append(PhaseGate(phase=phase, status=status))
        else:
            gates.append(PhaseGate(phase=phase, status=PhaseGateStatus.NOT_STARTED))
    return gates


class BrandingTeamOrchestrator:
    """Coordinates the 5-phase branding pipeline with gate validation."""
    """Coordinates the 5-phase branding pipeline with gate validation."""

    def __init__(self) -> None:
        # Phase agents
        self.strategic_core_agent = StrategicCoreAgent()
        self.narrative_agent = NarrativeMessagingAgent()
        self.visual_identity_agent = VisualIdentityAgent()
        self.channel_activation_agent = ChannelActivationAgent()
        self.governance_agent = GovernanceAgent()
        self.compliance = BrandComplianceAgent()

        # Legacy agents (kept for backward compatibility)
        # Phase agents
        self.strategic_core_agent = StrategicCoreAgent()
        self.narrative_agent = NarrativeMessagingAgent()
        self.visual_identity_agent = VisualIdentityAgent()
        self.channel_activation_agent = ChannelActivationAgent()
        self.governance_agent = GovernanceAgent()
        self.compliance = BrandComplianceAgent()

        # Legacy agents (kept for backward compatibility)
        self.codifier = BrandCodificationAgent()
        self.moodboard = MoodBoardIdeationAgent()
        self.refinement = CreativeRefinementAgent()
        self.guidelines = BrandGuidelinesAgent()
        self.wiki = BrandWikiAgent()

    def run(
        self,
        mission: BrandingMission,
        human_review: HumanReview,
        brand_checks: List[BrandCheckRequest] | None = None,
        store: Optional[BrandingStore] = None,
        client_id: Optional[str] = None,
        brand_id: Optional[str] = None,
        include_market_research: bool = False,
        include_design_assets: bool = False,
        target_phase: Optional[BrandPhase] = None,
    ) -> TeamOutput:
        """Run the branding pipeline up to the target phase (default: all phases).

        Parameters
        ----------
        target_phase : optional
            Stop after completing this phase. ``None`` means run all phases.
        """
        resolved_client_id: Optional[str] = client_id
        if store and brand_id:
            if client_id:
                brand = store.get_brand(client_id, brand_id)
            else:
                brand = None
                for c in store.list_clients():
                    brand = store.get_brand(c.id, brand_id)
                    if brand is not None:
                        resolved_client_id = c.id
                        break
            if brand is not None:
                mission = brand.mission
                if resolved_client_id is None:
                    resolved_client_id = brand.client_id
        else:
            resolved_client_id = client_id

        stop_idx = _phase_index(target_phase) if target_phase else len(_PHASE_ORDER) - 1

        # ---- Phase 1: Strategic Core ----
        strategic_core = self.strategic_core_agent.execute(mission)

        # Legacy bridge
        stop_idx = _phase_index(target_phase) if target_phase else len(_PHASE_ORDER) - 1

        # ---- Phase 1: Strategic Core ----
        strategic_core = self.strategic_core_agent.execute(mission)

        # Legacy bridge
        codification = self.codifier.codify(mission)
        mood_boards = self.moodboard.ideate(mission)
        refinement_plan = self.refinement.build_plan()
        writing_guidelines = self.guidelines.writing_guidelines(mission)
        brand_guidelines = self.guidelines.brand_guidelines(codification)
        design_system = self.guidelines.design_system()
        wiki_backlog = self.wiki.build_wiki_backlog(mission)
        checks = self.compliance.evaluate(brand_checks or [], mission)

        current_phase = BrandPhase.STRATEGIC_CORE

        # ---- Phase 2: Narrative & Messaging ----
        narrative: Optional[NarrativeMessagingOutput] = None
        if stop_idx >= 1:
            narrative = self.narrative_agent.execute(mission, strategic_core)
            current_phase = BrandPhase.NARRATIVE_MESSAGING

        # ---- Phase 3: Visual & Expressive Identity ----
        visual_identity: Optional[VisualIdentityOutput] = None
        if stop_idx >= 2 and narrative is not None:
            visual_identity = self.visual_identity_agent.execute(mission, strategic_core, narrative)
            current_phase = BrandPhase.VISUAL_IDENTITY

        # ---- Phase 4: Experience & Channel Activation ----
        channel_activation: Optional[ChannelActivationOutput] = None
        if stop_idx >= 3 and visual_identity is not None:
            channel_activation = self.channel_activation_agent.execute(
                mission,
                strategic_core,
                narrative,
                visual_identity,  # type: ignore[arg-type]
            )
            current_phase = BrandPhase.CHANNEL_ACTIVATION

        # ---- Phase 5: Governance & Evolution ----
        governance: Optional[GovernanceOutput] = None
        if stop_idx >= 4 and channel_activation is not None:
            governance = self.governance_agent.execute(mission, strategic_core)
            current_phase = BrandPhase.GOVERNANCE
            if human_review.approved:
                current_phase = BrandPhase.COMPLETE

        # ---- Integrations ----
        current_phase = BrandPhase.STRATEGIC_CORE

        # ---- Phase 2: Narrative & Messaging ----
        narrative: Optional[NarrativeMessagingOutput] = None
        if stop_idx >= 1:
            narrative = self.narrative_agent.execute(mission, strategic_core)
            current_phase = BrandPhase.NARRATIVE_MESSAGING

        # ---- Phase 3: Visual & Expressive Identity ----
        visual_identity: Optional[VisualIdentityOutput] = None
        if stop_idx >= 2 and narrative is not None:
            visual_identity = self.visual_identity_agent.execute(mission, strategic_core, narrative)
            current_phase = BrandPhase.VISUAL_IDENTITY

        # ---- Phase 4: Experience & Channel Activation ----
        channel_activation: Optional[ChannelActivationOutput] = None
        if stop_idx >= 3 and visual_identity is not None:
            channel_activation = self.channel_activation_agent.execute(
                mission,
                strategic_core,
                narrative,
                visual_identity,  # type: ignore[arg-type]
            )
            current_phase = BrandPhase.CHANNEL_ACTIVATION

        # ---- Phase 5: Governance & Evolution ----
        governance: Optional[GovernanceOutput] = None
        if stop_idx >= 4 and channel_activation is not None:
            governance = self.governance_agent.execute(mission, strategic_core)
            current_phase = BrandPhase.GOVERNANCE
            if human_review.approved:
                current_phase = BrandPhase.COMPLETE

        # ---- Integrations ----
        competitive_snapshot = None
        if include_market_research:
            try:
                from .adapters.market_research import request_market_research


                competitive_snapshot = request_market_research(mission)
            except Exception:
                competitive_snapshot = None

        design_asset_result = None
        if include_design_assets:
            from .adapters.design_assets import request_design_assets


            design_asset_result = request_design_assets(codification, mission.company_name)

        brand_book = _build_brand_book(
            strategic_core,
            narrative,
            visual_identity,
            channel_activation,
            governance,
            codification,
            writing_guidelines,
            brand_guidelines,
            design_system,
        )

        phase_gates = _build_phase_gates(current_phase, human_review.approved)

        if not human_review.approved:
            status = WorkflowStatus.NEEDS_HUMAN_DECISION
            phase_label = (
                _PHASE_ORDER[min(stop_idx, len(_PHASE_ORDER) - 1)].value.replace("_", " ").title()
            )
            mission_summary = (
                f"Phase '{phase_label}' artifacts are ready for stakeholder review. "
                f"Approval is required before advancing to the next phase."
            )
        elif current_phase == BrandPhase.COMPLETE:
            status = WorkflowStatus.READY_FOR_ROLLOUT
            mission_summary = (
                "All five branding phases complete. The brand system is finalized and "
                "ready for enterprise-wide rollout."
            )
        else:
            # Approved but not all phases are done — signal that the current
            # phase gate passed, but the brand is NOT ready for rollout yet.
            status = WorkflowStatus.NEEDS_HUMAN_DECISION
            phase_label = current_phase.value.replace("_", " ").title()
            mission_summary = f"Phase '{phase_label}' approved. Artifacts are locked and the next phase can begin."

        output = TeamOutput(
            status=status,
            mission_summary=mission_summary,
            current_phase=current_phase,
            phase_gates=phase_gates,
            strategic_core=strategic_core,
            narrative_messaging=narrative,
            visual_identity=visual_identity,
            channel_activation=channel_activation,
            governance=governance,
            codification=codification,
            mood_boards=mood_boards,
            creative_refinement=refinement_plan,
            writing_guidelines=writing_guidelines,
            brand_guidelines=brand_guidelines,
            design_system=design_system,
            wiki_backlog=wiki_backlog,
            brand_checks=checks,
            human_feedback=human_review.feedback
            or (
                "Approved for rollout."
                if human_review.approved
                else "Awaiting approval from brand leadership."
            ),
            competitive_snapshot=competitive_snapshot,
            design_asset_result=design_asset_result,
            brand_book=brand_book,
        )

        if store and brand_id and resolved_client_id:
            store.append_brand_version(resolved_client_id, brand_id, output)

        return output

    def run_phase(
        self,
        mission: BrandingMission,
        phase: BrandPhase,
        human_review: HumanReview,
        brand_checks: List[BrandCheckRequest] | None = None,
        store: Optional[BrandingStore] = None,
        client_id: Optional[str] = None,
        brand_id: Optional[str] = None,
    ) -> TeamOutput:
        """Convenience method: run the pipeline up to (and including) a specific phase."""
        return self.run(
            mission=mission,
            human_review=human_review,
            brand_checks=brand_checks,
            store=store,
            client_id=client_id,
            brand_id=brand_id,
            target_phase=phase,
        )


def _build_brand_book(
    strategic_core: StrategicCoreOutput,
    narrative: Optional[NarrativeMessagingOutput],
    visual_identity: Optional[VisualIdentityOutput],
    channel_activation: Optional[ChannelActivationOutput],
    governance: Optional[GovernanceOutput],
    codification,
    writing_guidelines,
    brand_guidelines: List[str],
    design_system,
) -> BrandBook:
    """Build consolidated brand document from all phase outputs."""
    sections_md: List[str] = []
    sections_data: dict = {}

    # Phase 1 — Strategic Core
    sections_md.append(f"# Brand Purpose\n{strategic_core.brand_purpose}")
    sections_md.append(f"# Mission\n{strategic_core.mission_statement}")
    sections_md.append(f"# Vision\n{strategic_core.vision_statement}")
    sections_md.append(f"# Positioning\n{strategic_core.positioning_statement}")
    sections_md.append(f"# Brand Promise\n{strategic_core.brand_promise}")
    sections_md.append(
        "# Core Values\n"
        + "\n".join(
            f"- **{cv.value}**: {cv.behavioral_definition}" for cv in strategic_core.core_values
        )
    )
    sections_data["positioning"] = strategic_core.positioning_statement
    sections_data["brand_promise"] = strategic_core.brand_promise
    sections_data["core_values"] = [cv.value for cv in strategic_core.core_values]
    sections_data["mission_statement"] = strategic_core.mission_statement
    sections_data["vision_statement"] = strategic_core.vision_statement

    # Phase 2 — Narrative & Messaging
    if narrative:
        sections_md.append(f"# Brand Story\n{narrative.brand_story}")
        sections_md.append(f"# Tagline\n{narrative.tagline}\n\n*{narrative.tagline_rationale}*")
        sections_md.append(
            "# Messaging Pillars\n"
            + "\n".join(
                f"- **{mp.pillar}**: {mp.key_message}" for mp in narrative.messaging_framework
            )
        )
        sections_data["tagline"] = narrative.tagline
        sections_data["brand_story"] = narrative.brand_story

    # Phase 3 — Visual Identity
    if visual_identity:
        sections_md.append(
            "# Color Palette\n"
            + "\n".join(
                f"- **{c.name}** ({c.hex_value}): {c.usage}" for c in visual_identity.color_palette
            )
        )
        sections_md.append(
            "# Typography\n"
            + "\n".join(
                f"- **{t.role}**: {t.font_family}" for t in visual_identity.typography_system
            )
        )
        sections_md.append(
            "# Voice & Tone\n"
            + "\n".join(
                f"- **{vt.context}**: {vt.tone}" for vt in visual_identity.voice_tone_spectrum
            )
        )
        sections_data["color_palette"] = [c.name for c in visual_identity.color_palette]
        sections_data["voice_principles"] = [vt.tone for vt in visual_identity.voice_tone_spectrum]

    # Phase 4 — Channel Activation
    if channel_activation:
        sections_md.append(
            "# Channel Guidelines\n"
            + "\n".join(
                f"## {cg.channel.title()}\n{cg.strategy}"
                for cg in channel_activation.channel_guidelines
            )
        )

    # Phase 5 — Governance
    if governance:
        sections_md.append(f"# Brand Governance\n{governance.ownership_model}")
        sections_md.append(f"# Evolution Framework\n{governance.evolution_framework}")

    # Legacy fallbacks
    sections_md.append("# Brand Guidelines\n" + "\n".join(f"- {g}" for g in brand_guidelines))
    sections_md.append(
        "# Design System Principles\n"
        + "\n".join(f"- {p}" for p in design_system.design_principles)
    )
    sections_data["narrative_pillars"] = codification.narrative_pillars
    sections_data["design_principles"] = design_system.design_principles

    content = "\n\n".join(sections_md)
    return BrandBook(content=content, sections=sections_data)
    """Build consolidated brand document from all phase outputs."""
    sections_md: List[str] = []
    sections_data: dict = {}

    # Phase 1 — Strategic Core
    sections_md.append(f"# Brand Purpose\n{strategic_core.brand_purpose}")
    sections_md.append(f"# Mission\n{strategic_core.mission_statement}")
    sections_md.append(f"# Vision\n{strategic_core.vision_statement}")
    sections_md.append(f"# Positioning\n{strategic_core.positioning_statement}")
    sections_md.append(f"# Brand Promise\n{strategic_core.brand_promise}")
    sections_md.append(
        "# Core Values\n"
        + "\n".join(
            f"- **{cv.value}**: {cv.behavioral_definition}" for cv in strategic_core.core_values
        )
    )
    sections_data["positioning"] = strategic_core.positioning_statement
    sections_data["brand_promise"] = strategic_core.brand_promise
    sections_data["core_values"] = [cv.value for cv in strategic_core.core_values]
    sections_data["mission_statement"] = strategic_core.mission_statement
    sections_data["vision_statement"] = strategic_core.vision_statement

    # Phase 2 — Narrative & Messaging
    if narrative:
        sections_md.append(f"# Brand Story\n{narrative.brand_story}")
        sections_md.append(f"# Tagline\n{narrative.tagline}\n\n*{narrative.tagline_rationale}*")
        sections_md.append(
            "# Messaging Pillars\n"
            + "\n".join(
                f"- **{mp.pillar}**: {mp.key_message}" for mp in narrative.messaging_framework
            )
        )
        sections_data["tagline"] = narrative.tagline
        sections_data["brand_story"] = narrative.brand_story

    # Phase 3 — Visual Identity
    if visual_identity:
        sections_md.append(
            "# Color Palette\n"
            + "\n".join(
                f"- **{c.name}** ({c.hex_value}): {c.usage}" for c in visual_identity.color_palette
            )
        )
        sections_md.append(
            "# Typography\n"
            + "\n".join(
                f"- **{t.role}**: {t.font_family}" for t in visual_identity.typography_system
            )
        )
        sections_md.append(
            "# Voice & Tone\n"
            + "\n".join(
                f"- **{vt.context}**: {vt.tone}" for vt in visual_identity.voice_tone_spectrum
            )
        )
        sections_data["color_palette"] = [c.name for c in visual_identity.color_palette]
        sections_data["voice_principles"] = [vt.tone for vt in visual_identity.voice_tone_spectrum]

    # Phase 4 — Channel Activation
    if channel_activation:
        sections_md.append(
            "# Channel Guidelines\n"
            + "\n".join(
                f"## {cg.channel.title()}\n{cg.strategy}"
                for cg in channel_activation.channel_guidelines
            )
        )

    # Phase 5 — Governance
    if governance:
        sections_md.append(f"# Brand Governance\n{governance.ownership_model}")
        sections_md.append(f"# Evolution Framework\n{governance.evolution_framework}")

    # Legacy fallbacks
    sections_md.append("# Brand Guidelines\n" + "\n".join(f"- {g}" for g in brand_guidelines))
    sections_md.append(
        "# Design System Principles\n"
        + "\n".join(f"- {p}" for p in design_system.design_principles)
    )
    sections_data["narrative_pillars"] = codification.narrative_pillars
    sections_data["design_principles"] = design_system.design_principles

    content = "\n\n".join(sections_md)
    return BrandBook(content=content, sections=sections_data)
