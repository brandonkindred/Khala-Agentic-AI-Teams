"""
Tech Lead agent: plan → Task Graph + stacks; groom tasks; assignments; code review.
Orchestrator performs actual Task Graph updates and git merge.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from coding_team.models import CodingTeamPlanInput
from coding_team.tech_lead_agent import prompts

logger = logging.getLogger(__name__)


def _plan_text(plan: CodingTeamPlanInput) -> str:
    """Build plan text for the LLM from plan input."""
    parts = [
        f"Title: {plan.requirements_title}",
        f"Description: {plan.requirements_description[:8000]}"
        if plan.requirements_description
        else "",
    ]
    if plan.project_overview:
        parts.append("Project overview: " + json.dumps(plan.project_overview, indent=2)[:4000])
    if plan.final_spec_content:
        parts.append("Spec: " + (plan.final_spec_content[:6000] or ""))
    if plan.architecture_overview:
        parts.append("Architecture: " + plan.architecture_overview[:3000])
    return "\n\n".join(p for p in parts if p)


class TechLeadAgent:
    """Tech Lead: given plan, produce tasks + stacks; groom tasks; suggest assignments; code review."""

    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def run_plan_to_task_graph(self, plan: CodingTeamPlanInput) -> Dict[str, Any]:
        """
        Given plan from Planning team, return { "tasks": [...], "stacks": [...] }.
        Orchestrator will add tasks to Task Graph and create Senior SWEs from stacks.
        """
        plan_text = _plan_text(plan)
        user = prompts.PLAN_TO_TASK_GRAPH_USER.format(plan_text=plan_text)
        try:
            data = self.llm.complete_json(
                user,
                temperature=0.2,
                system_prompt=prompts.PLAN_TO_TASK_GRAPH_SYSTEM,
                think=True,
            )
        except Exception as e:
            logger.warning("Tech Lead plan_to_task_graph LLM failed: %s", e)
            return {"tasks": [], "stacks": [{"name": "default", "tools_services": []}]}
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
        return {"tasks": tasks, "stacks": stacks}

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
            task_description=task_description[:3000],
            task_dependencies=json.dumps(task_dependencies),
            plan_context=plan_context[:6000],
        )
        try:
            data = self.llm.complete_json(
                user,
                temperature=0.2,
                system_prompt=prompts.GROOM_TASK_SYSTEM,
                think=True,
            )
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
        try:
            data = self.llm.complete_json(
                user,
                temperature=0.1,
                system_prompt=prompts.ASSIGNMENT_SYSTEM,
                think=True,
            )
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
            task_description=task_description[:2000],
            acceptance_criteria=json.dumps(acceptance_criteria),
            changes_summary=changes_summary[:8000],
        )
        try:
            data = self.llm.complete_json(
                user,
                temperature=0.1,
                system_prompt=prompts.CODE_REVIEW_SYSTEM,
                think=True,
            )
        except Exception as e:
            logger.warning("Tech Lead code_review LLM failed: %s", e)
            return {"approved": False, "reason": "Review failed", "requested_changes": []}
        return {
            "approved": bool(data.get("approved")),
            "reason": str(data.get("reason") or ""),
            "requested_changes": list(data.get("requested_changes") or []),
        }
