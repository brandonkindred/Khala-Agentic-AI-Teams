"""Tests for the lazy SE agent fleet built by ``_get_agents``.

The fleet is a :class:`_LazyAgentRegistry`: the agent is constructed on first
subscript and cached, while membership tests and iteration never construct. Only
``architecture`` is read in production, so these tests pin that building the
registry (and probing it with ``in`` / ``len`` / iteration) resolves *no*
``get_client`` calls, and that a subscript builds-and-caches exactly once.

Runs under the SE conftest, which sets ``LLM_PROVIDER=dummy`` — so the real
``get_client`` resolves a ``DummyLLMClient`` and agents construct for real.
"""

from __future__ import annotations

import sys
from pathlib import Path

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

import orchestrator  # noqa: E402
import pytest  # noqa: E402

# The single role the fleet factory exposes. Kept explicit so a drift in the
# registry's key set is a visible test failure, not a silent behavior change.
_EXPECTED_ROLES = {
    "architecture",
}


@pytest.fixture
def get_client_spy(monkeypatch):
    """Replace ``orchestrator.get_client`` with a call-recording delegate.

    Postconditions: yields the list of role keys passed to ``get_client``, in
    call order; each call still returns the real (dummy) client so agents
    construct normally.
    """
    real_get_client = orchestrator.get_client
    calls: list[str] = []

    def spy(key, *args, **kwargs):
        calls.append(key)
        return real_get_client(key, *args, **kwargs)

    monkeypatch.setattr(orchestrator, "get_client", spy)
    return calls


def test_building_and_probing_the_fleet_constructs_nothing(get_client_spy):
    """``_get_agents`` + membership/iteration/len must not build any agent."""
    agents = orchestrator._get_agents()

    assert isinstance(agents, orchestrator._LazyAgentRegistry)
    # Membership reflects the key set without constructing.
    assert "architecture" in agents
    # Iteration and length see every role, still without constructing.
    assert set(agents) == _EXPECTED_ROLES
    assert len(agents) == len(_EXPECTED_ROLES)

    assert get_client_spy == [], "probing the registry must not resolve any client"


def test_subscript_builds_and_caches_the_agent(get_client_spy):
    """First subscript builds via ``get_client``; repeat access is cached."""
    from software_engineering_team.architect_agents.architecture_expert import (
        ArchitectureExpertAgent,
    )

    agents = orchestrator._get_agents()

    arch = agents["architecture"]
    assert isinstance(arch, ArchitectureExpertAgent)
    assert get_client_spy == ["architecture"]

    # Second access returns the identical cached instance and adds no new call.
    assert agents["architecture"] is arch
    assert get_client_spy == ["architecture"]


def test_unknown_role_raises_key_error():
    """Subscripting a role that has no factory raises ``KeyError``."""
    agents = orchestrator._get_agents()

    with pytest.raises(KeyError):
        agents["does_not_exist"]
