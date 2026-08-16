"""Construction tests for the branding Strands graphs + agent factories.

These exercise the graph builders (``graphs/*``), the ``build_agent`` / ``build_compositor``
helpers and phase-order utilities (``graphs/shared``), and — transitively, since building the
graphs instantiates every node — the ~35 agent factories in ``agents.py``. They
run under ``LLM_PROVIDER=dummy`` (no real LLM, no Postgres): construction resolves
a dummy Strands model and never invokes it.
"""

from __future__ import annotations

import pytest
from strands import Agent
from strands.multiagent.graph import Graph

from branding_team.graphs.phase1_strategic_core import build_phase1_graph
from branding_team.graphs.phase2_narrative import build_phase2_graph, build_phase2_swarm
from branding_team.graphs.phase3_visual import build_phase3_graph
from branding_team.graphs.phase4_channel import build_phase4_graph
from branding_team.graphs.phase5_governance import build_phase5_graph
from branding_team.graphs.shared import (
    COMPOSITOR_AGENT_KEY,
    PHASE_ORDER,
    PHASE_TITLES,
    build_agent,
    build_compositor,
    phase_agent_key,
    phase_index,
    phase_order_text,
    serialize_mission,
    should_advance_past,
)
from branding_team.graphs.top_level import (
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    DEFAULT_NODE_TIMEOUT_SECONDS,
    build_branding_graph,
)
from branding_team.models import (
    BrandingMission,
    BrandPhase,
    GovernanceOutput,
    VisualIdentityOutput,
)
from branding_team.tests.conftest import make_mission

# ---------------------------------------------------------------------------
# Per-phase builders (each transitively constructs its specialist agents)
# ---------------------------------------------------------------------------


def test_build_phase1_graph_is_a_graph() -> None:
    assert isinstance(build_phase1_graph(), Graph)


def test_build_phase2_graph_is_a_graph() -> None:
    assert isinstance(build_phase2_graph(), Graph)


def test_build_phase2_swarm_alias_returns_graph() -> None:
    """Phase 2 used to be a Swarm; the alias still works and returns the Graph."""
    assert build_phase2_swarm is build_phase2_graph
    assert isinstance(build_phase2_swarm(), Graph)


def test_build_phase2_graph_wires_linear_chain() -> None:
    """Phase 2 uses a single-predecessor chain (Strands multi-in edges are OR-ready).

    Upstream narrative travels via cumulative ``structured_output`` models, not
    fan-in edges.
    """
    from branding_team.graphs.phase2_narrative import _PHASE2_NODE_ORDER

    graph = build_phase2_graph()
    edges = {(e.from_node.node_id, e.to_node.node_id) for e in graph.edges}
    expected = set(zip(_PHASE2_NODE_ORDER, _PHASE2_NODE_ORDER[1:]))
    assert edges == expected
    assert len(edges) == 5


def test_build_phase3_graph_is_a_graph() -> None:
    assert isinstance(build_phase3_graph(), Graph)


def test_build_phase3_graph_wires_diverge_and_fan_out() -> None:
    """Phase 3 diverge is a Graph fan-out into CreativeDirector (not a Swarm).

    ``structured_output=`` stops the agent loop, so handoff-based Swarm
    sequencing cannot drive the moodboard conceptualists.
    """
    from branding_team.graphs.phase3_visual import (
        _PHASE3_CONCEPTUALIST_VARIANTS,
        _PHASE3_SPECIALIST_FACTORIES,
    )

    graph = build_phase3_graph()
    edges = {(e.from_node.node_id, e.to_node.node_id) for e in graph.edges}
    node_ids = set(graph.nodes.keys())

    assert "diverge_swarm" not in node_ids

    conceptualists = {
        f"MoodBoardConceptualist_{variant}" for variant in _PHASE3_CONCEPTUALIST_VARIANTS
    }
    assert {n.node_id for n in graph.entry_points} == conceptualists

    for conceptualist in conceptualists:
        assert (conceptualist, "CreativeDirector") in edges
    assert ("CreativeDirector", "converge_decider") in edges

    for specialist in _PHASE3_SPECIALIST_FACTORIES:
        assert ("converge_decider", specialist) in edges
        assert (specialist, "visual_compositor") in edges


def test_make_moodboard_conceptualist_rejects_blank_variant() -> None:
    """Documented variant precondition is enforced before agent construction."""
    from branding_team.agents import make_moodboard_conceptualist

    with pytest.raises(AssertionError, match="variant must be a non-empty string"):
        make_moodboard_conceptualist("")
    with pytest.raises(AssertionError, match="variant must be a non-empty string"):
        make_moodboard_conceptualist("   ")


def test_build_phase4_graph_is_a_graph() -> None:
    assert isinstance(build_phase4_graph(), Graph)


def test_build_phase4_graph_wires_pure_fan_out() -> None:
    """Phase 4 has no compositor: all nine specialists are both entry points
    and terminal nodes, with no edges between them. Their typed fragments are
    merged into ``ChannelActivationOutput`` in Python by the orchestrator's
    Phase-4 ``merge_fn``, not by an LLM fan-in node.
    """
    graph = build_phase4_graph()
    expected = {
        "brand_experience_principler",
        "website_guide",
        "social_guide",
        "email_guide",
        "events_guide",
        "partnerships_guide",
        "internal_guide",
        "brand_architecture_builder",
        "brand_in_action_illustrator",
    }
    assert set(graph.nodes.keys()) == expected
    assert {n.node_id for n in graph.entry_points} == expected
    assert len(graph.edges) == 0
    assert "channel_compositor" not in graph.nodes


def test_make_channel_guide_rejects_blank_channel_or_description() -> None:
    """Documented channel/description preconditions are enforced before construction."""
    from branding_team.agents import _make_channel_guide
    from branding_team.models import ChannelGuidelineOutput

    with pytest.raises(AssertionError, match="channel must be a non-empty string"):
        _make_channel_guide("", "some description", ChannelGuidelineOutput)
    with pytest.raises(AssertionError, match="channel must be a non-empty string"):
        _make_channel_guide("   ", "some description", ChannelGuidelineOutput)
    with pytest.raises(AssertionError, match="description must be a non-empty string"):
        _make_channel_guide("website", "", ChannelGuidelineOutput)
    with pytest.raises(AssertionError, match="description must be a non-empty string"):
        _make_channel_guide("website", "   ", ChannelGuidelineOutput)


def test_make_channel_guide_rejects_non_basemodel_structured_output() -> None:
    """Documented structured_output precondition is enforced before construction."""
    from branding_team.agents import _make_channel_guide

    with pytest.raises(
        AssertionError, match="structured_output must be a Pydantic BaseModel subclass"
    ):
        _make_channel_guide("website", "a marketing site", dict)


def test_phase4_prompts_drop_redundant_json_reminder() -> None:
    """The Pydantic structured-output schema is the contract; the redundant
    "Output valid JSON" sentence is no longer needed for any of the 9 Phase 4
    factories migrated to ``build_agent(structured_output=...)``.
    """
    from branding_team.agents import (
        make_brand_architecture_builder,
        make_brand_experience_principler,
        make_brand_in_action_illustrator,
        make_email_guide,
        make_events_guide,
        make_internal_guide,
        make_partnerships_guide,
        make_social_guide,
        make_website_guide,
    )

    for factory in (
        make_website_guide,
        make_social_guide,
        make_email_guide,
        make_events_guide,
        make_partnerships_guide,
        make_internal_guide,
        make_brand_architecture_builder,
        make_brand_in_action_illustrator,
        make_brand_experience_principler,
    ):
        agent = factory()
        assert "Output valid JSON" not in agent.system_prompt, factory.__name__


def test_build_phase5_graph_is_a_graph() -> None:
    assert isinstance(build_phase5_graph(), Graph)


def test_phase5_prompts_drop_redundant_json_reminder() -> None:
    """The Pydantic structured-output schema is the contract; the redundant
    "Output valid JSON" sentence is no longer needed for any of the 7 Phase 5
    factories migrated to ``build_agent(structured_output=...)``.
    """
    from branding_team.agents import (
        make_approval_workflow_designer,
        make_asset_wiki_planner,
        make_brand_rules_codifier,
        make_evolution_framer,
        make_kpi_designer,
        make_ownership_definer,
        make_training_planner,
    )

    for factory in (
        make_ownership_definer,
        make_approval_workflow_designer,
        make_asset_wiki_planner,
        make_training_planner,
        make_kpi_designer,
        make_evolution_framer,
        make_brand_rules_codifier,
    ):
        agent = factory()
        assert "Output valid JSON" not in agent.system_prompt, factory.__name__


# ---------------------------------------------------------------------------
# Compositor (fan-in join) nodes — migrated to structured_output=
# ---------------------------------------------------------------------------

# The remaining phase-3/5 compositors are inline ``build_agent()`` calls in the
# graph files (not ``agents.py`` factories), so they are reached through the
# built graph's node executor rather than a factory. Each now carries its own
# ``structured_output=`` model instead of a prose "output valid JSON" reminder,
# which forces Strands' typed tool call and removes the compositors' reliance on
# the free-text ``_parse_model_from_text`` recovery path. Phase 4 no longer has
# a compositor (see ``test_build_phase4_graph_wires_pure_fan_out``).
_COMPOSITOR_CASES = [
    (build_phase3_graph, "visual_compositor", VisualIdentityOutput),
    (build_phase5_graph, "governance_compositor", GovernanceOutput),
]


@pytest.mark.parametrize(
    "build_graph,node_id,output_model",
    _COMPOSITOR_CASES,
    ids=[node_id for _build, node_id, _model in _COMPOSITOR_CASES],
)
def test_compositor_uses_structured_output_and_drops_json_reminder(
    build_graph, node_id, output_model
) -> None:
    """Each fan-in compositor passes ``structured_output=`` and no longer names
    a raw-JSON output format in its prose — the Pydantic schema is the contract.
    """
    graph = build_graph()
    executor = graph.nodes[node_id].executor

    # The Pydantic schema is wired as the agent's structured-output model
    # (Strands stores the ``structured_output_model=`` ctor arg here)...
    assert getattr(executor, "_default_structured_output_model", None) is output_model
    # ...and the redundant "output valid JSON" prose instruction is gone.
    assert "valid JSON" not in executor.system_prompt, node_id


# ---------------------------------------------------------------------------
# Top-level builder — each target_phase exercises a different gating branch
# ---------------------------------------------------------------------------


def test_default_graph_timeout_constants() -> None:
    """Monolithic graph and single-phase runs share these budgets."""
    assert DEFAULT_EXECUTION_TIMEOUT_SECONDS == 600.0
    assert DEFAULT_NODE_TIMEOUT_SECONDS == 180.0


@pytest.mark.parametrize(
    "target_phase",
    [
        None,
        BrandPhase.STRATEGIC_CORE,
        BrandPhase.NARRATIVE_MESSAGING,
        BrandPhase.VISUAL_IDENTITY,
        BrandPhase.CHANNEL_ACTIVATION,
        BrandPhase.GOVERNANCE,
    ],
)
def test_build_branding_graph_for_each_target_phase(target_phase) -> None:
    assert isinstance(build_branding_graph(target_phase=target_phase), Graph)


# ---------------------------------------------------------------------------
# build_agent helper (graphs/shared)
# ---------------------------------------------------------------------------


def test_build_agent_json_mode() -> None:
    agent = build_agent(name="a1", system_prompt="do x", output_mode="json")
    assert isinstance(agent, Agent)
    assert agent.name == "a1"


def test_build_agent_text_mode_with_description_and_tools() -> None:
    agent = build_agent(
        name="a2",
        system_prompt="do y",
        output_mode="text",
        description="a helper",
        tools=[],
    )
    assert isinstance(agent, Agent)


def test_build_agent_structured_output() -> None:
    agent = build_agent(name="a3", system_prompt="do z", structured_output=BrandingMission)
    assert isinstance(agent, Agent)


def test_build_agent_rejects_bad_output_mode() -> None:
    with pytest.raises(ValueError, match="output_mode must be"):
        build_agent(name="bad", system_prompt="x", output_mode="xml")  # type: ignore[arg-type]


def test_build_agent_with_agent_key_override() -> None:
    agent = build_agent(name="a4", system_prompt="do w", agent_key="branding_assistant")
    assert isinstance(agent, Agent)
    assert agent.name == "a4"


def _resolved_agent_key(agent: Agent) -> str:
    """Read back the ``agent_key`` that reached ``get_strands_model`` for *agent*.

    ``build_agent`` routes its ``agent_key`` argument through
    ``get_strands_model(agent_key, ...)`` into the ``LLMClientModel`` config;
    this recovers it the same way production code (and other teams' tests,
    e.g. ``llm_service/tests/test_strands_adapter.py``) verify per-agent
    routing — via the model's own ``get_config()``, not the ``build_agent``
    call arguments, so a mistake inside ``build_agent``'s forwarding would
    also be caught.
    """
    return agent.model.get_config()["agent_key"]


def test_build_agent_forwards_agent_key_to_model_config() -> None:
    """``build_agent``'s ``agent_key`` reaches ``get_strands_model`` (not just accepted)."""
    agent = build_agent(name="a5", system_prompt="do v", agent_key="branding_strategic_core")
    assert _resolved_agent_key(agent) == "branding_strategic_core"


def test_build_agent_default_agent_key_is_branding() -> None:
    """Omitting ``agent_key`` still resolves the historical "branding" default."""
    agent = build_agent(name="a6", system_prompt="do u")
    assert _resolved_agent_key(agent) == "branding"


def test_build_compositor_pins_compositor_agent_key() -> None:
    """``build_compositor`` is the one call site that decides the compositor tier;
    callers cannot override or omit it (no ``agent_key`` parameter is exposed)."""
    agent = build_compositor(name="a7", system_prompt="assemble")
    assert isinstance(agent, Agent)
    assert agent.name == "a7"
    assert _resolved_agent_key(agent) == COMPOSITOR_AGENT_KEY


def test_build_compositor_forwards_description() -> None:
    agent = build_compositor(name="a8", system_prompt="assemble", description="joins things")
    assert agent.description == "joins things"


# ---------------------------------------------------------------------------
# Per-phase agent_key tiers (graphs/shared.phase_agent_key + agents.py wiring)
# ---------------------------------------------------------------------------


def test_phase_agent_key_derives_from_phase_value() -> None:
    assert phase_agent_key(BrandPhase.STRATEGIC_CORE) == "branding_strategic_core"
    assert phase_agent_key(BrandPhase.NARRATIVE_MESSAGING) == "branding_narrative_messaging"
    assert phase_agent_key(BrandPhase.VISUAL_IDENTITY) == "branding_visual_identity"
    assert phase_agent_key(BrandPhase.CHANNEL_ACTIVATION) == "branding_channel_activation"
    assert phase_agent_key(BrandPhase.GOVERNANCE) == "branding_governance"


def test_phase_and_compositor_agent_keys_are_shell_safe() -> None:
    """Keys must be valid identifiers so ``LLM_MODEL_<agent_key>`` can be exported."""
    keys = [phase_agent_key(phase) for phase in PHASE_ORDER] + [COMPOSITOR_AGENT_KEY]
    for key in keys:
        assert key.isidentifier(), key
        assert "." not in key


def test_phase1_factories_use_strategic_core_agent_key() -> None:
    from branding_team.agents import (
        make_audience_segmenter,
        make_differentiation_mapper,
        make_discovery_auditor,
        make_positioning_synthesizer,
        make_purpose_vision_writer,
        make_values_articulator,
    )

    expected = phase_agent_key(BrandPhase.STRATEGIC_CORE)
    for factory in (
        make_discovery_auditor,
        make_purpose_vision_writer,
        make_values_articulator,
        make_audience_segmenter,
        make_differentiation_mapper,
        make_positioning_synthesizer,
    ):
        assert _resolved_agent_key(factory()) == expected, factory.__name__


def test_phase2_factories_use_narrative_messaging_agent_key() -> None:
    from branding_team.agents import (
        make_archetype_analyst,
        make_message_mapper,
        make_persona_builder,
        make_storyteller,
        make_tagline_writer,
        make_voice_principles_drafter,
    )

    expected = phase_agent_key(BrandPhase.NARRATIVE_MESSAGING)
    for factory in (
        make_storyteller,
        make_archetype_analyst,
        make_tagline_writer,
        make_message_mapper,
        make_persona_builder,
        make_voice_principles_drafter,
    ):
        assert _resolved_agent_key(factory()) == expected, factory.__name__


def test_phase3_factories_use_visual_identity_agent_key() -> None:
    from branding_team.agents import (
        make_color_system_builder,
        make_converge_decider,
        make_creative_director,
        make_design_system_codifier,
        make_iconography_director,
        make_logo_specifier,
        make_moodboard_conceptualist,
        make_photography_video_director,
        make_typography_builder,
        make_voice_tone_builder,
    )

    expected = phase_agent_key(BrandPhase.VISUAL_IDENTITY)
    for factory in (
        make_creative_director,
        make_converge_decider,
        make_logo_specifier,
        make_color_system_builder,
        make_typography_builder,
        make_iconography_director,
        make_photography_video_director,
        make_voice_tone_builder,
        make_design_system_codifier,
    ):
        assert _resolved_agent_key(factory()) == expected, factory.__name__
    assert _resolved_agent_key(make_moodboard_conceptualist("Editorial")) == expected


def test_phase4_factories_use_channel_activation_agent_key() -> None:
    from branding_team.agents import (
        make_brand_architecture_builder,
        make_brand_experience_principler,
        make_brand_in_action_illustrator,
        make_email_guide,
        make_events_guide,
        make_internal_guide,
        make_partnerships_guide,
        make_social_guide,
        make_website_guide,
    )

    expected = phase_agent_key(BrandPhase.CHANNEL_ACTIVATION)
    for factory in (
        make_brand_experience_principler,
        make_website_guide,
        make_social_guide,
        make_email_guide,
        make_events_guide,
        make_partnerships_guide,
        make_internal_guide,
        make_brand_architecture_builder,
        make_brand_in_action_illustrator,
    ):
        assert _resolved_agent_key(factory()) == expected, factory.__name__


def test_phase5_factories_use_governance_agent_key() -> None:
    from branding_team.agents import (
        make_approval_workflow_designer,
        make_asset_wiki_planner,
        make_brand_rules_codifier,
        make_evolution_framer,
        make_kpi_designer,
        make_ownership_definer,
        make_training_planner,
    )

    expected = phase_agent_key(BrandPhase.GOVERNANCE)
    for factory in (
        make_ownership_definer,
        make_approval_workflow_designer,
        make_asset_wiki_planner,
        make_training_planner,
        make_kpi_designer,
        make_evolution_framer,
        make_brand_rules_codifier,
    ):
        assert _resolved_agent_key(factory()) == expected, factory.__name__


def test_compositor_nodes_use_compositor_agent_key() -> None:
    """The two phase-terminal join agents share the cross-phase compositor tier,
    not their own phase's agent_key. Phase 4 has no compositor node."""
    graphs = {
        "visual_compositor": build_phase3_graph(),
        "governance_compositor": build_phase5_graph(),
    }
    for node_id, graph in graphs.items():
        compositor_agent = graph.nodes[node_id].executor
        assert _resolved_agent_key(compositor_agent) == COMPOSITOR_AGENT_KEY, node_id


# ---------------------------------------------------------------------------
# phase-order utilities (graphs/shared)
# ---------------------------------------------------------------------------


def test_phase_index_known_and_unknown() -> None:
    assert phase_index(BrandPhase.STRATEGIC_CORE) == 0
    assert phase_index(BrandPhase.GOVERNANCE) == len(PHASE_ORDER) - 1
    # COMPLETE is not a pipeline phase → out-of-list sentinel (len(PHASE_ORDER)).
    assert phase_index(BrandPhase.COMPLETE) == len(PHASE_ORDER)


def test_should_advance_past() -> None:
    # None target → always advance.
    assert should_advance_past(0, None) is True
    # Advance past phase 0 only if the target is beyond it.
    assert should_advance_past(0, BrandPhase.NARRATIVE_MESSAGING) is True
    assert should_advance_past(4, BrandPhase.STRATEGIC_CORE) is False


def test_phase_order_text_matches_hand_written_description() -> None:
    assert phase_order_text() == (
        "Phase 1 — Strategic Core\n"
        "Phase 2 — Narrative & Messaging\n"
        "Phase 3 — Visual & Expressive Identity\n"
        "Phase 4 — Experience & Channel Activation\n"
        "Phase 5 — Governance & Evolution"
    )


def test_phase_order_text_covers_every_phase_order_entry() -> None:
    assert set(PHASE_ORDER) <= set(PHASE_TITLES)


def test_phase_order_text_driven_by_phase_order(monkeypatch: pytest.MonkeyPatch) -> None:
    import branding_team.graphs.shared as shared_mod

    monkeypatch.setattr(
        shared_mod,
        "PHASE_ORDER",
        [BrandPhase.GOVERNANCE, BrandPhase.STRATEGIC_CORE],
    )
    assert shared_mod.phase_order_text() == (
        "Phase 1 — Governance & Evolution\nPhase 2 — Strategic Core"
    )


def test_serialize_mission_roundtrips_company_name() -> None:
    mission = make_mission(
        company_name="Acme Rebrand Co",
        company_description="A strategic studio for enterprise product teams",
    )
    text = serialize_mission(mission)
    assert mission.company_name in text
    assert mission.company_description in text
