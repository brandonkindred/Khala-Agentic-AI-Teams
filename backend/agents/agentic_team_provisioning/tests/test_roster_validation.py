"""Tests for roster validation."""

from agentic_team_provisioning.models import (
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
from agentic_team_provisioning.roster_validation import validate_roster


def _agent(name: str, *, full: bool = True) -> AgenticTeamAgent:
    if full:
        return AgenticTeamAgent(
            agent_name=name,
            role=f"{name} role",
            skills=["s1"],
            capabilities=["c1"],
            tools=["t1"],
            expertise=["e1"],
        )
    return AgenticTeamAgent(agent_name=name, role=f"{name} role")


def _process(name: str, step_agents: list[str], process_id: str = "p1") -> ProcessDefinition:
    return ProcessDefinition(
        process_id=process_id,
        name=name,
        trigger=ProcessTrigger(trigger_type=TriggerType.MESSAGE, description="go"),
        steps=[
            ProcessStep(
                step_id=f"s{i+1}",
                name=f"Step {i+1}",
                agents=[ProcessStepAgent(agent_name=a, role="does stuff")],
            )
            for i, a in enumerate(step_agents)
        ],
        output=ProcessOutput(description="done", destination="out"),
        status=ProcessStatus.DRAFT,
    )


def _team(agents: list[AgenticTeamAgent], processes: list[ProcessDefinition]) -> AgenticTeam:
    return AgenticTeam(
        team_id="t1", name="T", agents=agents, processes=processes,
    )


def test_fully_staffed():
    result = validate_roster(
        _team(
            agents=[_agent("A"), _agent("B")],
            processes=[_process("P1", ["A", "B"])],
        )
    )
    assert result.is_fully_staffed is True
    assert result.gaps == []
    assert result.agent_count == 2
    assert result.process_count == 1


def test_unrostered_agent():
    result = validate_roster(
        _team(
            agents=[_agent("A")],
            processes=[_process("P1", ["A", "Ghost"])],
        )
    )
    assert result.is_fully_staffed is False
    cats = [g.category for g in result.gaps]
    assert "unrostered_agent" in cats
    assert any("Ghost" in g.detail for g in result.gaps)


def test_unused_agent():
    result = validate_roster(
        _team(
            agents=[_agent("A"), _agent("Extra")],
            processes=[_process("P1", ["A"])],
        )
    )
    assert result.is_fully_staffed is False
    cats = [g.category for g in result.gaps]
    assert "unused_agent" in cats
    assert any("Extra" in g.detail for g in result.gaps)


def test_unstaffed_step():
    proc = ProcessDefinition(
        process_id="p1",
        name="P",
        trigger=ProcessTrigger(trigger_type=TriggerType.MESSAGE, description="go"),
        steps=[ProcessStep(step_id="s1", name="Empty step", agents=[])],
        output=ProcessOutput(description="done", destination="out"),
        status=ProcessStatus.DRAFT,
    )
    result = validate_roster(_team(agents=[_agent("A")], processes=[proc]))
    assert result.is_fully_staffed is False
    assert any(g.category == "unstaffed_step" for g in result.gaps)


def test_incomplete_profile():
    result = validate_roster(
        _team(
            agents=[_agent("A", full=False)],
            processes=[_process("P1", ["A"])],
        )
    )
    assert result.is_fully_staffed is False
    assert any(g.category == "incomplete_profile" for g in result.gaps)


def test_no_agents_no_processes():
    result = validate_roster(_team(agents=[], processes=[]))
    assert result.is_fully_staffed is True
    assert "no agents and no processes" in result.summary


def test_agents_but_no_processes():
    result = validate_roster(_team(agents=[_agent("A")], processes=[]))
    assert result.is_fully_staffed is True
    assert "no processes" in result.summary


def test_processes_but_no_agents():
    result = validate_roster(
        _team(agents=[], processes=[_process("P1", ["A"])])
    )
    assert result.is_fully_staffed is False
    assert "no agents" in result.summary
