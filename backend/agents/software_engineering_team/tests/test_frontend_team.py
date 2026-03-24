"""Tests for the Frontend Engineering Team."""

import inspect

from frontend_team import FrontendOrchestratorAgent
from frontend_team.feature_agent import FrontendExpertAgent
from frontend_team.orchestrator import _is_lightweight_task

from llm_service import DummyLLMClient
from software_engineering_team.shared.models import Task, TaskType


def test_frontend_orchestrator_run_workflow_signature_matches_frontend_expert() -> None:
    """FrontendOrchestratorAgent.run_workflow has the same signature as FrontendExpertAgent.run_workflow."""
    fe_sig = inspect.signature(FrontendExpertAgent.run_workflow)
    fo_sig = inspect.signature(FrontendOrchestratorAgent.run_workflow)

    fe_params = set(fe_sig.parameters)
    fo_params = set(fo_sig.parameters)

    assert fe_params == fo_params, (
        f"Signature mismatch: FrontendOrchestrator has {fo_params - fe_params} extra params, "
        f"missing {fe_params - fo_params}"
    )

    # Return type: both return FrontendWorkflowResult (from frontend_team.feature_agent.models)
    assert FrontendOrchestratorAgent.run_workflow.__annotations__.get("return") is None or (
        "FrontendWorkflowResult"
        in str(FrontendOrchestratorAgent.run_workflow.__annotations__.get("return", ""))
    )


def test_is_lightweight_task_returns_true_for_fix_task() -> None:
    """Lightweight path: fix-type tasks skip design phase."""
    task = Task(
        id="fix-a11y",
        type=TaskType.FRONTEND,
        title="Fix a11y issues",
        description="Fix accessibility issues in the login form: add aria-labels and fix focus order.",
        assignee="frontend",
    )
    assert _is_lightweight_task(task) is True


def test_is_lightweight_task_returns_true_for_resolve_task() -> None:
    """Lightweight path: resolve-type tasks skip design phase."""
    task = Task(
        id="resolve-bug",
        type=TaskType.FRONTEND,
        title="Resolve navigation bug",
        description="Resolve the bug where back button does not work.",
        assignee="frontend",
    )
    assert _is_lightweight_task(task) is True


def test_is_lightweight_task_returns_false_for_full_feature_task() -> None:
    """Full design path: new feature tasks run UX, UI, Design System."""
    task = Task(
        id="frontend-dashboard",
        type=TaskType.FRONTEND,
        title="Implement dashboard",
        description=(
            "Implement the main dashboard view with user statistics, recent activity feed, "
            "and quick action buttons. The dashboard should display key metrics and allow "
            "users to navigate to detailed views. Include loading and empty states."
        ),
        assignee="frontend",
    )
    assert _is_lightweight_task(task) is False


def test_is_lightweight_task_returns_true_for_refactor_task() -> None:
    """Lightweight path: refactor-type tasks skip design phase."""
    task = Task(
        id="refactor-form",
        type=TaskType.FRONTEND,
        title="Refactor form validation",
        description="Refactor the user form to use reactive validators and improve error messages.",
        assignee="frontend",
    )
    assert _is_lightweight_task(task) is True


def test_is_lightweight_task_returns_true_for_adjust_task() -> None:
    """Lightweight path: adjust-type tasks skip design phase."""
    task = Task(
        id="adjust-layout",
        type=TaskType.FRONTEND,
        title="Adjust layout",
        description="Adjust the grid layout to fix overflow on mobile.",
        assignee="frontend",
    )
    assert _is_lightweight_task(task) is True


def test_is_lightweight_task_returns_false_for_long_implement_task() -> None:
    """Full design path: long descriptions with 'implement' are not lightweight even under 400 chars."""
    task = Task(
        id="implement-feature",
        type=TaskType.FRONTEND,
        title="Implement feature",
        description=(
            "Implement the user profile page with avatar upload, bio editor, and preferences. "
            "Include validation and loading states. The page should integrate with the auth service."
        ),
        assignee="frontend",
    )
    assert _is_lightweight_task(task) is False


def test_frontend_orchestrator_instantiates_with_llm() -> None:
    """FrontendOrchestratorAgent can be instantiated with LLM client."""
    llm = DummyLLMClient()
    agent = FrontendOrchestratorAgent(llm)
    assert agent.llm is llm
    assert agent.feature_agent is not None
    assert agent.ux_designer is not None
    assert agent.ui_designer is not None
    assert agent.design_system is not None
    assert agent.frontend_architect is not None
    assert agent.ux_engineer is not None
    assert agent.performance_engineer is not None
    assert agent.build_release is not None
