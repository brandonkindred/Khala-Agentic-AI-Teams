"""Integration tests for the full software engineering team pipeline."""

import pytest

from software_engineering_team.shared.llm import DummyLLMClient
from software_engineering_team.shared.models import ProductRequirements, SystemArchitecture
from software_engineering_team.shared.models import ProductRequirements
from architecture_expert import ArchitectureExpertAgent, ArchitectureInput
from tech_lead_agent import TechLeadAgent, TechLeadInput
from devops_agent import DevOpsExpertAgent, DevOpsInput
from backend_agent import BackendExpertAgent, BackendInput
from frontend_team.feature_agent import FrontendExpertAgent, FrontendInput
from security_agent import CybersecurityExpertAgent, SecurityInput
from qa_agent import QAExpertAgent, QAInput


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

    # Run each specialist for their assigned tasks
    agent_map = {
        "devops": DevOpsExpertAgent(llm),
        "backend": BackendExpertAgent(llm),
        "frontend": FrontendExpertAgent(llm),
        "security": CybersecurityExpertAgent(llm),
        "qa": QAExpertAgent(llm),
    }

    for task_id in assignment.execution_order:
        task = next((t for t in assignment.tasks if t.id == task_id), None)
        if not task or task.assignee not in agent_map:
            continue

        agent = agent_map[task.assignee]
        if task.assignee == "devops":
            result = agent.run(
                DevOpsInput(
                    task_description=task.description,
                    requirements=task.requirements,
                    architecture=architecture,
                )
            )
            assert result.summary or result.pipeline_yaml or result.iac_content
        elif task.assignee == "backend":
            result = agent.run(
                BackendInput(
                    task_description=task.description,
                    requirements=task.requirements,
                    architecture=architecture,
                    language="python",
                )
            )
            assert result.language == "python"
        elif task.assignee == "frontend":
            result = agent.run(
                FrontendInput(
                    task_description=task.description,
                    requirements=task.requirements,
                    architecture=architecture,
                )
            )
            assert result is not None
        elif task.assignee == "security":
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
