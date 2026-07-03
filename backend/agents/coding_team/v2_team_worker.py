"""Adapters that let software-engineering v2 teams act as coding-team workers."""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any, Dict, List

from coding_team.models import StackSpec
from shared_dev_models import Task as SETask
from shared_dev_models import TaskStatus as SETaskStatus
from shared_dev_models import TaskType
from shared_git import branch_utils
from shared_git.git_utils import (
    DEVELOPMENT_BRANCH,
    checkout_branch,
    create_feature_branch,
    ensure_development_branch,
    initialize_new_repo,
)

logger = logging.getLogger(__name__)

_ALLOWED_TEAM_KINDS = frozenset({"backend", "frontend"})


def _feedback_lines(feedback: List[Dict[str, Any]]) -> List[str]:
    """Render prior Tech Lead/team feedback as concise actionable lines.

    Args:
        feedback: Feedback entries recorded by the coding-team Tech Lead or
            implementation workers. Dict entries may include source/type,
            reason/error/message, and requested_changes.

    Returns:
        Non-empty human-readable feedback lines suitable for v2 task context.
    """
    lines: List[str] = []
    for entry in feedback or []:
        if not isinstance(entry, dict):
            lines.append(str(entry))
            continue
        source = entry.get("source") or entry.get("type") or "review"
        reason = entry.get("reason") or entry.get("error") or entry.get("message") or ""
        if reason:
            lines.append(f"[{source}] {reason}")
        changes = entry.get("requested_changes") or []
        if isinstance(changes, list):
            for change in changes:
                lines.append(f"[{source}] {change}")
        elif changes:
            lines.append(f"[{source}] {changes}")
    return [ln for ln in lines if ln.strip()]


def _augment_description(task: Any, team_label: str) -> str:
    """Add coding-team revision feedback to the v2 team's task input.

    Args:
        task: Coding-team task-like object with title, description, and revision_feedback.
        team_label: Canonical recipient team label, such as ``backend_v2``.

    Returns:
        The task description augmented with explicit revision feedback when present.
    """
    description = task.description or task.title or task.id
    feedback = _feedback_lines(list(task.revision_feedback or []))
    if not feedback:
        return description
    rendered = "\n".join(f"- {line}" for line in feedback)
    return (
        f"{description}\n\n"
        f"CODING TEAM TECH LEAD FEEDBACK FOR {team_label}:\n"
        f"Address every item below on the existing task before sending the branch back "
        f"for Tech Lead review. Include a short summary of how each item was addressed.\n"
        f"{rendered}"
    )


def _changes_summary(
    *,
    team_label: str,
    branch: str,
    result_summary: str,
    feedback: List[Dict[str, Any]],
) -> str:
    """Build the review handoff summary expected by the coding-team Tech Lead.

    Args:
        team_label: Canonical v2 team label that performed the work.
        branch: Review branch returned to the coding-team Tech Lead.
        result_summary: Summary produced by the v2 workflow.
        feedback: Revision feedback the team was expected to address.

    Returns:
        A compact handoff summary for the coding-team review phase.
    """
    parts = [
        f"{team_label} completed the assigned coding-team task.",
        f"Review branch: {branch}",
    ]
    if result_summary:
        parts.append(f"Implementation summary:\n{result_summary}")
    feedback_lines = _feedback_lines(feedback)
    if feedback_lines:
        # Echo the feedback the team was asked to act on; this does NOT assert it was
        # resolved — the coding-team Tech Lead review verifies the actual diff.
        parts.append(
            "Revision feedback provided to the team (verify in review):\n"
            + "\n".join(f"- {line}" for line in feedback_lines)
        )
    return "\n\n".join(parts)


def _validate_task_interface(task: Any) -> None:
    """Validate the task shape required by the v2 team adapter."""
    required = (
        "id",
        "title",
        "description",
        "dependencies",
        "acceptance_criteria",
        "revision_feedback",
    )
    missing = [name for name in required if not hasattr(task, name)]
    if missing:
        raise ValueError(f"coding-team task is missing required field(s): {', '.join(missing)}")
    if not str(getattr(task, "id", "") or "").strip():
        raise ValueError("coding-team task is missing a non-empty id")
    list_fields = ("dependencies", "acceptance_criteria", "revision_feedback")
    invalid = [name for name in list_fields if not isinstance(getattr(task, name), list)]
    if invalid:
        raise ValueError("coding-team task field(s) must be lists: " + ", ".join(sorted(invalid)))


def _accepts_keyword(fn: Any, name: str) -> bool:
    """Return whether a callable accepts a named keyword argument.

    Args:
        fn: Callable to inspect.
        name: Keyword name to look for.

    Returns:
        True when the callable explicitly accepts the keyword or has ``**kwargs``.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        # Cannot introspect: assume the keyword is NOT accepted rather than risk a
        # "unexpected keyword argument" TypeError crashing the worker at call time.
        return False
    return any(
        param.kind == inspect.Parameter.VAR_KEYWORD or param_name == name
        for param_name, param in signature.parameters.items()
    )


def _task_feature_name(task: Any) -> str:
    """Build a stable, bounded, git-safe feature-branch suffix for a coding-team task.

    Delegates to the shared :func:`branch_utils.make_branch_suffix` so the coding-team
    worker and the software-engineering v2 deliver phases use a single slug
    implementation and produce identical branch names — the suffix ends in a stable
    hash of the raw task id, so two ids that slug identically after punctuation collapse
    (e.g. ``api.v1`` vs ``api-v1``) never collide and retries of the same task reuse it.

    Args:
        task: Coding-team task-like object with an id and optional title.

    Returns:
        A git-branch-safe slug ending in a stable task-id hash.
    """
    task_id = str(getattr(task, "id", "") or "task").strip() or "task"
    title = str(getattr(task, "title", "") or "").strip()
    return branch_utils.make_branch_suffix(task_id, title)


def _requirements_for_task(task: Any) -> str:
    """Build v2 task requirements without revision feedback.

    Args:
        task: Coding-team task-like object with description, title, id, and acceptance criteria.

    Returns:
        Explicit implementation requirements; revision feedback is intentionally
        kept in the description so planning can distinguish context from requirements.
    """
    requirements = task.description or task.title or task.id
    if task.acceptance_criteria:
        requirements += "\n\nAcceptance criteria:\n" + "\n".join(
            f"- {item}" for item in task.acceptance_criteria
        )
    return requirements


def _workflow_file_list(result: Any, deliver: Any) -> List[str]:
    """Return repo-relative paths the v2 workflow reports as delivered.

    Args:
        result: Workflow result object, optionally exposing ``final_files``.
        deliver: Deliver result object, optionally exposing ``delivered_files``.

    Returns:
        A sorted, de-duplicated list of repo-relative file paths.
    """
    delivered = getattr(deliver, "delivered_files", None)
    if isinstance(delivered, (list, tuple, set)):
        files = [str(path).strip() for path in delivered if str(path).strip()]
        if files:
            return sorted(dict.fromkeys(files))
    final_files = getattr(result, "final_files", None)
    if isinstance(final_files, dict):
        return sorted(str(path) for path in final_files if str(path).strip())
    return []


def _prepare_feature_branch(path: Path, task: Any) -> tuple[bool, str]:
    """Create or checkout the task branch before v2 execution can write files.

    Args:
        path: Repository path used by the v2 workflow.
        task: Coding-team task-like object with an optional existing feature_branch.

    Returns:
        ``(True, branch_name)`` on success, otherwise ``(False, reason)``.
    """
    existing_branch = str(getattr(task, "feature_branch", "") or "").strip()
    if existing_branch:
        ok, message = checkout_branch(path, existing_branch)
        return (True, existing_branch) if ok else (False, message)
    ready, message = _ensure_development_ready(path)
    if not ready:
        return False, message
    return create_feature_branch(path, DEVELOPMENT_BRANCH, _task_feature_name(task))


def _ensure_development_ready(path: Path) -> tuple[bool, str]:
    """Ensure git and development exist before creating a no-merge handoff branch.

    Args:
        path: Repository path to initialize or normalize.

    Returns:
        ``(True, message)`` when the repository can branch from development,
        otherwise ``(False, reason)``.
    """
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        return initialize_new_repo(path)
    return ensure_development_branch(path)


class V2TeamWorker:
    """Coding-team worker facade for backend_code_v2_team/frontend_code_v2_team.

    The adapter implements the coding-team worker interface while delegating the
    actual implementation workflow to one of the software-engineering v2 team
    leads. ``team_kind`` selects the target v2 team (``backend`` or
    ``frontend``), which controls the shared task type and canonical assignee
    label used during handoff. Coding-team tasks are expected to expose ``id``,
    ``title``, ``description``, ``dependencies``, ``acceptance_criteria``, and
    ``revision_feedback`` fields. When invoked by the coding-team orchestrator,
    the worker prepares or reuses a feature branch, calls the v2 workflow with
    ``merge_to_development=False``, and returns that branch to the Tech Lead for
    review instead of merging it directly.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        stack_spec: StackSpec,
        team_kind: str,
        team_lead: Any,
    ) -> None:
        """Store the coding-team worker identity and delegated v2 team lead."""
        if team_kind not in _ALLOWED_TEAM_KINDS:
            allowed = ", ".join(sorted(_ALLOWED_TEAM_KINDS))
            raise ValueError(f"team_kind must be one of: {allowed}")
        self.agent_id = agent_id
        self.stack_spec = stack_spec
        self.team_kind = team_kind
        self.team_lead = team_lead

    @property
    def _task_type(self) -> TaskType:
        """Return the software-engineering task type represented by this worker."""
        return TaskType.FRONTEND if self.team_kind == "frontend" else TaskType.BACKEND

    @property
    def _team_label(self) -> str:
        """Return the canonical coding-team target label for this v2 worker."""
        return "frontend_v2" if self.team_kind == "frontend" else "backend_v2"

    def _to_se_task(self, task: Any, feature_branch_name: str | None = None) -> SETask:
        """Convert a coding-team task into the shared v2 software-engineering task model.

        Args:
            task: Valid coding-team task-like object.
            feature_branch_name: Prepared branch to pass into the v2 workflow.

        Returns:
            A shared ``Task`` populated with v2 routing, branch, and metadata fields.
        """
        _validate_task_interface(task)
        description = _augment_description(task, self._team_label)
        requirements = _requirements_for_task(task)
        return SETask(
            id=task.id,
            type=self._task_type,
            title=task.title or task.id,
            description=description,
            assignee=self._team_label,
            requirements=requirements,
            dependencies=list(task.dependencies or []),
            acceptance_criteria=list(task.acceptance_criteria or []),
            status=SETaskStatus.PENDING,
            feature_branch_name=feature_branch_name or getattr(task, "feature_branch", None),
            metadata={
                "coding_team_task_id": task.id,
                "coding_team_agent_id": self.agent_id,
                "coding_team_target_team": getattr(task, "target_team", None) or self._team_label,
                "coding_team_revision_count": getattr(task, "revision_count", 0),
            },
        )

    def run_implement(
        self,
        task: Any,
        repo_path: str | Path,
        repo_context: str = "",
    ) -> Dict[str, Any]:
        """Execute the task via the v2 team and return a coding-team handoff result.

        Args:
            task: Coding-team task-like object to implement.
            repo_path: Repository root where the task should be implemented.
            repo_context: Legacy coding-team context string. V2 workflows inspect
                the repository directly, so this parameter is accepted for API
                compatibility and intentionally ignored.

        Returns:
            A dict containing status, feature_branch, changes_summary,
            files_to_create_or_edit, commands_run, open_questions, and error.
        """
        del repo_context  # v2 teams read repository context themselves.
        path = Path(repo_path).resolve()
        task_id = str(getattr(task, "id", "") or "unknown-task")
        try:
            _validate_task_interface(task)
        except ValueError as exc:
            logger.warning(
                "%s worker received malformed task %s: %s", self._team_label, task_id, exc
            )
            return {
                "status": "failed",
                "feature_branch": getattr(task, "feature_branch", None)
                or f"feature/{_task_feature_name(task)}",
                "changes_summary": "",
                "files_to_create_or_edit": [],
                "commands_run": [],
                "open_questions": [],
                "error": str(exc),
            }
        branch_ok, prepared_branch = _prepare_feature_branch(path, task)
        if not branch_ok:
            logger.warning(
                "%s worker could not prepare branch for task %s: %s",
                self._team_label,
                task_id,
                prepared_branch,
            )
            return {
                "status": "failed",
                "feature_branch": getattr(task, "feature_branch", None)
                or f"feature/{_task_feature_name(task)}",
                "changes_summary": "",
                "files_to_create_or_edit": [],
                "commands_run": [],
                "open_questions": [],
                "error": f"failed to prepare feature branch: {prepared_branch}",
            }
        se_task = self._to_se_task(task, feature_branch_name=prepared_branch)
        workflow_kwargs = {"repo_path": path, "task": se_task}
        if _accepts_keyword(self.team_lead.run_workflow, "merge_to_development"):
            workflow_kwargs["merge_to_development"] = False
        try:
            result = self.team_lead.run_workflow(**workflow_kwargs)
        except Exception as exc:  # noqa: BLE001 - worker failure is task-local
            logger.exception("%s worker failed for task %s", self._team_label, task_id)
            return {
                "status": "failed",
                "feature_branch": prepared_branch,
                "changes_summary": "",
                "files_to_create_or_edit": [],
                "commands_run": [],
                "open_questions": [],
                "error": str(exc),
            }

        deliver = getattr(result, "deliver_result", None)
        branch = (
            getattr(deliver, "branch_name", "")
            or getattr(se_task, "feature_branch_name", None)
            or getattr(task, "feature_branch", None)
            or f"feature/{_task_feature_name(task)}"
        )
        files_changed = _workflow_file_list(result, deliver)
        branch_ready = bool(getattr(deliver, "branch_ready", False))
        missing_success = object()
        result_success = getattr(result, "success", missing_success)
        success = branch_ready if result_success is missing_success else bool(result_success)
        if not success:
            reason = str(getattr(result, "failure_reason", "") or "v2 workflow did not complete")
            return {
                "status": "failed",
                "feature_branch": branch,
                "changes_summary": str(getattr(result, "summary", "") or ""),
                "files_to_create_or_edit": files_changed,
                "commands_run": list(getattr(deliver, "commit_messages", []) or []),
                "open_questions": [],
                "error": reason,
            }
        return {
            "status": "in_review",
            "feature_branch": branch,
            "changes_summary": _changes_summary(
                team_label=self._team_label,
                branch=branch,
                result_summary=str(getattr(result, "summary", "") or ""),
                feedback=list(task.revision_feedback or []),
            ),
            "files_to_create_or_edit": files_changed,
            "commands_run": list(getattr(deliver, "commit_messages", []) or []),
            "open_questions": [],
            "error": None,
        }
