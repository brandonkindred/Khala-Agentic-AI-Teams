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
import functools
import json
import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    List,
    NamedTuple,
    Optional,
    get_origin,
    get_type_hints,
)

from pydantic import BaseModel, ValidationError
from strands.multiagent.graph import GraphBuilder

from branding_team.shared.coro_runner import run_coroutine
from branding_team.shared.json_recovery import recover_json_object

from .agents import CHANNEL_SPECS, BrandComplianceAgent
from .graphs.phase1_strategic_core import build_phase1_graph
from .graphs.phase2_narrative import build_phase2_graph
from .graphs.phase3_visual import build_phase3_graph
from .graphs.phase4_channel import build_phase4_graph
from .graphs.phase5_governance import build_phase5_graph
from .graphs.shared import PHASE_ORDER, PHASE_OUTPUT_MODELS, phase_index, serialize_mission
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
from .shared.memoization import phase_input_hash
from .shared.phase_output_cache import PhaseOutputCache

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


# Phase 2 fan-out node id -> nest-under key on NarrativeMessagingOutput,
# or None to merge fields in flat. Each specialist's structured_output is an
# own-field-only model (Story 5b Step 1; see models.py) -- no two of the six
# fragments can set the same flat key, so the merge is a plain flat union with
# no collision-avoidance needed. require_all still insists every specialist
# actually ran. VoicePrinciplesDrafter already nests writing_guidelines in its
# own schema — no remap needed.
_PHASE2_NODE_MERGE: dict[str, Optional[str]] = {
    "Storyteller": None,
    "ArchetypeAnalyst": None,
    "TaglineWriter": None,
    "MessageMapper": None,
    "PersonaBuilder": None,
    "VoicePrinciplesDrafter": None,
}


# Phase 3 fan-out node id -> the VisualIdentityOutput key its structured_output
# nests under, or None to merge its fields in flat. The three
# MoodBoardConceptualist_* variants each emit a single MoodBoardConcept, all
# nesting under "mood_board_candidates" -- since that's a List field on
# VisualIdentityOutput, _merge_named_fragments appends each one as a list
# element, so all three survive the merge. converge_decider's
# CreativeRefinementDecision nests under "creative_refinement" (its
# default_factory merge target). Six of the seven post-converge specialists
# already match VisualIdentityOutput field names 1:1 and merge flat;
# design_system_codifier's DesignSystemDefinition nests under "design_system"
# since its own fields don't exist at the top level.
_PHASE3_NODE_MERGE: dict[str, Optional[str]] = {
    "MoodBoardConceptualist_Editorial": "mood_board_candidates",
    "MoodBoardConceptualist_Minimalist": "mood_board_candidates",
    "MoodBoardConceptualist_Bold": "mood_board_candidates",
    "converge_decider": "creative_refinement",
    "logo_specifier": None,
    "color_system_builder": None,
    "typography_builder": None,
    "iconography_director": None,
    "photography_video_director": None,
    "voice_tone_builder": None,
    "design_system_codifier": "design_system",
}


# Phase 4 fan-out node id -> the ChannelActivationOutput key its
# structured_output nests under, or None to merge its fields in flat. Three
# specialists (brand_experience_principler, brand_architecture_builder,
# brand_in_action_illustrator) already match ChannelActivationOutput field
# names 1:1 and merge flat. The channel-guide specialists (one per
# CHANNEL_SPECS entry) each emit a single ChannelGuidelineOutput for their
# own channel, all nesting under the same "channel_guidelines" key -- since
# that's a List field on ChannelActivationOutput, _merge_named_fragments
# appends each one as a list element instead of overwriting, so all of them
# survive the merge. Generated from CHANNEL_SPECS so adding a channel there
# automatically adds its merge entry here.
_PHASE4_NODE_MERGE: dict[str, Optional[str]] = {
    "brand_experience_principler": None,
    **{f"{channel}_guide": "channel_guidelines" for channel, _ in CHANNEL_SPECS},
    "brand_architecture_builder": None,
    "brand_in_action_illustrator": None,
}


# Phase 5 fan-out node id -> the GovernanceOutput key its structured_output
# nests under, or None to merge its fields in flat. Unlike Phase 4's six
# *_guide specialists (which all nest under the same list field), every
# Phase 5 specialist's structured_output matches a disjoint set of
# GovernanceOutput field names 1:1 (see models.py) -- so every value here is
# None and the merge is a plain flat union with no list-append case.
_PHASE5_NODE_MERGE: dict[str, Optional[str]] = {
    "ownership_definer": None,
    "approval_workflow_designer": None,
    "asset_wiki_planner": None,
    "training_planner": None,
    "kpi_designer": None,
    "evolution_framer": None,
    "brand_rules_codifier": None,
}


def _child_structured_output(child: Any) -> Optional[BaseModel]:
    """Recover a merge child's usable ``structured_output``, or ``None``.

    Preconditions:
        ``child`` is a value looked up from a nested ``MultiAgentResult.results``
        mapping — either a ``NodeResult``-shaped object or ``None`` (a node id
        not present in that run).
    Postconditions:
        Returns the last agent result's ``structured_output`` when ``child`` is
        present, exposes a non-empty ``get_agent_results()``, and that output is
        a ``pydantic.BaseModel``; returns ``None`` otherwise. The ``None`` cases
        are exactly the ``continue`` conditions the caller skips over.
    """
    if child is None or not hasattr(child, "get_agent_results"):
        return None
    child_agent_results = child.get_agent_results()
    if not child_agent_results:
        return None
    structured = getattr(child_agent_results[-1], "structured_output", None)
    if not isinstance(structured, BaseModel):
        return None
    return structured


def _apply_fragment(
    merged: dict[str, Any],
    data: dict[str, Any],
    nest_under: Optional[str],
    *,
    list_fields: frozenset[str] = frozenset(),
) -> None:
    """Fold one recognized fragment's dumped ``data`` into the ``merged`` accumulator.

    Preconditions:
        ``merged`` is the in-progress accumulator; ``data`` is a child's
        ``model_dump()``; ``nest_under`` is the child's optional nest-under key;
        ``list_fields`` is the set of ``model_class`` field names typed as a
        list.
    Postconditions:
        Mutates ``merged`` in place and returns ``None``.
        - When ``nest_under`` names a list field, ``data`` is appended as one
          element under it (several single-item fragments combine into one list,
          e.g. Phase 4's ``channel_guidelines``).
        - When ``nest_under`` names a non-list field, ``data`` is placed under
          it (last writer wins).
        - When ``nest_under`` is ``None``, ``data`` overwrites ``merged``'s
          matching keys (last writer wins).
        No other keys are touched.
    """
    if nest_under and nest_under in list_fields:
        merged.setdefault(nest_under, []).append(data)
    elif nest_under:
        merged[nest_under] = data
    else:
        merged.update(data)


def _merge_named_fragments(
    node_result: Any,
    model_class: type[BaseModel],
    node_merge: dict[str, Optional[str]],
    *,
    require_all: bool = False,
) -> Optional[BaseModel]:
    """Merge every recognized child's ``structured_output`` into one phase output.

    The sole merge implementation for all five phases: each wraps several
    named agents as a single top-level node whose nested
    ``MultiAgentResult.results`` is keyed by node/agent id. All five phases
    bind ``node_merge``/``require_all`` directly as a ``functools.partial``
    in ``_PHASE_SPEC``.

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

        When ``nest_under`` names a ``model_class`` field typed as a list
        (e.g. Phase 4's ``channel_guidelines``), every child mapped to that
        key appends its dump as one list element instead of overwriting —
        this is how several single-item fragments (one per channel) combine
        into one list field. Non-list nest_under fields keep the original
        single-value-assignment behavior.
    """
    nested_results = getattr(getattr(node_result, "result", None), "results", None)
    if not isinstance(nested_results, dict):
        return None

    # ``field.annotation`` can still be an unresolved ``ForwardRef`` (e.g.
    # ``VisualIdentityOutput.mood_board_candidates: List["MoodBoardConcept"]``,
    # forward-referencing a class defined later in models.py) even though the
    # model itself validates fine -- Pydantic resolves forward refs lazily for
    # validation but doesn't retroactively rewrite ``model_fields[...].annotation``.
    # ``get_type_hints`` forces that resolution, so a list field isn't
    # misdetected as scalar (which would silently overwrite instead of
    # collecting its fragments, then fail model validation).
    resolved_hints = get_type_hints(model_class)
    list_fields = {name for name, hint in resolved_hints.items() if get_origin(hint) is list}

    merged: dict[str, Any] = {}
    found_ids: set[str] = set()
    for child_node_id, nest_under in node_merge.items():
        structured = _child_structured_output(nested_results.get(child_node_id))
        if structured is None:
            continue
        found_ids.add(child_node_id)
        _apply_fragment(
            merged,
            structured.model_dump(),
            nest_under,
            list_fields=list_fields,
        )

    if not found_ids:
        return None
    if require_all and found_ids != set(node_merge):
        return None
    try:
        return model_class.model_validate(merged)
    except ValidationError:
        return None


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
            Phase 2's fan-out graph, Phase 3's, Phase 4's, and Phase 5's
            fan-out), the merge function to try first. All five current phases
            supply one; ``None`` remains valid for a hypothetical future phase
            whose node has no fan-out to merge. When ``merge_fn`` is absent or
            returns ``None``, ``_extract_phase_output`` falls through to
            ``_extract_from_single_agent``'s last-resort text parse of the
            node's last agent reply — there is no ``structured_output``
            shortcut for any phase today.
            Invariant: a merge function must return ``None`` to signal "could
            not merge" — never a default-constructed ``model_cls()`` — since
            ``_extract_phase_output`` trusts any non-``None`` return as a
            successful, non-degraded extraction.
        context_phases: Which upstream ``BrandPhase``s' outputs this phase
            should receive as context. Defaults to ``()`` (no filtering —
            current behavior, where every phase can see all prior outputs,
            is unaffected). Infrastructure only: not yet consumed by
            ``_phase_task`` and not yet populated for any phase.
    """

    builder_fn: Callable[[], Any]
    node_id: str
    model_cls: type[BaseModel]
    merge_fn: Optional[Callable[[Any, type[BaseModel]], Optional[BaseModel]]] = None
    context_phases: tuple[BrandPhase, ...] = ()


# Per-phase spec, keyed by BrandPhase in PHASE_ORDER order. This is the single
# source of truth for node ids, output models, and extraction strategy shared
# by ``run`` (monolithic graph) and ``run_single_phase`` (isolated per-phase
# graph, e.g. Temporal activities).
_PHASE_SPEC: dict[BrandPhase, _PhaseSpec] = {
    BrandPhase.STRATEGIC_CORE: _PhaseSpec(
        build_phase1_graph,
        "phase1_strategic_core",
        PHASE_OUTPUT_MODELS[BrandPhase.STRATEGIC_CORE],
        merge_fn=functools.partial(_merge_named_fragments, node_merge=_PHASE1_NODE_MERGE),
    ),
    BrandPhase.NARRATIVE_MESSAGING: _PhaseSpec(
        build_phase2_graph,
        "phase2_narrative",
        PHASE_OUTPUT_MODELS[BrandPhase.NARRATIVE_MESSAGING],
        merge_fn=functools.partial(
            _merge_named_fragments, node_merge=_PHASE2_NODE_MERGE, require_all=True
        ),
    ),
    BrandPhase.VISUAL_IDENTITY: _PhaseSpec(
        build_phase3_graph,
        "phase3_visual",
        PHASE_OUTPUT_MODELS[BrandPhase.VISUAL_IDENTITY],
        merge_fn=functools.partial(
            _merge_named_fragments, node_merge=_PHASE3_NODE_MERGE, require_all=True
        ),
    ),
    BrandPhase.CHANNEL_ACTIVATION: _PhaseSpec(
        build_phase4_graph,
        "phase4_channel",
        PHASE_OUTPUT_MODELS[BrandPhase.CHANNEL_ACTIVATION],
        merge_fn=functools.partial(
            _merge_named_fragments, node_merge=_PHASE4_NODE_MERGE, require_all=True
        ),
    ),
    BrandPhase.GOVERNANCE: _PhaseSpec(
        build_phase5_graph,
        "phase5_governance",
        PHASE_OUTPUT_MODELS[BrandPhase.GOVERNANCE],
        merge_fn=functools.partial(
            _merge_named_fragments, node_merge=_PHASE5_NODE_MERGE, require_all=True
        ),
    ),
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
        phase_cache: Optional[PhaseOutputCache] = None,
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
            - ``phase_cache`` is a ``PhaseOutputCache`` instance (possibly
              empty) or ``None``.

        Postconditions:
            - Returns a fully populated ``TeamOutput``.
            - When ``store``, ``brand_id``, and a resolved ``client_id`` are
              present, persists the output via ``store.append_brand_version``.
              Raises ``BrandVersionAppendConflict`` if the brand row disappeared
              during the run (``append_brand_version`` returns ``None``), so the
              caller can mark the run as failed instead of reporting success
              without persistence.
            - When ``phase_cache`` is ``None`` (the default), execution is
              identical to omitting it entirely -- the pipeline always runs as
              one monolithic graph invocation, exactly as before this
              parameter existed.
            - When ``phase_cache`` is supplied, each phase up to
              ``target_phase`` is run in isolation (see
              ``_run_phases_with_cache``): a phase whose input hash matches a
              cached entry reuses that cached output instead of being
              invoked; a miss runs the phase via ``run_single_phase`` and, if
              the result is not degraded, stores it in ``phase_cache`` for a
              future call.
            - ``phase_cache`` is thread-path-only by construction, not by
              convention: the Temporal activity (``temporal/activities.py``)
              calls ``run_single_phase`` directly and never this method, and
              ``run_single_phase`` itself takes no ``phase_cache`` parameter
              -- so a warm cache elsewhere in the process cannot alter the
              Temporal workflow branch, which keeps invoking every phase.
        """
        # ---- Resolve brand from store if applicable ----
        mission, resolved_client_id = self._resolve_mission(mission, store, client_id, brand_id)

        stop_idx = phase_index(target_phase) if target_phase else len(PHASE_ORDER) - 1

        if phase_cache is None:
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
        else:
            extractions = self._run_phases_with_cache(mission, stop_idx, phase_cache)

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

    def _run_phases_with_cache(
        self,
        mission: BrandingMission,
        stop_idx: int,
        cache: PhaseOutputCache,
    ) -> List[tuple[Optional[BaseModel], bool]]:
        """Run each phase up to ``stop_idx`` in isolation, reusing cache hits.

        Unlike the monolithic-graph path this replaces, each phase is invoked
        one at a time via ``run_single_phase`` so a cache hit can genuinely
        skip invoking that phase, rather than discarding a freshly computed
        result in favor of the cached one.

        Preconditions:
            - ``stop_idx`` is a valid index into ``PHASE_ORDER`` (as computed
              by ``run`` from ``target_phase``).
            - ``cache`` is a ``PhaseOutputCache`` instance (possibly empty).
        Postconditions:
            - Returns exactly ``len(PHASE_ORDER)`` ``(output, degraded)``
              pairs in ``PHASE_ORDER`` order, matching the shape and contract
              of the monolithic path's extraction list: phases beyond
              ``stop_idx`` are ``(None, False)``.
            - Each phase's input hash is computed from ``mission`` and the
              upstream outputs actually produced earlier in this same call
              (cache hits or fresh runs) -- never from a previous call's
              accumulated state -- so a changed upstream phase naturally
              yields a different hash for every downstream phase, causing
              them to miss and recompute without any separate invalidation
              step.
            - Only a non-degraded phase output is ever stored in ``cache``; a
              degraded (default-constructed) output is returned but never
              cached, so a transient parse failure cannot poison a later call.
        """
        upstream_models: dict[BrandPhase, BaseModel] = {}
        prior_outputs: dict[str, dict] = {}
        extractions: List[tuple[Optional[BaseModel], bool]] = []
        for min_idx, phase in enumerate(PHASE_ORDER):
            if stop_idx < min_idx:
                extractions.append((None, False))
                continue

            input_hash = phase_input_hash(phase, mission, upstream_models)
            cached_output = cache.get(phase, input_hash)
            if cached_output is not None:
                output, degraded = cached_output, False
            else:
                output, degraded = self.run_single_phase(mission, phase, prior_outputs)
                if not degraded:
                    cache.put(phase, input_hash, output)

            extractions.append((output, degraded))
            upstream_models[phase] = output
            prior_outputs[phase.value] = output.model_dump(mode="json")

        return extractions

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
              default-constructed model); ``degraded`` is ``True`` only when
              extraction fell through to that default-construction fallback,
              not whenever the returned value merely looks default-shaped
              (see ``_extract_phase_output``'s postcondition). The caller (the
              Temporal phase activity) owns folding ``degraded`` into the
              run's durable degradation record — this method does not persist
              anything itself.
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
        phase_cache: Optional[PhaseOutputCache] = None,
    ) -> TeamOutput:
        """Convenience method: run the pipeline up to (and including) a specific
        phase. Accepts an optional ``phase_cache`` to reuse cached phase
        outputs (see ``run()``)."""
        return self.run(
            mission=mission,
            human_review=human_review,
            brand_checks=brand_checks,
            store=store,
            client_id=client_id,
            brand_id=brand_id,
            target_phase=phase,
            phase_cache=phase_cache,
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
        phase's extraction strategy: every phase's spec declares a
        ``merge_fn`` (each wraps several named sub-agents as a single
        top-level node), so it's tried first, merging every recognized
        child's ``structured_output`` into one ``model_class`` instance; if
        that succeeds, its result is returned directly. When ``merge_fn``
        returns ``None`` (a partial or malformed nested result), or
        ``node_id`` is unrecognized (no spec at all), extraction falls
        through to a single last-resort fallback: parsing the last agent's
        last text block. Anything that doesn't yield a value through either
        path degrades to a default-constructed ``model_class()``.

        Preconditions:
            - ``result`` is the Strands graph invocation result (or a test
              double shaped like one); ``node_id`` identifies a node in
              ``result.result``; ``model_class`` is the expected output model
              for that node.
        Postconditions:
            - Returns ``(output, degraded)``. ``output`` is a parsed
              ``model_class`` instance on success (from the phase's merge_fn
              or the last-resort text parse), or a default-constructed
              ``model_class()`` when neither yields a value or the node
              result is missing/malformed. ``degraded`` reflects which code
              path produced ``output``, not a property of the value itself:
              it is ``True`` only when extraction fell through every
              recognized path to the default-construction fallback, and every
              such fall-through is logged (with traceback for unexpected
              errors) rather than swallowed silently. Any non-``None`` return
              from ``spec.merge_fn`` is trusted as a successful extraction and
              always yields ``degraded=False``, even if that value happens to
              equal ``model_class()`` — merge functions must return ``None``,
              never a default instance, to signal failure (see
              ``_PhaseSpec.merge_fn``). Callers that assemble a
              ``TeamOutput`` must fold ``degraded`` phases into
              ``TeamOutput.degraded_phases`` themselves — this method does
              not know which ``BrandPhase`` it was called for.
        """
        spec = _SPEC_BY_NODE_ID.get(node_id)
        try:
            node_result = _locate_node_result(result, node_id)
            if node_result is not None:
                if spec is not None and spec.merge_fn is not None:
                    merged = spec.merge_fn(node_result, model_class)
                    if merged is not None:
                        return merged, False
                parsed = _extract_from_single_agent(node_result, model_class)
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


_NO_RESULT_ATTR = object()


def _locate_node_result(result: Any, node_id: str) -> Optional[Any]:
    """Walk a Strands graph invocation ``result`` down to one node's result.

    Preconditions:
        ``result`` is the graph invocation result (or a test double shaped like
        one); ``node_id`` identifies the node to fetch.
    Postconditions:
        Returns the ``NodeResult`` for ``node_id`` when ``result`` exposes a
        node-id-keyed mapping (duck-typed via ``.get``) that contains
        ``node_id`` and the fetched value itself wraps a ``result``; returns
        ``None`` for any missing/malformed link in that chain (mapping
        attribute absent or not a mapping, node id absent, or a node result
        without a ``result``).

        The mapping is read from ``result.result`` when that attribute is
        present at all (even if ``None`` or not mapping-shaped — preserves
        every existing caller/test double built against this historical
        attribute name), and only falls back to ``result.results`` (plural)
        when ``.result`` is genuinely absent -- the shape real Strands
        ``GraphResult``/``MultiAgentResult`` objects use (they carry no
        ``.result`` attribute at all, only ``.results``).
    """
    result_obj = getattr(result, "result", _NO_RESULT_ATTR)
    if result_obj is _NO_RESULT_ATTR:
        result_obj = getattr(result, "results", None)
    if result_obj is None or not hasattr(result_obj, "get"):
        return None
    node_result = result_obj.get(node_id)
    if node_result and hasattr(node_result, "result"):
        return node_result
    return None


def _extract_from_single_agent(
    node_result: Any, model_class: type[BaseModel]
) -> Optional[BaseModel]:
    """Last-resort text parse of a node's last agent result, or ``None``.

    The single fallback for a phase whose merge_fn returned ``None`` (a
    partial or malformed nested result) or whose node id is unrecognized:
    parse the last agent's last text block. There is no structured_output
    shortcut here — every current phase declares a merge_fn (see
    ``_PhaseSpec.merge_fn``), so a lone agent's own structured_output is
    never a legitimate stand-in for a phase's full output.

    Preconditions:
        ``node_result`` is a ``NodeResult`` exposing ``get_agent_results()``.
    Postconditions:
        Returns a validated ``model_class`` instance parsed from the last
        agent's last text block; returns ``None`` when there are no agent
        results, the last agent has no usable ``message``, or the text
        doesn't parse — in which case the caller degrades to a
        default-constructed model.
    """
    agent_results = node_result.get_agent_results()
    if not agent_results:
        return None
    last = agent_results[-1]
    if hasattr(last, "message") and last.message:
        text = _collect_message_text(last.message)
        parsed = _parse_model_from_text(text, model_class)
        if parsed is not None:
            return parsed
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
