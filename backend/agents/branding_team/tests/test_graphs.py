"""Construction tests for the branding Strands graphs + agent factories.

These exercise the graph builders (``graphs/*``), the ``build_agent`` helper and
phase-order utilities (``graphs/shared``), and — transitively, since building the
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
    PHASE_ORDER,
    PHASE_TITLES,
    build_agent,
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
    ChannelActivationOutput,
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

# The three phase-3/4/5 compositors are inline ``build_agent()`` calls in the
# graph files (not ``agents.py`` factories), so they are reached through the
# built graph's node executor rather than a factory. Each now carries its own
# ``structured_output=`` model instead of a prose "output valid JSON" reminder,
# which forces Strands' typed tool call and removes the compositors' reliance on
# the free-text ``_parse_model_from_text`` recovery path.
_COMPOSITOR_CASES = [
    (build_phase3_graph, "visual_compositor", VisualIdentityOutput),
    (build_phase4_graph, "channel_compositor", ChannelActivationOutput),
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
