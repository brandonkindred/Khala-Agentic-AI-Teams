"""Unit tests for ProblemSolverAgent."""

from __future__ import annotations

import json

from software_engineering_team.problem_solver_agent.agent import ProblemSolverAgent
from software_engineering_team.problem_solver_agent.models import ProblemSolverInput
from software_engineering_team.shared import llm as llm_mod
from software_engineering_team.tests.conftest import _patch_fenced_response, _strands_model_double


def test_problem_solver_run_happy_path_returns_populated_output(monkeypatch) -> None:
    """A plain (non-fenced) JSON response is parsed via the bare json.loads branch
    inside complete_json_with_continuation, and every output field round-trips."""
    payload = {
        "plan": "Investigate null token handling in the auth middleware.",
        "execution_steps": "Add a None-guard before calling decode().",
        "review_checks": "Confirm existing login tests still pass.",
        "testing_strategy": "Add a unit test that submits a missing token.",
        "fix_recommendation": "Return 401 early when the token is None.",
    }
    text = json.dumps(payload)
    monkeypatch.setattr(llm_mod, "Agent", lambda *a, **kw: lambda prompt, **kw2: text)

    agent = ProblemSolverAgent(llm_client=_strands_model_double())
    result = agent.run(
        ProblemSolverInput(
            task_description="Fix intermittent 500 on login",
            bug_description="AttributeError: NoneType has no attribute 'decode'",
            specialty="auth",
            current_code_snapshot="def decode(token):\n    return token.decode()\n",
            cycle=2,
        )
    )

    assert result.plan == payload["plan"]
    assert result.execution_steps == payload["execution_steps"]
    assert result.review_checks == payload["review_checks"]
    assert result.testing_strategy == payload["testing_strategy"]
    assert result.fix_recommendation == payload["fix_recommendation"]


def test_problem_solver_run_recovers_fenced_json_response(monkeypatch) -> None:
    """A markdown-fenced LLM response is recovered by complete_json_with_continuation's
    extract_json_from_response fallback instead of crashing on a bare json.loads."""
    payload = {
        "plan": "Bisect the recent deploy to isolate the regression.",
        "execution_steps": "Revert the last migration and re-run the failing job.",
        "review_checks": "Check job_store state transitions for the affected job.",
        "testing_strategy": "Re-run the failing job under the reverted migration.",
        "fix_recommendation": "Add a guard before the migration reads job_store rows.",
    }
    _patch_fenced_response(monkeypatch, payload)

    agent = ProblemSolverAgent(llm_client=_strands_model_double())
    result = agent.run(
        ProblemSolverInput(
            bug_description="Job store migration crashes on empty rows",
            specialty="data",
        )
    )

    assert result.plan == payload["plan"]
    assert result.execution_steps == payload["execution_steps"]
    assert result.review_checks == payload["review_checks"]
    assert result.testing_strategy == payload["testing_strategy"]
    assert result.fix_recommendation == payload["fix_recommendation"]
