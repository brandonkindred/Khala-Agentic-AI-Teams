"""Tests for coding-team adapters around frontend/backend code-v2 teams."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from coding_team.models import StackSpec, Task
from coding_team.v2_team_worker import V2TeamWorker


class _FakeV2Lead:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def run_workflow(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            summary="Implemented UI changes and updated tests.",
            deliver_result=SimpleNamespace(
                branch_name="feature/ui-task",
                branch_ready=True,
                commit_messages=["feat(ui): add task"],
            ),
            failure_reason="",
        )


def test_v2_worker_requests_branch_handoff_and_threads_feedback(tmp_path) -> None:
    """The v2 worker calls the team in no-merge mode and includes Tech Lead feedback."""
    lead = _FakeV2Lead()
    worker = V2TeamWorker(
        agent_id="frontend_v2",
        stack_spec=StackSpec(name="frontend_v2", tools_services=["Angular"]),
        team_kind="frontend",
        team_lead=lead,
    )
    task = Task(
        id="ui-task",
        title="Build UI",
        description="Implement the Angular view.",
        target_team="frontend_v2",
        acceptance_criteria=["renders list"],
        revision_feedback=[
            {
                "source": "tech_lead",
                "reason": "Missing accessibility labels",
                "requested_changes": ["Add aria-labels to icon buttons"],
            }
        ],
    )

    out = worker.run_implement(task, tmp_path)

    assert out["status"] == "in_review"
    assert out["feature_branch"] == "feature/ui-task"
    assert "Feedback addressed" in out["changes_summary"]
    assert "Add aria-labels" in out["changes_summary"]
    assert lead.calls[0]["merge_to_development"] is False
    se_task = lead.calls[0]["task"]
    assert se_task.assignee == "frontend_v2"
    assert "CODING TEAM TECH LEAD FEEDBACK" in se_task.description
    assert "Missing accessibility labels" in se_task.description
    assert "renders list" in se_task.requirements


def test_v2_worker_failure_reports_task_local_failure(tmp_path) -> None:
    class _FailingLead:
        def run_workflow(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                success=False,
                summary="",
                failure_reason="pre-flight failed",
                deliver_result=SimpleNamespace(branch_name="feature/api", branch_ready=False),
            )

    worker = V2TeamWorker(
        agent_id="backend_v2",
        stack_spec=StackSpec(name="backend_v2", tools_services=["Python"]),
        team_kind="backend",
        team_lead=_FailingLead(),
    )

    out = worker.run_implement(Task(id="api", title="API", description="Build API"), tmp_path)

    assert out["status"] == "failed"
    assert out["feature_branch"] == "feature/api"
    assert out["error"] == "pre-flight failed"


def test_v2_worker_rejects_malformed_task_before_v2_handoff(tmp_path) -> None:
    class _Lead:
        def __init__(self) -> None:
            self.called = False

        def run_workflow(self, **_kwargs: Any) -> Any:
            self.called = True
            return SimpleNamespace(success=True)

    lead = _Lead()
    worker = V2TeamWorker(
        agent_id="backend_v2",
        stack_spec=StackSpec(name="backend_v2", tools_services=["Python"]),
        team_kind="backend",
        team_lead=lead,
    )

    out = worker.run_implement(SimpleNamespace(id="bad-task", title="Bad"), tmp_path)

    assert out["status"] == "failed"
    assert "missing required field" in out["error"]
    assert lead.called is False


def test_v2_worker_preserves_failed_result_even_when_branch_ready(tmp_path) -> None:
    class _PartialLead:
        def run_workflow(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                success=False,
                summary="Implemented with unresolved microtask review failures.",
                failure_reason="1 microtask failed review",
                deliver_result=SimpleNamespace(
                    branch_name="feature/api",
                    branch_ready=True,
                    commit_messages=["feat(api): partial work"],
                ),
            )

    worker = V2TeamWorker(
        agent_id="backend_v2",
        stack_spec=StackSpec(name="backend_v2", tools_services=["Python"]),
        team_kind="backend",
        team_lead=_PartialLead(),
    )

    out = worker.run_implement(Task(id="api", title="API", description="Build API"), tmp_path)

    assert out["status"] == "failed"
    assert out["feature_branch"] == "feature/api"
    assert out["changes_summary"] == "Implemented with unresolved microtask review failures."
    assert out["commands_run"] == ["feat(api): partial work"]
    assert out["error"] == "1 microtask failed review"


def test_v2_worker_uses_branch_ready_as_legacy_success_fallback(tmp_path) -> None:
    class _LegacyLead:
        def run_workflow(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                summary="Implemented API changes.",
                deliver_result=SimpleNamespace(
                    branch_name="feature/api",
                    branch_ready=True,
                    commit_messages=["feat(api): add endpoint"],
                ),
            )

    worker = V2TeamWorker(
        agent_id="backend_v2",
        stack_spec=StackSpec(name="backend_v2", tools_services=["Python"]),
        team_kind="backend",
        team_lead=_LegacyLead(),
    )

    out = worker.run_implement(Task(id="api", title="API", description="Build API"), tmp_path)

    assert out["status"] == "in_review"
    assert out["feature_branch"] == "feature/api"
