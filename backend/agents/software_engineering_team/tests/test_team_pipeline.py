"""Integration tests for the software engineering specialist pipeline."""

from architecture_expert import ArchitectureExpertAgent, ArchitectureInput
from qa_agent import QAExpertAgent, QAInput
from security_agent import CybersecurityExpertAgent, SecurityInput

from llm_service import DummyLLMClient
from shared.dev_models.models import ProductRequirements


def test_full_pipeline_with_dummy_llm() -> None:
    """
    Run Architecture -> Specialists with DummyLLMClient.

    Verifies the pipeline completes without errors and produces expected
    outputs. Task planning and code generation are owned by the coding_team
    pipeline, so this test covers the remaining SE specialists that the
    thread-mode orchestrator can still fan out to.
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

    # Specialist gates run against the architecture produced above.
    security_result = CybersecurityExpertAgent(llm).run(
        SecurityInput(
            code="",
            task_description="Review authentication and data handling for the API",
            architecture=architecture,
        )
    )
    assert security_result.vulnerabilities is not None

    qa_result = QAExpertAgent(llm).run(
        QAInput(
            code="",
            task_description="Exercise CRUD and auth flows for the API",
            architecture=architecture,
        )
    )
    assert qa_result.bugs_found is not None
