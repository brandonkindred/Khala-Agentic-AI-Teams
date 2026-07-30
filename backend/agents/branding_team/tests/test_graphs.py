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
from branding_team.graphs.top_level import build_branding_graph
from branding_team.models import BrandingMission, BrandPhase
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


def test_build_phase3_graph_is_a_graph() -> None:
    assert isinstance(build_phase3_graph(), Graph)


def test_build_phase4_graph_is_a_graph() -> None:
    assert isinstance(build_phase4_graph(), Graph)


def test_build_phase5_graph_is_a_graph() -> None:
    assert isinstance(build_phase5_graph(), Graph)


# ---------------------------------------------------------------------------
# Top-level builder — each target_phase exercises a different gating branch
# ---------------------------------------------------------------------------


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
    text = serialize_mission(
        make_mission(
            company_description="A strategic studio for enterprise product teams",
        )
    )
    assert "Northstar Labs" in text
