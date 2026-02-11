"""Tech Lead agent: orchestrates tasks from product requirements and architecture."""

from __future__ import annotations

import logging
from typing import Any, Dict

from shared.llm import LLMClient
from shared.models import Task, TaskAssignment, TaskStatus, TaskType

from .models import TechLeadInput, TechLeadOutput
from .prompts import TECH_LEAD_PROMPT

logger = logging.getLogger(__name__)


class TechLeadAgent:
    """
    Staff-level Tech Lead that bridges product management and engineering.
    Uses product requirements and system architecture to plan and distribute
    tasks amongst DevOps, Security, Backend, Frontend, and QA agents.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: TechLeadInput) -> TechLeadOutput:
        """Plan and assign tasks to the team."""
        reqs = input_data.requirements
        arch = input_data.architecture

        context_parts = [
            f"**Product Title:** {reqs.title}",
            f"**Description:** {reqs.description}",
            "**Acceptance Criteria:**",
            *[f"- {c}" for c in reqs.acceptance_criteria],
            "**Constraints:**",
            *[f"- {c}" for c in reqs.constraints],
            f"**Priority:** {reqs.priority}",
        ]

        if arch:
            context_parts.extend([
                "",
                "**System Architecture:**",
                arch.overview,
                "",
                "**Components:**",
                *[f"- {c.name} ({c.type}): {c.description}" for c in arch.components],
                "",
                "**Architecture Document (excerpt):**",
                (arch.architecture_document or "")[:2000] + ("..." if len(arch.architecture_document or "") > 2000 else ""),
            ])

        prompt = TECH_LEAD_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)

        data = self.llm.complete_json(prompt, temperature=0.2)

        tasks = []
        for t in data.get("tasks") or []:
            if isinstance(t, dict) and t.get("id") and t.get("assignee"):
                try:
                    task_type = TaskType(t.get("type", "backend"))
                except ValueError:
                    task_type = TaskType.BACKEND
                tasks.append(
                    Task(
                        id=t["id"],
                        type=task_type,
                        description=t.get("description", ""),
                        assignee=t["assignee"],
                        requirements=t.get("requirements", ""),
                        dependencies=t.get("dependencies", []),
                        status=TaskStatus.PENDING,
                    )
                )

        execution_order = data.get("execution_order") or [t.id for t in tasks]
        assignment = TaskAssignment(
            tasks=tasks,
            execution_order=execution_order,
            rationale=data.get("rationale", ""),
        )

        return TechLeadOutput(
            assignment=assignment,
            summary=data.get("summary", ""),
        )
