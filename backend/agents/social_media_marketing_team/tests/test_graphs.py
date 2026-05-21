"""Tests for the social marketing campaign_graph and consensus_swarm builders.

The Strands ``GraphBuilder`` / ``Swarm`` constructors validate the wired
topology when ``.build()`` is invoked, so we test the builders via
controllable fakes injected at module level. This exercises every line
in both modules without spinning up a real LLM agent.
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# consensus_swarm
# ---------------------------------------------------------------------------


def test_build_consensus_swarm_wires_three_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirm consensus swarm builds with the three named agents and the
    Swarm constructor receives the configured handoffs/timeout."""
    from social_media_marketing_team.graphs import consensus_swarm as csmod

    built_agents: list[dict[str, Any]] = []

    class _FakeAgent:
        def __init__(self, name):
            self.name = name

    def _fake_build_agent(*, name, system_prompt, description="", **kwargs):
        built_agents.append({"name": name, "prompt": system_prompt, "desc": description})
        return _FakeAgent(name)

    captured: dict[str, Any] = {}

    class _FakeSwarm:
        def __init__(self, *, nodes, entry_point, max_handoffs, execution_timeout):
            captured["nodes"] = nodes
            captured["entry_point"] = entry_point
            captured["max_handoffs"] = max_handoffs
            captured["execution_timeout"] = execution_timeout

    monkeypatch.setattr(csmod, "build_agent", _fake_build_agent)
    monkeypatch.setattr(csmod, "Swarm", _FakeSwarm)

    result = csmod.build_consensus_swarm()
    assert isinstance(result, _FakeSwarm)
    names = [a["name"] for a in built_agents]
    assert names == ["campaign_strategist", "creative_director", "audience_analyst"]
    assert captured["max_handoffs"] == 10
    assert captured["execution_timeout"] == 300.0
    assert captured["entry_point"].name == "campaign_strategist"
    assert len(captured["nodes"]) == 3


# ---------------------------------------------------------------------------
# campaign_graph
# ---------------------------------------------------------------------------


class _FakeNode:
    def __init__(self, node_id):
        self.node_id = node_id


class _FakeGraph:
    pass


class _FakeBuilder:
    def __init__(self):
        self.graph_id = None
        self.execution_timeout = None
        self.node_timeout = None
        self.entry_point = None
        self.nodes: list[_FakeNode] = []
        self.edges: list[tuple[_FakeNode, _FakeNode]] = []

    def set_graph_id(self, graph_id):
        self.graph_id = graph_id

    def set_execution_timeout(self, timeout):
        self.execution_timeout = timeout

    def set_node_timeout(self, timeout):
        self.node_timeout = timeout

    def add_node(self, agent_or_swarm, node_id):
        node = _FakeNode(node_id)
        self.nodes.append(node)
        return node

    def set_entry_point(self, node_or_id):
        self.entry_point = node_or_id

    def add_edge(self, src, dst):
        self.edges.append((src, dst))

    def build(self):
        return _FakeGraph()


def test_build_campaign_graph_wires_full_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: consensus → concept_gen → 4 platform specialists → experiment."""
    from social_media_marketing_team.graphs import campaign_graph as cgmod

    monkeypatch.setattr(cgmod, "GraphBuilder", _FakeBuilder)

    # Avoid touching the real Swarm or LLMs
    monkeypatch.setattr(cgmod, "build_consensus_swarm", lambda: object())
    monkeypatch.setattr(cgmod, "build_agent", lambda **kwargs: object())

    graph = cgmod.build_campaign_graph()
    assert isinstance(graph, _FakeGraph)


def test_build_campaign_graph_records_expected_node_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inspect the builder after construction to confirm topology."""
    from social_media_marketing_team.graphs import campaign_graph as cgmod

    builder_holder: dict[str, _FakeBuilder] = {}

    class _CapturingBuilder(_FakeBuilder):
        def build(self):
            builder_holder["b"] = self
            return _FakeGraph()

    monkeypatch.setattr(cgmod, "GraphBuilder", _CapturingBuilder)
    monkeypatch.setattr(cgmod, "build_consensus_swarm", lambda: object())
    monkeypatch.setattr(cgmod, "build_agent", lambda **kwargs: object())

    cgmod.build_campaign_graph()
    b = builder_holder["b"]
    assert b.graph_id == "social_media_campaign"
    assert b.execution_timeout == 600.0
    assert b.node_timeout == 180.0
    node_ids = [n.node_id for n in b.nodes]
    # consensus + concept_generation + 4 platforms + experiment_design
    assert "consensus" in node_ids
    assert "concept_generation" in node_ids
    assert {"linkedin", "facebook", "instagram", "x_twitter"}.issubset(set(node_ids))
    assert "experiment_design" in node_ids
    # consensus is entry (passed as string id by the builder)
    assert b.entry_point == "consensus"

    # Edge counts: consensus->concept_gen (1) + 4 platform fan-out + 4 fan-in = 9
    assert len(b.edges) == 1 + 4 + 4
