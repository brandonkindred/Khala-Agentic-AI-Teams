"""Adapter that lets the DevOps team act as a coding-team worker.

Sibling of ``v2_team_worker.py``: implements the same duck-typed coding-team
worker contract (``agent_id``, ``stack_spec``, ``team_kind``,
``run_implement(task, repo_path) -> dict``) but delegates to
``DevOpsTeamLeadAgent.run_task`` instead of a v2 team's ``run_workflow``. Only
constructed by ``worker_factory._build_implementation_worker`` when
``CODING_TEAM_DEVOPS_ROUTING`` routes a stack to ``"devops"`` (see
``team_routing.py``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from software_engineering_team.devops_team.models import (
    DevOpsCompletionPackage,
    DevOpsConstraints,
    DevOpsTaskSpec,
    PlatformScope,
    RepoContext,
    SecretsConstraints,
    TaskGoal,
    TaskScope,
)
from software_engineering_team.devops_team.orchestrator import _legacy_environment_from_text
from software_engineering_team.models import StackSpec
from software_engineering_team.v2_team_worker import (
    _changes_summary,
    _failed_result,
    _feedback_lines,
    _prepare_feature_branch,
    _task_feature_name,
    _validate_task_interface,
)

logger = logging.getLogger(__name__)

_TEAM_LABEL = "devops"

# Static defaults mirroring devops_team/orchestrator.py's _build_legacy_spec, for
# DevOpsTaskSpec fields the coding-team Task model does not carry. Deliberate
# scope cut: no rich platform_scope/repo_context/constraints planning yet (see
# the plan's "Scope cuts" section) -- this can grow richer once the Tech Lead's
# planning phase threads real repo/platform context through.
_DEFAULT_CLOUD = "on-premises"
_DEFAULT_APP_REPO = "application"
_DEFAULT_INFRA_REPO = "platform-infra"
_DEFAULT_SECRETS_SOURCE = "managed_secret_store"
_DEFAULT_ACCEPTANCE_CRITERIA = (
    "CI/CD workflow exists and validates",
    "Deployment strategy and rollback documented",
    "Security and policy review executed",
)
_DEFAULT_ROLLBACK_REQUIREMENTS = ("Rollback to previous known good release",)
_DEFAULT_SECURITY_CONSTRAINTS = ("No plaintext credentials", "Least privilege IAM")
_DEFAULT_COMPLIANCE_CONSTRAINTS = ("Audit trail required",)


def _augment_goal_summary(task: Any) -> str:
    """Fold coding-team Tech Lead revision feedback into the DevOps goal summary."""
    summary = task.description or task.title or task.id
    feedback = _feedback_lines(list(task.revision_feedback or []))
    if not feedback:
        return summary
    rendered = "\n".join(f"- {line}" for line in feedback)
    return (
        f"{summary}\n\n"
        f"CODING TEAM TECH LEAD FEEDBACK FOR {_TEAM_LABEL}:\n"
        f"Address every item below on the existing task before sending the branch back "
        f"for Tech Lead review. Include a short summary of how each item was addressed.\n"
        f"{rendered}"
    )


def _derive_environment(task: Any) -> str:
    """Infer the target environment from the task's own text.

    Reuses ``_legacy_environment_from_text`` -- the same inference
    ``_build_legacy_spec`` already applies for free-text ``run_workflow``
    callers -- rather than pinning every coding-team-dispatched task to
    ``"dev"``. Pinning would silently strip the production approval-gate
    check (``_enforce_env_policy``) and leave ``release_readiness.
    required_approvals``/``handoff.prod_approval_required`` empty even when
    the task is explicitly about production infrastructure (e.g. "add a
    production deployment workflow"), letting production-targeting config
    merge without the scrutiny DevOps normally enforces for it.

    Preconditions: none.
    Postconditions:
        - Returns ``"production"`` or ``"staging"`` per
          ``_legacy_environment_from_text``'s rules (defaults to
          ``"staging"`` absent an explicit production signal -- matching
          ``_build_legacy_spec``'s existing default for the same problem).
        - A task that IS production-scoped but whose description carries no
          explicit approval-gate language will correctly fail Phase 1's
          environment-policy gate rather than silently proceeding -- the
          same trade-off ``run_workflow`` callers already accept.
    """
    combined_text = f"{task.description or ''} {task.title or ''}".lower()
    return _legacy_environment_from_text(combined_text)


def _revision_feedback_scope_note(task: Any) -> Optional[str]:
    """Render ``task.revision_feedback`` as a single scope/acceptance-criteria entry.

    None of the Phase 2 specialist agents (``InfrastructureAsCodeAgent``,
    ``CICDPipelineAgent``, ``DeploymentStrategyAgent``) read
    ``DevOpsTaskSpec.goal.summary`` -- only ``scope.included`` (IaC) and
    ``acceptance_criteria`` (CI/CD, Deployment). Folding feedback only into
    ``goal.summary``, as ``_augment_goal_summary`` does, would leave every
    specialist agent blind to it on a revision round -- likely regenerating
    the same rejected output until the revision cap. This note is additionally
    injected into both ``scope.included`` and ``acceptance_criteria`` (see
    ``_to_devops_task_spec``) to reach all three.

    Returns ``None`` when there is no revision feedback to report.
    """
    feedback = _feedback_lines(list(task.revision_feedback or []))
    if not feedback:
        return None
    return "REVISION FEEDBACK TO ADDRESS: " + "; ".join(feedback)


def _to_devops_task_spec(task: Any) -> DevOpsTaskSpec:
    """Build a structured ``DevOpsTaskSpec`` from a coding-team ``Task``.

    Preconditions:
        - ``task`` satisfies the coding-team worker task interface (validated
          by the caller via ``_validate_task_interface`` before this runs).
    Postconditions:
        - ``title`` is exactly ``task.title`` (or ``task.id`` when blank),
          never truncated -- it must match what ``make_branch_suffix`` hashes
          when the pipeline cuts its feature branch, or the worker and
          pipeline disagree on the branch name.
        - ``environment``/``platform_scope.environments`` are derived from the
          task's own text via ``_derive_environment`` (never pinned to
          ``"dev"``), so a genuinely production-scoped task retains the
          production environment-policy/approval-gate checks instead of
          silently losing them.
        - Fields the coding-team ``Task`` model does not carry (cloud/runtime,
          repo_context, constraints) use the static ``_DEFAULT_*`` module
          constants, mirroring ``_build_legacy_spec``'s existing defaulting
          pattern for the same problem.
        - When ``task.revision_feedback`` is non-empty, a rendered note is
          appended to both ``scope.included`` and ``acceptance_criteria`` (see
          ``_revision_feedback_scope_note``) so every Phase 2 specialist agent
          sees it, not just whichever field each happens to read.
    """
    goal_summary = _augment_goal_summary(task)
    environment = _derive_environment(task)
    scope_excluded = [task.out_of_scope] if getattr(task, "out_of_scope", "") else []
    scope_included = [task.description or task.title or task.id]
    acceptance_criteria = list(task.acceptance_criteria or []) or list(_DEFAULT_ACCEPTANCE_CRITERIA)
    feedback_note = _revision_feedback_scope_note(task)
    if feedback_note:
        scope_included = scope_included + [feedback_note]
        acceptance_criteria = acceptance_criteria + [feedback_note]
    return DevOpsTaskSpec(
        task_id=task.id,
        title=task.title or task.id,
        priority=str(getattr(task, "priority", "") or "medium"),
        platform_scope=PlatformScope(cloud=_DEFAULT_CLOUD, environments=["dev", environment]),
        repo_context=RepoContext(
            app_repo=_DEFAULT_APP_REPO,
            infra_repo=_DEFAULT_INFRA_REPO,
            pipeline_repo=_DEFAULT_APP_REPO,
        ),
        goal=TaskGoal(summary=goal_summary),
        scope=TaskScope(included=scope_included, excluded=scope_excluded),
        constraints=DevOpsConstraints(secrets=SecretsConstraints(source=_DEFAULT_SECRETS_SOURCE)),
        acceptance_criteria=acceptance_criteria,
        dependencies=list(task.dependencies or []),
        rollback_requirements=list(_DEFAULT_ROLLBACK_REQUIREMENTS),
        security_constraints=list(_DEFAULT_SECURITY_CONSTRAINTS),
        compliance_constraints=list(_DEFAULT_COMPLIANCE_CONSTRAINTS),
        environment=environment,
    )


def _devops_result_summary(pkg: Optional[DevOpsCompletionPackage]) -> str:
    """Render a completion package's gates/readiness/notes as review prose."""
    if pkg is None:
        return ""
    lines: List[str] = []
    if pkg.quality_gates:
        gates = ", ".join(f"{name}={status}" for name, status in sorted(pkg.quality_gates.items()))
        lines.append(f"Quality gates: {gates}")
    rr = pkg.release_readiness
    if rr is not None:
        lines.append(
            f"Release readiness: strategy={rr.deployment_strategy or 'unspecified'}, "
            f"rollback_available={rr.rollback_available}, "
            f"alerting_configured={rr.alerting_configured}"
        )
    if pkg.notes:
        lines.append("Notes:\n" + "\n".join(f"- {note}" for note in pkg.notes))
    if pkg.risks_remaining:
        lines.append("Risks remaining:\n" + "\n".join(f"- {risk}" for risk in pkg.risks_remaining))
    return "\n\n".join(lines)


class DevOpsTeamWorker:
    """Coding-team worker facade for ``devops_team``.

    Preconditions:
        - ``team_lead`` is a ``DevOpsTeamLeadAgent`` (or duck-typed
          equivalent exposing ``run_task(spec, *, repo_path, merge_to_development)``).
    Invariants:
        - ``team_kind`` is always ``"devops"`` -- fixed at construction, not a
          caller-supplied parameter, since only one devops worker shape exists.
    """

    def __init__(self, *, agent_id: str, stack_spec: StackSpec, team_lead: Any) -> None:
        self.agent_id = agent_id
        self.stack_spec = stack_spec
        self.team_kind = "devops"
        self.team_lead = team_lead

    def run_implement(self, task: Any, repo_path: str | Path) -> Dict[str, Any]:
        """Execute the task via DevOps and return a coding-team handoff result.

        Args:
            task: Coding-team task-like object to implement.
            repo_path: Repository root where the task should be implemented.

        Returns:
            A dict containing status, feature_branch, changes_summary,
            files_to_create_or_edit, commands_run, open_questions, and error --
            the same generic shape ``V2TeamWorker.run_implement`` returns.
        """
        path = Path(repo_path).resolve()
        task_id = str(getattr(task, "id", "") or "unknown-task")
        try:
            _validate_task_interface(task)
        except ValueError as exc:
            logger.warning("devops worker received malformed task %s: %s", task_id, exc)
            return _failed_result(
                getattr(task, "feature_branch", None) or f"feature/{_task_feature_name(task)}",
                str(exc),
            )
        branch_ok, prepared_branch = _prepare_feature_branch(path, task)
        if not branch_ok:
            logger.warning(
                "devops worker could not prepare branch for task %s: %s", task_id, prepared_branch
            )
            return _failed_result(
                getattr(task, "feature_branch", None) or f"feature/{_task_feature_name(task)}",
                f"failed to prepare feature branch: {prepared_branch}",
            )
        spec = _to_devops_task_spec(task)
        try:
            result = self.team_lead.run_task(spec, repo_path=path, merge_to_development=False)
        except Exception as exc:  # noqa: BLE001 - worker failure is task-local
            logger.exception("devops worker failed for task %s", task_id)
            return _failed_result(prepared_branch, str(exc))

        pkg = result.completion_package
        branch = (pkg.git_operations.branch_created if pkg is not None else "") or prepared_branch
        files_changed = list(pkg.files_changed) if pkg is not None else []
        commands_run = (
            [commit.message for commit in pkg.git_operations.commits if commit.message]
            if pkg is not None
            else []
        )
        # A "completed" package with no delivered artifacts (pkg.files_changed is always
        # exactly aggregated_artifacts.keys() -- see devops_team/doc_runbook_agent/agent.py)
        # means Phase 2 produced nothing and Phase 3/5 both skip writing entirely: the
        # feature branch this worker cut has zero new commits. Accepting that as
        # "in_review" would hand the Tech Lead an empty diff, which can be approved and
        # merged as if the task were actually implemented. Treat it as incomplete instead.
        completed_without_artifacts = (
            result.success and pkg is not None and pkg.status == "completed" and not files_changed
        )
        success = bool(
            result.success and pkg is not None and pkg.status == "completed" and files_changed
        )
        if not success:
            summary = _devops_result_summary(pkg)
            if completed_without_artifacts:
                reason = "DevOps pipeline completed without producing any files to deliver"
            else:
                reason = str(result.failure_reason or "DevOps pipeline did not complete")
            # Fold the rendered gate/notes detail into the error itself, not just
            # changes_summary: the swarm's _handle_incomplete_implementation only persists
            # result["error"] into revision_feedback, so a bare "Quality gates failed"
            # label (with the actionable findings stranded in changes_summary) would leave
            # the next attempt with nothing concrete to act on.
            error_detail = f"{reason}\n\n{summary}" if summary else reason
            return _failed_result(
                branch,
                error_detail,
                changes_summary=summary,
                files_to_create_or_edit=files_changed,
                commands_run=commands_run,
            )
        return {
            "status": "in_review",
            "feature_branch": branch,
            "changes_summary": _changes_summary(
                team_label=_TEAM_LABEL,
                branch=branch,
                result_summary=_devops_result_summary(pkg),
                feedback=list(task.revision_feedback or []),
            ),
            "files_to_create_or_edit": files_changed,
            "commands_run": commands_run,
            "open_questions": [],
            "error": None,
        }
