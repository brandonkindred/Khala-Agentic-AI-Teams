"""
Senior Software Engineer agent: parameterized by StackSpec; implements one task at a time.
Requests task from Task Graph (via orchestrator); implements (code + tests); reports done / in_review.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict

from strands import Agent

from agent_git_tools import GIT_TOOL_DEFINITIONS, GitToolContext, build_git_tool_handlers
from coding_team.models import StackSpec, Task
from coding_team.senior_software_engineer_agent import prompts

logger = logging.getLogger(__name__)


def _parse_json_response(raw: str) -> Dict[str, Any]:
    """Parse a JSON response from an agent, stripping markdown fences if present."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def _build_strands_tools(handlers: Dict[str, Any], tool_definitions: list) -> list:
    """Convert git tool definitions + handlers into Strands-compatible tool callables."""
    tools = []
    for tool_def in tool_definitions:
        func_info = tool_def.get("function", {})
        name = func_info.get("name")
        if name and name in handlers:
            handler = handlers[name]

            def make_tool(n, h, desc, params):
                def tool_fn(**kwargs):
                    return h(kwargs)
                tool_fn.__name__ = n
                tool_fn.__doc__ = desc
                return tool_fn

            tools.append(make_tool(name, handler, func_info.get("description", ""), func_info.get("parameters", {})))
    return tools


class SeniorSWEAgent:
    """
    Senior SWE: one per stack. Given a task from the Task Graph, produces implementation
    (summary + optional file edits). Orchestrator is responsible for: feature branch,
    applying edits, running tests/linter, commit, marking task In Review.
    """

    def __init__(self, agent_id: str, stack_spec: StackSpec, llm: Any) -> None:
        self.agent_id = agent_id
        self.stack_spec = stack_spec
        self._model = llm

    def run_implement(
        self,
        task: Task,
        repo_path: str | Path,
        repo_context: str = "",
        *,
        use_git_tools: bool = True,
    ) -> Dict[str, Any]:
        """
        Implement the task. Returns dict with:
        - status: "in_review" | "failed"
        - feature_branch: suggested branch name (orchestrator may override)
        - changes_summary: for Tech Lead review
        - files_to_create_or_edit: optional list of {path, content} for orchestrator to apply
        - error: optional error message if failed
        """
        path = Path(repo_path).resolve()
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
        system = prompts.IMPLEMENT_TASK_SYSTEM
        if use_git_tools:
            system += (
                "\n\nYou may call the provided Git tools to inspect the repository, create a feature branch, "
                "write files, and commit. The repository path is fixed by the runtime; do not pass repo_path. "
                "When finished, respond with a single JSON object matching the schema above (summary, "
                "files_to_create_or_edit, commands_run, ready_for_review) and do not call tools in that message."
            )
        try:
            if use_git_tools:
                ctx = GitToolContext(
                    path,
                    allow_merge_to_default_branch=False,
                )
                handlers = build_git_tool_handlers(ctx)
                strands_tools = _build_strands_tools(handlers, GIT_TOOL_DEFINITIONS)
                agent = Agent(
                    model=self._model,
                    system_prompt=system,
                    tools=strands_tools,
                )
                result = agent(user + "\n\nWhen done, respond with valid JSON only, no markdown fences.")
                raw = str(result).strip()
                data = _parse_json_response(raw)
            else:
                agent = Agent(
                    model=self._model,
                    system_prompt=prompts.IMPLEMENT_TASK_SYSTEM,
                )
                result = agent(user + "\n\nRespond with valid JSON only, no markdown fences.")
                raw = str(result).strip()
                data = _parse_json_response(raw)
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
        branch = data.get("feature_branch")
        if not isinstance(branch, str) or not branch.strip():
            branch = f"feature/{task.id}"
        return {
            "status": "in_review" if ready else "in_progress",
            "feature_branch": branch.strip(),
            "changes_summary": summary,
            "files_to_create_or_edit": [f for f in files if isinstance(f, dict) and f.get("path")],
            "commands_run": [str(c) for c in commands],
            "error": None,
        }
