"""
Senior Software Engineer agent: parameterized by StackSpec; implements one task at a time.
Requests task from Task Graph (via orchestrator); implements (code + tests); reports done / in_review.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from coding_team.models import StackSpec, Task
from coding_team.senior_software_engineer_agent import prompts

logger = logging.getLogger(__name__)


class SeniorSWEAgent:
    """
    Senior SWE: one per stack. Given a task from the Task Graph, produces implementation
    (summary + optional file edits). Orchestrator is responsible for: feature branch,
    applying edits, running tests/linter, commit, marking task In Review.
    """

    def __init__(self, agent_id: str, stack_spec: StackSpec, llm: Any) -> None:
        self.agent_id = agent_id
        self.stack_spec = stack_spec
        self.llm = llm

    def run_implement(
        self,
        task: Task,
        repo_path: str | Path,
        repo_context: str = "",
    ) -> Dict[str, Any]:
        """
        Implement the task. Returns dict with:
        - status: "in_review" | "failed"
        - feature_branch: suggested branch name (orchestrator may override)
        - changes_summary: for Tech Lead review
        - files_to_create_or_edit: optional list of {path, content} for orchestrator to apply
        - error: optional error message if failed
        """
        Path(repo_path)
        stack_name = self.stack_spec.name or self.agent_id
        tools_services = ", ".join(self.stack_spec.tools_services or [])
        user = prompts.IMPLEMENT_TASK_USER.format(
            stack_name=stack_name,
            tools_services=tools_services,
            task_title=task.title,
            task_description=task.description[:6000],
            acceptance_criteria=json.dumps(task.acceptance_criteria),
            repo_context=repo_context[:4000] or "No existing code context provided.",
        )
        try:
            data = self.llm.complete_json(
                user,
                temperature=0.2,
                system_prompt=prompts.IMPLEMENT_TASK_SYSTEM,
            )
        except Exception as e:
            logger.warning("Senior SWE implement LLM failed: %s", e)
            return {
                "status": "failed",
                "feature_branch": f"feature/{task.id}",
                "changes_summary": "",
                "error": str(e),
            }
        summary = str(data.get("summary") or "Implementation completed.")
        files = data.get("files_to_create_or_edit")
        if not isinstance(files, list):
            files = []
        commands = data.get("commands_run") or []
        ready = bool(data.get("ready_for_review", True))
        return {
            "status": "in_review" if ready else "in_progress",
            "feature_branch": f"feature/{task.id}",
            "changes_summary": summary,
            "files_to_create_or_edit": [f for f in files if isinstance(f, dict) and f.get("path")],
            "commands_run": [str(c) for c in commands],
            "error": None,
        }
