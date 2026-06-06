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
from strands.tools.tools import PythonAgentTool
from strands.types.tools import ToolResult, ToolSpec, ToolUse

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


def _render_revision_feedback(feedback: list) -> str:
    """Render prior-round revision feedback into a human-readable bullet list for the prompt.

    Preconditions:
        - feedback is the task's revision_feedback list (entries are dicts; Tech Lead entries
          carry "reason"/"requested_changes", quality-gate entries carry "type"/"error"/etc.).
    Postconditions:
        - Returns a non-empty bullet string when feedback has content, else "".
    """
    lines: list[str] = []
    for entry in feedback or []:
        if not isinstance(entry, dict):
            lines.append(f"- {entry}")
            continue
        source = entry.get("source") or entry.get("type") or "review"
        reason = entry.get("reason") or entry.get("error") or entry.get("message") or ""
        if reason:
            lines.append(f"- [{source}] {reason}")
        for change in entry.get("requested_changes") or []:
            lines.append(f"  - {change}")
    return "\n".join(lines)


def _make_python_agent_tool(
    name: str, handler: Any, description: str, parameters: dict
) -> PythonAgentTool:
    """Wrap one git tool handler in a Strands ``PythonAgentTool``.

    Preconditions:
        - ``name`` is non-empty; ``parameters`` is the definition's JSON schema.
        - ``handler`` accepts the parsed-arguments dict and returns a
          JSON-serializable value (or a plain string).
    Postconditions:
        - The returned tool's spec mirrors the definition verbatim
          (``parameters`` under ``inputSchema.json``), so the model sees the
          schema the definitions declare — not one derived from a signature.
        - Invoking the tool dispatches to ``handler``; a handler exception
          becomes a ``status="error"`` ToolResult instead of aborting the
          whole agent invocation.
    """
    spec: ToolSpec = {
        "name": name,
        "description": description or name,
        "inputSchema": {"json": parameters},
    }

    def tool_func(tool_use: ToolUse, **_invocation_state: Any) -> ToolResult:
        tool_use_id = tool_use.get("toolUseId", "")
        try:
            out = handler(tool_use.get("input") or {})
        except Exception as e:
            return {
                "toolUseId": tool_use_id,
                "status": "error",
                "content": [{"text": f"{type(e).__name__}: {e}"}],
            }
        text = out if isinstance(out, str) else json.dumps(out)
        return {
            "toolUseId": tool_use_id,
            "status": "success",
            "content": [{"text": text}],
        }

    return PythonAgentTool(name, spec, tool_func)


def _build_strands_tools(handlers: Dict[str, Any], tool_definitions: list) -> list:
    """Convert OpenAI-style git tool definitions + handlers into Strands tools.

    The Strands registry only registers recognized tool types (``AgentTool``
    instances, ``@tool``-decorated functions, modules, ...); a plain closure
    is dropped with an "unrecognized tool specification" warning and the
    agent silently runs without git tools. Each definition is therefore
    wrapped in a ``PythonAgentTool`` carrying the definition's exact schema.

    Preconditions:
        - ``tool_definitions`` entries are OpenAI-style function definitions
          (``{"function": {"name", "description", "parameters"}}``).
        - ``handlers`` maps tool names to callables accepting the parsed
          arguments dict.
    Postconditions:
        - Returns one registrable tool per definition whose name has a
          handler; definitions without handlers are skipped.
    """
    tools = []
    for tool_def in tool_definitions:
        func_info = tool_def.get("function", {})
        name = func_info.get("name")
        if name and name in handlers:
            tools.append(
                _make_python_agent_tool(
                    name,
                    handlers[name],
                    func_info.get("description", ""),
                    func_info.get("parameters", {}),
                )
            )
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
            task_description=task.description,
            acceptance_criteria=json.dumps(task.acceptance_criteria),
            repo_context=repo_context[:4000] or "No existing code context provided.",
        )
        feedback_text = _render_revision_feedback(task.revision_feedback)
        if feedback_text:
            user = prompts.REVISION_FEEDBACK_BLOCK.format(feedback=feedback_text) + "\n" + user
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
                result = agent(
                    user + "\n\nWhen done, respond with valid JSON only, no markdown fences."
                )
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
