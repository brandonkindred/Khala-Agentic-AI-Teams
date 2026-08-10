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
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared.git.git_utils import (
    DEVELOPMENT_BRANCH,
    BaselineDiffUnavailable,
    list_changed_and_deleted,
    reset_hard_to,
)
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
from software_engineering_team.devops_team.orchestrator import (
    DEFAULT_LEGACY_ACCEPTANCE_CRITERIA,
    DEFAULT_LEGACY_APP_REPO,
    DEFAULT_LEGACY_CLOUD,
    DEFAULT_LEGACY_COMPLIANCE_CONSTRAINTS,
    DEFAULT_LEGACY_INFRA_REPO,
    DEFAULT_LEGACY_ROLLBACK_REQUIREMENTS,
    DEFAULT_LEGACY_SECRETS_SOURCE,
    DEFAULT_LEGACY_SECURITY_CONSTRAINTS,
    LEGACY_WORD_TOKEN,
    NEGATION_TOKENS,
    legacy_environment_from_text,
)
from software_engineering_team.models import StackSpec
from software_engineering_team.v2_team_worker import (
    augment_description,
    changes_summary,
    failed_result,
    feedback_lines,
    prepare_feature_branch,
    task_feature_name,
    validate_task_interface,
)

logger = logging.getLogger(__name__)

_TEAM_LABEL = "devops"

# DevOpsTaskSpec fields the coding-team Task model does not carry use the same
# static defaults devops_team/orchestrator.py's _build_legacy_spec already uses
# for the same problem (imported above as the module's public re-exports, so
# there is exactly one copy of each value). Deliberate scope cut: no rich
# platform_scope/repo_context/constraints planning yet (see the plan's "Scope
# cuts" section) -- this can grow richer once the Tech Lead's planning phase
# threads real repo/platform context through.


_ENV_NAME = r"(?:production|prod|staging|stage|dev|development)"
_ENV_REDIRECT_TOKEN = re.compile(
    r"\b" + _ENV_NAME + r"[- ]only\b"
    r"|\bdeploy(?:ed|ing)?\s+(?:to|on|into)\s+(?:the\s+)?" + _ENV_NAME + r"\b"
    r"|\btarget(?:s|ing)?\s+(?:environment\s+)?(?:is\s+|:\s*)?(?:the\s+)?" + _ENV_NAME + r"\b"
    r"|\b" + _ENV_NAME + r"\s+(?:environment|env)\b"
    r"|\b" + _ENV_NAME + r"\s+is\s+(?:fine|ok|okay|good|acceptable)\b",
    re.IGNORECASE,
)


def _latest_feedback_environment_text(task: Any) -> Optional[str]:
    """Return the newest revision-feedback line that carries an explicit
    target-environment redirect, or ``None`` when no feedback line does.

    ``task.revision_feedback`` is append-only, so an early round's "make this
    production" and a later round's "actually, staging only" (or "make this
    dev-only") both persist in the list. Blending every entry into one
    combined-text scan means a stale early mention can never be overridden --
    ``legacy_environment_from_text`` returns ``"production"`` if ANY clause
    across the combined text implies it. Scanning newest-first and stopping at
    the first line that carries an explicit redirect lets the latest
    instruction win instead.

    Matching requires explicit target-environment wording (``"X-only"``,
    ``"deploy to X"``, ``"target X"``, ``"X environment"``, ``"X is fine"``) --
    a bare environment-name mention is NOT enough. A feedback line can mention
    "development"/"staging" incidentally without redirecting the target
    environment at all -- e.g. "rebase onto development" (the git branch, not
    the deploy environment) or "stage the generated files" (the git-add verb,
    not the staging environment) -- and treating those as redirects would
    silently drop a genuine production task's approval-gate policy checks.

    Preconditions: none.
    Postconditions:
        - Returns the single newest feedback line carrying an explicit
          target-environment redirect (case-insensitive), or ``None`` if no
          feedback line does -- callers should fall back to the original task
          text in that case.
    """
    for line in reversed(feedback_lines(list(task.revision_feedback or []))):
        if _ENV_REDIRECT_TOKEN.search(line):
            return line.lower()
    return None


def _environment_resolution_text(task: Any) -> str:
    """Return the single text span that determines both ``environment`` and
    ``platform_scope.environments`` -- the newest revision-feedback line that
    mentions an environment (see ``_latest_feedback_environment_text``) when
    one exists, since the latest instruction is authoritative and must govern
    both the production/staging verdict and any dev-only exclusion together;
    otherwise the task's own description, title, and acceptance criteria (a
    groomed task commonly carries its environment scope in a criterion, e.g.
    "Production deploy requires explicit approval" or "development only; do
    not deploy to staging", rather than the title/description, and the CI/CD
    and Deployment specialist agents already consume ``acceptance_criteria``
    directly).
    """
    latest_feedback_text = _latest_feedback_environment_text(task)
    if latest_feedback_text is not None:
        return latest_feedback_text
    parts = [task.description or "", task.title or "", *(task.acceptance_criteria or [])]
    return " ".join(parts).lower()


def _derive_environment(task: Any) -> str:
    """Infer the target environment from ``_environment_resolution_text(task)``.

    Reuses ``legacy_environment_from_text`` -- the same inference
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
          ``legacy_environment_from_text``'s rules (defaults to
          ``"staging"`` absent an explicit production signal -- matching
          ``_build_legacy_spec``'s existing default for the same problem;
          also the value returned for a dev-only task, since the model has no
          distinct "dev" environment enum -- ``_platform_environments``
          separately excludes staging from the environments list for that
          case).
        - A task that IS production-scoped but whose description carries no
          explicit approval-gate language will correctly fail Phase 1's
          environment-policy gate rather than silently proceeding -- the
          same trade-off ``run_workflow`` callers already accept.
    """
    return legacy_environment_from_text(_environment_resolution_text(task))


_STAGING_INTERVENING_TOKENS = frozenset(
    {"to", "in", "on", "the", "a", "an", "environment", "env", "deploy", "deployed", "deploying"}
)
_DEV_ONLY_PATTERN = re.compile(r"\bdev[- ]?only\b|\bdevelopment[- ]?only\b", re.IGNORECASE)


def _excludes_staging(text: str) -> bool:
    """Return True when ``text`` explicitly excludes staging as a target environment.

    Recognizes an explicit "dev only"/"development-only" statement, or
    "staging" governed by a preceding negation token (``no``/``not``/``non``,
    allowing intervening fillers like "deploy to") -- the same word-adjacency
    negation pattern ``_scope_item_mentions_approval`` already uses for
    "approval", reused here for "staging" rather than reimplemented.
    """
    if _DEV_ONLY_PATTERN.search(text):
        return True
    tokens = LEGACY_WORD_TOKEN.findall(text.lower())
    for i, token in enumerate(tokens):
        if token not in ("staging", "stage"):
            continue
        j = i - 1
        while j >= 0 and tokens[j] in _STAGING_INTERVENING_TOKENS:
            j -= 1
        if j >= 0 and tokens[j] in NEGATION_TOKENS:
            return True
    return False


def _platform_environments(task: Any, environment: str) -> List[str]:
    """Build the ``platform_scope.environments`` list for ``environment``.

    Preconditions: ``environment`` is ``"production"`` or ``"staging"`` (the
      only values ``_derive_environment`` returns).
    Postconditions:
        - ``"production"`` always returns ``["dev", "production"]`` -- a
          production-scoped task never claims to also be dev-only.
        - ``"staging"`` returns ``["dev"]`` alone when
          ``_environment_resolution_text(task)`` -- the SAME text
          ``_derive_environment`` used, so a dev-only exclusion never
          disagrees with the environment verdict it came from -- explicitly
          excludes staging (see ``_excludes_staging``, e.g. "dev only; do not
          deploy to staging"), so the CI/CD and Deployment specialists --
          which read this list -- don't generate staging configuration the
          task explicitly ruled out; otherwise returns ``["dev", "staging"]``
          as before.
    """
    if environment == "production":
        return ["dev", "production"]
    text = _environment_resolution_text(task)
    return ["dev"] if _excludes_staging(text) else ["dev", "staging"]


def _task_scope_note(task: Any) -> Optional[str]:
    """Render the task's title and description together as an acceptance-criteria entry.

    ``CICDPipelineAgent.build_context`` and ``DeploymentStrategyAgent.build_context``
    read ``acceptance_criteria`` (plus environments/constraints) but never
    ``goal.summary`` or ``scope`` -- ``DeploymentStrategyAgent`` doesn't even read
    ``title`` -- only ``InfrastructureAsCodeAgent.build_context`` reads ``scope``.
    So an operational requirement recorded only in ``task.description`` (e.g.
    "use GitLab CI, deploy to ECS") or only in ``task.title`` (e.g. a title of
    "Add canary Kubernetes deployment" paired with a generic description like
    "Configure deployment for the service", which never repeats "canary") is
    invisible to those two specialists unless it also reaches
    ``acceptance_criteria`` -- the same problem ``_revision_feedback_scope_note``
    solves for revision feedback. A nonempty description does not guarantee it
    restates every requirement the title carries, so both are combined rather
    than the title being dropped whenever a description exists.

    Deliberately excludes ``task.out_of_scope``: ``_enforce_env_policy`` scans
    ``scope.included`` (which ``acceptance_criteria`` feeds into, see
    ``_to_devops_task_spec``) for a POSITIVE "approval" mention to satisfy its
    mandatory production approval-gate check. Folding an exclusion like
    "Manual approval gates" in here would make an explicitly EXCLUDED approval
    gate satisfy that check. ``scope.excluded`` (which the policy check never
    scans) already carries ``task.out_of_scope`` verbatim; see
    ``_to_devops_task_spec``'s docstring for how it additionally reaches
    ``CICDPipelineAgent``/``DeploymentStrategyAgent`` alongside
    ``InfrastructureAsCodeAgent``.

    Returns ``None`` when the task carries neither a description nor a title.
    """
    title = (task.title or "").strip()
    description = (task.description or "").strip()
    if title and description and title != description:
        text = f"{title}: {description}"
    else:
        text = description or title
    if not text:
        return None
    return f"TASK SCOPE: {text}"


def _revision_feedback_scope_note(task: Any) -> Optional[str]:
    """Render ``task.revision_feedback`` as a single scope/acceptance-criteria entry.

    None of the Phase 2 specialist agents (``InfrastructureAsCodeAgent``,
    ``CICDPipelineAgent``, ``DeploymentStrategyAgent``) read
    ``DevOpsTaskSpec.goal.summary`` -- only ``scope.included`` (IaC) and
    ``acceptance_criteria`` (CI/CD, Deployment). Folding feedback only into
    ``goal.summary``, as ``augment_description`` does, would leave every
    specialist agent blind to it on a revision round -- likely regenerating
    the same rejected output until the revision cap. This note is additionally
    injected into both ``scope.included`` and ``acceptance_criteria`` (see
    ``_to_devops_task_spec``) to reach all three.

    Returns ``None`` when there is no revision feedback to report.
    """
    feedback = feedback_lines(list(task.revision_feedback or []))
    if not feedback:
        return None
    return "REVISION FEEDBACK TO ADDRESS: " + "; ".join(feedback)


def _to_devops_task_spec(task: Any) -> DevOpsTaskSpec:
    """Build a structured ``DevOpsTaskSpec`` from a coding-team ``Task``.

    Preconditions:
        - ``task`` satisfies the coding-team worker task interface (validated
          by the caller via ``validate_task_interface`` before this runs).
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
          repo_context, constraints) use the same ``DEFAULT_LEGACY_*`` constants
          ``_build_legacy_spec`` uses for the same problem (imported from
          ``devops_team.orchestrator``, not re-declared here).
        - When ``task.revision_feedback`` is non-empty, a rendered note is
          appended to both ``scope.included`` and ``acceptance_criteria`` (see
          ``_revision_feedback_scope_note``) so every Phase 2 specialist agent
          sees it, not just whichever field each happens to read.
        - ``task.acceptance_criteria`` is copied into ``scope.included`` too
          (not just the ``acceptance_criteria`` field): ``_enforce_env_policy``
          only scans ``scope.included`` for approval-gate language, so an
          approval requirement recorded as a groomed acceptance criterion
          (e.g. "Production deploy requires explicit approval") must be
          visible there too, or a correctly production-derived task would
          fail Phase 1 despite already carrying the required gate structurally.
        - The task's own description (or title, absent a description) is also
          folded into ``acceptance_criteria`` as a leading entry (see
          ``_task_scope_note``) so ``CICDPipelineAgent``/``DeploymentStrategyAgent``
          -- which read ``acceptance_criteria`` but never ``goal``/``scope``/
          (for ``DeploymentStrategyAgent``) ``title`` -- see the requirement
          too, not just ``InfrastructureAsCodeAgent`` (which reads ``scope``
          directly). Deliberately excludes ``out_of_scope``: see
          ``_task_scope_note``'s docstring for why folding an exclusion in here
          would let it satisfy the production approval-gate policy check.
    """
    goal_summary = augment_description(task, _TEAM_LABEL)
    environment = _derive_environment(task)
    platform_environments = _platform_environments(task, environment)
    scope_excluded = [task.out_of_scope] if getattr(task, "out_of_scope", "") else []
    acceptance_criteria = list(task.acceptance_criteria or []) or list(
        DEFAULT_LEGACY_ACCEPTANCE_CRITERIA
    )
    scope_note = _task_scope_note(task)
    if scope_note:
        acceptance_criteria = [scope_note] + acceptance_criteria
    scope_included = [task.description or task.title or task.id, *acceptance_criteria]
    feedback_note = _revision_feedback_scope_note(task)
    if feedback_note:
        scope_included = scope_included + [feedback_note]
        acceptance_criteria = acceptance_criteria + [feedback_note]
    return DevOpsTaskSpec(
        task_id=task.id,
        title=task.title or task.id,
        priority=str(getattr(task, "priority", "") or "medium"),
        platform_scope=PlatformScope(
            cloud=DEFAULT_LEGACY_CLOUD, environments=platform_environments
        ),
        repo_context=RepoContext(
            app_repo=DEFAULT_LEGACY_APP_REPO,
            infra_repo=DEFAULT_LEGACY_INFRA_REPO,
            pipeline_repo=DEFAULT_LEGACY_APP_REPO,
        ),
        goal=TaskGoal(summary=goal_summary),
        scope=TaskScope(included=scope_included, excluded=scope_excluded),
        constraints=DevOpsConstraints(
            secrets=SecretsConstraints(source=DEFAULT_LEGACY_SECRETS_SOURCE)
        ),
        acceptance_criteria=acceptance_criteria,
        dependencies=list(task.dependencies or []),
        rollback_requirements=list(DEFAULT_LEGACY_ROLLBACK_REQUIREMENTS),
        security_constraints=list(DEFAULT_LEGACY_SECURITY_CONSTRAINTS),
        compliance_constraints=list(DEFAULT_LEGACY_COMPLIANCE_CONSTRAINTS),
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


def _reset_prepared_branch_to_development(path: Path, task_id: str) -> tuple[bool, str]:
    """Wipe the just-checked-out feature branch's content back to development's tip.

    DevOps's Phase 2 specialists (IaC/CI-CD/Deployment) always regenerate their
    full artifact set from scratch every round -- there is nothing incremental
    to preserve on a retry, whether that retry follows a DevOps-internal gate
    rejection or a Tech Lead review rejection asking for changes, unlike an LLM
    applying patches. ``write_agent_output`` only ever adds/overwrites paths
    present in the new artifact map, never removing ones absent from it, so any
    file left over from a prior round -- including one the new round correctly
    omits -- would otherwise leak into the eventual diff unreviewed.

    Must be called AFTER ``prepare_feature_branch`` has already checked out
    the branch this task will use (whether newly created or reused/existing):
    ``reset_hard_to`` resets whatever is CURRENTLY checked out, so calling it
    before that checkout would reset the wrong branch (e.g. a different task's
    leftover state in this shared per-agent worktree) instead of this task's.
    For a brand-new branch this is a true no-op (it was just created from
    development's tip); for a reused/existing branch it clears prior commits
    and validation-tool leftovers (e.g. ``.terraform.lock.hcl``) alike, since
    ``reset_hard_to`` also cleans untracked files.

    Preconditions:
        - ``prepare_feature_branch(path, task)`` returned success; ``path``
          is currently checked out on the branch that call prepared.
    Postconditions:
        - Returns ``(True, message)`` on success. On failure returns
          ``(False, message)`` -- the caller must abort rather than proceed:
          continuing on a branch this reset failed to clean would silently
          reuse a rejected round's stale commits/validation leftovers, which
          is exactly what this reset exists to prevent.
    """
    ok, msg = reset_hard_to(path, DEVELOPMENT_BRANCH)
    if not ok:
        logger.warning("Failed to reset devops branch state for task %s: %s", task_id, msg)
    return ok, msg


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
            the same generic shape ``V2TeamWorker.run_implement`` returns, used
            for both outcomes rather than raising on failure. On success,
            ``status`` is ``"in_review"`` and ``error`` is ``None``. On failure
            (malformed task, branch prep/reset failure, a DevOps exception, or a
            blocked/incomplete/no-diff DevOps result), ``status`` is
            ``"failed"``, ``error`` is a non-empty descriptive string, and
            ``feature_branch``/``changes_summary``/``files_to_create_or_edit``/
            ``commands_run`` may still be partially populated (e.g. the branch
            that was prepared, or files/gates from a blocked DevOps package) so
            Tech Lead revision feedback stays actionable.
        """
        path = Path(repo_path).resolve()
        task_id = str(getattr(task, "id", "") or "unknown-task")
        try:
            validate_task_interface(task)
        except ValueError as exc:
            logger.warning("devops worker received malformed task %s: %s", task_id, exc)
            return failed_result(
                getattr(task, "feature_branch", None) or f"feature/{task_feature_name(task)}",
                str(exc),
            )
        branch_ok, prepared_branch = prepare_feature_branch(path, task)
        if not branch_ok:
            logger.warning(
                "devops worker could not prepare branch for task %s: %s", task_id, prepared_branch
            )
            return failed_result(
                getattr(task, "feature_branch", None) or f"feature/{task_feature_name(task)}",
                f"failed to prepare feature branch: {prepared_branch}",
            )
        reset_ok, reset_msg = _reset_prepared_branch_to_development(path, task_id)
        if not reset_ok:
            return failed_result(
                prepared_branch, f"failed to reset stale branch state: {reset_msg}"
            )
        spec = _to_devops_task_spec(task)
        try:
            result = self.team_lead.run_task(spec, repo_path=path, merge_to_development=False)
        except Exception as exc:  # noqa: BLE001 - worker failure is task-local
            logger.exception("devops worker failed for task %s", task_id)
            return failed_result(prepared_branch, str(exc))

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
        candidate_success = bool(
            result.success and pkg is not None and pkg.status == "completed" and files_changed
        )
        # files_changed being non-empty only proves Phase 2 GENERATED content, not that it
        # differs from what's already on the branch: write_files_and_commit/commit_working_tree
        # both treat "nothing to commit" (regenerated content identical to the existing tree --
        # e.g. a revision round whose artifacts happen to match a prior accepted attempt) as
        # success, so pkg.git_operations.commits can carry a synthetic commit message with no
        # actual commit behind it. Verify the branch actually diverges from development before
        # accepting it as review-ready; fail closed (treat as no verified diff) if the check
        # itself can't run, matching list_changed_and_deleted's own fail-closed contract.
        completed_without_diff = False
        if candidate_success:
            try:
                changed, deleted = list_changed_and_deleted(path, DEVELOPMENT_BRANCH)
            except BaselineDiffUnavailable as exc:
                logger.warning(
                    "devops worker could not verify branch diff for task %s: %s", task_id, exc
                )
                changed, deleted = [], []
            if not changed and not deleted:
                completed_without_diff = True
        success = candidate_success and not completed_without_diff
        if not success:
            summary = _devops_result_summary(pkg)
            if completed_without_diff:
                reason = "DevOps pipeline reported completion but the branch has no changes versus development"
            elif completed_without_artifacts:
                reason = "DevOps pipeline completed without producing any files to deliver"
            else:
                reason = str(result.failure_reason or "DevOps pipeline did not complete")
            # Fold the rendered gate/notes detail into the error itself, not just
            # changes_summary: the swarm's _handle_incomplete_implementation only persists
            # result["error"] into revision_feedback, so a bare "Quality gates failed"
            # label (with the actionable findings stranded in changes_summary) would leave
            # the next attempt with nothing concrete to act on.
            error_detail = f"{reason}\n\n{summary}" if summary else reason
            return failed_result(
                branch,
                error_detail,
                changes_summary=summary,
                files_to_create_or_edit=files_changed,
                commands_run=commands_run,
            )
        return {
            "status": "in_review",
            "feature_branch": branch,
            "changes_summary": changes_summary(
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
