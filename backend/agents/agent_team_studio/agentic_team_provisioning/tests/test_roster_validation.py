"""Tests for roster validation."""

from __future__ import annotations

import pytest

from agent_registry.models import AgentManifest, CognitionSpec, SourceInfo
from agent_team_studio.agentic_team_provisioning.manifest_generation import manifest_agent_id
from agent_team_studio.agentic_team_provisioning.models import (
    AgenticTeam,
    AgenticTeamAgent,
    ProcessDefinition,
    ProcessOutput,
    ProcessStatus,
    ProcessStep,
    ProcessStepAgent,
    ProcessTrigger,
    TriggerType,
)
from agent_team_studio.agentic_team_provisioning.roster_validation import validate_roster

_TEAM_ID = "t1"
_SOURCE = SourceInfo(entrypoint="pkg.mod:Agent")


class _FakeRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, AgentManifest] = {}

    def get(self, agent_id: str) -> AgentManifest | None:
        return self._by_id.get(agent_id)

    def register(self, manifest: AgentManifest, source_path=None, *, require_persist: bool = False) -> None:
        del source_path, require_persist
        self._by_id[manifest.id] = manifest


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> _FakeRegistry:
    reg = _FakeRegistry()
    monkeypatch.setattr("agent_registry.get_registry", lambda: reg)
    return reg


def _register(registry: _FakeRegistry, manifest: AgentManifest) -> None:
    registry.register(manifest)


def _agent(registry: _FakeRegistry, name: str, *, full: bool = True) -> AgenticTeamAgent:
    manifest_id = manifest_agent_id(_TEAM_ID, name)
    if full:
        _register(
            registry,
            AgentManifest(
                id=manifest_id,
                team=_TEAM_ID,
                name=name,
                summary=f"{name} role",
                tags=["s1"],
                cognition=CognitionSpec(tools=["t1"]),
                source=_SOURCE,
            ),
        )
    else:
        _register(
            registry,
            AgentManifest(
                id=manifest_id,
                team="",
                name=name,
                summary=f"{name} role",
                tags=[],
                cognition=None,
                source=_SOURCE,
            ),
        )
    return AgenticTeamAgent(
        agent_name=name,
        source="generated",
        manifest_id=manifest_id,
    )


def _process(name: str, step_agents: list[str], process_id: str = "p1") -> ProcessDefinition:
    return ProcessDefinition(
        process_id=process_id,
        name=name,
        trigger=ProcessTrigger(trigger_type=TriggerType.MESSAGE, description="go"),
        steps=[
            ProcessStep(
                step_id=f"s{i + 1}",
                name=f"Step {i + 1}",
                agents=[ProcessStepAgent(agent_name=a, role="does stuff")],
            )
            for i, a in enumerate(step_agents)
        ],
        output=ProcessOutput(description="done", destination="out"),
        status=ProcessStatus.DRAFT,
    )


def _team(agents: list[AgenticTeamAgent], processes: list[ProcessDefinition]) -> AgenticTeam:
    return AgenticTeam(
        team_id=_TEAM_ID,
        name="T",
        agents=agents,
        processes=processes,
    )


def test_fully_staffed(registry: _FakeRegistry) -> None:
    result = validate_roster(
        _team(
            agents=[_agent(registry, "A"), _agent(registry, "B")],
            processes=[_process("P1", ["A", "B"])],
        )
    )
    assert result.is_fully_staffed is True
    assert result.gaps == []
    assert result.agent_count == 2
    assert result.process_count == 1


def test_unrostered_agent(registry: _FakeRegistry) -> None:
    result = validate_roster(
        _team(
            agents=[_agent(registry, "A")],
            processes=[_process("P1", ["A", "Ghost"])],
        )
    )
    assert result.is_fully_staffed is False
    cats = [g.category for g in result.gaps]
    assert "unrostered_agent" in cats
    assert any("Ghost" in g.detail for g in result.gaps)


def test_unused_agent(registry: _FakeRegistry) -> None:
    result = validate_roster(
        _team(
            agents=[_agent(registry, "A"), _agent(registry, "Extra")],
            processes=[_process("P1", ["A"])],
        )
    )
    assert result.is_fully_staffed is False
    cats = [g.category for g in result.gaps]
    assert "unused_agent" in cats
    assert any("Extra" in g.detail for g in result.gaps)


def test_unstaffed_step(registry: _FakeRegistry) -> None:
    proc = ProcessDefinition(
        process_id="p1",
        name="P",
        trigger=ProcessTrigger(trigger_type=TriggerType.MESSAGE, description="go"),
        steps=[ProcessStep(step_id="s1", name="Empty step", agents=[])],
        output=ProcessOutput(description="done", destination="out"),
        status=ProcessStatus.DRAFT,
    )
    result = validate_roster(_team(agents=[_agent(registry, "A")], processes=[proc]))
    assert result.is_fully_staffed is False
    assert any(g.category == "unstaffed_step" for g in result.gaps)


def test_incomplete_profile(registry: _FakeRegistry) -> None:
    result = validate_roster(
        _team(
            agents=[_agent(registry, "A", full=False)],
            processes=[_process("P1", ["A"])],
        )
    )
    assert result.is_fully_staffed is False
    assert any(g.category == "incomplete_profile" for g in result.gaps)


def test_depth_does_not_require_capabilities(registry: _FakeRegistry) -> None:
    """Manifest projection never fills capabilities; depth uses skills/tools/expertise only."""
    manifest_id = manifest_agent_id(_TEAM_ID, "A")
    _register(
        registry,
        AgentManifest(
            id=manifest_id,
            team=_TEAM_ID,
            name="A",
            summary="role",
            tags=["s1"],
            cognition=CognitionSpec(tools=["t1"]),
            source=_SOURCE,
        ),
    )
    agent = AgenticTeamAgent(agent_name="A", source="generated", manifest_id=manifest_id)
    result = validate_roster(_team(agents=[agent], processes=[_process("P1", ["A"])]))
    assert result.is_fully_staffed is True
    assert result.gaps == []


def test_no_agents_no_processes() -> None:
    result = validate_roster(_team(agents=[], processes=[]))
    assert result.is_fully_staffed is True
    assert "no agents and no processes" in result.summary


def test_agents_but_no_processes(registry: _FakeRegistry) -> None:
    result = validate_roster(_team(agents=[_agent(registry, "A")], processes=[]))
    assert result.is_fully_staffed is True
    assert "no processes" in result.summary


def test_processes_but_no_agents() -> None:
    result = validate_roster(_team(agents=[], processes=[_process("P1", ["A"])]))
    assert result.is_fully_staffed is False
    assert "no agents" in result.summary
