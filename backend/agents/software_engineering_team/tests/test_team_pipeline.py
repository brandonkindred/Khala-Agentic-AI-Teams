"""Integration tests for the full software engineering team pipeline."""

from architecture_expert import ArchitectureExpertAgent, ArchitectureInput
from qa_agent import QAExpertAgent, QAInput
from security_agent import CybersecurityExpertAgent, SecurityInput
from tech_lead_agent import TechLeadAgent, TechLeadInput

from llm_service import DummyLLMClient
from software_engineering_team.shared.models import ProductRequirements


def test_full_pipeline_with_dummy_llm() -> None:
    """
    Run Architecture -> Tech Lead -> Specialists with DummyLLMClient.

    Verifies the pipeline completes without errors and produces expected outputs.
    """
    spec = """
# Task Manager API

## Overview
Build a REST API for task management.

## Requirements
- CRUD for tasks
- JWT auth
- PostgreSQL

## Acceptance Criteria
- POST /tasks, GET /tasks
"""
    requirements = ProductRequirements(
        title="Task Manager API",
        description=spec,
        acceptance_criteria=["POST /tasks, GET /tasks"],
        constraints=["PostgreSQL"],
        priority="medium",
    )
    llm = DummyLLMClient()

    # Architecture
    arch_agent = ArchitectureExpertAgent(llm_client=llm)
    arch_output = arch_agent.run(
        ArchitectureInput(
            requirements=requirements,
            technology_preferences=["Python", "FastAPI"],
        )
    )
    assert arch_output.architecture.overview
    architecture = arch_output.architecture

    # Tech Lead
    tech_lead = TechLeadAgent(llm_client=llm)
    tech_output = tech_lead.run(TechLeadInput(requirements=requirements, architecture=architecture))
    assignment = tech_output.assignment
    assert assignment.tasks
    assert assignment.execution_order

    # Run each specialist for their assigned tasks. Backend/frontend code generation is now owned
    # by the coding_team pipeline, so this pipeline test covers the remaining SE specialists.
    agent_map = {
        "security": CybersecurityExpertAgent(llm),
        "qa": QAExpertAgent(llm),
    }

    for task_id in assignment.execution_order:
        task = next((t for t in assignment.tasks if t.id == task_id), None)
        if not task or task.assignee not in agent_map:
            continue

        agent = agent_map[task.assignee]
        if task.assignee == "security":
            result = agent.run(
                SecurityInput(
                    code="",
                    task_description=task.description,
                    architecture=architecture,
                )
            )
            assert result.vulnerabilities is not None
        elif task.assignee == "qa":
            result = agent.run(
                QAInput(
                    code="",
                    task_description=task.description,
                    architecture=architecture,
                )
            )
            assert result.bugs_found is not None
