"""Tech Lead agent: produces Initiative/Epic/Story hierarchy from product requirements."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from shared.llm import LLMClient
from shared.models import Task, TaskAssignment, TaskStatus, TaskType, TaskUpdate
from shared.task_parsing import (
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
        prompt = TECH_LEAD_ANALYZE_CODEBASE_PROMPT + "\n\n---\n\n**EXISTING CODEBASE:**\n" + existing_codebase
        data = self.llm.complete_json(prompt, temperature=0.1)
        return json.dumps(data, indent=2)

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
            story_count = sum(len(e.stories) for i in hierarchy.initiatives for e in i.epics)
            logger.info(
                "Tech Lead: using Planning V2 hierarchy (%s tasks across %s stories)",
                task_count,
                story_count,
            )
            # Generate a summary without re-planning
            summary = f"Using pre-existing Planning V2 hierarchy with {task_count} tasks across {story_count} stories."
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
        data = self.llm.complete_json(prompt, temperature=0.2)

        if data.get("spec_clarification_needed"):
            clarification_questions = data.get("clarification_questions") or []
            if not isinstance(clarification_questions, list):
                clarification_questions = [str(clarification_questions)] if clarification_questions else []
            logger.warning("Tech Lead: spec is unclear, requesting clarification: %s", clarification_questions[:3])
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
            assignment = parse_assignment_from_data(data)

        mapping = data.get("requirement_task_mapping") or []
        logger.info(
            "Tech Lead: produced %s tasks across %s stories (execution order: %s)",
            len(assignment.tasks),
            sum(len(e.stories) for i in hierarchy.initiatives for e in i.epics),
            assignment.execution_order[:10] if len(assignment.execution_order) > 10 else assignment.execution_order,
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
        remaining_open = [q for q in (input_data.open_questions or []) if q not in resolved_question_texts]

        if resolved:
            context_parts.extend([
                "",
                "**USER-PROVIDED RESOLUTIONS (use these exactly):**",
                *[f"- **{r.get('question', '')}** -> {r.get('answer', '')}" for r in resolved if isinstance(r, dict)],
            ])
        if remaining_open:
            context_parts.extend([
                "",
                "**OPEN QUESTIONS (resolve with best-practice defaults):**",
                *[f"- {q}" for q in remaining_open],
            ])
        if input_data.assumptions:
            context_parts.extend([
                "",
                "**Assumptions from Spec Intake:**",
                *[f"- {a}" for a in input_data.assumptions],
            ])

        if po:
            context_parts.extend([
                "",
                "**Project Overview:**",
                f"- Primary goal: {po.get('primary_goal', '')}",
                f"- Delivery strategy: {po.get('delivery_strategy', '')}",
            ])
            milestones = po.get("milestones", [])
            if milestones:
                context_parts.append("- Milestones: " + ", ".join(m.get("name", "") for m in milestones))

        features_doc = po.get("features_and_functionality_doc", "") if po else ""
        if features_doc:
            context_parts.extend([
                "",
                "**Features and Functionality:**",
                "---",
                features_doc[:20000],
                "---",
            ])

        if input_data.spec_content:
            context_parts.extend([
                "",
                "**Full Specification:**",
                "---",
                input_data.spec_content[:30000],
                "---",
            ])

        if codebase_analysis:
            context_parts.extend([
                "",
                "**Codebase Analysis:**",
                "---",
                codebase_analysis[:10000],
                "---",
            ])

        if input_data.existing_tasks:
            task_lines = [
                "",
                "**Existing tasks (extend or reprioritize):**",
                "---",
            ]
            for t in input_data.existing_tasks:
                task_lines.append(f"- **id:** {t.id} | **type:** {t.type} | **title:** {t.title} | **status:** {t.status} | **assignee:** {t.assignee}")
                task_lines.append(f"  **description:** {t.description[:500]}{'...' if len(t.description) > 500 else ''}")
                if t.requirements:
                    task_lines.append(f"  **requirements:** {t.requirements[:300]}{'...' if len(t.requirements) > 300 else ''}")
                if t.acceptance_criteria:
                    task_lines.append("  **acceptance_criteria:** " + "; ".join(t.acceptance_criteria[:5]))
                if t.dependencies:
                    task_lines.append(f"  **dependencies:** {t.dependencies}")
                task_lines.append("")
            context_parts.extend(task_lines)
            context_parts.append("---")

        if input_data.architecture:
            arch = input_data.architecture
            context_parts.extend([
                "",
                "**System Architecture:**",
                arch.overview,
                "",
                "**Components:**",
                *[f"- {c.name} ({c.type}): {c.description}" for c in arch.components],
            ])

        if input_data.repo_path:
            context_parts.extend(["", f"**Repo path:** {input_data.repo_path}"])

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
        from shared.context_sizing import compute_spec_excerpt_chars

        logger.info("Tech Lead: refining task %s with %s clarification requests", task.id, len(clarification_requests))
        max_spec = compute_spec_excerpt_chars(self.llm)
        spec_excerpt = (spec_content or "")[:max_spec] + ("..." if len(spec_content or "") > max_spec else "")
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
            context_parts.extend([
                "",
                "**Architecture overview:**",
                architecture.overview,
            ])

        prompt = TECH_LEAD_REFINE_TASK_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

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
        from shared.context_sizing import compute_spec_excerpt_chars

        logger.info("Tech Lead: evaluating QA feedback for task %s", task.id)
        qa_bugs = getattr(qa_result, "bugs_found", []) or []
        bugs_text = "\n".join(
            f"- [{getattr(b, 'severity', b.get('severity', 'medium'))}] {getattr(b, 'description', b.get('description', ''))} "
            f"(location: {getattr(b, 'location', b.get('location', ''))}) | "
            f"Recommendation: {getattr(b, 'recommendation', b.get('recommendation', ''))}"
            for b in qa_bugs
        )
        max_spec = compute_spec_excerpt_chars(self.llm)
        spec_excerpt = (spec_content or "")[:max_spec] + ("..." if len(spec_content or "") > max_spec else "")
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
        data = self.llm.complete_json(prompt, temperature=0.2)

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
        from shared.context_sizing import compute_requirement_mapping_chars, compute_spec_excerpt_chars

        logger.info("Tech Lead: evaluating if security review should run (%s completed code tasks)", len(completed_code_task_ids))
        max_spec = compute_spec_excerpt_chars(self.llm)
        max_mapping = compute_requirement_mapping_chars(self.llm)
        context_parts = [
            "**Completed backend/frontend task IDs:**",
            ", ".join(completed_code_task_ids),
            "",
            "**Spec:**",
            (spec_content or "")[:max_spec] + ("..." if len(spec_content or "") > max_spec else ""),
            "",
            "**Requirement-task mapping:**",
            str(requirement_task_mapping)[:max_mapping],
        ]
        prompt = TECH_LEAD_SHOULD_RUN_SECURITY_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1)
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

        completed_summary = "\n".join(
            f"- [{t.id}] {t.title}: {t.description[:120]}..." if len(t.description) > 120 else f"- [{t.id}] {t.title}: {t.description}"
            for t in completed_tasks
        ) or "None yet"

        remaining_summary = "\n".join(
            f"- [{t.id}] {t.title}: {t.description[:120]}..." if len(t.description) > 120 else f"- [{t.id}] {t.title}: {t.description}"
            for t in remaining_tasks
        ) or "None remaining"

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
            context_parts.extend([
                "",
                "**FAILURE REASON (create tasks to fix these specific errors):**",
                failure_reason[:4000] + ("..." if len(failure_reason) > 4000 else ""),
            ])

        if architecture:
            context_parts.extend([
                "",
                "**Architecture overview:**",
                architecture.overview,
            ])

        if codebase_summary:
            from shared.context_sizing import compute_existing_code_chars
            max_code = compute_existing_code_chars(self.llm)
            code_excerpt = codebase_summary[:max_code] + ("..." if len(codebase_summary) > max_code else "")
            context_parts.extend([
                "",
                "**Current codebase state:**",
                code_excerpt,
            ])

        prompt = TECH_LEAD_REVIEW_PROGRESS_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

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
                readme_missing_or_empty
                and task_update.agent_type in ("backend", "frontend")
            )

            should_update = force_docs_because_readme_empty
            rationale = ""
            if not force_docs_because_readme_empty:
                from shared.context_sizing import compute_spec_excerpt_chars
                max_code = compute_spec_excerpt_chars(self.llm)
                code_excerpt = (codebase_summary or "")[:max_code] + ("..." if len(codebase_summary or "") > max_code else "")
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
                    context_parts.insert(0, "**Repository README.md:** missing or empty (you MUST set should_update_docs to true).")

                prompt = TECH_LEAD_TRIGGER_DOCS_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
                data = self.llm.complete_json(prompt, temperature=0.1)
                should_update = bool(data.get("should_update_docs", False))
                rationale = data.get("rationale", "")

            logger.info(
                "Tech Lead: should_update_docs=%s for task %s (%s)%s",
                should_update,
                task_update.task_id,
                rationale[:100] if rationale else "N/A",
                " (forced: README missing, empty, or minimal)" if force_docs_because_readme_empty else "",
            )

            if not should_update:
                return

            logger.info("Tech Lead: triggering Documentation Agent for task %s", task_update.task_id)
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
                existing_pipeline=existing_pipeline if existing_pipeline and existing_pipeline != "# No code files found" else None,
                tech_stack=["Python", "FastAPI", "PostgreSQL", "Docker"],
                target_repo="backend",
                build_verifier=build_verifier,
                task_id="devops-backend",
                subdir="",
            )
            if workflow_result.success:
                logger.info("Tech Lead: DevOps for backend completed (workflow)")
            else:
                logger.warning("Tech Lead: DevOps for backend workflow failed: %s", workflow_result.failure_reason)
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
                existing_pipeline=existing_pipeline if existing_pipeline and existing_pipeline != "# No code files found" else None,
                tech_stack=["JavaScript", "TypeScript", "Node", "Docker"],
                target_repo="frontend",
                build_verifier=build_verifier,
                task_id="devops-frontend",
                subdir="",
            )
            if workflow_result.success:
                logger.info("Tech Lead: DevOps for frontend completed (workflow)")
            else:
                logger.warning("Tech Lead: DevOps for frontend workflow failed: %s", workflow_result.failure_reason)
            return workflow_result.success
        except Exception as e:
            logger.warning("Tech Lead: DevOps for frontend failed (non-blocking): %s", e)
            return False
