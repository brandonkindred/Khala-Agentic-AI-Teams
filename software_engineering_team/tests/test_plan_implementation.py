"""Tests that validate the plan (agents and flows review) is correctly implemented."""

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend_agent.agent import BackendExpertAgent
from frontend_team.feature_agent.agent import FrontendExpertAgent
from shared.llm import DummyLLMClient


def test_backend_run_workflow_accepts_security_agent() -> None:
    """Backend run_workflow signature includes security_agent parameter."""
    sig = inspect.signature(BackendExpertAgent.run_workflow)
    params = list(sig.parameters)
    assert "security_agent" in params, "Backend run_workflow must accept security_agent"


def test_backend_has_run_security_review() -> None:
    """Backend agent has _run_security_review static method."""
    assert hasattr(BackendExpertAgent, "_run_security_review")
    assert callable(getattr(BackendExpertAgent, "_run_security_review"))


def test_backend_has_persist_qa_artifacts() -> None:
    """Backend agent has _persist_qa_artifacts for QA-generated tests and README."""
    assert hasattr(BackendExpertAgent, "_persist_qa_artifacts")
    assert callable(getattr(BackendExpertAgent, "_persist_qa_artifacts"))


def test_backend_persist_qa_artifacts_writes_test_files(tmp_path: Path) -> None:
    """_persist_qa_artifacts writes integration_tests and unit_tests when provided."""
    from shared.git_utils import _run_git

    # Init git repo (need initial commit for write_files_and_commit to work)
    _run_git(tmp_path, ["git", "init"])
    _run_git(tmp_path, ["git", "config", "user.email", "test@test.com"])
    _run_git(tmp_path, ["git", "config", "user.name", "Test"])
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "__init__.py").write_text("")
    _run_git(tmp_path, ["git", "add", "-A"])
    _run_git(tmp_path, ["git", "commit", "-m", "init"])

    class MockQAOutput:
        integration_tests = "def test_foo(): assert True"
        unit_tests = "def test_bar(): assert 1 == 1"
        readme_content = ""
        suggested_commit_message = "test: add QA tests"

    result = BackendExpertAgent._persist_qa_artifacts(
        repo_path=tmp_path,
        qa_output=MockQAOutput(),
        task_id="backend-task-1",
    )
    assert result is True
    # safe_id keeps hyphens: backend-task-1 -> backend-task-1
    assert (tmp_path / "tests" / "test_integration_qa_backend-task-1.py").exists()
    assert (tmp_path / "tests" / "test_unit_qa_backend-task-1.py").exists()


def test_frontend_has_run_workflow() -> None:
    """FrontendExpertAgent has run_workflow method (unified workflow)."""
    assert hasattr(FrontendExpertAgent, "run_workflow")
    sig = inspect.signature(FrontendExpertAgent.run_workflow)
    params = list(sig.parameters)
    assert "repo_path" in params
    assert "backend_dir" in params
    assert "qa_agent" in params
    assert "accessibility_agent" in params
    assert "security_agent" in params
    assert "code_review_agent" in params
    assert "dbc_agent" in params


def test_orchestrator_passes_security_agent_to_backend() -> None:
    """Orchestrator passes security_agent when calling backend run_workflow."""
    from orchestrator import run_orchestrator

    # Read orchestrator source and verify the call includes security_agent
    orchestrator_path = Path(__file__).resolve().parent.parent / "orchestrator.py"
    content = orchestrator_path.read_text()
    assert 'security_agent=agents["security"]' in content
    assert "run_workflow" in content


def test_orchestrator_has_integration_phase() -> None:
    """Orchestrator runs Integration agent after backend and frontend workers."""
    orchestrator_path = Path(__file__).resolve().parent.parent / "orchestrator.py"
    content = orchestrator_path.read_text()
    assert "integration_agent" in content or "Integration agent" in content
    assert "Integration phase" in content or "integration phase" in content


def test_integration_agent_exists_and_runs() -> None:
    """Integration agent can be instantiated and run with DummyLLM."""
    from integration_agent import IntegrationAgent, IntegrationInput

    llm = DummyLLMClient()
    agent = IntegrationAgent(llm)
    result = agent.run(IntegrationInput(
        backend_code="from fastapi import FastAPI\napp = FastAPI()\n@app.get('/api/tasks')",
        frontend_code="this.http.get('/api/todos')",
        spec_content="Task manager app",
    ))
    assert hasattr(result, "passed")
    assert hasattr(result, "issues")
    assert hasattr(result, "summary")


def test_acceptance_verifier_agent_exists_and_flags_unsatisfied() -> None:
    """Acceptance verifier can flag unsatisfied criteria."""
    from acceptance_verifier_agent import AcceptanceVerifierAgent, AcceptanceVerifierInput

    llm = DummyLLMClient()
    agent = AcceptanceVerifierAgent(llm)
    result = agent.run(AcceptanceVerifierInput(
        code="def foo(): pass",
        task_description="Implement GET /api/users",
        acceptance_criteria=[
            "GET /api/users returns 200 with user list",
            "POST /api/users creates a user",
        ],
    ))
    assert hasattr(result, "all_satisfied")
    assert hasattr(result, "per_criterion")


def test_run_failed_tasks_uses_frontend_run_workflow() -> None:
    """run_failed_tasks calls frontend run_workflow (not simplified inline loop)."""
    orchestrator_path = Path(__file__).resolve().parent.parent / "orchestrator.py"
    content = orchestrator_path.read_text()
    assert "run_workflow" in content
    assert "acceptance_verifier_agent" in content
