"""Tests for the coding-team adapter around the DevOps team."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from shared.git.branch_utils import make_branch_suffix
from software_engineering_team import v2_team_worker as worker_mod
from software_engineering_team.devops_team.models import (
    DevOpsCompletionPackage,
    DevOpsTaskSpec,
    DevOpsTeamResult,
    GitCommitMetadata,
    GitOperationsMetadata,
    ReleaseReadiness,
)
from software_engineering_team.devops_team_worker import DevOpsTeamWorker, _to_devops_task_spec
from software_engineering_team.models import StackSpec, Task
from software_engineering_team.v2_team_worker import _task_feature_name


class _FakeDevOpsLead:
    """Records ``run_task`` calls and returns a scripted (or default happy-path) result."""

    def __init__(self, result: Optional[DevOpsTeamResult] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._result = result

    def run_task(self, spec: DevOpsTaskSpec, **kwargs: Any) -> DevOpsTeamResult:
        self.calls.append({"spec": spec, **kwargs})
        if self._result is not None:
            return self._result
        return DevOpsTeamResult(
            success=True,
            completion_package=DevOpsCompletionPackage(
                task_id=spec.task_id,
                status="completed",
                files_changed=["infra/main.tf"],
                quality_gates={"iac_validate": "pass"},
                release_readiness=ReleaseReadiness(deployment_strategy="rolling"),
                notes=["Provisioned infra."],
                git_operations=GitOperationsMetadata(
                    branch_created=f"feature/{make_branch_suffix(spec.task_id, spec.title)}",
                    commits=[GitCommitMetadata(hash="abc123", message="feat(devops): implement")],
                ),
            ),
        )


def _patch_branch_handoff(monkeypatch, *, branch: str = "feature/devops-task") -> None:
    """Stub the git branch-prep calls ``_prepare_feature_branch`` (imported from
    ``v2_team_worker``) reaches through that module's own namespace."""

    def _ensure_development_ready(repo_path):
        return True, "development ready"

    def _create_feature_branch(repo_path, base_branch, feature_name):
        return True, branch

    def _checkout_branch(repo_path, branch_name):
        return True, f"Checked out {branch_name}"

    monkeypatch.setattr(worker_mod, "_ensure_development_ready", _ensure_development_ready)
    monkeypatch.setattr(worker_mod, "create_feature_branch", _create_feature_branch)
    monkeypatch.setattr(worker_mod, "checkout_branch", _checkout_branch)


def _base_task(**overrides: Any) -> Task:
    defaults: Dict[str, Any] = dict(
        id="provision",
        title="Provision infra",
        description="Add CI/CD pipeline",
    )
    defaults.update(overrides)
    return Task(**defaults)


def test_devops_worker_team_kind_is_fixed() -> None:
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=_FakeDevOpsLead(),
    )
    assert worker.team_kind == "devops"


def test_devops_worker_calls_run_task_in_handoff_mode(tmp_path, monkeypatch) -> None:
    _patch_branch_handoff(monkeypatch, branch="feature/provision")
    lead = _FakeDevOpsLead()
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=lead,
    )

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "in_review"
    assert len(lead.calls) == 1
    call = lead.calls[0]
    assert call["merge_to_development"] is False
    assert Path(call["repo_path"]) == Path(tmp_path).resolve()
    assert isinstance(call["spec"], DevOpsTaskSpec)


def test_devops_worker_builds_spec_from_task() -> None:
    long_title = "A" * 200  # deliberately long: must NOT be truncated
    task = _base_task(
        title=long_title,
        acceptance_criteria=["pipeline lints"],
        dependencies=["other"],
        out_of_scope="cluster provisioning",
    )

    spec = _to_devops_task_spec(task)

    assert spec.task_id == "provision"
    assert spec.title == long_title
    assert spec.acceptance_criteria == ["pipeline lints"]
    assert spec.dependencies == ["other"]
    assert spec.environment == "dev"
    assert spec.platform_scope.environments == ["dev"]
    assert spec.scope.excluded == ["cluster provisioning"]
    assert spec.repo_context.infra_repo == "platform-infra"
    assert spec.constraints.secrets.source == "managed_secret_store"
    assert spec.rollback_requirements
    assert spec.security_constraints
    assert spec.compliance_constraints


def test_devops_worker_spec_defaults_acceptance_criteria_when_task_has_none() -> None:
    spec = _to_devops_task_spec(_base_task(acceptance_criteria=[]))
    assert spec.acceptance_criteria  # falls back to module defaults, never empty


def test_devops_worker_threads_revision_feedback_into_goal_summary(tmp_path, monkeypatch) -> None:
    _patch_branch_handoff(monkeypatch)
    lead = _FakeDevOpsLead()
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=lead,
    )
    task = _base_task(
        revision_feedback=[{"source": "tech_lead", "reason": "Add rollback docs"}],
    )

    out = worker.run_implement(task, tmp_path)

    spec = lead.calls[0]["spec"]
    assert "CODING TEAM TECH LEAD FEEDBACK" in spec.goal.summary
    assert "Add rollback docs" in spec.goal.summary
    assert "Revision feedback provided" in out["changes_summary"]
    # goal.summary alone isn't enough: none of the Phase 2 specialist agents
    # (IaC/CICD/Deployment) read it -- only scope.included and acceptance_criteria.
    # The feedback must also land in both of those or a revision round regenerates
    # the same rejected artifacts blind.
    assert any("Add rollback docs" in item for item in spec.scope.included)
    assert any("Add rollback docs" in item for item in spec.acceptance_criteria)


def test_devops_worker_spec_has_no_feedback_note_without_revision_feedback() -> None:
    spec = _to_devops_task_spec(_base_task())
    assert not any("REVISION FEEDBACK" in item for item in spec.scope.included)
    assert not any("REVISION FEEDBACK" in item for item in spec.acceptance_criteria)


def test_devops_worker_translates_completed_package(tmp_path, monkeypatch) -> None:
    _patch_branch_handoff(monkeypatch)
    lead = _FakeDevOpsLead()
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=lead,
    )

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "in_review"
    assert out["feature_branch"].startswith("feature/")
    assert out["files_to_create_or_edit"] == ["infra/main.tf"]
    assert out["commands_run"] == ["feat(devops): implement"]
    assert out["error"] is None
    assert out["open_questions"] == []
    assert "iac_validate=pass" in out["changes_summary"]
    assert "Provisioned infra." in out["changes_summary"]


def test_devops_worker_translates_blocked_package(tmp_path, monkeypatch) -> None:
    _patch_branch_handoff(monkeypatch)
    blocked = DevOpsTeamResult(
        success=False,
        failure_reason="Quality gates failed",
        completion_package=DevOpsCompletionPackage(
            task_id="provision",
            status="blocked",
            files_changed=["infra/main.tf"],
            quality_gates={"security_review": "fail"},
            notes=["Security findings unresolved."],
            git_operations=GitOperationsMetadata(branch_created="feature/provision"),
        ),
    )
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=_FakeDevOpsLead(blocked),
    )

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "failed"
    # The error itself carries the rendered gate/notes detail, not just the generic
    # failure_reason label -- _handle_incomplete_implementation only persists
    # result["error"] into revision_feedback, so the next attempt needs the specifics
    # here, not stranded in changes_summary alone.
    assert out["error"].startswith("Quality gates failed")
    assert "security_review=fail" in out["error"]
    assert "Security findings unresolved." in out["error"]
    assert out["files_to_create_or_edit"] == ["infra/main.tf"]
    assert "security_review=fail" in out["changes_summary"]


def test_devops_worker_translates_failed_result_without_package(tmp_path, monkeypatch) -> None:
    _patch_branch_handoff(monkeypatch, branch="feature/provision")
    failed = DevOpsTeamResult(
        success=False, failure_reason="Clarification required", completion_package=None
    )
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=_FakeDevOpsLead(failed),
    )

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "failed"
    assert out["error"] == "Clarification required"
    assert out["feature_branch"] == "feature/provision"
    assert out["files_to_create_or_edit"] == []


def test_devops_worker_contains_run_task_exception(tmp_path, monkeypatch) -> None:
    _patch_branch_handoff(monkeypatch, branch="feature/provision")

    class _RaisingLead:
        def run_task(self, spec, **kwargs):
            raise RuntimeError("boom")

    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=_RaisingLead(),
    )

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "failed"
    assert "boom" in out["error"]


def test_devops_worker_falls_back_to_task_branch_when_git_operations_omit_it(
    tmp_path, monkeypatch
) -> None:
    """When the completion package's git_operations.branch_created is blank (but real
    files were still delivered), the worker falls back to the branch it prepared itself."""
    _patch_branch_handoff(monkeypatch, branch="feature/provision")
    no_branch = DevOpsTeamResult(
        success=True,
        completion_package=DevOpsCompletionPackage(
            task_id="provision",
            status="completed",
            files_changed=["infra/main.tf"],
            git_operations=GitOperationsMetadata(branch_created=""),
        ),
    )
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=_FakeDevOpsLead(no_branch),
    )

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "in_review"
    assert out["feature_branch"] == "feature/provision"


def test_devops_worker_rejects_completed_package_with_no_delivered_files(
    tmp_path, monkeypatch
) -> None:
    """A 'completed' package with an empty files_changed means Phase 2 produced nothing
    and Phase 3/5 both skipped writing -- the feature branch has zero new commits. This
    must NOT be accepted as in_review (an empty diff can be trivially approved and
    merged as if the task were actually implemented)."""
    _patch_branch_handoff(monkeypatch, branch="feature/provision")
    empty = DevOpsTeamResult(
        success=True,
        completion_package=DevOpsCompletionPackage(
            task_id="provision",
            status="completed",
            files_changed=[],
            git_operations=GitOperationsMetadata(branch_created=""),
        ),
    )
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=_FakeDevOpsLead(empty),
    )

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "failed"
    assert "without producing any files to deliver" in out["error"].lower()
    assert out["feature_branch"] == "feature/provision"
    assert out["files_to_create_or_edit"] == []


def test_devops_worker_failure_error_carries_gate_detail_for_revision_feedback(
    tmp_path, monkeypatch
) -> None:
    """The swarm's _handle_incomplete_implementation persists only result["error"] into
    revision_feedback (it ignores changes_summary), so a failure's error string must
    itself carry the actionable gate/notes detail -- not just a terse generic label."""
    _patch_branch_handoff(monkeypatch, branch="feature/provision")
    blocked = DevOpsTeamResult(
        success=False,
        failure_reason="Quality gates failed",
        completion_package=DevOpsCompletionPackage(
            task_id="provision",
            status="blocked",
            files_changed=["infra/main.tf"],
            quality_gates={"iac_validate": "fail"},
            notes=["Terraform plan produced invalid resource references."],
            git_operations=GitOperationsMetadata(branch_created="feature/provision"),
        ),
    )
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=_FakeDevOpsLead(blocked),
    )

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "failed"
    assert "iac_validate=fail" in out["error"]
    assert "Terraform plan produced invalid resource references." in out["error"]


def test_devops_worker_branch_name_matches_pipeline_suffix() -> None:
    """The worker's branch-prep name must match what the pipeline independently
    computes from the spec, or the two disagree on which branch to review."""
    task = _base_task()
    spec = _to_devops_task_spec(task)
    assert _task_feature_name(task) == make_branch_suffix(spec.task_id, spec.title)


def test_devops_worker_reports_branch_preparation_failure_before_run_task(
    tmp_path, monkeypatch
) -> None:
    def _ensure_development_ready(repo_path):
        return False, "no base commit"

    monkeypatch.setattr(worker_mod, "_ensure_development_ready", _ensure_development_ready)
    lead = _FakeDevOpsLead()
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=lead,
    )

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "failed"
    assert "failed to prepare feature branch" in out["error"]
    assert lead.calls == []


def test_devops_worker_rejects_malformed_task_before_run_task(tmp_path) -> None:
    from types import SimpleNamespace

    lead = _FakeDevOpsLead()
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=lead,
    )
    malformed = SimpleNamespace(id="", title="x", description="x")

    out = worker.run_implement(malformed, tmp_path)

    assert out["status"] == "failed"
    assert lead.calls == []
