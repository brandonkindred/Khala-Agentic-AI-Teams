"""
Tech Lead agent: plan → Task Graph + stacks; groom tasks; assignments; code review.
Orchestrator performs actual Task Graph updates and git merge.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List

from strands import Agent

from coding_team.hitl import normalize_open_questions as _normalize_open_questions
from coding_team.models import CodingTeamPlanInput
from coding_team.tech_lead_agent import prompts
from llm_service import call_llm_with_retries

logger = logging.getLogger(__name__)


def _review_retry_attempts() -> int:
    """Total review attempts (retries + 1) before giving up.

    A reviewer call can fail for transient reasons (rate limit, network timeout, provider
    outage) that say nothing about the implementation and are usually recoverable, so we retry
    with backoff rather than failing the task on the first error. Configurable via
    CODING_TEAM_REVIEW_RETRIES (default 2 retries → 3 attempts; garbage/negative → default;
    floored at 1 attempt).
    """
    raw = os.environ.get("CODING_TEAM_REVIEW_RETRIES")
    try:
        retries = int(raw) if raw is not None and raw.strip() != "" else 2
    except (TypeError, ValueError):
        retries = 2
    # A negative value is meaningless as a retry count; rather than silently collapsing it to a
    # single attempt (which would strip all transient-failure protection for a disabling-style
    # value like -1), treat it as garbage and restore the documented default.
    if retries < 0:
        retries = 2
    return retries + 1


def _render_resolved_questions(resolved: List[Dict[str, Any]]) -> str:
    """Render user-supplied decisions as 'question → answer' lines for the planning prompt.

    Preconditions:
        - ``resolved`` entries are dicts; an answer carries ``question_text`` and a human-readable
          ``answer`` (or ``selected_answer`` / ``selected_option_id`` / ``other_text``).
    Postconditions:
        - Returns a non-empty bullet string when any decision has content, else "".
    """
    lines: List[str] = []
    for entry in resolved or []:
        if not isinstance(entry, dict):
            continue
        q = entry.get("question_text") or entry.get("question") or entry.get("question_id") or ""
        a = (
            entry.get("answer")
            or entry.get("selected_answer")
            or entry.get("other_text")
            or entry.get("selected_option_id")
            or ""
        )
        if q or a:
            lines.append(f"- {q} → {a}")
    return "\n".join(lines)


def _plan_text(plan: CodingTeamPlanInput) -> str:
    """Build plan text for the LLM from plan input.

    Every field is passed through in full: the plan, spec, and architecture are the primary inputs
    the Tech Lead reasons over, and clipping them would hide requirements from the Task Graph. The
    user's resolved decisions and recorded assumptions are included too, so the Tech Lead breaks the
    plan down according to choices the user actually made, not ones an agent invented. Unanswered
    open questions are NOT rendered here: by the time this runs the orchestrator's decision gate has
    guaranteed there are none (the job pauses otherwise). Inputs are never truncated.

    Postconditions:
        - Each non-empty plan field (including resolved decisions and assumptions) appears verbatim
          (uncut) in the returned text.
    """
    parts = [
        f"Title: {plan.requirements_title}",
        f"Description: {plan.requirements_description}" if plan.requirements_description else "",
    ]
    if plan.project_overview:
        parts.append("Project overview: " + json.dumps(plan.project_overview, indent=2))
    if plan.final_spec_content:
        parts.append("Spec: " + plan.final_spec_content)
    if plan.architecture_overview:
        parts.append("Architecture: " + plan.architecture_overview)
    resolved_text = _render_resolved_questions(plan.resolved_questions or [])
    if resolved_text:
        parts.append(
            "User decisions (these were answered by the user — implement them exactly, "
            "do not revisit):\n" + resolved_text
        )
    if plan.assumptions:
        parts.append("Assumptions on record:\n" + "\n".join(f"- {a}" for a in plan.assumptions))
    return "\n\n".join(p for p in parts if p)


def _agent_call_json(agent: Agent, prompt: str) -> Dict[str, Any]:
    """Call a Strands Agent and parse the result as JSON."""
    result = agent(prompt)
    raw = str(result).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


class TechLeadAgent:
    """Tech Lead: given plan, produce tasks + stacks; groom tasks; suggest assignments; code review."""

    def __init__(self, model: Any) -> None:
        self._model = model
        self._plan_agent = Agent(model=model, system_prompt=prompts.PLAN_TO_TASK_GRAPH_SYSTEM)
        self._groom_agent = Agent(model=model, system_prompt=prompts.GROOM_TASK_SYSTEM)
        self._assignment_agent = Agent(model=model, system_prompt=prompts.ASSIGNMENT_SYSTEM)
        self._review_agent = Agent(model=model, system_prompt=prompts.CODE_REVIEW_SYSTEM)

    def run_plan_to_task_graph(self, plan: CodingTeamPlanInput) -> Dict[str, Any]:
        """
        Given plan from Planning team, return { "tasks": [...], "stacks": [...], "open_questions": [...] }.
        Orchestrator will add tasks to Task Graph and create Senior SWEs from stacks. A non-empty
        ``open_questions`` means the Tech Lead needs a product/design decision it must not make
        itself; the orchestrator pauses the job for the user rather than building tasks.

        Postconditions:
            - Returns a dict with "tasks" (possibly empty), a non-empty "stacks" (defaulted), and
              "open_questions" (possibly empty). Never decides an open question on the caller's
              behalf.
        """
        plan_text = _plan_text(plan)
        user = prompts.PLAN_TO_TASK_GRAPH_USER.format(plan_text=plan_text)
        user += "\n\nRespond with valid JSON only, no markdown fences."
        try:
            data = _agent_call_json(self._plan_agent, user)
        except Exception as e:
            logger.warning("Tech Lead plan_to_task_graph LLM failed: %s", e)
            return {
                "tasks": [],
                "stacks": [{"name": "default", "tools_services": []}],
                "open_questions": [],
            }
        tasks_raw = data.get("tasks") or []
        stacks_raw = data.get("stacks") or []
        tasks = []
        for t in tasks_raw:
            if isinstance(t, dict) and t.get("id"):
                tasks.append(
                    {
                        "id": str(t["id"]),
                        "title": t.get("title", t["id"]),
                        "description": t.get("description", ""),
                        "dependencies": list(t.get("dependencies") or []),
                    }
                )
        stacks = []
        for s in stacks_raw:
            if isinstance(s, dict):
                name = s.get("name") or "stack"
                tools = s.get("tools_services")
                if not isinstance(tools, list):
                    tools = []
                stacks.append({"name": name, "tools_services": [str(x) for x in tools]})
        if not stacks:
            stacks = [{"name": "default", "tools_services": []}]
        return {
            "tasks": tasks,
            "stacks": stacks,
            "open_questions": _normalize_open_questions(data.get("open_questions")),
        }

    def run_groom_task(
        self,
        task_id: str,
        task_title: str,
        task_description: str,
        task_dependencies: List[str],
        plan_context: str,
    ) -> Dict[str, Any]:
        """Groom one task: acceptance criteria, out of scope, enriched description, priority, subtasks."""
        user = prompts.GROOM_TASK_USER.format(
            task_id=task_id,
            task_title=task_title,
            task_description=task_description,
            task_dependencies=json.dumps(task_dependencies),
            plan_context=plan_context,
        )
        user += "\n\nRespond with valid JSON only, no markdown fences."
        try:
            data = _agent_call_json(self._groom_agent, user)
        except Exception as e:
            logger.warning("Tech Lead groom_task LLM failed: %s", e)
            return {
                "acceptance_criteria": [],
                "out_of_scope": "",
                "description_enriched": task_description,
                "priority": "medium",
                "subtasks": [],
                "task_dependencies": task_dependencies,
            }
        return {
            "acceptance_criteria": list(data.get("acceptance_criteria") or []),
            "out_of_scope": str(data.get("out_of_scope") or ""),
            "description_enriched": str(data.get("description_enriched") or task_description),
            "priority": str(data.get("priority") or "medium"),
            "subtasks": list(data.get("subtasks") or []),
            "task_dependencies": list(data.get("task_dependencies") or task_dependencies),
        }

    def run_assignments(
        self,
        agent_ids: List[str],
        ready_tasks: List[Dict[str, Any]],
        free_agents: List[str],
    ) -> Dict[str, Any]:
        """Suggest assignments: list of { agent_id, task_id }. Orchestrator calls Task Graph assign."""
        user = prompts.ASSIGNMENT_USER.format(
            agent_ids=json.dumps(agent_ids),
            ready_tasks=json.dumps(ready_tasks),
            free_agents=json.dumps(free_agents),
        )
        user += "\n\nRespond with valid JSON only, no markdown fences."
        try:
            data = _agent_call_json(self._assignment_agent, user)
        except Exception as e:
            logger.warning("Tech Lead assignments LLM failed: %s", e)
            return {"assignments": []}
        assignments = data.get("assignments") or []
        return {
            "assignments": [
                a
                for a in assignments
                if isinstance(a, dict) and a.get("agent_id") and a.get("task_id")
            ]
        }

    def run_code_review(
        self,
        task_title: str,
        task_description: str,
        acceptance_criteria: List[str],
        changes_summary: str,
    ) -> Dict[str, Any]:
        """Review feature branch: approved (bool), reason (str), requested_changes (list)."""
        user = prompts.CODE_REVIEW_USER.format(
            task_title=task_title,
            task_description=task_description,
            acceptance_criteria=json.dumps(acceptance_criteria),
            changes_summary=changes_summary,
        )
        user += "\n\nRespond with valid JSON only, no markdown fences."
        attempts = _review_retry_attempts()

        def _attempt_review() -> Dict[str, Any]:
            data = _agent_call_json(self._review_agent, user)
            # A response that parses as JSON but carries no verdict (missing/null "approved") is not
            # a substantive rejection — it's an unusable review. Raise so the shared retry envelope
            # retries it and it ultimately surfaces error=True (fail once), rather than silently
            # becoming approved=False and burning the revision loop.
            if data.get("approved") is None:
                raise ValueError(f"review response missing 'approved' verdict: {data!r}")
            return data

        # Reuse the platform's jittered-exponential-backoff retry envelope rather than a bespoke
        # loop, so review retry/backoff tuning stays consistent with every other LLM caller.
        try:
            data = call_llm_with_retries(_attempt_review, max_attempts=attempts)
        except Exception as e:  # noqa: BLE001 — a failed review must never abort the swarm
            # Flag an infrastructure failure (error=True) distinctly from a substantive rejection so
            # the orchestrator fails the task once with a clear diagnostic rather than re-sending the
            # same failing prompt through the revision loop every round.
            logger.warning("Tech Lead code_review failed after %d attempts: %s", attempts, e)
            return {
                "approved": False,
                "error": True,
                "reason": f"Review could not be completed after {attempts} attempts: {e}",
                "requested_changes": [],
            }
        return {
            "approved": bool(data.get("approved")),
            "error": False,
            "reason": str(data.get("reason") or ""),
            "requested_changes": list(data.get("requested_changes") or []),
        }
