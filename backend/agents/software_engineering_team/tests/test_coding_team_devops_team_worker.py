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
    _reset_prepared_branch_to_development,
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


def _patch_branch_handoff(
    monkeypatch, *, branch: str = "feature/devops-task", diff: tuple = (["stub.tf"], [])
) -> None:
    """Stub the git branch-prep calls ``_prepare_feature_branch`` (imported from
    ``v2_team_worker``) reaches through that module's own namespace, the post-prepare
    reset (real git operations never run against a bare ``tmp_path``, so it must be a
    harmless no-op here), and the post-completion diff check (defaults to reporting a
    non-empty diff so existing happy-path tests don't have to know about it; override
    ``diff=([], [])`` for tests specifically exercising the no-diff rejection path).

    Preconditions: ``monkeypatch`` is pytest's ``monkeypatch`` fixture; ``worker_mod``
        and ``devops_worker_mod`` (this file's module-level imports) are the real
        ``v2_team_worker``/``devops_team_worker`` modules.
    Postconditions: ``_ensure_development_ready``, ``create_feature_branch``, and
        ``checkout_branch`` (on ``worker_mod``) and ``reset_hard_to`` and
        ``list_changed_and_deleted`` (on ``devops_worker_mod``) are replaced with
        deterministic stubs for the remainder of the test.
    """

    def _ensure_development_ready(repo_path):
        return True, "development ready"

    def _create_feature_branch(repo_path, base_branch, feature_name):
        return True, branch

    def _checkout_branch(repo_path, branch_name):
        return True, f"Checked out {branch_name}"

    monkeypatch.setattr(worker_mod, "_ensure_development_ready", _ensure_development_ready)
    monkeypatch.setattr(worker_mod, "create_feature_branch", _create_feature_branch)
    monkeypatch.setattr(worker_mod, "checkout_branch", _checkout_branch)
    monkeypatch.setattr(devops_worker_mod, "reset_hard_to", lambda p, ref: (True, "ok"))
    monkeypatch.setattr(devops_worker_mod, "list_changed_and_deleted", lambda p, base: diff)


def _base_task(**overrides: Any) -> Task:
    defaults: Dict[str, Any] = dict(
        id="provision",
        title="Provision infra",
        description="Add CI/CD pipeline",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _devops_worker(lead: Optional[Any] = None) -> DevOpsTeamWorker:
    """Build a ``DevOpsTeamWorker`` with the standard test stack spec.

    Postconditions: returns a worker whose ``team_lead`` is ``lead`` (or a
        fresh ``_FakeDevOpsLead()`` when omitted).
    """
    return DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=lead if lead is not None else _FakeDevOpsLead(),
    )


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
    # A leading TASK SCOPE entry is prepended (finding: CI/CD and Deployment
    # specialists read acceptance_criteria but never goal/scope) -- the task's
    # own criteria still follow it, untouched. out_of_scope is deliberately
    # NOT folded in here (see _task_scope_note) -- it stays confined to
    # scope.excluded, which the production approval-gate policy never scans.
    assert spec.acceptance_criteria[-1] == "pipeline lints"
    assert "Add CI/CD pipeline" in spec.acceptance_criteria[0]
    assert "cluster provisioning" not in spec.acceptance_criteria[0]
    assert spec.dependencies == ["other"]
    # No explicit production language in the task text -> staging (the default
    # for the same "no signal" case).
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


def test_devops_worker_spec_derives_staging_for_empty_title_and_description() -> None:
    """No text at all is the same "no signal" case as text with no production
    mention -- must default to staging, not raise or misclassify."""
    task = _base_task(title="", description="", acceptance_criteria=[])
    spec = _to_devops_task_spec(task)
    assert spec.environment == "staging"
    assert spec.platform_scope.environments == ["dev", "staging"]


def test_devops_worker_spec_mixed_signals_prefer_production() -> None:
    """_legacy_environment_from_text scans clause-by-clause and returns "production"
    as soon as ANY clause carries an unnegated production mention -- a negation in
    one clause (e.g. the description) does not suppress a separate, unguarded
    production mention in another clause (e.g. the title). This is a deliberate
    fail-open bias toward keeping the production approval-gate check rather than
    silently dropping it on a genuinely mixed-signal task."""
    task = _base_task(
        title="Add production deployment workflow",
        description="This pipeline is explicitly not for production; staging only.",
    )
    spec = _to_devops_task_spec(task)
    assert spec.environment == "production"


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


def test_devops_worker_translates_multiple_commits(tmp_path, monkeypatch) -> None:
    """commands_run must include every commit's message, not just the first/last,
    when a completion package's git_operations carries more than one commit."""
    _patch_branch_handoff(monkeypatch)
    multi_commit = DevOpsTeamResult(
        success=True,
        completion_package=DevOpsCompletionPackage(
            task_id="provision",
            status="completed",
            files_changed=["infra/main.tf", "infra/variables.tf"],
            git_operations=GitOperationsMetadata(
                branch_created="feature/provision",
                commits=[
                    GitCommitMetadata(hash="a1", message="feat(devops): plan"),
                    GitCommitMetadata(hash="a2", message="feat(devops): apply"),
                ],
            ),
        ),
    )
    worker = _devops_worker(_FakeDevOpsLead(multi_commit))

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "in_review"
    assert out["commands_run"] == ["feat(devops): plan", "feat(devops): apply"]


def test_devops_worker_translates_empty_commits_list(tmp_path, monkeypatch) -> None:
    """An empty (but present) commits list must degrade to an empty commands_run,
    not raise."""
    _patch_branch_handoff(monkeypatch)
    no_commits = DevOpsTeamResult(
        success=True,
        completion_package=DevOpsCompletionPackage(
            task_id="provision",
            status="completed",
            files_changed=["infra/main.tf"],
            git_operations=GitOperationsMetadata(branch_created="feature/provision", commits=[]),
        ),
    )
    worker = _devops_worker(_FakeDevOpsLead(no_commits))

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "in_review"
    assert out["commands_run"] == []


def test_devops_worker_translates_default_git_operations(tmp_path, monkeypatch) -> None:
    """``DevOpsCompletionPackage.git_operations`` is a non-Optional field defaulting
    to a fresh ``GitOperationsMetadata()`` -- Pydantic rejects ``git_operations=None``
    outright (a completion package can never omit it). A package that leaves it at
    that default (blank ``branch_created``, empty ``commits``) must still translate
    cleanly: falling back to the worker's own prepared branch and an empty
    commands_run, not raise on the absent metadata."""
    _patch_branch_handoff(monkeypatch, branch="feature/provision")
    default_git_ops = DevOpsTeamResult(
        success=True,
        completion_package=DevOpsCompletionPackage(
            task_id="provision",
            status="completed",
            files_changed=["infra/main.tf"],
        ),
    )
    worker = _devops_worker(_FakeDevOpsLead(default_git_ops))

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "in_review"
    assert out["feature_branch"] == "feature/provision"
    assert out["commands_run"] == []


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


def test_devops_worker_contains_non_runtime_error_exceptions(tmp_path, monkeypatch) -> None:
    """The ``except Exception`` handler around ``run_task`` must not be narrowed to
    RuntimeError in practice -- a ValueError (e.g. from DevOpsTaskSpec/pydantic
    validation deeper in the pipeline) must be caught and surfaced the same way,
    not escape and crash the worker."""
    _patch_branch_handoff(monkeypatch, branch="feature/provision")

    class _RaisingLead:
        def run_task(self, spec, **kwargs):
            raise ValueError("invalid spec")

    worker = _devops_worker(_RaisingLead())

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "failed"
    assert "invalid spec" in out["error"]


def test_devops_worker_contains_spec_building_exception(tmp_path, monkeypatch) -> None:
    """``_to_devops_task_spec`` runs inside the same try/except as ``run_task`` (not
    before it): a spec-building failure that ``validate_task_interface`` didn't catch
    must still return a failed_result instead of an unhandled crash."""
    _patch_branch_handoff(monkeypatch, branch="feature/provision")

    def _raise(task):
        raise TypeError("cannot build spec")

    monkeypatch.setattr(devops_worker_mod, "_to_devops_task_spec", _raise)
    worker = _devops_worker()

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "failed"
    assert "cannot build spec" in out["error"]


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


# ------------------------------------------------------ branch reset on retry


def test_reset_prepared_branch_to_development_resets_current_branch(tmp_path, monkeypatch) -> None:
    calls: List[Any] = []
    monkeypatch.setattr(
        devops_worker_mod, "reset_hard_to", lambda p, ref: calls.append((p, ref)) or (True, "ok")
    )

    ok, msg = _reset_prepared_branch_to_development(tmp_path, "provision")

    assert ok, msg
    assert calls == [(tmp_path, devops_worker_mod.DEVELOPMENT_BRANCH)]


def test_reset_prepared_branch_to_development_reports_reset_failure(tmp_path, monkeypatch) -> None:
    """A reset failure must be reported (not swallowed): the caller needs it to
    decide whether to abort rather than proceed on possibly-stale branch state."""
    monkeypatch.setattr(devops_worker_mod, "reset_hard_to", lambda p, ref: (False, "boom"))

    ok, msg = _reset_prepared_branch_to_development(tmp_path, "provision")

    assert not ok
    assert "boom" in msg


def test_devops_worker_resets_branch_after_preparing_it_for_new_branch(
    tmp_path, monkeypatch
) -> None:
    """Proves the reset runs AFTER branch preparation, not before: resetting
    whatever was checked out BEFORE _prepare_feature_branch ran would reset the
    wrong branch (e.g. a different task's leftover state in this shared per-agent
    worktree) instead of the branch this task is actually about to use."""
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
    monkeypatch.setattr(devops_worker_mod, "list_changed_and_deleted", lambda p, base: (["x"], []))
    lead = _FakeDevOpsLead()
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=lead,
    )

    worker.run_implement(_base_task(), tmp_path)

    assert order == ["ensure_development", "create", "reset"]


def test_devops_worker_resets_branch_after_preparing_it_for_existing_branch(
    tmp_path, monkeypatch
) -> None:
    """A Tech-Lead-review revision round DOES record task.feature_branch and reuses
    that branch's name, but DevOps regenerates its full artifact set from scratch
    every round -- there is nothing incremental to preserve, so the branch content
    is reset here too (finding: a rejected artifact the revision correctly omits
    must not linger on the branch just because the branch name is being reused)."""
    order: List[str] = []
    monkeypatch.setattr(
        devops_worker_mod, "reset_hard_to", lambda p, ref: order.append("reset") or (True, "ok")
    )
    monkeypatch.setattr(
        worker_mod,
        "checkout_branch",
        lambda repo_path, branch: order.append("checkout") or (True, f"Checked out {branch}"),
    )
    monkeypatch.setattr(devops_worker_mod, "list_changed_and_deleted", lambda p, base: (["x"], []))
    lead = _FakeDevOpsLead()
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=lead,
    )

    worker.run_implement(_base_task(feature_branch="feature/existing"), tmp_path)

    assert order == ["checkout", "reset"]


def test_devops_worker_aborts_when_branch_reset_fails(tmp_path, monkeypatch) -> None:
    """If the post-checkout reset fails (lock, timeout, other git error), run_implement
    must abort rather than proceed on the branch as-is: Phase 3 would then reuse that
    currently-checked-out (possibly stale/rejected) branch and only add/overwrite paths
    in the new artifact map, leaving files from a prior rejected round in the eventual
    diff unreviewed."""
    _patch_branch_handoff(monkeypatch, branch="feature/provision")
    monkeypatch.setattr(devops_worker_mod, "reset_hard_to", lambda p, ref: (False, "boom"))
    lead = _FakeDevOpsLead()
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=lead,
    )

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "failed"
    assert "boom" in out["error"]
    assert lead.calls == []


# --------------------------------------------------- actual-diff verification (finding D)


def test_devops_worker_rejects_completed_result_with_no_actual_diff(tmp_path, monkeypatch) -> None:
    """pkg.files_changed is only the generated artifact map's keys, not proof anything
    differs from what's already on the branch: write_files_and_commit/commit_working_tree
    both treat regenerated-but-identical content as a successful "nothing to commit". A
    nonempty artifact map alone must not be accepted as review-ready."""
    _patch_branch_handoff(monkeypatch, branch="feature/provision", diff=([], []))
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=_FakeDevOpsLead(),
    )

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "failed"
    assert "no changes" in out["error"].lower()
    assert out["feature_branch"]  # pkg.git_operations.branch_created wins over prepared_branch


def test_devops_worker_fails_closed_when_diff_check_raises(tmp_path, monkeypatch) -> None:
    """list_changed_and_deleted itself fails closed (raises BaselineDiffUnavailable)
    when the merge-base/diff can't be computed -- the worker must not treat that as
    "no news is good news" and wave the result through as review-ready."""
    _patch_branch_handoff(monkeypatch, branch="feature/provision")

    def _raise(path, base):
        raise devops_worker_mod.BaselineDiffUnavailable("cannot compute merge base")

    monkeypatch.setattr(devops_worker_mod, "list_changed_and_deleted", _raise)
    worker = DevOpsTeamWorker(
        agent_id="devops_worker",
        stack_spec=StackSpec(name="devops", tools_services=[]),
        team_lead=_FakeDevOpsLead(),
    )

    out = worker.run_implement(_base_task(), tmp_path)

    assert out["status"] == "failed"


# ------------------------------------------- environment from revision feedback (finding G)


def test_devops_worker_derives_environment_from_revision_feedback() -> None:
    """A Tech Lead rejection can redirect a generic task toward production (e.g. "this
    needs to be the production deploy workflow"). That feedback text is injected into
    scope.included/acceptance_criteria for the Phase 2 specialists, so environment
    derivation must see it too, or the specialists could generate production-targeting
    artifacts while the spec itself stayed pinned to the original (non-production) read."""
    task = _base_task(
        title="Add deployment workflow",
        description="Set up the deploy pipeline",
        revision_feedback=[
            {"source": "tech_lead", "reason": "This must target production, not staging."}
        ],
    )

    spec = _to_devops_task_spec(task)

    assert spec.environment == "production"
    assert spec.platform_scope.environments == ["dev", "production"]


def test_devops_worker_newest_revision_feedback_overrides_stale_production_mention() -> None:
    """revision_feedback is append-only, so an early round's "make this production"
    and a later round's "actually, staging only" both persist in the list. Blending
    every entry into one combined-text scan means the stale production mention can
    never be overridden. The NEWEST feedback line that mentions an environment must
    win, not every historical mention blended together."""
    task = _base_task(
        title="Add deployment workflow",
        description="Set up the deploy pipeline",
        revision_feedback=[
            {"source": "tech_lead", "reason": "This must target production."},
            {"source": "tech_lead", "reason": "Actually, staging is fine for now."},
        ],
    )

    spec = _to_devops_task_spec(task)

    assert spec.environment == "staging"
    assert spec.platform_scope.environments == ["dev", "staging"]


def test_devops_worker_falls_back_to_task_text_when_no_feedback_mentions_environment() -> None:
    """A feedback entry that doesn't mention any environment at all (e.g. a purely
    cosmetic revision request) must not be treated as a "no signal, default to
    staging" override -- the original task text should still decide."""
    task = _base_task(
        title="Add production deployment workflow",
        description="Deploy to production with a blue-green rollout.",
        revision_feedback=[{"source": "tech_lead", "reason": "Please rename the workflow file."}],
    )

    spec = _to_devops_task_spec(task)

    assert spec.environment == "production"


# --------------------------------------- explicit development-only environment scope (finding O)


def test_devops_worker_spec_excludes_staging_for_dev_only_task() -> None:
    """A task explicitly scoped to dev-only must not also claim staging in
    platform_scope.environments -- the CI/CD and Deployment specialists read that
    list directly and could otherwise generate staging configuration the task
    explicitly excluded."""
    task = _base_task(
        title="Add dev-only smoke test workflow",
        description="Create a dev-only workflow; do not deploy to staging.",
    )

    spec = _to_devops_task_spec(task)

    assert spec.environment == "staging"
    assert spec.platform_scope.environments == ["dev"]


def test_devops_worker_spec_keeps_staging_when_not_explicitly_excluded() -> None:
    spec = _to_devops_task_spec(
        _base_task(title="Add lint pipeline", description="Lint and test on PRs")
    )
    assert spec.platform_scope.environments == ["dev", "staging"]


def test_devops_worker_spec_production_never_dev_only() -> None:
    """A production-scoped task never claims to be dev-only, regardless of any
    incidental "staging" phrasing -- production always gets ["dev", "production"]."""
    task = _base_task(
        title="Add production deployment workflow",
        description="Deploy to production; skip the intermediate staging step.",
    )

    spec = _to_devops_task_spec(task)

    assert spec.environment == "production"
    assert spec.platform_scope.environments == ["dev", "production"]


def test_devops_worker_spec_excludes_staging_from_acceptance_criteria() -> None:
    """A groomed task can carry its dev-only scope in an acceptance criterion rather
    than the title/description -- the same field _derive_environment already scans
    for the production/staging verdict must also govern the dev-only exclusion, or
    the two would disagree about whether staging is in play."""
    task = _base_task(
        title="Add deployment workflow",
        description="Set up the deploy pipeline",
        acceptance_criteria=["development only; do not deploy to staging"],
    )

    spec = _to_devops_task_spec(task)

    assert spec.platform_scope.environments == ["dev"]


def test_devops_worker_spec_dev_only_revision_feedback_overrides_stale_production() -> None:
    """A task originally scoped to production can be redirected to dev-only by
    revision feedback that never says "staging" at all -- only "development"/"dev".
    The newest such redirect must still be recognized as an environment signal and
    exclude staging, not fall through to the stale original production text."""
    task = _base_task(
        title="Add production deployment workflow",
        description="Deploy to production with a blue-green rollout.",
        revision_feedback=[
            {"source": "tech_lead", "reason": "Actually, make this development-only for now."}
        ],
    )

    spec = _to_devops_task_spec(task)

    assert spec.platform_scope.environments == ["dev"]


def test_devops_worker_ignores_incidental_environment_word_in_feedback() -> None:
    """A feedback line can mention an environment word without redirecting the
    target environment at all -- e.g. "rebase onto development" names the git
    branch, not the deploy environment. Treating a bare mention as a redirect
    would silently drop a genuinely production-scoped task's approval-gate
    policy checks."""
    task = _base_task(
        title="Add production deployment workflow",
        description="Deploy to production with a blue-green rollout.",
        revision_feedback=[
            {"source": "tech_lead", "reason": "Rebase onto development before merging."}
        ],
    )

    spec = _to_devops_task_spec(task)

    assert spec.environment == "production"
    assert spec.platform_scope.environments == ["dev", "production"]


def test_devops_worker_ignores_git_stage_verb_in_feedback() -> None:
    """Feedback that reads "Stage the generated files" uses "stage" as the
    git-add verb, not the staging environment -- it must not be treated as an
    environment redirect."""
    task = _base_task(
        title="Add production deployment workflow",
        description="Deploy to production with a blue-green rollout.",
        revision_feedback=[
            {"source": "tech_lead", "reason": "Stage the generated files before committing."}
        ],
    )

    spec = _to_devops_task_spec(task)

    assert spec.environment == "production"


# ------------------------------------------------- task scope in acceptance_criteria (finding H)


def test_devops_worker_spec_folds_task_scope_into_acceptance_criteria() -> None:
    """CICDPipelineAgent.build_context and DeploymentStrategyAgent.build_context read
    acceptance_criteria but never goal.summary or scope -- only InfrastructureAsCodeAgent
    reads scope. An operational requirement recorded only in the task description (e.g.
    "use GitLab CI, deploy to ECS") would otherwise be invisible to those two specialists."""
    task = _base_task(
        description="Use GitLab CI and deploy to ECS with a canary rollout.",
        out_of_scope="Do not touch the legacy Jenkins pipeline.",
    )

    spec = _to_devops_task_spec(task)

    assert "Use GitLab CI and deploy to ECS with a canary rollout." in spec.acceptance_criteria[0]
    # out_of_scope must NOT be folded into acceptance_criteria/scope.included -- see
    # test_devops_worker_scope_note_excludes_out_of_scope_to_protect_approval_policy.
    assert "Do not touch the legacy Jenkins pipeline." not in spec.acceptance_criteria[0]


def test_devops_worker_spec_scope_note_includes_title_alongside_description() -> None:
    """A nonempty description does not guarantee it restates every requirement the
    title carries (e.g. title "Add canary Kubernetes deployment" vs. a generic
    description "Configure deployment for the service" that never says "canary").
    DeploymentStrategyAgent.build_context reads neither title nor scope, so the
    title's requirement must reach acceptance_criteria alongside the description,
    not be dropped whenever a description happens to be present."""
    task = _base_task(
        title="Add canary Kubernetes deployment",
        description="Configure deployment for the service.",
    )

    spec = _to_devops_task_spec(task)

    assert "Add canary Kubernetes deployment" in spec.acceptance_criteria[0]
    assert "Configure deployment for the service." in spec.acceptance_criteria[0]


def test_devops_worker_scope_note_excludes_out_of_scope_to_protect_approval_policy() -> None:
    """A production task that lists an approval gate under out_of_scope (e.g. "Manual
    approval gates" -- explicitly NOT implementing one) must not have that exclusion
    folded into acceptance_criteria/scope.included: _enforce_env_policy scans
    scope.included for a POSITIVE "approval" mention to satisfy its mandatory
    production approval-gate check, so an excluded approval gate would otherwise
    incorrectly satisfy it. scope.excluded (never scanned by that policy) still
    carries the exclusion verbatim for InfrastructureAsCodeAgent."""
    task = _base_task(
        title="Add production deployment workflow",
        description="Deploy to production with a blue-green rollout.",
        out_of_scope="Manual approval gates",
    )

    spec = _to_devops_task_spec(task)

    assert not any("approval" in item.lower() for item in spec.acceptance_criteria)
    assert not any("approval" in item.lower() for item in spec.scope.included)
    assert spec.scope.excluded == ["Manual approval gates"]


def test_devops_worker_spec_scope_note_falls_back_to_title_without_description() -> None:
    """DeploymentStrategyAgent.build_context reads neither title nor goal/scope --
    only acceptance_criteria (plus environments/constraints). A task carrying its
    requirement only in the title (a valid Task: description defaults to empty)
    must still reach that specialist via the acceptance-criteria scope note."""
    task = _base_task(title="Add canary Kubernetes deployment", description="")

    spec = _to_devops_task_spec(task)

    assert "Add canary Kubernetes deployment" in spec.acceptance_criteria[0]


def test_devops_worker_spec_scope_note_omitted_without_description_or_exclusion() -> None:
    task = _base_task(title="", description="", out_of_scope="", acceptance_criteria=["a criterion"])

    spec = _to_devops_task_spec(task)

    assert spec.acceptance_criteria == ["a criterion"]
