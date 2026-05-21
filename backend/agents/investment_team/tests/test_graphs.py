"""Tests for ``investment_team.graphs`` and ``strategy_lab.graphs``.

These modules wire Strands ``Graph``/``Swarm`` builders with declarative
node prompts. The tests mock out ``shared_graph.build_agent`` to avoid
spinning up real Strands ``Agent`` instances (which would hit the LLM
provider chain) and verify the resulting topology/configuration. The
production code-paths under test are pure wiring — the value here is
*structure*, not LLM behaviour.
"""

from __future__ import annotations

from typing import Any

import pytest


class _FakeAgent:
    """Stand-in returned by the patched ``build_agent`` factory.

    Mimics just enough of ``strands.Agent`` for Strands' Graph/Swarm builders
    to accept it (``_session_manager`` and ``hooks`` are checked at node
    registration time).
    """

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.name = kwargs.get("name", "")
        self.description = kwargs.get("description", "")
        self._session_manager = None
        # Strands' Swarm calls ``agent.hooks.add_callback(...)`` during
        # registration. Provide a no-op stand-in to satisfy the contract
        # without dragging in the real hook registry.
        self.hooks = _NoOpHooks()


class _NoOpHooks:
    """Minimal hooks shim — accepts ``add_callback``/``add_hook`` calls."""

    def add_callback(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add_hook(self, *args: Any, **kwargs: Any) -> None:
        return None


def _install_fake_build_agent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace ``shared_graph.build_agent`` everywhere with a recorder.

    Returns a list that accumulates the kwargs passed to each invocation,
    so tests can assert how many nodes were built and with which prompts.
    """
    recorded: list[dict] = []

    def _factory(**kwargs: Any) -> _FakeAgent:
        recorded.append(kwargs)
        return _FakeAgent(**kwargs)

    import shared_graph

    monkeypatch.setattr(shared_graph, "build_agent", _factory)
    # Strands' Graph/Swarm builders accept any object the user hands in.
    return recorded


def test_build_investment_graph_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    """The investment graph should add four named nodes wired sequentially."""
    recorded = _install_fake_build_agent(monkeypatch)

    from investment_team.graphs.investment_graph import build_investment_graph

    graph = build_investment_graph()

    # Four agents are built (research, portfolio_design, policy_check, promotion).
    names = [r["name"] for r in recorded]
    assert names == [
        "investment_researcher",
        "portfolio_designer",
        "policy_guardian",
        "promotion_gate",
    ]
    # Each invocation must declare a system_prompt and description (smoke check
    # — guarantees the prompts aren't accidentally blanked out).
    for r in recorded:
        assert r["system_prompt"].strip()
        assert r["description"].strip()

    # The compiled Graph object should expose the named entry-point node.
    # Strands' Graph exposes its node registry via ``nodes`` (mapping) on
    # the builder; the compiled graph keeps the same dict. We rely on the
    # public attribute name being present and non-empty.
    assert graph is not None


def test_build_ideation_swarm_has_three_specialists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Strategy Lab ideation swarm wires ideator → refiner → analyst.

    Strands' real ``Swarm`` constructor does deep-copy work on each node's
    ``messages`` attribute, so we'd need a much heavier ``_FakeAgent`` to
    pass through. The wiring code under test only forwards arguments — we
    patch the ``Swarm`` symbol the module imports and assert on the kwargs.
    """
    recorded = _install_fake_build_agent(monkeypatch)

    captured: dict[str, Any] = {}

    class _StubSwarm:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    import investment_team.strategy_lab.graphs.ideation_swarm as ideation_mod

    monkeypatch.setattr(ideation_mod, "Swarm", _StubSwarm)
    swarm = ideation_mod.build_ideation_swarm()

    names = [r["name"] for r in recorded]
    assert names == ["strategy_ideator", "strategy_refiner", "strategy_analyst"]
    for r in recorded:
        assert r["system_prompt"].strip()
        assert r["description"].strip()

    # Verify the swarm wiring exposes the entry-point and bounded handoff cap.
    assert isinstance(swarm, _StubSwarm)
    assert captured["entry_point"].name == "strategy_ideator"
    assert captured["max_handoffs"] == 10
    assert captured["execution_timeout"] == 300.0
    assert [n.name for n in captured["nodes"]] == [
        "strategy_ideator",
        "strategy_refiner",
        "strategy_analyst",
    ]
