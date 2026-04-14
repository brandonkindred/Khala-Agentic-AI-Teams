"""Unit tests for the DevOps Expert agent."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from devops_agent.agent import DevOpsExpertAgent, _gather_codebase_context

from software_engineering_team.tests.conftest import ConfigurableLLM


def test_devops_run_workflow_calls_plan_task_without_error() -> None:
    """run_workflow calls _plan_task and completes without AttributeError."""
    import subprocess
    import tempfile

    mock_llm = ConfigurableLLM()
    mock_llm._max_context_tokens = 16384
    mock_llm.complete_json_mock.side_effect = [
        {
            "feature_intent": "Containerize",
            "what_changes": ["Dockerfile"],
            "algorithms_data_structures": "",
            "tests_needed": "",
        },
        {
            "pipeline_yaml": "",
            "dockerfile": 'FROM node:20\nCMD ["node"]',
            "summary": "Done",
            "needs_clarification": False,
            "clarification_requests": [],
        },
    ]
    agent = DevOpsExpertAgent(llm_client=mock_llm)
    build_verifier = MagicMock(return_value=(True, ""))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        (path / "package.json").write_text("{}")
        subprocess.run(["git", "init"], cwd=path, capture_output=True, check=False)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True, check=False
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], cwd=path, capture_output=True, check=False
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"], cwd=path, capture_output=True, check=False
        )
        result = agent.run_workflow(
            repo_path=path,
            task_description="Containerize frontend",
            requirements="Add Dockerfile",
            build_verifier=build_verifier,
            task_id="devops-frontend",
        )
    assert result.success
    assert mock_llm.complete_json_mock.call_count >= 1


def test_devops_plan_task_returns_plan_markdown() -> None:
    """_plan_task parses LLM JSON and returns plan markdown."""
    mock_llm = ConfigurableLLM()
    mock_llm._max_context_tokens = 16384
    mock_llm.complete_json_mock.return_value = {
        "feature_intent": "Containerize the backend for build and deploy",
        "what_changes": ["Dockerfile", ".github/workflows/ci.yml"],
        "algorithms_data_structures": "Multi-stage Docker build; non-root user in container",
        "tests_needed": "YAML parse must succeed; docker build must complete",
    }
    agent = DevOpsExpertAgent(llm_client=mock_llm)
    plan_text = agent._plan_task(
        task_description="Containerize and deploy the backend",
        requirements="Add Dockerfile and CI pipeline",
        architecture=None,
        existing_pipeline=None,
        target_repo="backend",
    )
    assert plan_text
    assert "Containerize the backend" in plan_text
    assert "Dockerfile" in plan_text
    assert ".github/workflows/ci.yml" in plan_text
    assert "Multi-stage" in plan_text or "Docker" in plan_text
    assert "docker build" in plan_text or "YAML" in plan_text


def test_gather_codebase_context_includes_deps() -> None:
    """_gather_codebase_context includes requirements.txt and package.json when present."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        (path / "requirements.txt").write_text("fastapi==0.100.0\nuvicorn")
        (path / "package.json").write_text('{"name": "app", "scripts": {"start": "ng serve"}}')
        ctx = _gather_codebase_context(path)
    assert "requirements.txt" in ctx
    assert "fastapi" in ctx
    assert "package.json" in ctx
    assert "ng serve" in ctx
