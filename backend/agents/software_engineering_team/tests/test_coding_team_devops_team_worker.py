"""Tests for the coding-team adapter around the DevOps team."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from shared.git.branch_utils import make_branch_suffix
from software_engineering_team import devops_team_worker as devops_worker_mod
from software_engineering_team import v2_team_worker as worker_mod
from software_engineering_team.devops_team.models import (
    DevOpsCompletionPackage,
    DevOpsTaskSpec,
    DevOpsTeamResult,
    GitCommitMetadata,
    GitOperationsMetadata,
    ReleaseReadiness,
)
from software_engineering_team.devops_team_worker import (
    DevOpsTeamWorker,
    _reset_stale_feature_branch,
    _to_devops_task_spec,
)
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
    # No explicit production language in the task text -> staging (matches
    # _build_legacy_spec's existing default for the same "no signal" case).
    assert spec.environment == "staging"
    assert spec.platform_scope.environments == ["dev", "staging"]
    assert spec.scope.excluded == ["cluster provisioning"]
    assert spec.repo_context.infra_repo == "platform-infra"
    assert spec.constraints.secrets.source == "managed_secret_store"
    assert spec.rollback_requirements
    assert spec.security_constraints
    assert spec.compliance_constraints


def test_devops_worker_spec_defaults_acceptance_criteria_when_task_has_none() -> None:
    spec = _to_devops_task_spec(_base_task(acceptance_criteria=[]))
    assert spec.acceptance_criteria  # falls back to module defaults, never empty


def test_devops_worker_spec_derives_production_environment_from_task_text() -> None:
    """A task explicitly about production infra must NOT be silently downgraded to
    dev/staging -- that would strip the production approval-gate check
    (_enforce_env_policy) and leave release_readiness.required_approvals /
    handoff.prod_approval_required empty even though the artifacts target prod."""
    task = _base_task(
        title="Add production deployment workflow",
        description="Deploy the billing service to production with a blue-green rollout",
    )

    spec = _to_devops_task_spec(task)

    assert spec.environment == "production"
    assert spec.platform_scope.environments == ["dev", "production"]


def test_devops_worker_spec_derives_staging_environment_when_no_prod_signal() -> None:
    spec = _to_devops_task_spec(
        _base_task(title="Add lint pipeline", description="Lint and test on PRs")
    )
    assert spec.environment == "staging"
    assert spec.platform_scope.environments == ["dev", "staging"]


def test_devops_worker_spec_respects_non_production_exclusion() -> None:
    """_legacy_environment_from_text's negation handling ("not for production",
    "non-production", ...) applies here too -- reused, not reimplemented."""
    task = _base_task(
        title="Add staging smoke test",
        description="This workflow is explicitly not for production use.",
    )
    spec = _to_devops_task_spec(task)
    assert spec.environment == "staging"


def test_devops_worker_derives_environment_from_acceptance_criteria() -> None:
    """A groomed task commonly carries its production signal in a criterion
    ("Production deploy requires explicit approval") rather than the title/
    description -- the CI/CD and Deployment specialists already consume
    acceptance_criteria directly, so they can produce production-targeting
    artifacts even when the description reads generically."""
    task = _base_task(
        title="Add deployment workflow",
        description="Set up the deploy pipeline",
        acceptance_criteria=["Production deploy requires explicit approval"],
    )

    spec = _to_devops_task_spec(task)

    assert spec.environment == "production"
    assert spec.platform_scope.environments == ["dev", "production"]


def test_devops_worker_spec_copies_acceptance_criteria_into_scope_included() -> None:
    """_enforce_env_policy only scans scope.included for approval-gate language,
    so an approval requirement recorded as a groomed acceptance criterion must
    be visible there too, or a correctly production-derived task would fail
    Phase 1 despite already carrying the required gate structurally."""
    task = _base_task(
        acceptance_criteria=[
            "Production deploy requires explicit approval",
            "Rollback tested",
        ]
    )

    spec = _to_devops_task_spec(task)

    assert "Production deploy requires explicit approval" in spec.scope.included
    assert "Rollback tested" in spec.scope.included


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


# ------------------------------------------------------ stale-branch reset on retry


def test_reset_stale_feature_branch_noop_when_feature_branch_recorded(
    tmp_path, monkeypatch
) -> None:
    """A Tech-Lead-review rejection (not a DevOps-internal gate rejection) already
    recorded task.feature_branch and wants that same branch revised in place --
    _prepare_feature_branch's own existing_branch path handles that, so no reset."""
    (tmp_path / ".git").mkdir()
    calls: List[Any] = []
    monkeypatch.setattr(
        devops_worker_mod, "reset_hard_to", lambda p, ref: calls.append((p, ref)) or (True, "ok")
    )

    _reset_stale_feature_branch(tmp_path, _base_task(feature_branch="feature/existing"))

    assert calls == []


def test_reset_stale_feature_branch_noop_when_not_a_git_repo(tmp_path, monkeypatch) -> None:
    calls: List[Any] = []
    monkeypatch.setattr(
        devops_worker_mod, "reset_hard_to", lambda p, ref: calls.append((p, ref)) or (True, "ok")
    )

    _reset_stale_feature_branch(tmp_path, _base_task())  # no .git dir

    assert calls == []


def test_reset_stale_feature_branch_resets_when_no_recorded_branch(tmp_path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    calls: List[Any] = []
    monkeypatch.setattr(
        devops_worker_mod, "reset_hard_to", lambda p, ref: calls.append((p, ref)) or (True, "ok")
    )

    _reset_stale_feature_branch(tmp_path, _base_task())

    assert calls == [(tmp_path, devops_worker_mod.DEVELOPMENT_BRANCH)]


def test_reset_stale_feature_branch_swallows_reset_failure(tmp_path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(devops_worker_mod, "reset_hard_to", lambda p, ref: (False, "boom"))

    _reset_stale_feature_branch(tmp_path, _base_task())  # must not raise


def test_devops_worker_resets_stale_branch_before_preparing_it(tmp_path, monkeypatch) -> None:
    """Proves the reset runs BEFORE branch preparation -- the whole point is to clear
    a rejected attempt's stale commits before create_feature_branch would otherwise
    reuse the still-checked-out branch as-is."""
    (tmp_path / ".git").mkdir()
    order: List[str] = []
    monkeypatch.setattr(
        devops_worker_mod, "reset_hard_to", lambda p, ref: order.append("reset") or (True, "ok")
    )

    def _ensure_development_ready(repo_path):
        order.append("ensure_development")
        return True, "development ready"

    def _create_feature_branch(repo_path, base_branch, feature_name):
        order.append("create")
        return True, "feature/provision"

    monkeypatch.setattr(worker_mod, "_ensure_development_ready", _ensure_development_ready)
    monkeypatch.setattr(worker_mod, "create_feature_branch", _create_feature_branch)
    lead = _FakeDevOpsLead()
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=lead,
    )

    worker.run_implement(_base_task(), tmp_path)

    assert order == ["reset", "ensure_development", "create"]


def test_devops_worker_does_not_reset_when_feature_branch_already_recorded(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / ".git").mkdir()
    calls: List[Any] = []
    monkeypatch.setattr(
        devops_worker_mod, "reset_hard_to", lambda p, ref: calls.append((p, ref)) or (True, "ok")
    )
    monkeypatch.setattr(
        worker_mod, "checkout_branch", lambda repo_path, branch: (True, f"Checked out {branch}")
    )
    lead = _FakeDevOpsLead()
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=lead,
    )

    worker.run_implement(_base_task(feature_branch="feature/existing"), tmp_path)

    assert calls == []
