"""Tests for Tech Lead agent."""

import pytest
from tech_lead_agent import TechLeadAgent, TechLeadInput

from llm_service import DummyLLMClient
from software_engineering_team.shared.models import ProductRequirements, SystemArchitecture


@pytest.fixture
def requirements() -> ProductRequirements:
    return ProductRequirements(
        title="Task Manager",
        description="REST API for tasks",
        acceptance_criteria=["CRUD for tasks"],
        constraints=[],
        priority="medium",
    )


@pytest.fixture
def architecture() -> SystemArchitecture:
    return SystemArchitecture(
        overview="API + WebApp",
        components=[],
    )


def test_tech_lead_assigns_tasks(
    requirements: ProductRequirements, architecture: SystemArchitecture
) -> None:
    """Tech Lead returns TaskAssignment with tasks and execution order."""
    llm = DummyLLMClient()
    agent = TechLeadAgent(llm_client=llm)
    result = agent.run(TechLeadInput(requirements=requirements, architecture=architecture))
    assert len(result.assignment.tasks) >= 1
    assert result.assignment.execution_order
    assert all(t.id in result.assignment.execution_order for t in result.assignment.tasks)


def test_tech_lead_tasks_have_assignees(
    requirements: ProductRequirements, architecture: SystemArchitecture
) -> None:
    """Each task has an assignee."""
    llm = DummyLLMClient()
    agent = TechLeadAgent(llm_client=llm)
    result = agent.run(TechLeadInput(requirements=requirements, architecture=architecture))
    for task in result.assignment.tasks:
        assert task.assignee
        assert task.description or task.id
