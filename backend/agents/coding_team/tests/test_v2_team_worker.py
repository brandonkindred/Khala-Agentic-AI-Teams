"""Tests for coding-team adapters around frontend/backend code-v2 teams."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from coding_team import v2_team_worker as worker_mod
from coding_team.models import StackSpec, Task
from coding_team.v2_team_worker import V2TeamWorker


class _FakeV2Lead:
    def __init__(self, order: List[str] | None = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.order = order

    def run_workflow(self, **kwargs: Any) -> Any:
        if self.order is not None:
            self.order.append("workflow")
        self.calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            summary="Implemented UI changes and updated tests.",
            deliver_result=SimpleNamespace(
                branch_name="feature/ui-task",
                branch_ready=True,
                commit_messages=["feat(ui): add task"],
                delivered_files=["src/app.component.ts"],
            ),
            failure_reason="",
        )


def _patch_branch_handoff(monkeypatch, *, branch: str = "feature/api", order=None):
    def _create_feature_branch(repo_path, base_branch, feature_name):
        if order is not None:
            order.append("create")
        return True, branch

    def _checkout_branch(repo_path, branch_name):
        if order is not None:
            order.append("checkout")
        return True, f"Checked out {branch_name}"

    monkeypatch.setattr(worker_mod, "create_feature_branch", _create_feature_branch)
    monkeypatch.setattr(worker_mod, "checkout_branch", _checkout_branch)


def test_v2_worker_requests_branch_handoff_and_threads_feedback(tmp_path, monkeypatch) -> None:
    """The v2 worker calls the team in no-merge mode and includes Tech Lead feedback."""
    order: List[str] = []
    _patch_branch_handoff(monkeypatch, branch="feature/ui-task", order=order)
    lead = _FakeV2Lead(order)
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
    assert out["files_to_create_or_edit"] == ["src/app.component.ts"]
    assert "Feedback addressed" in out["changes_summary"]
    assert "Add aria-labels" in out["changes_summary"]
    assert order == ["create", "workflow"]
    assert lead.calls[0]["merge_to_development"] is False
    se_task = lead.calls[0]["task"]
    assert se_task.assignee == "frontend_v2"
    assert se_task.feature_branch_name == "feature/ui-task"
    assert "CODING TEAM TECH LEAD FEEDBACK" in se_task.description
    assert "Missing accessibility labels" in se_task.description
    assert "CODING TEAM TECH LEAD FEEDBACK" not in se_task.requirements
    assert "Missing accessibility labels" not in se_task.requirements
    assert "renders list" in se_task.requirements


def test_v2_worker_failure_reports_task_local_failure(tmp_path, monkeypatch) -> None:
    _patch_branch_handoff(monkeypatch)

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


def test_v2_worker_reports_branch_preparation_failure_before_v2_handoff(
    tmp_path, monkeypatch
) -> None:
    class _Lead:
        def __init__(self) -> None:
            self.called = False

        def run_workflow(self, **_kwargs: Any) -> Any:
            self.called = True
            return SimpleNamespace(success=True)

    def _fail_create_feature_branch(repo_path, base_branch, feature_name):
        return False, "development branch missing"

    lead = _Lead()
    monkeypatch.setattr(worker_mod, "create_feature_branch", _fail_create_feature_branch)
    worker = V2TeamWorker(
        agent_id="backend_v2",
        stack_spec=StackSpec(name="backend_v2", tools_services=["Python"]),
        team_kind="backend",
        team_lead=lead,
    )

    out = worker.run_implement(Task(id="api", title="API", description="Build API"), tmp_path)

    assert out["status"] == "failed"
    assert "development branch missing" in out["error"]
    assert lead.called is False


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


def test_v2_worker_rejects_non_list_task_fields_before_v2_handoff(tmp_path) -> None:
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

    out = worker.run_implement(
        SimpleNamespace(
            id="bad-task",
            title="Bad",
            description="Bad task",
            dependencies="api",
            acceptance_criteria=[],
            revision_feedback=[],
        ),
        tmp_path,
    )

    assert out["status"] == "failed"
    assert "must be lists" in out["error"]
    assert "dependencies" in out["error"]
    assert lead.called is False


def test_task_feature_name_truncates_long_titles_with_hash() -> None:
    task = Task(id="task-123", title="x" * 200, description="Build API")

    name = worker_mod._task_feature_name(task)

    assert len(name) <= worker_mod._MAX_FEATURE_SLUG_LENGTH
    assert name.startswith("task-123-")
    assert len(name.rsplit("-", 1)[-1]) == 8


def test_v2_worker_uses_final_files_when_deliver_result_has_no_file_list(
    tmp_path, monkeypatch
) -> None:
    _patch_branch_handoff(monkeypatch)

    class _Lead:
        def run_workflow(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                success=True,
                summary="Implemented API.",
                final_files={"app.py": "print('ok')", "tests/test_app.py": "def test_ok(): pass"},
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
        team_lead=_Lead(),
    )

    out = worker.run_implement(Task(id="api", title="API", description="Build API"), tmp_path)

    assert out["status"] == "in_review"
    assert out["files_to_create_or_edit"] == ["app.py", "tests/test_app.py"]


def test_v2_worker_supports_legacy_workflow_without_merge_keyword(tmp_path, monkeypatch) -> None:
    _patch_branch_handoff(monkeypatch)

    class _LegacyLead:
        def __init__(self) -> None:
            self.calls: List[Dict[str, Any]] = []

        def run_workflow(self, repo_path, task) -> Any:
            self.calls.append({"repo_path": repo_path, "task": task})
            return SimpleNamespace(
                success=True,
                summary="Implemented API.",
                deliver_result=SimpleNamespace(
                    branch_name="feature/api",
                    branch_ready=True,
                    commit_messages=["feat(api): add endpoint"],
                ),
            )

    lead = _LegacyLead()
    worker = V2TeamWorker(
        agent_id="backend_v2",
        stack_spec=StackSpec(name="backend_v2", tools_services=["Python"]),
        team_kind="backend",
        team_lead=lead,
    )

    out = worker.run_implement(Task(id="api", title="API", description="Build API"), tmp_path)

    assert out["status"] == "in_review"
    assert len(lead.calls) == 1


def test_v2_worker_does_not_retry_internal_type_error_in_merge_mode(tmp_path, monkeypatch) -> None:
    _patch_branch_handoff(monkeypatch)

    class _TypeErrorLead:
        def __init__(self) -> None:
            self.calls: List[Dict[str, Any]] = []

        def run_workflow(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            raise TypeError("internal workflow bug")

    lead = _TypeErrorLead()
    worker = V2TeamWorker(
        agent_id="backend_v2",
        stack_spec=StackSpec(name="backend_v2", tools_services=["Python"]),
        team_kind="backend",
        team_lead=lead,
    )

    out = worker.run_implement(Task(id="api", title="API", description="Build API"), tmp_path)

    assert out["status"] == "failed"
    assert out["error"] == "internal workflow bug"
    assert len(lead.calls) == 1
    assert lead.calls[0]["merge_to_development"] is False


def test_v2_worker_preserves_failed_result_even_when_branch_ready(tmp_path, monkeypatch) -> None:
    _patch_branch_handoff(monkeypatch)

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


def test_v2_worker_uses_branch_ready_as_legacy_success_fallback(tmp_path, monkeypatch) -> None:
    _patch_branch_handoff(monkeypatch)

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
