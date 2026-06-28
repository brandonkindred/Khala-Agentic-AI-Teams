"""Tests for coding-team adapters around frontend/backend code-v2 teams."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

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
    def _ensure_development_ready(repo_path):
        if order is not None:
            order.append("ensure_development")
        return True, "development ready"

    def _create_feature_branch(repo_path, base_branch, feature_name):
        if order is not None:
            order.append("create")
        return True, branch

    def _checkout_branch(repo_path, branch_name):
        if order is not None:
            order.append("checkout")
        return True, f"Checked out {branch_name}"

    monkeypatch.setattr(worker_mod, "_ensure_development_ready", _ensure_development_ready)
    monkeypatch.setattr(worker_mod, "create_feature_branch", _create_feature_branch)
    monkeypatch.setattr(worker_mod, "checkout_branch", _checkout_branch)


def _init_main_repo(path) -> None:
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    (path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=path, capture_output=True, check=True)


def test_v2_worker_rejects_unknown_team_kind() -> None:
    """Unknown team kinds fail fast instead of silently routing as backend."""
    with pytest.raises(ValueError, match="team_kind must be one of"):
        V2TeamWorker(
            agent_id="qa_v2",
            stack_spec=StackSpec(name="qa_v2", tools_services=["pytest"]),
            team_kind="qa",
            team_lead=_FakeV2Lead(),
        )


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
    assert "Revision feedback provided" in out["changes_summary"]
    assert "Add aria-labels" in out["changes_summary"]
    assert order == ["ensure_development", "create", "workflow"]
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
    """Failed v2 workflow results stay local to the task handoff."""
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
    """Branch preparation failures prevent v2 workflow execution."""

    class _Lead:
        def __init__(self) -> None:
            self.called = False

        def run_workflow(self, **_kwargs: Any) -> Any:
            self.called = True
            return SimpleNamespace(success=True)

    def _fail_create_feature_branch(repo_path, base_branch, feature_name):
        return False, "development branch missing"

    lead = _Lead()
    monkeypatch.setattr(
        worker_mod, "_ensure_development_ready", lambda repo_path: (True, "development ready")
    )
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


def test_v2_worker_prepares_development_before_branch_on_main_only_repo(tmp_path) -> None:
    """A main-only repo gets a development branch before handoff branch creation."""
    _init_main_repo(tmp_path)

    class _Lead:
        def __init__(self) -> None:
            self.calls: List[Dict[str, Any]] = []

        def run_workflow(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            branch = kwargs["task"].feature_branch_name
            return SimpleNamespace(
                success=True,
                summary="Implemented API.",
                deliver_result=SimpleNamespace(
                    branch_name=branch,
                    branch_ready=True,
                    commit_messages=["feat(api): add endpoint"],
                ),
                failure_reason="",
            )

    lead = _Lead()
    worker = V2TeamWorker(
        agent_id="backend_v2",
        stack_spec=StackSpec(name="backend_v2", tools_services=["Python"]),
        team_kind="backend",
        team_lead=lead,
    )

    out = worker.run_implement(Task(id="api", title="Build API", description="Build API"), tmp_path)

    assert out["status"] == "in_review"
    assert out["feature_branch"].startswith("feature/api-build-api")
    assert lead.calls[0]["task"].feature_branch_name == out["feature_branch"]
    branches = subprocess.run(
        ["git", "branch", "--list", "development"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        text=True,
    )
    current = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        text=True,
    )
    assert "development" in branches.stdout
    assert current.stdout.strip() == out["feature_branch"]


def test_v2_worker_initializes_empty_repo_before_task_branch(tmp_path) -> None:
    """An empty repo path is initialized before creating the task branch."""

    class _Lead:
        def __init__(self) -> None:
            self.calls: List[Dict[str, Any]] = []

        def run_workflow(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            branch = kwargs["task"].feature_branch_name
            return SimpleNamespace(
                success=True,
                summary="Implemented API.",
                deliver_result=SimpleNamespace(branch_name=branch, branch_ready=True),
                failure_reason="",
            )

    lead = _Lead()
    worker = V2TeamWorker(
        agent_id="backend_v2",
        stack_spec=StackSpec(name="backend_v2", tools_services=["Python"]),
        team_kind="backend",
        team_lead=lead,
    )

    out = worker.run_implement(Task(id="api", title="Build API", description="Build API"), tmp_path)

    assert out["status"] == "in_review"
    assert (tmp_path / ".git").exists()
    assert lead.calls[0]["task"].feature_branch_name == out["feature_branch"]
    branches = subprocess.run(
        ["git", "branch", "--list", "development"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        text=True,
    )
    assert "development" in branches.stdout


def test_v2_worker_rejects_malformed_task_before_v2_handoff(tmp_path) -> None:
    """Malformed task objects fail before branch or workflow side effects."""

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
    """List-typed task fields are validated before v2 handoff."""

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


def test_accepts_keyword_detects_kwarg_and_varkwargs() -> None:
    """Explicit keyword and **kwargs signatures are accepted; a missing one is not."""

    def fn_explicit(merge_to_development=True):
        return None

    def fn_varkw(**kwargs):
        return None

    def fn_missing(x):
        return None

    assert worker_mod._accepts_keyword(fn_explicit, "merge_to_development") is True
    assert worker_mod._accepts_keyword(fn_varkw, "merge_to_development") is True
    assert worker_mod._accepts_keyword(fn_missing, "merge_to_development") is False


def test_accepts_keyword_returns_false_when_signature_unintrospectable(monkeypatch) -> None:
    """If inspect.signature raises (e.g. a C-extension callable), default to NOT accepted
    rather than risking an unexpected-keyword TypeError at call time."""

    def _raise(_fn):
        raise ValueError("no signature available")

    monkeypatch.setattr(worker_mod.inspect, "signature", _raise)
    assert worker_mod._accepts_keyword(lambda **kw: None, "merge_to_development") is False


def test_task_feature_name_truncates_long_titles_with_hash() -> None:
    """Long task titles produce bounded branch names with hash disambiguation."""
    task = Task(id="task-123", title="x" * 200, description="Build API")

    name = worker_mod._task_feature_name(task)

    # Bounded by the shared branch_utils slug caps (task-id 20 + title 40 + 8-char hash).
    assert len(name) <= 70
    assert name.startswith("task-123-")
    assert len(name.rsplit("-", 1)[-1]) == 8


def test_task_feature_name_disambiguates_punctuation_only_collisions() -> None:
    """Task ids that slug identically still get distinct branch names via the id hash."""
    name_a = worker_mod._task_feature_name(Task(id="api.v1", title="Build API"))
    name_b = worker_mod._task_feature_name(Task(id="api-v1", title="Build API"))

    # Same human-readable slug prefix, but the trailing task-id hash keeps them apart so
    # create_feature_branch cannot clobber one task's unmerged branch with the other's.
    assert name_a != name_b
    assert name_a.startswith("api-v1-")
    assert name_b.startswith("api-v1-")
    # Stable per task id: a retry of the same task reuses the same branch name.
    assert worker_mod._task_feature_name(Task(id="api.v1", title="Build API")) == name_a


def test_v2_worker_uses_final_files_when_deliver_result_has_no_file_list(
    tmp_path, monkeypatch
) -> None:
    """Workflow final_files backfill Tech Lead review file lists."""
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
    """Legacy workflow fakes without merge_to_development remain supported."""
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
    """Internal TypeError failures are not retried in default merge mode."""
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
    """Explicit workflow failure stays failed even when a branch was prepared."""
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
    """Older workflow results can still use branch_ready as the success signal."""
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
