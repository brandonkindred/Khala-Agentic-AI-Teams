"""Adapters that let software-engineering v2 teams act as coding-team workers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from coding_team.models import StackSpec
from software_engineering_team.shared.models import Task as SETask
from software_engineering_team.shared.models import TaskStatus as SETaskStatus
from software_engineering_team.shared.models import TaskType

logger = logging.getLogger(__name__)


def _feedback_lines(feedback: List[Dict[str, Any]]) -> List[str]:
    """Render prior Tech Lead/team feedback as concise actionable lines."""
    lines: List[str] = []
    for entry in feedback or []:
        if not isinstance(entry, dict):
            lines.append(str(entry))
            continue
        source = entry.get("source") or entry.get("type") or "review"
        reason = entry.get("reason") or entry.get("error") or entry.get("message") or ""
        if reason:
            lines.append(f"[{source}] {reason}")
        for change in entry.get("requested_changes") or []:
            lines.append(f"[{source}] {change}")
    return [ln for ln in lines if ln.strip()]


def _augment_description(task: Any, team_label: str) -> str:
    """Add coding-team revision feedback to the v2 team's task input."""
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
    """Build the review handoff summary expected by the coding-team Tech Lead."""
    parts = [
        f"{team_label} completed the assigned coding-team task.",
        f"Review branch: {branch}",
    ]
    if result_summary:
        parts.append(f"Implementation summary:\n{result_summary}")
    feedback_lines = _feedback_lines(feedback)
    if feedback_lines:
        parts.append(
            "Feedback addressed:\n" + "\n".join(f"- {line}" for line in feedback_lines)
        )
    return "\n\n".join(parts)


class V2TeamWorker:
    """Coding-team worker facade for backend_code_v2_team/frontend_code_v2_team."""

    def __init__(
        self,
        *,
        agent_id: str,
        stack_spec: StackSpec,
        team_kind: str,
        team_lead: Any,
    ) -> None:
        self.agent_id = agent_id
        self.stack_spec = stack_spec
        self.team_kind = team_kind
        self.team_lead = team_lead

    @property
    def _task_type(self) -> TaskType:
        return TaskType.FRONTEND if self.team_kind == "frontend" else TaskType.BACKEND

    @property
    def _team_label(self) -> str:
        return "frontend_v2" if self.team_kind == "frontend" else "backend_v2"

    def _to_se_task(self, task: Any) -> SETask:
        description = _augment_description(task, self._team_label)
        requirements = description
        if task.acceptance_criteria:
            requirements += "\n\nAcceptance criteria:\n" + "\n".join(
                f"- {item}" for item in task.acceptance_criteria
            )
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
            feature_branch_name=getattr(task, "feature_branch", None),
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
        """Execute the task via the v2 team and return a coding-team handoff result."""
        del repo_context  # v2 teams read repository context themselves.
        path = Path(repo_path).resolve()
        se_task = self._to_se_task(task)
        try:
            result = self.team_lead.run_workflow(
                repo_path=path,
                task=se_task,
                merge_to_development=False,
            )
        except TypeError:
            # Defensive compatibility for any injected fake that has not adopted the
            # merge_to_development flag yet. Real in-repo v2 teams support it.
            result = self.team_lead.run_workflow(repo_path=path, task=se_task)
        except Exception as exc:  # noqa: BLE001 - worker failure is task-local
            logger.warning("%s worker failed for task %s: %s", self._team_label, task.id, exc)
            return {
                "status": "failed",
                "feature_branch": getattr(task, "feature_branch", None) or f"feature/{task.id}",
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
            or f"feature/{task.id}"
        )
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
                "files_to_create_or_edit": [],
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
            "files_to_create_or_edit": [],
            "commands_run": list(getattr(deliver, "commit_messages", []) or []),
            "open_questions": [],
            "error": None,
        }
