"""Orchestrator for the 5-phase branding strategy team.

Thin wrapper that builds the top-level Strands SDK graph, invokes it with the
serialised ``BrandingMission``, runs brand-compliance checks separately, and
assembles the final ``TeamOutput``.

Phase gate logic:
  Phase 1 → 2: Strategy is validated with stakeholders
  Phase 2 → 3: Messaging is approved and stable
  Phase 3 → 4: Identity system is locked
  Phase 4 → 5: At least one full channel is live
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, List, Optional

from .agents import BrandComplianceAgent
from .graphs.shared import PHASE_ORDER, phase_index, serialize_mission
from .graphs.top_level import build_branding_graph
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


def _build_phase_gates(up_to_phase: BrandPhase, approved: bool) -> List[PhaseGate]:
    """Build gate statuses for all phases up to and including the target phase."""
    gates: List[PhaseGate] = []
    target_idx = phase_index(up_to_phase)
    for i, phase in enumerate(PHASE_ORDER):
        if i < target_idx:
            gates.append(PhaseGate(phase=phase, status=PhaseGateStatus.APPROVED))
        elif i == target_idx:
            status = PhaseGateStatus.APPROVED if approved else PhaseGateStatus.PENDING_REVIEW
            gates.append(PhaseGate(phase=phase, status=status))
        else:
            gates.append(PhaseGate(phase=phase, status=PhaseGateStatus.NOT_STARTED))
    return gates


class BrandingTeamOrchestrator:
    """Coordinates the 5-phase branding pipeline via Strands SDK graphs."""

    def __init__(self) -> None:
        self.compliance = BrandComplianceAgent()

    def run(
        self,
        mission: BrandingMission,
        human_review: HumanReview,
        brand_checks: List[BrandCheckRequest] | None = None,
        store: Optional["BrandingStore"] = None,
        client_id: Optional[str] = None,
        brand_id: Optional[str] = None,
        include_market_research: bool = False,
        include_design_assets: bool = False,
        target_phase: Optional[BrandPhase] = None,
    ) -> TeamOutput:
        """Run the branding pipeline up to *target_phase* (default: all phases).

        The pipeline is built as a Strands SDK ``Graph`` whose nodes are
        per-phase sub-graphs and swarms.  Brand-compliance checks run outside
        the graph because their inputs come from the API request.

        Preconditions:
            - ``mission`` is a valid ``BrandingMission`` and ``human_review`` a
              ``HumanReview``; a brand identity is resolvable either from
              ``mission`` or from ``store`` + ``client_id``/``brand_id`` (see
              :meth:`_resolve_brand_mission`).
            - ``target_phase`` is a ``BrandPhase`` or ``None`` (``None`` ⇒ run
              every phase in ``PHASE_ORDER``).
        Postconditions:
            - Returns a ``TeamOutput`` carrying the phases up to ``target_phase``
              (see :meth:`_extract_phases`), the derived ``current_phase`` (see
              :meth:`_determine_current_phase`), and any brand-compliance results;
              the individual helper contracts hold.
        """
        # Resolve mission/owner from the store, then run and interpret the graph.
        # Each step below is a small, separately-testable helper so this method
        # reads as the pipeline outline rather than its mechanics.
        mission, resolved_client_id = self._resolve_brand_mission(
            store, client_id, brand_id, mission
        )
        stop_idx = phase_index(target_phase) if target_phase else len(PHASE_ORDER) - 1

        result = self._invoke_graph(mission, target_phase)
        (
            strategic_core,
            narrative,
            visual_identity,
            channel_activation,
            governance,
        ) = self._extract_phases(result, stop_idx)
        current_phase = self._determine_current_phase(
            narrative, visual_identity, channel_activation, governance, human_review.approved
        )

        # Compliance checks run outside the graph (their inputs come from the request).
        checks = self.compliance.evaluate(brand_checks or [], mission)
        competitive_snapshot, design_asset_result = self._run_integrations(
            mission, strategic_core, include_market_research, include_design_assets
        )
        brand_book = _build_brand_book(
            strategic_core, narrative, visual_identity, channel_activation, governance
        )
        phase_gates = _build_phase_gates(current_phase, human_review.approved)
        status, mission_summary = self._determine_status(human_review, current_phase, stop_idx)

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

    @staticmethod
    def _resolve_brand_mission(store, client_id, brand_id, mission):
        """Resolve the brand's mission and owning client from the store.

        Preconditions:
            - ``mission`` is the request-supplied mission (used as-is when no
              stored brand is found).
        Postconditions:
            - Returns ``(mission, resolved_client_id)``. When ``store`` and
              ``brand_id`` are set and the brand exists, ``mission`` is the
              stored mission and ``resolved_client_id`` is its owner; otherwise
              ``mission`` is unchanged and ``resolved_client_id`` is ``client_id``.
        """
        resolved_client_id = client_id
        if store and brand_id:
            if client_id:
                brand = store.get_brand(client_id, brand_id)
            else:
                # Brand ids are globally unique, so resolve in one query rather
                # than scanning every client (the former list_clients → get_brand
                # N+1, which issued one query per client).
                brand = store.get_brand_by_id(brand_id)
                if brand is not None:
                    resolved_client_id = brand.client_id
            if brand is not None:
                mission = brand.mission
                if resolved_client_id is None:
                    resolved_client_id = brand.client_id
        return mission, resolved_client_id

    @staticmethod
    def _invoke_graph(mission, target_phase):
        """Build the per-phase Strands graph and invoke it, returning the result.

        Runs the async graph on a worker thread when already inside a running
        event loop (so a nested ``asyncio.run`` does not clash), else directly.

        Preconditions:
            - ``mission`` is the (possibly store-resolved) mission to render into
              the graph task prompt; ``target_phase`` is the phase to stop at, or
              ``None`` to run the full pipeline.
        Postconditions:
            - Returns the raw multi-agent graph result for downstream extraction.
        """
        graph = build_branding_graph(target_phase=target_phase)
        task = (
            f"Create a comprehensive brand strategy for the following company.\n\n"
            f"Branding Mission:\n{serialize_mission(mission)}"
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(lambda: asyncio.run(graph.invoke_async(task))).result()
        return asyncio.run(graph.invoke_async(task))

    def _extract_phases(self, result, stop_idx):
        """Extract the up-to-five phase outputs from the graph result.

        Preconditions:
            - ``result`` is the object returned by :meth:`_invoke_graph`;
              ``stop_idx`` is the index (0-4) of the last phase to extract.
        Postconditions:
            - ``strategic_core`` is always extracted; each later phase is
              extracted only when ``stop_idx`` reached it, else ``None``.
            - Returns the five outputs in pipeline order.
        """
        strategic_core = self._extract_phase_output(
            result, "phase1_strategic_core", StrategicCoreOutput
        )
        narrative = (
            self._extract_phase_output(result, "phase2_narrative", NarrativeMessagingOutput)
            if stop_idx >= 1
            else None
        )
        visual_identity = (
            self._extract_phase_output(result, "phase3_visual", VisualIdentityOutput)
            if stop_idx >= 2
            else None
        )
        channel_activation = (
            self._extract_phase_output(result, "phase4_channel", ChannelActivationOutput)
            if stop_idx >= 3
            else None
        )
        governance = (
            self._extract_phase_output(result, "phase5_governance", GovernanceOutput)
            if stop_idx >= 4
            else None
        )
        return strategic_core, narrative, visual_identity, channel_activation, governance

    @staticmethod
    def _determine_current_phase(
        narrative, visual_identity, channel_activation, governance, approved
    ):
        """Map the highest produced phase output to a :class:`BrandPhase`.

        Preconditions:
            - Each phase argument is its extracted output or ``None`` when that
              phase did not run; ``approved`` is the human-review decision.
        Postconditions:
            - Returns ``COMPLETE`` only when governance was produced *and*
              ``approved`` is true; otherwise the highest produced phase.
        """
        current_phase = BrandPhase.STRATEGIC_CORE
        if narrative is not None:
            current_phase = BrandPhase.NARRATIVE_MESSAGING
        if visual_identity is not None:
            current_phase = BrandPhase.VISUAL_IDENTITY
        if channel_activation is not None:
            current_phase = BrandPhase.CHANNEL_ACTIVATION
        if governance is not None:
            current_phase = BrandPhase.GOVERNANCE
            if approved:
                current_phase = BrandPhase.COMPLETE
        return current_phase

    @staticmethod
    def _run_integrations(mission, strategic_core, include_market_research, include_design_assets):
        """Run the optional market-research and design-asset integrations.

        Preconditions:
            - ``strategic_core`` is the phase-1 output (the design-asset input);
              ``include_market_research``/``include_design_assets`` are the
              request flags gating each integration.
        Postconditions:
            - Returns ``(competitive_snapshot, design_asset_result)``; each is
              ``None`` when its integration is disabled. Market-research failures
              degrade to ``None`` (the pipeline must not fail on an integration).
        """
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

            design_asset_result = request_design_assets(strategic_core, mission.company_name)
        return competitive_snapshot, design_asset_result

    @staticmethod
    def _determine_status(human_review, current_phase, stop_idx):
        """Derive the workflow status and human-facing summary for the output.

        Preconditions:
            - ``current_phase`` is the resolved :class:`BrandPhase`; ``stop_idx``
              is the index of the last phase that ran; ``human_review`` carries
              the approval decision.
        Postconditions:
            - Returns ``(WorkflowStatus, summary)`` for one of three cases:
              review pending (not approved), ready for rollout (complete), or
              approved mid-pipeline (next phase unlocked).
        """
        if not human_review.approved:
            phase_label = (
                PHASE_ORDER[min(stop_idx, len(PHASE_ORDER) - 1)].value.replace("_", " ").title()
            )
            return (
                WorkflowStatus.NEEDS_HUMAN_DECISION,
                f"Phase '{phase_label}' artifacts are ready for stakeholder review. "
                f"Approval is required before advancing to the next phase.",
            )
        if current_phase == BrandPhase.COMPLETE:
            return (
                WorkflowStatus.READY_FOR_ROLLOUT,
                "All five branding phases complete. The brand system is finalized and "
                "ready for enterprise-wide rollout.",
            )
        phase_label = current_phase.value.replace("_", " ").title()
        return (
            WorkflowStatus.NEEDS_HUMAN_DECISION,
            f"Phase '{phase_label}' approved. Artifacts are locked and the next phase can begin.",
        )

    def run_phase(
        self,
        mission: BrandingMission,
        phase: BrandPhase,
        human_review: HumanReview,
        brand_checks: List[BrandCheckRequest] | None = None,
        store: Optional["BrandingStore"] = None,
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

    @staticmethod
    def _extract_phase_output(result, node_id: str, model_class):
        """Best-effort extraction of a phase output from graph results.

        The graph node results contain ``AgentResult`` or ``MultiAgentResult``
        objects.  We attempt to parse the agent's last text output as the
        structured model.  If parsing fails, return a default instance.
        """
        try:
            if hasattr(result, "result") and hasattr(result.result, "get"):
                node_result = result.result.get(node_id)
                if node_result and hasattr(node_result, "result"):
                    agent_results = node_result.get_agent_results()
                    if agent_results:
                        last = agent_results[-1]
                        if hasattr(last, "message") and last.message:
                            text = ""
                            for block in last.message.get("content", []):
                                if isinstance(block, dict) and block.get("text"):
                                    text += block["text"]
                                elif hasattr(block, "text"):
                                    text += block.text
                            if text:
                                start = text.find("{")
                                end = text.rfind("}") + 1
                                if start >= 0 and end > start:
                                    return model_class.model_validate_json(text[start:end])
        except Exception:
            pass
        return model_class()


def _build_brand_book(
    strategic_core: Optional[StrategicCoreOutput],
    narrative: Optional[NarrativeMessagingOutput],
    visual_identity: Optional[VisualIdentityOutput],
    channel_activation: Optional[ChannelActivationOutput],
    governance: Optional[GovernanceOutput],
) -> BrandBook:
    """Build consolidated brand document from all phase outputs."""
    sections_md: List[str] = []
    sections_data: dict = {}

    if strategic_core:
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
        if visual_identity.design_system:
            sections_md.append(
                "# Design System Principles\n"
                + "\n".join(f"- {p}" for p in visual_identity.design_system.design_principles)
            )
            sections_data["design_principles"] = visual_identity.design_system.design_principles

    if channel_activation:
        sections_md.append(
            "# Channel Guidelines\n"
            + "\n".join(
                f"## {cg.channel.title()}\n{cg.strategy}"
                for cg in channel_activation.channel_guidelines
            )
        )

    if governance:
        sections_md.append(f"# Brand Governance\n{governance.ownership_model}")
        sections_md.append(f"# Evolution Framework\n{governance.evolution_framework}")
        if governance.brand_guidelines:
            sections_md.append(
                "# Brand Guidelines\n" + "\n".join(f"- {g}" for g in governance.brand_guidelines)
            )

    content = "\n\n".join(sections_md)
    return BrandBook(content=content, sections=sections_data)
