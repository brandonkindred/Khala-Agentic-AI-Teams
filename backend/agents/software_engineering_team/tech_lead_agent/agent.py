"""Tech Lead agent: produces Initiative/Epic/Story hierarchy from product requirements."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List

from llm_service import LLMClient, compact_text
from software_engineering_team.shared.models import (
    Task,
    TaskStatus,
    TaskType,
    TaskUpdate,
)

if TYPE_CHECKING:
    from software_engineering_team.shared.models import PlanningHierarchy, ProductRequirements
from software_engineering_team.shared.task_parsing import (
    flatten_hierarchy_to_assignment,
    parse_assignment_from_data,
    parse_hierarchy_from_data,
)

from .models import TechLeadInput, TechLeadOutput
from .prompts import (
    TECH_LEAD_ANALYZE_CODEBASE_PROMPT,
    TECH_LEAD_EVALUATE_QA_PROMPT,
    TECH_LEAD_PROMPT,
    TECH_LEAD_REFINE_TASK_PROMPT,
    TECH_LEAD_REVIEW_PROGRESS_PROMPT,
    TECH_LEAD_SHOULD_RUN_SECURITY_PROMPT,
    TECH_LEAD_TRIGGER_DOCS_PROMPT,
)

logger = logging.getLogger(__name__)


class TechLeadAgent:
    """
    Staff-level Tech Lead that bridges product management and engineering.
    Produces an Initiative -> Epic -> Story hierarchy from product requirements
    and system architecture. Stories are distributed to backend, frontend,
    and devops engineers.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def _analyze_codebase(self, existing_codebase: str) -> str:
        """Analyze the existing codebase to understand what already exists."""
        logger.info("Tech Lead: Analyzing existing codebase (%s chars)", len(existing_codebase))
        prompt = (
            TECH_LEAD_ANALYZE_CODEBASE_PROMPT
            + "\n\n---\n\n**EXISTING CODEBASE:**\n"
            + existing_codebase
        )
        data = self.llm.complete_json(prompt, temperature=0.1, think=True)
        return json.dumps(data, indent=2)

    def _read_plan_artifacts(self, repo_path: str) -> str:
        """
        Read all markdown planning artifacts from /plan folder.

        Returns concatenated content with file headers for context.
        """
        plan_dir = Path(repo_path) / "plan"
        if not plan_dir.exists():
            return ""

        artifacts: List[str] = []
        for md_file in sorted(plan_dir.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8")
                if content.strip():
                    artifacts.append(f"--- {md_file.name} ---\n{content}")
            except (IOError, OSError, UnicodeDecodeError) as e:
                logger.warning(
                    "Tech Lead: failed to read %s: %s. Next step -> Continuing with remaining artifacts",
                    md_file,
                    e,
                )

        # Also read the shared planning document from plan/planning_team/
        shared_doc = plan_dir / "planning_team" / "planning_document.md"
        if shared_doc.exists():
            try:
                content = shared_doc.read_text(encoding="utf-8")
                if content.strip():
                    artifacts.insert(0, f"--- planning_team/planning_document.md ---\n{content}")
            except (IOError, OSError, UnicodeDecodeError) as e:
                logger.warning(
                    "Tech Lead: failed to read shared planning doc: %s",
                    e,
                )

        if artifacts:
            logger.info("Tech Lead: read %d plan artifacts from %s", len(artifacts), plan_dir)

        return "\n\n".join(artifacts)

    def _generate_detailed_summary(
        self,
        hierarchy: "PlanningHierarchy",
        init_count: int,
        epic_count: int,
        story_count: int,
        task_count: int,
        team_counts: Dict[str, int],
        plan_artifacts: str,
        requirements: "ProductRequirements",
    ) -> str:
        """
        Generate a detailed development plan summary from the Planning V2 hierarchy
        and plan artifacts.
        """

        parts: List[str] = []

        # Header with project info
        parts.append(f"# Development Plan: {requirements.title}\n")
        parts.append("\n## Overview\n")
        parts.append(
            f"This development plan covers {requirements.description[:200]}{'...' if len(requirements.description) > 200 else ''}\n"
        )

        # Hierarchy summary
        parts.append("\n## Planning Hierarchy Summary\n")
        parts.append(f"- **Initiatives:** {init_count}\n")
        parts.append(f"- **Epics:** {epic_count}\n")
        parts.append(f"- **Stories:** {story_count}\n")
        parts.append(f"- **Tasks:** {task_count}\n")

        # Team breakdown
        parts.append("\n## Task Distribution by Team\n")
        for team, count in sorted(team_counts.items()):
            parts.append(f"- **{team.title()}:** {count} tasks\n")

        # Initiative details
        parts.append("\n## Initiatives\n")
        for init in hierarchy.initiatives:
            parts.append(f"\n### {init.title}\n")
            parts.append(f"{init.description}\n")
            parts.append(f"\n**Epics ({len(init.epics)}):**\n")
            for epic in init.epics:
                story_tasks = sum(len(s.tasks) for s in epic.stories)
                parts.append(
                    f"- **{epic.title}**: {len(epic.stories)} stories, {story_tasks} tasks\n"
                )
                if epic.description:
                    parts.append(
                        f"  {epic.description[:150]}{'...' if len(epic.description) > 150 else ''}\n"
                    )

        # Key acceptance criteria from requirements
        if requirements.acceptance_criteria:
            parts.append("\n## Key Acceptance Criteria\n")
            for i, criterion in enumerate(requirements.acceptance_criteria[:10], 1):
                parts.append(f"{i}. {criterion}\n")

        # Architecture and design context from artifacts
        if plan_artifacts:
            # Extract key sections from artifacts
            parts.append("\n## Planning Context\n")
            parts.append("Planning artifacts include: ")

            artifact_names = []
            for line in plan_artifacts.split("\n"):
                if line.startswith("--- ") and line.endswith(" ---"):
                    artifact_names.append(line.strip("- ").strip())

            if artifact_names:
                parts.append(", ".join(artifact_names))
            parts.append("\n")

        # Execution order hint
        if hierarchy.execution_order:
            parts.append("\n## Execution Order\n")
            parts.append("Tasks will be executed in dependency order. First tasks: ")
            parts.append(", ".join(hierarchy.execution_order[:5]))
            if len(hierarchy.execution_order) > 5:
                parts.append(f" (and {len(hierarchy.execution_order) - 5} more)")
            parts.append("\n")

        summary = "".join(parts)
        logger.info("Tech Lead: generated detailed summary (%d chars)", len(summary))
        return summary

    def run(self, input_data: TechLeadInput) -> TechLeadOutput:
        """
        Produce an Initiative -> Epic -> Story hierarchy based on the requirements, existing codebase, and system architecture, existing tasks, and the spec.

        If a planning_hierarchy is provided (from Planning V2), use it directly
        instead of generating new tasks. The Tech Lead still produces a development
        plan summary but does not re-create the task breakdown.

        Single-path LLM call: build context, call LLM, parse hierarchy, flatten
        to TaskAssignment for execution.
        """
        logger.info("Tech Lead: planning for %s", input_data.requirements.title)

        # If Planning V2 hierarchy is provided, use it directly
        if input_data.planning_hierarchy:
            hierarchy = input_data.planning_hierarchy
            assignment = flatten_hierarchy_to_assignment(hierarchy)
            task_count = len(assignment.tasks)
            epic_count = sum(len(i.epics) for i in hierarchy.initiatives)
            story_count = sum(len(e.stories) for i in hierarchy.initiatives for e in i.epics)
            init_count = len(hierarchy.initiatives)

            # Count tasks by team
            team_counts: Dict[str, int] = {}
            for task in assignment.tasks:
                team = task.assignee or "unassigned"
                team_counts[team] = team_counts.get(team, 0) + 1

            logger.info(
                "Tech Lead: using Planning V2 hierarchy (%s tasks across %s stories)",
                task_count,
                story_count,
            )

            # Read plan artifacts for additional context
            plan_artifacts = ""
            if input_data.repo_path:
                plan_artifacts = input_data.plan_artifacts_content or self._read_plan_artifacts(
                    input_data.repo_path
                )

            # Generate a detailed development plan summary
            summary = self._generate_detailed_summary(
                hierarchy=hierarchy,
                init_count=init_count,
                epic_count=epic_count,
                story_count=story_count,
                task_count=task_count,
                team_counts=team_counts,
                plan_artifacts=plan_artifacts,
                requirements=input_data.requirements,
            )

            return TechLeadOutput(
                assignment=assignment,
                planning_hierarchy=hierarchy,
                summary=summary,
                requirement_task_mapping=[],
                spec_clarification_needed=False,
                clarification_questions=[],
            )

        existing_codebase = input_data.existing_codebase or ""

        codebase_analysis = ""
        if existing_codebase:
            codebase_analysis = self._analyze_codebase(existing_codebase)

        prompt = self._build_planning_prompt(input_data, codebase_analysis)
        data = self.llm.complete_json(prompt, temperature=0.2, think=True)

        if data.get("spec_clarification_needed"):
            clarification_questions = data.get("clarification_questions") or []
            if not isinstance(clarification_questions, list):
                clarification_questions = (
                    [str(clarification_questions)] if clarification_questions else []
                )
            logger.warning(
                "Tech Lead: spec is unclear, requesting clarification: %s",
                clarification_questions[:3],
            )
            return TechLeadOutput(
                assignment=None,
                planning_hierarchy=None,
                summary=data.get("summary", "Spec is incomplete or ambiguous."),
                requirement_task_mapping=[],
                spec_clarification_needed=True,
                clarification_questions=clarification_questions,
            )

        hierarchy = parse_hierarchy_from_data(data)
        assignment = flatten_hierarchy_to_assignment(hierarchy)

        if not assignment.tasks:
            logger.info(
                "Tech Lead: hierarchy flattening produced no tasks. Next step -> Using fallback assignment parsing"
            )
            assignment = parse_assignment_from_data(data)

        mapping = data.get("requirement_task_mapping") or []
        logger.info(
            "Tech Lead: produced %s tasks across %s stories (execution order: %s)",
            len(assignment.tasks),
            sum(len(e.stories) for i in hierarchy.initiatives for e in i.epics),
            assignment.execution_order[:10]
            if len(assignment.execution_order) > 10
            else assignment.execution_order,
        )
        return TechLeadOutput(
            assignment=assignment,
            planning_hierarchy=hierarchy,
            summary=data.get("summary", ""),
            requirement_task_mapping=mapping,
            spec_clarification_needed=False,
            clarification_questions=[],
        )

    def _build_planning_prompt(self, input_data: TechLeadInput, codebase_analysis: str) -> str:
        """Assemble the full planning prompt from input data."""
        reqs = input_data.requirements
        po = getattr(input_data, "project_overview", None) or {}

        context_parts: List[str] = [
            f"**Product Title:** {reqs.title}",
            f"**Description:** {reqs.description}",
            "**Acceptance Criteria:**",
            *[f"- {c}" for c in reqs.acceptance_criteria],
            "**Constraints:**",
            *[f"- {c}" for c in reqs.constraints],
            f"**Priority:** {reqs.priority}",
        ]

        resolved = input_data.resolved_questions or []
        resolved_question_texts = {r.get("question", "") for r in resolved if isinstance(r, dict)}
        remaining_open = [
            q for q in (input_data.open_questions or []) if q not in resolved_question_texts
        ]

        if resolved:
            context_parts.extend(
                [
                    "",
                    "**USER-PROVIDED RESOLUTIONS (use these exactly):**",
                    *[
                        f"- **{r.get('question', '')}** -> {r.get('answer', '')}"
                        for r in resolved
                        if isinstance(r, dict)
                    ],
                ]
            )
        if remaining_open:
            context_parts.extend(
                [
                    "",
                    "**OPEN QUESTIONS (resolve with best-practice defaults):**",
                    *[f"- {q}" for q in remaining_open],
                ]
            )
        if input_data.assumptions:
            context_parts.extend(
                [
                    "",
                    "**Assumptions from Spec Intake:**",
                    *[f"- {a}" for a in input_data.assumptions],
                ]
            )

        if po:
            context_parts.extend(
                [
                    "",
                    "**Project Overview:**",
                    f"- Primary goal: {po.get('primary_goal', '')}",
                    f"- Delivery strategy: {po.get('delivery_strategy', '')}",
                ]
            )
            milestones = po.get("milestones", [])
            if milestones:
                context_parts.append(
                    "- Milestones: " + ", ".join(m.get("name", "") for m in milestones)
                )

        features_doc = po.get("features_and_functionality_doc", "") if po else ""
        if features_doc:
            context_parts.extend(
                [
                    "",
                    "**Features and Functionality:**",
                    "---",
                    features_doc,
                    "---",
                ]
            )

        if input_data.spec_content:
            context_parts.extend(
                [
                    "",
                    "**Full Specification:**",
                    "---",
                    input_data.spec_content,
                    "---",
                ]
            )

        if codebase_analysis:
            context_parts.extend(
                [
                    "",
                    "**Codebase Analysis:**",
                    "---",
                    codebase_analysis,
                    "---",
                ]
            )

        if input_data.existing_tasks:
            task_lines = [
                "",
                "**Existing tasks (extend or reprioritize):**",
                "---",
            ]
            for t in input_data.existing_tasks:
                task_lines.append(
                    f"- **id:** {t.id} | **type:** {t.type} | **title:** {t.title} | **status:** {t.status} | **assignee:** {t.assignee}"
                )
                task_lines.append(
                    f"  **description:** {t.description[:500]}{'...' if len(t.description) > 500 else ''}"
                )
                if t.requirements:
                    task_lines.append(
                        f"  **requirements:** {t.requirements[:300]}{'...' if len(t.requirements) > 300 else ''}"
                    )
                if t.acceptance_criteria:
                    task_lines.append(
                        "  **acceptance_criteria:** " + "; ".join(t.acceptance_criteria[:5])
                    )
                if t.dependencies:
                    task_lines.append(f"  **dependencies:** {t.dependencies}")
                task_lines.append("")
            context_parts.extend(task_lines)
            context_parts.append("---")

        if input_data.architecture:
            arch = input_data.architecture
            context_parts.extend(
                [
                    "",
                    "**System Architecture:**",
                    arch.overview,
                    "",
                    "**Components:**",
                    *[f"- {c.name} ({c.type}): {c.description}" for c in arch.components],
                ]
            )

        if input_data.repo_path:
            context_parts.extend(["", f"**Repo path:** {input_data.repo_path}"])

        # Include plan artifacts if available (from Planning V2 or explicit input)
        plan_artifacts = input_data.plan_artifacts_content or ""
        if not plan_artifacts and input_data.repo_path:
            plan_artifacts = self._read_plan_artifacts(input_data.repo_path)

        if plan_artifacts:
            context_parts.extend(
                [
                    "",
                    "**Planning Artifacts (from /plan folder - use these for context):**",
                    "---",
                    plan_artifacts,
                    "---",
                ]
            )

        return TECH_LEAD_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)

    def refine_task(
        self,
        task: Task,
        clarification_requests: list,
        spec_content: str,
        architecture=None,
    ) -> Task:
        """
        Refine a task based on specialist clarification requests.
        Returns an updated Task with more detailed description, requirements, and acceptance criteria.
        """
        from software_engineering_team.shared.context_sizing import compute_spec_excerpt_chars

        logger.info(
            "Tech Lead: refining task %s with %s clarification requests",
            task.id,
            len(clarification_requests),
        )
        max_spec = compute_spec_excerpt_chars(self.llm)
        spec_excerpt = compact_text(spec_content or "", max_spec, self.llm, "specification excerpt")
        context_parts = [
            f"**Task ID:** {task.id}",
            f"**Current description:** {task.description}",
            f"**Current requirements:** {task.requirements}",
            f"**Current acceptance criteria:** {task.acceptance_criteria}",
            "",
            "**Clarification questions from specialist:**",
            *[f"- {q}" for q in clarification_requests],
            "",
            "**Spec (excerpt):**",
            spec_excerpt,
        ]
        if architecture:
            context_parts.extend(
                [
                    "",
                    "**Architecture overview:**",
                    architecture.overview,
                ]
            )

        prompt = TECH_LEAD_REFINE_TASK_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2, think=True)

        return Task(
            id=task.id,
            type=task.type,
            title=data.get("title") or data.get("name", task.title),
            description=data.get("description", task.description),
            user_story=data.get("user_story", task.user_story),
            assignee=task.assignee,
            requirements=data.get("requirements", task.requirements),
            dependencies=task.dependencies,
            acceptance_criteria=data.get("acceptance_criteria", task.acceptance_criteria),
            status=task.status,
        )

    def evaluate_qa_and_create_fix_tasks(
        self,
        task: Task,
        qa_result,
        spec_content: str,
        architecture=None,
    ) -> list:
        """
        Evaluate QA feedback and create fix tasks if the delivered code does not meet spec.
        Returns a list of new Task objects (may be empty).
        """
        from software_engineering_team.shared.context_sizing import compute_spec_excerpt_chars

        logger.info("Tech Lead: evaluating QA feedback for task %s", task.id)
        qa_bugs = getattr(qa_result, "bugs_found", []) or []
        bugs_text = "\n".join(
            f"- [{getattr(b, 'severity', b.get('severity', 'medium'))}] {getattr(b, 'description', b.get('description', ''))} "
            f"(location: {getattr(b, 'location', b.get('location', ''))}) | "
            f"Recommendation: {getattr(b, 'recommendation', b.get('recommendation', ''))}"
            for b in qa_bugs
        )
        max_spec = compute_spec_excerpt_chars(self.llm)
        spec_excerpt = compact_text(spec_content or "", max_spec, self.llm, "specification excerpt")
        context_parts = [
            f"**Completed task:** id={task.id}, assignee={task.assignee}, description={task.description}",
            f"**QA approved:** {getattr(qa_result, 'approved', True)}",
            f"**QA bugs found ({len(qa_bugs)}):**",
            bugs_text or "None",
            "",
            "**Spec (excerpt):**",
            spec_excerpt,
        ]
        if architecture:
            context_parts.extend(["", "**Architecture:**", architecture.overview])

        prompt = TECH_LEAD_EVALUATE_QA_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2, think=True)

        tasks = []
        for t in data.get("tasks") or []:
            if isinstance(t, dict) and t.get("id"):
                assignee = t.get("assignee") or task.assignee
                try:
                    task_type = TaskType(t.get("type", task.type.value))
                except ValueError:
                    task_type = task.type
                acc = t.get("acceptance_criteria") or []
                if not isinstance(acc, list):
                    acc = [str(acc)] if acc else []
                tasks.append(
                    Task(
                        id=t["id"],
                        type=task_type,
                        title=t.get("title") or t.get("name") or t.get("label", ""),
                        description=t.get("description", ""),
                        user_story=t.get("user_story", ""),
                        assignee=assignee,
                        requirements=t.get("requirements", ""),
                        dependencies=t.get("dependencies", [task.id]),
                        acceptance_criteria=acc,
                        status=TaskStatus.PENDING,
                    )
                )
        logger.info("Tech Lead: created %s fix tasks from QA feedback", len(tasks))
        return tasks

    def should_run_security(
        self,
        completed_code_task_ids: list,
        spec_content: str,
        requirement_task_mapping: list,
    ) -> bool:
        """
        Determine whether to run security review. Returns True only when code covers 90%+ of spec.
        """
        if not completed_code_task_ids:
            return False
        from software_engineering_team.shared.context_sizing import (
            compute_requirement_mapping_chars,
            compute_spec_excerpt_chars,
        )

        logger.info(
            "Tech Lead: evaluating if security review should run (%s completed code tasks)",
            len(completed_code_task_ids),
        )
        max_spec = compute_spec_excerpt_chars(self.llm)
        max_mapping = compute_requirement_mapping_chars(self.llm)
        context_parts = [
            "**Completed backend/frontend task IDs:**",
            ", ".join(completed_code_task_ids),
            "",
            "**Spec:**",
            compact_text(spec_content or "", max_spec, self.llm, "specification"),
            "",
            "**Requirement-task mapping:**",
            compact_text(
                str(requirement_task_mapping), max_mapping, self.llm, "requirement-task mapping"
            ),
        ]
        prompt = TECH_LEAD_SHOULD_RUN_SECURITY_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1, think=True)
        run_security = bool(data.get("run_security", False))
        logger.info("Tech Lead: run_security=%s (%s)", run_security, data.get("rationale", "")[:80])
        return run_security

    def review_progress(
        self,
        task_update: TaskUpdate,
        spec_content: str,
        architecture,
        completed_tasks: List[Task],
        remaining_tasks: List[Task],
        codebase_summary: str,
    ) -> List[Task]:
        """
        Review completed work against the spec after receiving a task update.
        Identifies gaps in spec coverage and creates new tasks to fill them.
        Returns a list of new Task objects (may be empty).
        """
        if getattr(task_update, "failure_class", None) == "llm_connectivity":
            logger.info(
                "Tech Lead: task %s failed due to LLM connectivity; not creating fix tasks (orchestrator will pause job).",
                task_update.task_id,
            )
            return []

        logger.info(
            "Tech Lead: reviewing progress after task %s (%s) - %s completed, %s remaining",
            task_update.task_id,
            task_update.agent_type,
            len(completed_tasks),
            len(remaining_tasks),
        )

        completed_summary = (
            "\n".join(
                f"- [{t.id}] {t.title}: {t.description[:120]}..."
                if len(t.description) > 120
                else f"- [{t.id}] {t.title}: {t.description}"
                for t in completed_tasks
            )
            or "None yet"
        )

        remaining_summary = (
            "\n".join(
                f"- [{t.id}] {t.title}: {t.description[:120]}..."
                if len(t.description) > 120
                else f"- [{t.id}] {t.title}: {t.description}"
                for t in remaining_tasks
            )
            or "None remaining"
        )

        context_parts = [
            "**TASK UPDATE (just completed):**",
            f"- Task ID: {task_update.task_id}",
            f"- Agent type: {task_update.agent_type}",
            f"- Status: {task_update.status}",
            f"- Summary: {task_update.summary}",
            f"- Files changed: {', '.join(task_update.files_changed) if task_update.files_changed else 'None reported'}",
            "",
            f"**COMPLETED TASKS ({len(completed_tasks)}):**",
            completed_summary,
            "",
            f"**REMAINING TASKS ({len(remaining_tasks)}):**",
            remaining_summary,
            "",
            "**FULL SPEC (source of truth):**",
            "---",
            spec_content or "(no spec provided)",
            "---",
        ]

        failure_reason = getattr(task_update, "failure_reason", None)
        if failure_reason:
            context_parts.extend(
                [
                    "",
                    "**FAILURE REASON (create tasks to fix these specific errors):**",
                    failure_reason,
                ]
            )

        if architecture:
            context_parts.extend(
                [
                    "",
                    "**Architecture overview:**",
                    architecture.overview,
                ]
            )

        if codebase_summary:
            from software_engineering_team.shared.context_sizing import compute_existing_code_chars

            max_code = compute_existing_code_chars(self.llm)
            context_parts.extend(
                [
                    "",
                    "**Current codebase state:**",
                    compact_text(codebase_summary, max_code, self.llm, "codebase summary"),
                ]
            )

        prompt = TECH_LEAD_REVIEW_PROGRESS_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2, think=True)

        new_tasks: List[Task] = []
        for t in data.get("tasks") or []:
            if isinstance(t, dict) and t.get("id"):
                assignee = t.get("assignee") or "backend"
                try:
                    task_type = TaskType(t.get("type", "backend"))
                except ValueError:
                    task_type = TaskType.BACKEND
                acc = t.get("acceptance_criteria") or []
                if not isinstance(acc, list):
                    acc = [str(acc)] if acc else []
                new_tasks.append(
                    Task(
                        id=t["id"],
                        type=task_type,
                        title=t.get("title") or t.get("name") or t.get("label", ""),
                        description=t.get("description", ""),
                        user_story=t.get("user_story", ""),
                        assignee=assignee,
                        requirements=t.get("requirements", ""),
                        dependencies=t.get("dependencies", []),
                        acceptance_criteria=acc,
                        status=TaskStatus.PENDING,
                    )
                )

        spec_compliance = data.get("spec_compliance_pct", 0)
        gaps = data.get("gaps_identified") or []
        rationale = data.get("rationale", "")

        logger.info(
            "Tech Lead: progress review complete - spec_compliance=%s%%, gaps=%s, new_tasks=%s. %s",
            spec_compliance,
            len(gaps),
            len(new_tasks),
            rationale[:150],
        )

        if gaps:
            logger.info("Tech Lead: identified gaps: %s", [g[:60] for g in gaps[:5]])

        return new_tasks

    def trigger_documentation_update(
        self,
        doc_agent,
        repo_path,
        task_update: TaskUpdate,
        spec_content: str,
        architecture,
        codebase_summary: str,
    ) -> None:
        """
        Decide whether documentation needs updating after a task completes,
        and if so, trigger the Documentation Agent.
        """
        from pathlib import Path

        logger.info(
            "Tech Lead: evaluating documentation update for task %s (%s)",
            task_update.task_id,
            task_update.agent_type,
        )

        try:
            path = Path(repo_path).resolve()
            readme_file = path / "README.md"
            readme_content = (
                readme_file.read_text(encoding="utf-8", errors="replace").strip()
                if readme_file.exists()
                else ""
            )
            readme_missing_or_empty = (
                not readme_file.exists() or not readme_content or len(readme_content) < 100
            )
            force_docs_because_readme_empty = (
                readme_missing_or_empty and task_update.agent_type in ("backend", "frontend")
            )

            should_update = force_docs_because_readme_empty
            rationale = ""
            if not force_docs_because_readme_empty:
                from software_engineering_team.shared.context_sizing import (
                    compute_spec_excerpt_chars,
                )

                max_code = compute_spec_excerpt_chars(self.llm)
                code_excerpt = compact_text(
                    codebase_summary or "", max_code, self.llm, "codebase summary"
                )
                context_parts = [
                    f"**Task ID:** {task_update.task_id}",
                    f"**Agent type:** {task_update.agent_type}",
                    f"**Status:** {task_update.status}",
                    f"**Summary:** {task_update.summary}",
                    f"**Files changed:** {', '.join(task_update.files_changed) if task_update.files_changed else 'None reported'}",
                    "",
                    "**Current codebase state (excerpt):**",
                    code_excerpt,
                ]
                if readme_missing_or_empty:
                    context_parts.insert(
                        0,
                        "**Repository README.md:** missing or empty (you MUST set should_update_docs to true).",
                    )

                prompt = TECH_LEAD_TRIGGER_DOCS_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
                data = self.llm.complete_json(prompt, temperature=0.1, think=True)
                should_update = bool(data.get("should_update_docs", False))
                rationale = data.get("rationale", "")

            logger.info(
                "Tech Lead: should_update_docs=%s for task %s (%s)%s",
                should_update,
                task_update.task_id,
                rationale[:100] if rationale else "N/A",
                " (forced: README missing, empty, or minimal)"
                if force_docs_because_readme_empty
                else "",
            )

            if not should_update:
                return

            logger.info(
                "Tech Lead: triggering Documentation Agent for task %s", task_update.task_id
            )
            doc_result = doc_agent.run_full_workflow(
                repo_path=repo_path,
                task_id=task_update.task_id,
                task_summary=task_update.summary,
                agent_type=task_update.agent_type,
                spec_content=spec_content,
                architecture=architecture,
                codebase_content=codebase_summary,
            )
            logger.info(
                "Tech Lead: Documentation Agent completed for task %s: %s",
                task_update.task_id,
                doc_result.summary[:200] if doc_result.summary else "no summary",
            )

        except Exception as e:
            logger.warning(
                "Tech Lead: documentation update failed for task %s (non-blocking): %s",
                task_update.task_id,
                e,
            )

    def trigger_devops_for_backend(
        self,
        devops_agent,
        repo_path,
        architecture,
        spec_content: str,
        existing_pipeline: str | None = None,
        build_verifier=None,
    ) -> bool:
        """
        Trigger the DevOps agent to add containerization and deployment for the backend repo.
        Returns True if run and write succeeded, False otherwise (non-blocking).
        """
        from pathlib import Path

        path = Path(repo_path).resolve()
        if not (path / ".git").exists():
            logger.warning("Tech Lead: skip DevOps for backend (not a git repo): %s", path)
            return False
        logger.info("Tech Lead: triggering DevOps for backend repo (containerize and deploy)")
        try:
            workflow_result = devops_agent.run_workflow(
                repo_path=path,
                task_description="Add containerization and deployment for the backend application. Produce a Dockerfile and CI/CD so this repo can be built and deployed. Backend is Python/FastAPI.",
                requirements="Dockerfile for Python/FastAPI (pip install, uvicorn). CI/CD: install deps, run tests (pytest), build image. Make repo self-contained for build and deploy.",
                architecture=architecture,
                existing_pipeline=existing_pipeline
                if existing_pipeline and existing_pipeline != "# No code files found"
                else None,
                tech_stack=["Python", "FastAPI", "PostgreSQL", "Docker"],
                target_repo="backend",
                build_verifier=build_verifier,
                task_id="devops-backend",
                subdir="",
            )
            if workflow_result.success:
                logger.info("Tech Lead: DevOps for backend completed (workflow)")
            else:
                logger.warning(
                    "Tech Lead: DevOps for backend workflow failed: %s",
                    workflow_result.failure_reason,
                )
            return workflow_result.success
        except Exception as e:
            logger.warning("Tech Lead: DevOps for backend failed (non-blocking): %s", e)
            return False

    def trigger_devops_for_frontend(
        self,
        devops_agent,
        repo_path,
        architecture,
        spec_content: str,
        existing_pipeline: str | None = None,
        build_verifier=None,
    ) -> bool:
        """
        Trigger the DevOps agent to add containerization and deployment for the frontend repo.
        Returns True if run and write succeeded, False otherwise (non-blocking).
        """
        from pathlib import Path

        path = Path(repo_path).resolve()
        if not (path / ".git").exists():
            logger.warning("Tech Lead: skip DevOps for frontend (not a git repo): %s", path)
            return False
        logger.info("Tech Lead: triggering DevOps for frontend repo (containerize and deploy)")
        try:
            workflow_result = devops_agent.run_workflow(
                repo_path=path,
                task_description="Add containerization and deployment for the frontend application. Produce a Dockerfile and CI/CD so this repo can be built and deployed. Frontend is a JavaScript/TypeScript application (React, Angular, or Vue).",
                requirements="Dockerfile: multi-stage build (npm ci, npm run build; serve with nginx or Node). CI/CD: install deps, run tests, build image. Make repo self-contained for build and deploy.",
                architecture=architecture,
                existing_pipeline=existing_pipeline
                if existing_pipeline and existing_pipeline != "# No code files found"
                else None,
                tech_stack=["JavaScript", "TypeScript", "Node", "Docker"],
                target_repo="frontend",
                build_verifier=build_verifier,
                task_id="devops-frontend",
                subdir="",
            )
            if workflow_result.success:
                logger.info("Tech Lead: DevOps for frontend completed (workflow)")
            else:
                logger.warning(
                    "Tech Lead: DevOps for frontend workflow failed: %s",
                    workflow_result.failure_reason,
                )
            return workflow_result.success
        except Exception as e:
            logger.warning("Tech Lead: DevOps for frontend failed (non-blocking): %s", e)
            return False
