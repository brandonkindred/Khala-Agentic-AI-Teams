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
        logger.info("Tech Lead: planning tasks for %s", input_data.requirements.title)
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

        if input_data.repo_path:
            context_parts.extend(["", f"**Repo path:** {input_data.repo_path}"])
        if input_data.spec_content:
            context_parts.extend([
                "",
                "**Full initial_spec.md (use this to generate the complete build plan):**",
                "---",
                input_data.spec_content[:6000] + ("..." if len(input_data.spec_content or "") > 6000 else ""),
                "---",
            ])

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
            if isinstance(t, dict) and t.get("id"):
                assignee = t.get("assignee") or "devops"
                try:
                    task_type = TaskType(t.get("type", "backend"))
                except ValueError:
                    task_type = TaskType.BACKEND
                acc = t.get("acceptance_criteria") or []
                if not isinstance(acc, list):
                    acc = [str(acc)] if acc else []
                tasks.append(
                    Task(
                        id=t["id"],
                        type=task_type,
                        description=t.get("description", ""),
                        assignee=assignee,
                        requirements=t.get("requirements", ""),
                        dependencies=t.get("dependencies", []),
                        label=t.get("label"),
                        acceptance_criteria=acc,
                        status=TaskStatus.PENDING,
                    )
                )

        execution_order = data.get("execution_order") or [t.id for t in tasks]
        assignment = TaskAssignment(
            tasks=tasks,
            execution_order=execution_order,
            rationale=data.get("rationale", ""),
        )

        logger.info("Tech Lead: assigned %s tasks in order %s", len(tasks), assignment.execution_order)
        return TechLeadOutput(
            assignment=assignment,
            summary=data.get("summary", ""),
        )
