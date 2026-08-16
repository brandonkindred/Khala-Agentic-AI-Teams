"""Orchestrator for the 5-phase branding strategy team.

``BrandingTeamOrchestrator`` resolves the mission from ``BrandingStore`` when
a client/brand pair is supplied, builds the top-level Strands SDK graph,
invokes it with the serialised ``BrandingMission``, optionally gathers sibling
integrations (market research + design assets) concurrently, runs brand-
compliance checks separately, assembles the final ``TeamOutput``, and appends
a brand version when a store is attached. If that append returns ``None``
(brand row deleted between resolve and finalize), ``run`` raises
``BrandVersionAppendConflict`` so callers can mark the run failed instead of
reporting success without persistence.

Phase gate logic:
  Phase 1 → 2: Strategy is validated with stakeholders
  Phase 2 → 3: Messaging is approved and stable
  Phase 3 → 4: Identity system is locked
  Phase 4 → 5: At least one full channel is live
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Callable, Iterable, List, NamedTuple, Optional

from pydantic import BaseModel, ValidationError
from strands.multiagent.graph import GraphBuilder

from branding_team.shared.coro_runner import run_coroutine
from branding_team.shared.json_recovery import recover_json_object

from .agents import BrandComplianceAgent
from .graphs.phase1_strategic_core import build_phase1_graph
from .graphs.phase2_narrative import build_phase2_graph
from .graphs.phase3_visual import build_phase3_graph
from .graphs.phase4_channel import build_phase4_graph
from .graphs.phase5_governance import build_phase5_graph
from .graphs.shared import PHASE_ORDER, phase_index, serialize_mission
from .graphs.top_level import (
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    DEFAULT_NODE_TIMEOUT_SECONDS,
    build_branding_graph,
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

logger = logging.getLogger(__name__)


# Phase 1 fan-out node id -> the StrategicCoreOutput key its structured_output
# nests under, or None to merge its fields in flat. Every value except
# discovery_auditor's matches a StrategicCoreOutput field name 1:1 (see
# agents.py/models.py). discovery_auditor alone nests: StrategicCoreOutput.
# brand_discovery and discovery_auditor's own structured_output= are both
# typed BrandDiscoveryAudit (see models.py), so no conversion is needed here.
_PHASE1_NODE_MERGE: dict[str, Optional[str]] = {
    "discovery_auditor": "brand_discovery",
    "purpose_vision_writer": None,
    "values_articulator": None,
    "audience_segmenter": None,
    "differentiation_mapper": None,
    "positioning_synthesizer": None,
}


# Phase 2 linear Graph node id -> nest-under key on NarrativeMessagingOutput,
# or None to merge fields in flat. Each specialist's structured_output is a
# cumulative carry-forward model (see models.py). Merge uses prefer_first so
# upstream-owned keys are not overwritten by later re-emissions; require_all
# still insists every specialist actually ran. VoicePrinciplesDrafter already
# nests writing_guidelines in its own schema — no remap needed.
_PHASE2_NODE_MERGE: dict[str, Optional[str]] = {
    "Storyteller": None,
    "ArchetypeAnalyst": None,
    "TaglineWriter": None,
    "MessageMapper": None,
    "PersonaBuilder": None,
    "VoicePrinciplesDrafter": None,
}


def _merge_named_fragments(
    node_result: Any,
    model_class: type[BaseModel],
    node_merge: dict[str, Optional[str]],
    *,
    require_all: bool = False,
    prefer_first: bool = False,
) -> Optional[BaseModel]:
    """Merge every recognized child's ``structured_output`` into one phase output.

    Shared by Phase 1 (graph fan-out) and Phase 2 (sequential graph): both wrap
    several named agents as a single top-level node whose nested
    ``MultiAgentResult.results`` is keyed by node/agent id.

    Preconditions:
        ``node_result`` is the ``NodeResult`` for a single top-level graph node
        (may or may not wrap a nested multi-agent result); ``node_merge`` maps
        child ids to an optional nest-under key on ``model_class``.
    Postconditions:
        Returns a validated ``model_class`` instance merging every recognized
        child's ``structured_output`` when at least one was found (or, when
        ``require_all`` is True, when every id in ``node_merge`` was found);
        returns None when ``node_result`` doesn't wrap a nested multi-agent
        result, none of ``node_merge``'s ids are present, ``require_all`` is
        True and at least one id is missing, or the merged data fails
        validation — in every None case the caller falls back to its existing
        single-agent-result logic unchanged.

        When ``prefer_first`` is True, the first child that sets a flat key
        wins (later dumps do not overwrite). Phase 2 needs this because each
        specialist's cumulative ``structured_output`` re-emits upstream fields
        that a real LLM may rewrite.
    """
    nested_results = getattr(getattr(node_result, "result", None), "results", None)
    if not isinstance(nested_results, dict):
        return None

    merged: dict[str, Any] = {}
    found_ids: set[str] = set()
    for child_node_id, nest_under in node_merge.items():
        child = nested_results.get(child_node_id)
        if child is None or not hasattr(child, "get_agent_results"):
            continue
        child_agent_results = child.get_agent_results()
        if not child_agent_results:
            continue
        structured = getattr(child_agent_results[-1], "structured_output", None)
        if not isinstance(structured, BaseModel):
            continue
        found_ids.add(child_node_id)
        data = structured.model_dump()
        if nest_under:
            if prefer_first and nest_under in merged:
                continue
            merged[nest_under] = data
        elif prefer_first:
            for key, value in data.items():
                merged.setdefault(key, value)
        else:
            merged.update(data)

    if not found_ids:
        return None
    if require_all and found_ids != set(node_merge):
        return None
    try:
        return model_class.model_validate(merged)
    except ValidationError:
        return None


def _merge_phase1_fragments(node_result: Any, model_class: type[BaseModel]) -> Optional[BaseModel]:
    """Merge every Phase 1 fan-out node's ``structured_output`` into one phase output.

    Phase 1 wraps six agents (five parallel specialists + a synthesizer) as a
    single top-level ``"phase1_strategic_core"`` node (see
    ``graphs/top_level.py``); the specialists' fragments are just as real as
    the synthesizer's, but a flat ``get_agent_results()[-1]`` only ever sees
    the last (synthesizer) result. This walks the nested ``MultiAgentResult``
    directly — keyed by node id, per ``strands.multiagent.base.MultiAgentResult``
    — to recover each specialist's own typed output.

    Preconditions:
        ``node_result`` is the ``NodeResult`` for a single top-level graph node
        (may or may not wrap a nested multi-agent result).
    Postconditions:
        Returns a validated ``model_class`` instance merging every recognized
        Phase 1 node's ``structured_output`` when at least one was found;
        returns None when ``node_result`` doesn't wrap a nested multi-agent
        result, none of ``_PHASE1_NODE_MERGE``'s node ids are present (e.g.
        every other phase, which uses different node ids), or the merged
        data fails validation — in every None case the caller falls back to
        its existing single-agent-result logic unchanged.
    """
    return _merge_named_fragments(node_result, model_class, _PHASE1_NODE_MERGE)


def _merge_phase2_fragments(node_result: Any, model_class: type[BaseModel]) -> Optional[BaseModel]:
    """Merge every Phase 2 specialist's ``structured_output`` into one phase output.

    Phase 2 wraps six sequential Graph agents as a single top-level
    ``"phase2_narrative"`` node (see ``graphs/top_level.py`` /
    ``graphs/phase2_narrative.py``); a flat ``get_agent_results()[-1]`` only
    ever sees VoicePrinciplesDrafter. This recovers each agent's typed
    fragment the same way Phase 1 does. All six specialists must be present
    — a partial run (e.g. entry agent only) must not validate as a complete
    ``NarrativeMessagingOutput`` via field defaults.

    Cumulative carry-forward models re-emit upstream fields; merge keeps the
    first value for each key so a later specialist cannot overwrite the
    authoritative upstream fragment (e.g. Voice rewriting ``brand_story``).

    Preconditions:
        ``node_result`` is the ``NodeResult`` for a single top-level graph node
        (may or may not wrap a nested multi-agent result).
    Postconditions:
        Returns a validated ``model_class`` instance merging every Phase 2
        agent's ``structured_output`` when all of ``_PHASE2_NODE_MERGE``'s
        node ids were found; returns None when any specialist is missing or
        the merged data fails validation — same None contract as
        ``_merge_phase1_fragments``.
    """
    return _merge_named_fragments(
        node_result, model_class, _PHASE2_NODE_MERGE, require_all=True, prefer_first=True
    )


class _PhaseSpec(NamedTuple):
    """Everything ``run``/``run_single_phase``/``_extract_phase_output`` need for one phase.

    Unifies what used to be two separate tables (``_PHASE_EXTRACTION`` keyed by
    node id, ``_PHASE_SPEC`` keyed by ``BrandPhase``) that duplicated node
    id/model mappings while leaving each phase's actual extraction strategy as
    implicit control flow inside ``_extract_phase_output``. ``min_idx`` for the
    monolithic ``run`` is not stored here — it's derived from a phase's
    position in ``PHASE_ORDER``, which is always 1:1 with this table's order.

    Attributes:
        builder_fn: Builds the phase's sub-graph/swarm (used by
            ``run_single_phase`` to wrap it as an isolated node).
        node_id: The id the monolithic ``build_branding_graph`` assigns this
            phase's node — shared by ``run`` and ``run_single_phase`` so both
            paths reuse ``_extract_phase_output`` verbatim.
        model_cls: The phase's output model.
        merge_fn: When the phase's node wraps several named sub-agents whose
            fragments must be merged into one ``model_cls`` (Phase 1's fan-out,
            Phase 2's sequential graph), the merge function to try first.
            ``None`` for phases whose terminal node's own output already is
            the complete phase output (Phases 3-5's compositor nodes).
        check_structured_output: Whether the single-agent fallback may accept
            the last agent's own ``structured_output`` as the phase output.
            ``False`` only for Phase 2, where the last agent
            (VoicePrinciplesDrafter) only ever emits its own fragment, and
            subset-validating that against ``NarrativeMessagingOutput`` would
            silently report a non-degraded output with every other field
            defaulted empty.
    """

    builder_fn: Callable[[], Any]
    node_id: str
    model_cls: type[BaseModel]
    merge_fn: Optional[Callable[[Any, type[BaseModel]], Optional[BaseModel]]] = None
    check_structured_output: bool = True


# Per-phase spec, keyed by BrandPhase in PHASE_ORDER order. This is the single
# source of truth for node ids, output models, and extraction strategy shared
# by ``run`` (monolithic graph) and ``run_single_phase`` (isolated per-phase
# graph, e.g. Temporal activities).
_PHASE_SPEC: dict[BrandPhase, _PhaseSpec] = {
    BrandPhase.STRATEGIC_CORE: _PhaseSpec(
        build_phase1_graph,
        "phase1_strategic_core",
        StrategicCoreOutput,
        merge_fn=_merge_phase1_fragments,
    ),
    BrandPhase.NARRATIVE_MESSAGING: _PhaseSpec(
        build_phase2_graph,
        "phase2_narrative",
        NarrativeMessagingOutput,
        merge_fn=_merge_phase2_fragments,
        check_structured_output=False,
    ),
    BrandPhase.VISUAL_IDENTITY: _PhaseSpec(
        build_phase3_graph, "phase3_visual", VisualIdentityOutput
    ),
    BrandPhase.CHANNEL_ACTIVATION: _PhaseSpec(
        build_phase4_graph, "phase4_channel", ChannelActivationOutput
    ),
    BrandPhase.GOVERNANCE: _PhaseSpec(build_phase5_graph, "phase5_governance", GovernanceOutput),
}

# node id -> spec, so `_extract_phase_output` (which only receives a node id,
# not a BrandPhase) can look up a phase's extraction strategy directly.
_SPEC_BY_NODE_ID: dict[str, _PhaseSpec] = {spec.node_id: spec for spec in _PHASE_SPEC.values()}


async def _gather_integrations(
    mission: BrandingMission,
    strategic_core: Optional[StrategicCoreOutput],
    include_market_research: bool,
    include_design_assets: bool,
) -> tuple[Optional[Any], Optional[Any]]:
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
        # Import outside the try so a broken import (e.g. a missing dependency)
        # raises immediately instead of being swallowed as "service unavailable"
        # alongside genuine call failures (network errors, timeouts).
        from .adapters.market_research import request_market_research_async

        try:
            return await request_market_research_async(mission)
        except Exception as exc:
            logger.warning(
                "Market research request failed for %s: %s",
                mission.company_name,
                exc,
                exc_info=True,
            )
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
    """Build gate statuses for every phase in the branding workflow.

    Phases before ``up_to_phase`` are marked APPROVED, ``up_to_phase`` itself is
    marked APPROVED or PENDING_REVIEW depending on ``approved``, and all later
    phases are marked NOT_STARTED.

    Preconditions:
        ``up_to_phase`` is a pipeline phase in ``PHASE_ORDER`` (so
        ``phase_index`` returns a real index, not the COMPLETE sentinel).

    Postconditions:
        Returns exactly ``len(PHASE_ORDER)`` ``PhaseGate`` entries in
        ``PHASE_ORDER`` order, with statuses as described above.
    """
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
        """Run the branding pipeline up to and including *target_phase*
        (default: all phases). When *target_phase* is None, every phase is
        executed.

        The pipeline is built as a Strands SDK ``Graph`` whose nodes are
        per-phase sub-graphs and swarms.  Brand-compliance checks run outside
        the graph because their inputs come from the API request.

        Preconditions:
            - ``target_phase`` is ``None`` or one of the runnable ``BrandPhase``
              values (``COMPLETE`` is allowed as the upper bound but is not a
              runnable graph node, and is treated the same as ``None`` --
              every phase runs).
            - When ``store`` and ``brand_id`` are supplied, ``store`` implements
              ``get_brand``/``get_brand_by_id``/``append_brand_version``.

        Postconditions:
            - Returns a fully populated ``TeamOutput``.
            - When ``store``, ``brand_id``, and a resolved ``client_id`` are
              present, persists the output via ``store.append_brand_version``.
              Raises ``BrandVersionAppendConflict`` if the brand row disappeared
              during the run (``append_brand_version`` returns ``None``), so the
              caller can mark the run as failed instead of reporting success
              without persistence.
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
        result = run_coroutine(graph.invoke_async(task))

        # ---- Extract phase outputs from graph node results (table-driven) ----
        # min_idx is a phase's position in PHASE_ORDER, which _PHASE_SPEC's
        # order always matches.
        phase_specs = [_PHASE_SPEC[phase] for phase in PHASE_ORDER]
        extractions = [
            self._extract_phase_output(result, spec.node_id, spec.model_cls)
            if stop_idx >= min_idx
            else (None, False)
            for min_idx, spec in enumerate(phase_specs)
        ]
        strategic_core, narrative, visual_identity, channel_activation, governance = (
            output for output, _ in extractions
        )
        degraded_phases = [
            phase for phase, (_, degraded) in zip(PHASE_ORDER, extractions) if degraded
        ]

        # ---- Run compliance checks (outside the graph) ----
        checks = self.compliance.evaluate(brand_checks or [], mission)

        # ---- Integrations (run concurrently; see _gather_integrations) ----
        competitive_snapshot, design_asset_result = run_coroutine(
            _gather_integrations(
                mission, strategic_core, include_market_research, include_design_assets
            )
        )

        # ---- Assemble the final output (shared with the Temporal finalize activity) ----
        output = self._assemble_team_output(
            mission=mission,
            human_review=human_review,
            strategic_core=strategic_core,
            narrative=narrative,
            visual_identity=visual_identity,
            channel_activation=channel_activation,
            governance=governance,
            checks=checks,
            competitive_snapshot=competitive_snapshot,
            design_asset_result=design_asset_result,
            degraded_phases=degraded_phases,
        )

        if store and brand_id and resolved_client_id:
            appended = store.append_brand_version(resolved_client_id, brand_id, output)
            if appended is None:
                # Brand could have been deleted between resolve and finalize.
                # Surface a failure instead of returning an output that wasn't persisted.
                from .store import BrandVersionAppendConflict

                raise BrandVersionAppendConflict(
                    "Brand row disappeared while appending brand version "
                    f"(client_id={resolved_client_id}, brand_id={brand_id})"
                )

        return output

    def run_single_phase(
        self,
        mission: BrandingMission,
        phase: BrandPhase,
        prior_outputs: Optional[dict[str, dict]] = None,
    ) -> tuple[BaseModel, bool]:
        """Run a single pipeline phase in isolation and return its output model.

        The monolithic ``build_branding_graph`` wires phases as sequential nodes,
        so a downstream phase normally receives its predecessor's output through a
        Strands edge. To run one phase alone (as a Temporal activity does), we wrap
        that phase's sub-graph/swarm as a *single node* — reusing the same
        top-level node id the monolithic graph assigns it — so the invoke result
        has the identical ``result.result[node_id]`` shape ``_extract_phase_output``
        already parses. The lost cross-phase edge is compensated by injecting the
        serialized ``prior_outputs`` into the task string (see ``_phase_task``).

        Preconditions:
            - ``phase`` is one of the five pipeline phases (a key of
              ``_PHASE_SPEC``); ``BrandPhase.COMPLETE`` is not a runnable phase.
            - ``prior_outputs`` maps upstream ``BrandPhase`` value strings to their
              JSON-safe phase-output dicts (``model_dump(mode="json")``), or is
              ``None``/empty for the first phase.
        Postconditions:
            - Returns ``(output, degraded)`` from ``_extract_phase_output``:
              ``output`` is never ``None`` (a parse failure yields a
              default-constructed model); ``degraded`` is ``True`` iff that
              default was used. The caller (the Temporal phase activity) owns
              folding ``degraded`` into the run's durable degradation record —
              this method does not persist anything itself.
        """
        if phase not in _PHASE_SPEC:
            raise ValueError(f"{phase!r} is not a runnable branding phase")
        spec = _PHASE_SPEC[phase]

        builder = GraphBuilder()
        builder.set_graph_id(f"branding_phase_{phase.value}")
        builder.set_execution_timeout(DEFAULT_EXECUTION_TIMEOUT_SECONDS)
        builder.set_node_timeout(DEFAULT_NODE_TIMEOUT_SECONDS)
        builder.add_node(spec.builder_fn(), node_id=spec.node_id)
        builder.set_entry_point(spec.node_id)
        graph = builder.build()

        task = self._phase_task(mission, phase, prior_outputs or {})
        result = run_coroutine(graph.invoke_async(task))
        return self._extract_phase_output(result, spec.node_id, spec.model_cls)

    @staticmethod
    def _phase_task(
        mission: BrandingMission,
        phase: BrandPhase,
        prior_outputs: dict[str, dict],
    ) -> str:
        """Build the task string for an isolated phase run.

        Preconditions:
            - ``prior_outputs`` maps upstream phase value strings to JSON-safe
              phase-output dicts (possibly empty).
        Postconditions:
            - Returns the same mission-seeded task the monolithic graph uses,
              extended with the serialized upstream outputs when present so an
              isolated downstream phase sees the context the sequential edge would
              otherwise carry (a superset — never less context).
        """
        base = (
            "Create a comprehensive brand strategy for the following company.\n\n"
            f"Branding Mission:\n{serialize_mission(mission)}"
        )
        if prior_outputs:
            blocks = "\n\n".join(
                f"### {name} (approved prior-phase output) ###\n"
                f"{json.dumps(payload, indent=2, default=str)}"
                for name, payload in prior_outputs.items()
            )
            base += (
                "\n\nContext from completed upstream phases — build on these and do "
                f"not contradict them:\n{blocks}"
            )
        return base

    def _assemble_team_output(
        self,
        *,
        mission: BrandingMission,
        human_review: HumanReview,
        strategic_core: Optional[StrategicCoreOutput],
        narrative: Optional[NarrativeMessagingOutput],
        visual_identity: Optional[VisualIdentityOutput],
        channel_activation: Optional[ChannelActivationOutput],
        governance: Optional[GovernanceOutput],
        checks: List[Any],
        competitive_snapshot: Any,
        design_asset_result: Any,
        degraded_phases: Optional[List[BrandPhase]] = None,
    ) -> TeamOutput:
        """Assemble the final ``TeamOutput`` from computed phase artifacts.

        Shared by the thread path (``run``) and the Temporal ``finalize`` activity
        so the brand-book, phase-gate, status, and summary derivation live in one
        place — the primary guard against Temporal-vs-thread output divergence.

        Preconditions:
            - The five phase outputs are their respective models or ``None`` (a
              phase not reached for this ``stop_idx``); ``checks`` is the compliance
              result list; ``competitive_snapshot``/``design_asset_result`` are the
              integration results (or ``None`` when disabled); ``degraded_phases``
              lists only phases actually reached this run whose output could not
              be parsed and was default-constructed by ``_extract_phase_output``
              (or ``None``/empty when every reached phase parsed successfully) —
              the caller owns de-duplication and ordering, this method passes
              the list through as-is.
        Postconditions:
            - Returns a fully-populated ``TeamOutput`` whose ``degraded_phases``
              reflects the caller-supplied list; performs no I/O and no
              persistence (the caller owns ``store.append_brand_version``).
            - ``human_feedback`` falls back to a status-appropriate default
              message ("Approved for rollout." / "Awaiting approval from
              brand leadership.") whenever ``human_review.feedback`` is
              falsy -- including an explicitly-passed ``feedback=""`` --
              because ``HumanReview.feedback`` (``backend/shared/hitl/
              models.py``) is typed ``str = ""``, not ``Optional[str]``, so
              "omitted" and "explicitly empty" cannot be distinguished at
              this boundary without widening ``HumanReview``'s public shape,
              which is out of scope here.
        """
        current_phase = self._determine_current_phase(
            narrative, visual_identity, channel_activation, governance, human_review.approved
        )
        brand_book = _build_brand_book(
            strategic_core, narrative, visual_identity, channel_activation, governance
        )
        phase_gates = _build_phase_gates(current_phase, human_review.approved)
        status, mission_summary = self._build_status_summary(human_review, current_phase)

        return TeamOutput(
            status=status,
            mission_summary=mission_summary,
            current_phase=current_phase,
            phase_gates=phase_gates,
            degraded_phases=degraded_phases or [],
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
            found, its mission replaces the passed one. If no ``client_id`` was
            supplied, ``resolved_client_id`` is filled in from the brand;
            otherwise the supplied ``client_id`` is preserved. If no stored brand
            is found, the inputs pass through unchanged.
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

        ``strategic_core`` is deliberately not a parameter here: STRATEGIC_CORE
        is the pipeline's first phase and always the floor, so this function
        never needs to observe whether it parsed cleanly.

        Preconditions:
            Each of ``narrative``/``visual_identity``/``channel_activation``/
            ``governance`` is either that phase's output model or ``None`` when
            the phase was not reached this run (stopped before it via
            ``stop_idx``/``target_phase``). A non-``None`` value may be a
            default-constructed instance -- ``_extract_phase_output`` never
            returns ``None`` on a parse failure, only a degraded default -- so
            a degraded phase is indistinguishable from a cleanly-parsed one at
            this call site.
        Postconditions:
            Returns the phase of the last non-None output (STRATEGIC_CORE if
            all four are None); when ``governance`` is present and
            ``approved`` is True, returns ``BrandPhase.COMPLETE`` instead.
            Invariant: a degraded (default-constructed) phase output still
            counts as "reached" for this promotion -- callers that need to
            distinguish degraded from clean must consult
            ``TeamOutput.degraded_phases`` separately.
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
    ) -> tuple[WorkflowStatus, str]:
        """Derive the workflow status and human-facing summary line.

        Preconditions:
            ``current_phase`` is the value ``_determine_current_phase`` just
            returned for this same run (the furthest phase actually reached).
        Postconditions:
            Returns ``(status, mission_summary)``: NEEDS_HUMAN_DECISION with a
            review prompt when unapproved, READY_FOR_ROLLOUT when the run is
            complete, else NEEDS_HUMAN_DECISION with a phase-approved summary.
            Both the unapproved branch and the approved-but-incomplete branch
            derive their embedded phase name from ``current_phase`` -- never
            from a separately-computed stop index -- so the two can never
            disagree about which phase they describe.
        """
        if not human_review.approved:
            phase_label = current_phase.value.replace("_", " ").title()
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
    def _extract_phase_output(
        result: Any, node_id: str, model_class: type[BaseModel]
    ) -> tuple[BaseModel, bool]:
        """Best-effort extraction of a phase output from graph results.

        The graph node results contain ``AgentResult`` or ``MultiAgentResult``
        objects. ``node_id`` is looked up in ``_SPEC_BY_NODE_ID`` to find that
        phase's extraction strategy: when the spec has a ``merge_fn`` (Phase 1's
        fan-out, Phase 2's sequential graph — both wrap several named agents as
        a single top-level node), it's tried first, merging every recognized
        child's ``structured_output`` into one ``model_class`` instance; if
        that succeeds, its result is returned directly. Every other phase
        (whose spec has no ``merge_fn``, or an unrecognized node id with no
        spec at all) skips straight to the per-node fallback below: when the
        node's agent was built with ``structured_output=``, Strands forces a
        tool call to produce the payload and populates
        ``AgentResult.structured_output`` instead of the message's text blocks
        — so that's checked next, unless the spec sets
        ``check_structured_output=False`` (Phase 2 only: a lone specialist's
        own fragment must never be accepted as a complete phase output, since
        subset validation against ``NarrativeMessagingOutput`` would succeed
        via defaults). Agents without usable structured output fall back to
        parsing the last text block.

        Preconditions:
            - ``result`` is the Strands graph invocation result (or a test
              double shaped like one); ``node_id`` identifies a node in
              ``result.result``; ``model_class`` is the expected output model
              for that node.
        Postconditions:
            - Returns ``(output, degraded)``. ``output`` is a parsed
              ``model_class`` instance on success (from the phase's merge_fn,
              structured output, or text parsing), or a default-constructed
              ``model_class()`` when none of those yield a value or the node
              result is missing/malformed. ``degraded`` is ``True`` iff a
              default was returned, and every such fall-through is logged
              (with traceback for unexpected errors) rather than swallowed
              silently. Callers that assemble a ``TeamOutput`` must fold
              ``degraded`` phases into ``TeamOutput.degraded_phases``
              themselves — this method does not know which ``BrandPhase`` it
              was called for.
        """
        spec = _SPEC_BY_NODE_ID.get(node_id)
        try:
            result_obj = getattr(result, "result", None)
            if result_obj is not None and hasattr(result_obj, "get"):
                node_result = result_obj.get(node_id)
                if node_result and hasattr(node_result, "result"):
                    if spec is not None and spec.merge_fn is not None:
                        merged = spec.merge_fn(node_result, model_class)
                        if merged is not None:
                            return merged, False
                    agent_results = node_result.get_agent_results()
                    if agent_results:
                        last = agent_results[-1]
                        if spec is None or spec.check_structured_output:
                            structured = getattr(last, "structured_output", None)
                            if isinstance(structured, BaseModel):
                                parsed = _merge_structured_output(structured, model_class)
                                if parsed is not None:
                                    return parsed, False
                        if hasattr(last, "message") and last.message:
                            text = _collect_message_text(last.message)
                            parsed = _parse_model_from_text(text, model_class)
                            if parsed is not None:
                                return parsed, False
        except Exception:
            # Malformed JSON / schema mismatch already returns None from
            # _parse_model_from_text; reaching here means an unexpected error
            # walking the graph result. Log it rather than swallow silently.
            logger.warning(
                "Unexpected error extracting phase output for node %s; using default",
                node_id,
                exc_info=True,
            )
            return model_class(), True
        logger.warning(
            "Could not extract phase output for node %s from agent text; using default",
            node_id,
        )
        return model_class(), True


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


def _merge_structured_output(
    structured: BaseModel, model_class: type[BaseModel]
) -> Optional[BaseModel]:
    """Validate an agent's typed ``structured_output`` against a phase's output model.

    ``structured`` is often a specialist fragment of ``model_class`` — e.g. the
    positioning synthesizer only emits ``positioning_statement``/``brand_promise``
    out of the full ``StrategicCoreOutput`` schema — which validates fine since
    every field on the phase output models has a default. Dump-then-validate is
    required for those subset payloads; it is not leftover twin-model conversion.

    Preconditions:
        ``structured`` is a ``pydantic.BaseModel`` instance; ``model_class`` is
        the phase output model type to validate against.
    Postconditions:
        Returns a validated ``model_class`` instance when fields match (including
        subset payloads that fill missing fields from defaults); returns
        ``None`` on a genuine schema mismatch — same failure contract as
        ``_parse_model_from_text``.
    """
    try:
        return model_class.model_validate(structured.model_dump())
    except ValidationError:
        return None


def _parse_model_from_text(text: str, model_class: type[BaseModel]) -> Optional[BaseModel]:
    """Best-effort parse of ``text`` into ``model_class``; ``None`` on failure.

    Delegates JSON recovery (whole-string parse, then fenced/prose-wrapped
    brace-slice fallback) to the shared ``recover_json_object`` helper rather
    than re-deriving that logic here. Recovery is anchored on
    ``model_class``'s field names so a reply containing more than one JSON
    object (e.g. the real payload followed by a usage/metadata echo) selects
    the object that actually carries the expected schema — every field on
    these phase models defaults, so an unanchored recovery could otherwise
    validate successfully against an unrelated trailing object and silently
    report success on a payload no agent ever produced.

    Preconditions:
        ``model_class`` is a ``pydantic.BaseModel`` subclass.
    Postconditions:
        Returns a validated ``model_class`` instance when JSON can be
        recovered from ``text`` and validates against the schema; returns
        ``None`` for empty text, unrecoverable text, text where no candidate
        object carries any of ``model_class``'s field names, or a
        ``ValidationError`` (schema mismatch). Any other exception raised by
        ``model_class.model_validate`` is a genuine bug (a broken custom
        validator, a non-model ``model_class``) and propagates rather than
        being swallowed.
    """
    if not text:
        return None
    data = recover_json_object(text, required_keys=model_class.model_fields.keys())
    if data is None:
        return None
    # ValidationError covers a schema mismatch between the recovered JSON and
    # model_class; anything else is a genuine bug and should surface.
    try:
        return model_class.model_validate(data)
    except ValidationError:
        return None


def _bullets(title: str, items: Iterable[Any], fmt: Callable[[Any], str] = lambda x: x) -> str:
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


# Process-wide orchestrator singleton. It is stateless (only holds the regex
# ``BrandComplianceAgent``), so one shared instance is safe. Defined here — the
# canonical domain module — so both the API layer and the Temporal activities
# import it from here rather than coupling the worker to ``api.main``.
orchestrator = BrandingTeamOrchestrator()
