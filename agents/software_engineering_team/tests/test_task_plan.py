"""Tests for shared TaskPlan model."""

import pytest

from software_engineering_team.shared.task_plan import TaskPlan


def test_task_plan_to_markdown() -> None:
    """TaskPlan.to_markdown() produces markdown with all fields."""
    plan = TaskPlan(
        feature_intent="Add CRUD for tasks",
        what_changes="app/routers/tasks.py, app/models/task.py",
        algorithms_data_structures="Dict for O(1) lookup",
        tests_needed="tests/test_task_endpoints.py",
    )
    md = plan.to_markdown()
    assert "Add CRUD for tasks" in md
    assert "app/routers/tasks.py" in md
    assert "O(1) lookup" in md
    assert "tests/test_task_endpoints.py" in md


def test_task_plan_from_llm_json() -> None:
    """TaskPlan.from_llm_json parses LLM JSON output."""
    data = {
        "feature_intent": "Add auth",
        "what_changes": ["app/routers/auth.py", "app/services/auth.py"],
        "algorithms_data_structures": "JWT with bcrypt",
        "tests_needed": "tests/test_auth.py",
    }
    plan = TaskPlan.from_llm_json(data)
    assert plan.feature_intent == "Add auth"
    assert plan.what_changes == ["app/routers/auth.py", "app/services/auth.py"]
    assert plan.algorithms_data_structures == "JWT with bcrypt"
    assert plan.tests_needed == "tests/test_auth.py"


def test_task_plan_from_llm_json_tolerates_missing_keys() -> None:
    """TaskPlan.from_llm_json tolerates missing or empty keys."""
    plan = TaskPlan.from_llm_json({})
    assert plan.feature_intent == ""
    assert plan.what_changes == ""
    assert plan.algorithms_data_structures == ""
    assert plan.tests_needed == ""
