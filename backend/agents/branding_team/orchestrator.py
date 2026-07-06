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
import atexit
import concurrent.futures
import logging
from typing import TYPE_CHECKING, List, Optional

from pydantic import BaseModel, ValidationError

from .agents import BrandComplianceAgent
from .config import env_int
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

logger = logging.getLogger(__name__)


# Phase extraction table: (graph node id, output model, min stop_idx to include).
# Strategic core (min_idx 0) is always extracted; later phases are only pulled
# when the run advanced far enough (``stop_idx``). Order matches PHASE_ORDER.
_PHASE_EXTRACTION = (
    ("phase1_strategic_core", StrategicCoreOutput, 0),
    ("phase2_narrative", NarrativeMessagingOutput, 1),
    ("phase3_visual", VisualIdentityOutput, 2),
    ("phase4_channel", ChannelActivationOutput, 3),
    ("phase5_governance", GovernanceOutput, 4),
)


def _offload_pool_workers() -> int:
    """Worker cap for ``_OFFLOAD_POOL`` (env-tunable, clamped to >= 1).

    Default of 4 avoids serializing concurrent offloaded runs (e.g. multiple
    async Temporal activities on the same loop each calling ``_run_coro``)
    behind a single worker.
    """
    return env_int("BRANDING_RUN_CORO_OFFLOAD_WORKERS", 4, minimum=1)


# Shared pool for the rare case where _run_coro is invoked from a thread that
# already has a running event loop. Reused across calls so we don't spin up
# (and tear down) a fresh executor on every invocation.
_OFFLOAD_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=_offload_pool_workers(), thread_name_prefix="branding-run-coro"
)
# Best-effort cleanup on interpreter exit. Threads in this pool only run
# briefly per offloaded coroutine (see _run_coro), so this should not delay
# shutdown in practice; wait=False avoids blocking exit on a stuck run.
atexit.register(_OFFLOAD_POOL.shutdown, wait=False)


def _run_coro(coro):
    """Run *coro* to completion from synchronous code.

    Uses ``asyncio.run`` when no loop runs in this thread; otherwise drives it on
    a shared worker thread (``_OFFLOAD_POOL``) so we never call ``asyncio.run``
    inside an active loop.

    Preconditions:
        ``coro`` is an un-awaited coroutine/awaitable. When called from a thread
        that already has a running loop, ``coro`` MUST NOT depend on objects
        bound to that loop (e.g. an ``asyncio.Queue`` or lock created on it): the
        offload path runs it on a *new* event loop in another thread, so
        loop-bound objects would fail. The branding coroutines passed here
        (``graph.invoke_async``, ``_gather_integrations``) allocate their own
        primitives, so they are safe.
    Postconditions:
        Returns the coroutine's result or propagates whatever it raises; never
        calls ``asyncio.run`` while a loop is already running in this thread.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return _OFFLOAD_POOL.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


async def _gather_integrations(
    mission: BrandingMission,
    strategic_core: Optional[StrategicCoreOutput],
    include_market_research: bool,
    include_design_assets: bool,
):
    """Run the optional market-research and design-asset integrations concurrently.

    The two integrations are independent — market research is a multi-minute
    poll, design assets a quick request — so overlapping them (rather than the
    prior sequential calls) halves the added latency when both are enabled and
    frees the pipeline worker sooner.

    Preconditions:
        ``mission`` carries the fields the market-research payload needs when
        ``include_market_research`` is set.
    Postconditions:
        Returns ``(competitive_snapshot, design_asset_result)``. Each is None
        when its integration is disabled. Market-research failures are swallowed
        to None (best-effort context); design-asset errors propagate, matching
        the prior sequential behaviour.
    """

    async def _market_research():
        if not include_market_research:
            return None
        try:
            from .adapters.market_research import request_market_research_async

            return await request_market_research_async(mission)
        except Exception:
            return None

    async def _design_assets():
        if not include_design_assets:
            return None
        from .adapters.design_assets import request_design_assets

        # request_design_assets is synchronous; run it off the loop so a future
        # networked implementation never blocks the concurrent market-research poll.
        return await asyncio.to_thread(request_design_assets, strategic_core, mission.company_name)

    return await asyncio.gather(_market_research(), _design_assets())


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
        """
        # ---- Resolve brand from store if applicable ----
        mission, resolved_client_id = self._resolve_mission(mission, store, client_id, brand_id)

        stop_idx = phase_index(target_phase) if target_phase else len(PHASE_ORDER) - 1

        # ---- Build and invoke the graph ----
        graph = build_branding_graph(target_phase=target_phase)
        task = (
            f"Create a comprehensive brand strategy for the following company.\n\n"
            f"Branding Mission:\n{serialize_mission(mission)}"
        )
        result = _run_coro(graph.invoke_async(task))

        # ---- Extract phase outputs from graph node results (table-driven) ----
        strategic_core, narrative, visual_identity, channel_activation, governance = [
            self._extract_phase_output(result, node_id, model_cls) if stop_idx >= min_idx else None
            for node_id, model_cls, min_idx in _PHASE_EXTRACTION
        ]

        # ---- Determine current phase ----
        current_phase = self._determine_current_phase(
            narrative, visual_identity, channel_activation, governance, human_review.approved
        )

        # ---- Run compliance checks (outside the graph) ----
        checks = self.compliance.evaluate(brand_checks or [], mission)

        # ---- Integrations (run concurrently; see _gather_integrations) ----
        competitive_snapshot, design_asset_result = _run_coro(
            _gather_integrations(
                mission, strategic_core, include_market_research, include_design_assets
            )
        )

        # ---- Build brand book ----
        brand_book = _build_brand_book(
            strategic_core, narrative, visual_identity, channel_activation, governance
        )

        # ---- Phase gates ----
        phase_gates = _build_phase_gates(current_phase, human_review.approved)

        # ---- Status determination ----
        status, mission_summary = self._build_status_summary(human_review, current_phase, stop_idx)

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
    def _resolve_mission(
        mission: BrandingMission,
        store: Optional["BrandingStore"],
        client_id: Optional[str],
        brand_id: Optional[str],
    ) -> tuple[BrandingMission, Optional[str]]:
        """Resolve the mission (and client id) from the store when a brand is given.

        Preconditions:
            When ``store`` and ``brand_id`` are both set, ``store`` exposes
            ``get_brand``/``get_brand_by_id``.
        Postconditions:
            Returns ``(mission, resolved_client_id)``. When a stored brand is
            found its mission replaces the passed one and the client id is filled
            in; otherwise the inputs pass through unchanged.
        """
        resolved_client_id: Optional[str] = client_id
        if store and brand_id:
            if client_id:
                brand = store.get_brand(client_id, brand_id)
            else:
                # One indexed lookup instead of scanning every client's brands.
                found = store.get_brand_by_id(brand_id)
                if found is not None:
                    resolved_client_id, brand = found
                else:
                    brand = None
            if brand is not None:
                mission = brand.mission
                if resolved_client_id is None:
                    resolved_client_id = brand.client_id
        return mission, resolved_client_id

    @staticmethod
    def _determine_current_phase(
        narrative: Optional[NarrativeMessagingOutput],
        visual_identity: Optional[VisualIdentityOutput],
        channel_activation: Optional[ChannelActivationOutput],
        governance: Optional[GovernanceOutput],
        approved: bool,
    ) -> BrandPhase:
        """Return the furthest phase reached, promoting to COMPLETE when approved.

        Postconditions:
            Returns the phase of the last non-None output; when governance is
            present and ``approved`` is True, returns ``BrandPhase.COMPLETE``.
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
    def _build_status_summary(
        human_review: HumanReview,
        current_phase: BrandPhase,
        stop_idx: int,
    ) -> tuple[WorkflowStatus, str]:
        """Derive the workflow status and human-facing summary line.

        Postconditions:
            Returns ``(status, mission_summary)``: NEEDS_HUMAN_DECISION with a
            review prompt when unapproved, READY_FOR_ROLLOUT when the run is
            complete, else NEEDS_HUMAN_DECISION with a phase-approved summary.
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
                            text = _collect_message_text(last.message)
                            parsed = _parse_model_from_text(text, model_class)
                            if parsed is not None:
                                return parsed
        except Exception:
            # Malformed JSON / schema mismatch already returns None from
            # _parse_model_from_text; reaching here means an unexpected error
            # walking the graph result. Log it rather than swallow silently.
            logger.warning(
                "Unexpected error extracting phase output for node %s; using default",
                node_id,
                exc_info=True,
            )
        return model_class()


def _collect_message_text(message: dict) -> str:
    """Join all text blocks of a Strands agent ``message`` into one string.

    Uses ``"".join`` over collected fragments rather than repeated ``+=`` so
    assembly is linear, not quadratic, in the number/size of content blocks.

    Preconditions:
        ``message`` is a Strands message mapping (``.get("content")`` yields a
        list of content blocks). Passing a non-mapping is a caller bug.
    Postconditions:
        Returns the concatenation of every block's text (dict ``text`` key or
        ``.text`` attribute); returns ``""`` when there is no text content.
    """
    parts: List[str] = []
    for block in message.get("content", []):
        if isinstance(block, dict) and block.get("text"):
            parts.append(block["text"])
        elif getattr(block, "text", None):
            parts.append(block.text)
    return "".join(parts)


def _parse_model_from_text(text: str, model_class: type[BaseModel]) -> Optional[BaseModel]:
    """Best-effort parse of ``text`` into ``model_class``; None on failure.

    Tries the whole string first, then falls back to the outermost
    ``{ ... }`` slice for replies that wrap JSON in prose.
    """
    if not text:
        return None
    # ValidationError covers both malformed JSON and schema mismatch from
    # model_validate_json; anything else is a genuine bug and should surface.
    try:
        return model_class.model_validate_json(text)
    except ValidationError:
        pass
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return model_class.model_validate_json(text[start:end])
        except ValidationError:
            return None
    return None


def _bullets(title: str, items, fmt=lambda x: x) -> str:
    """Render a markdown section: a ``# title`` heading over a bullet list.

    Preconditions:
        ``items`` is iterable; ``fmt`` maps one item to its bullet body text
        (the ``- `` prefix is added here).
    Postconditions:
        Returns ``"# {title}\n" + "\n".join("- " + fmt(x) for x in items)``.
    """
    body = "\n".join(f"- {fmt(x)}" for x in items)
    return f"# {title}\n{body}"


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
            _bullets(
                "Core Values",
                strategic_core.core_values,
                lambda cv: f"**{cv.value}**: {cv.behavioral_definition}",
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
            _bullets(
                "Messaging Pillars",
                narrative.messaging_framework,
                lambda mp: f"**{mp.pillar}**: {mp.key_message}",
            )
        )
        sections_data["tagline"] = narrative.tagline
        sections_data["brand_story"] = narrative.brand_story

    if visual_identity:
        sections_md.append(
            _bullets(
                "Color Palette",
                visual_identity.color_palette,
                lambda c: f"**{c.name}** ({c.hex_value}): {c.usage}",
            )
        )
        sections_md.append(
            _bullets(
                "Typography",
                visual_identity.typography_system,
                lambda t: f"**{t.role}**: {t.font_family}",
            )
        )
        sections_md.append(
            _bullets(
                "Voice & Tone",
                visual_identity.voice_tone_spectrum,
                lambda vt: f"**{vt.context}**: {vt.tone}",
            )
        )
        sections_data["color_palette"] = [c.name for c in visual_identity.color_palette]
        sections_data["voice_principles"] = [vt.tone for vt in visual_identity.voice_tone_spectrum]
        if visual_identity.design_system:
            sections_md.append(
                _bullets(
                    "Design System Principles", visual_identity.design_system.design_principles
                )
            )
            sections_data["design_principles"] = visual_identity.design_system.design_principles

    if channel_activation:
        # Channel guidelines use ``##`` sub-headings, not bullets, so build inline.
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
            sections_md.append(_bullets("Brand Guidelines", governance.brand_guidelines))

    content = "\n\n".join(sections_md)
    return BrandBook(content=content, sections=sections_data)
