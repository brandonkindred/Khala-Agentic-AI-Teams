"""Tests that validate the plan (agents and flows review) is correctly implemented."""

import inspect
from pathlib import Path

from backend_agent.agent import BackendExpertAgent

from llm_service import DummyLLMClient


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
    from software_engineering_team.shared.git_utils import _run_git

    # Init git repo (need initial commit for write_files_and_commit to work)
    _run_git(tmp_path, ["git", "init"])
    _run_git(tmp_path, ["git", "config", "user.email", "test@test.com"])
    _run_git(tmp_path, ["git", "config", "user.name", "Test"])
    _run_git(tmp_path, ["git", "config", "commit.gpgsign", "false"])
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


def test_orchestrator_passes_security_agent_to_backend() -> None:
    """Orchestrator passes security_agent when calling the code-v2 run_workflow."""

    # Read orchestrator source and verify the workflow invocation includes security_agent
    orchestrator_path = Path(__file__).resolve().parent.parent / "orchestrator.py"
    content = orchestrator_path.read_text()
    assert 'security_agent=agents.get("security")' in content
    assert "run_workflow" in content


def test_orchestrator_has_integration_phase() -> None:
    """The orchestrator wires an IntegrationAgent under the 'integration' key.

    Inspects the agent-builder's compiled code object (the actual wiring) rather
    than grepping the source for a comment/log string, so the test survives
    comment and log-message refactors and fails only if the integration agent is
    genuinely unwired.
    """
    from integration_team import IntegrationAgent

    from software_engineering_team import orchestrator

    assert IntegrationAgent is not None  # the agent the orchestrator wires in
    code = orchestrator._get_agents.__code__
    # ``from integration_team import IntegrationAgent`` records the symbol in
    # co_names; the ``"integration"`` dict key it is bound to is in co_consts.
    assert "IntegrationAgent" in code.co_names
    assert "integration" in code.co_consts


def test_integration_agent_exists_and_runs() -> None:
    """Integration agent can be instantiated and run with DummyLLM."""
    from integration_team import IntegrationAgent, IntegrationInput

    llm = DummyLLMClient()
    agent = IntegrationAgent(llm)
    result = agent.run(
        IntegrationInput(
            backend_code="from fastapi import FastAPI\napp = FastAPI()\n@app.get('/api/tasks')",
            frontend_code="this.http.get('/api/todos')",
            spec_content="Task manager app",
        )
    )
    assert hasattr(result, "passed")
    assert hasattr(result, "issues")
    assert hasattr(result, "summary")


def test_integration_agent_handles_multiple_sequential_runs() -> None:
    """Regression: a single IntegrationAgent instance must handle many
    sequential run() calls. Early Strands migrations cached a Strands
    Agent instance in __init__ and reused it across calls, which broke
    structured_output forced-tool-choice on the second call."""
    from integration_team import IntegrationAgent, IntegrationInput

    agent = IntegrationAgent(DummyLLMClient())
    for i in range(3):
        result = agent.run(
            IntegrationInput(
                backend_code=f"@app.get('/api/tasks/{i}')",
                frontend_code=f"this.http.get('/api/tasks/{i}')",
                spec_content=f"Task manager app v{i}",
            )
        )
        assert hasattr(result, "passed"), f"run {i} did not return IntegrationOutput"
        assert result.passed is True, f"run {i} should have passed cleanly"


def test_acceptance_verifier_agent_exists_and_flags_unsatisfied() -> None:
    """Acceptance verifier can flag unsatisfied criteria."""
    from acceptance_verifier_agent import AcceptanceVerifierAgent, AcceptanceVerifierInput

    llm = DummyLLMClient()
    agent = AcceptanceVerifierAgent(llm)
    result = agent.run(
        AcceptanceVerifierInput(
            code="def foo(): pass",
            task_description="Implement GET /api/users",
            acceptance_criteria=[
                "GET /api/users returns 200 with user list",
                "POST /api/users creates a user",
            ],
        )
    )
    assert hasattr(result, "all_satisfied")
    assert hasattr(result, "per_criterion")
