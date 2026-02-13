"""Tech Lead agent: orchestrates tasks from product requirements and architecture."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from shared.llm import LLMClient
from shared.models import Task, TaskAssignment, TaskStatus, TaskType, TaskUpdate
from shared.task_validation import validate_assignment

from .models import TechLeadInput, TechLeadOutput
from .prompts import (
    TECH_LEAD_ANALYZE_CODEBASE_PROMPT,
    TECH_LEAD_ANALYZE_SPEC_PROMPT,
    TECH_LEAD_EVALUATE_QA_PROMPT,
    TECH_LEAD_PROMPT,
    TECH_LEAD_REFINE_TASK_PROMPT,
    TECH_LEAD_REVIEW_PROGRESS_PROMPT,
    TECH_LEAD_SHOULD_RUN_SECURITY_PROMPT,
    TECH_LEAD_TRIGGER_DOCS_PROMPT,
)

logger = logging.getLogger(__name__)

MAX_TECH_LEAD_RETRIES = 3


class TechLeadAgent:
    """
    Staff-level Tech Lead that bridges product management and engineering.
    Uses product requirements and system architecture to plan and distribute
    tasks amongst DevOps, Security, Backend, Frontend, and QA agents.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def _parse_assignment_from_data(self, data: Dict[str, Any]) -> TaskAssignment:
        """Parse LLM JSON output into TaskAssignment. Filters out security/qa (orchestrator invokes those)."""
        tasks = []
        for t in data.get("tasks") or []:
            if isinstance(t, dict) and t.get("id"):
                assignee = t.get("assignee") or "devops"
                try:
                    task_type = TaskType(t.get("type", "backend"))
                except ValueError:
                    task_type = TaskType.BACKEND
                # Skip standalone security/qa - orchestrator invokes them in response to coding work
                if task_type in (TaskType.SECURITY, TaskType.QA):
                    continue
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
                        dependencies=t.get("dependencies", []),
                        acceptance_criteria=acc,
                        status=TaskStatus.PENDING,
                    )
                )

        execution_order = data.get("execution_order") or [t.id for t in tasks]
        # Filter execution_order to only include task IDs we kept
        valid_ids = {t.id for t in tasks}
        execution_order = [tid for tid in execution_order if tid in valid_ids]

        # Enforce interleaving: backend and frontend tasks should alternate
        execution_order = self._interleave_execution_order(execution_order, {t.id: t for t in tasks})

        return TaskAssignment(
            tasks=tasks,
            execution_order=execution_order,
            rationale=data.get("rationale", ""),
        )

    @staticmethod
    def _interleave_execution_order(execution_order: List[str], tasks_by_id: Dict[str, Any]) -> List[str]:
        """
        Enforce interleaving of backend and frontend tasks in the execution order.

        Non-coding tasks (git_setup, devops) stay at the front in their original order.
        Backend and frontend tasks are then interleaved: 1 backend, 1 frontend, 1 backend, etc.
        """
        prefix: List[str] = []        # git_setup + devops tasks (keep at front)
        backend_queue: List[str] = []  # backend tasks in original order
        frontend_queue: List[str] = [] # frontend tasks in original order

        for tid in execution_order:
            task = tasks_by_id.get(tid)
            if not task:
                prefix.append(tid)
                continue
            assignee = getattr(task, "assignee", None) or ""
            if assignee == "backend":
                backend_queue.append(tid)
            elif assignee == "frontend":
                frontend_queue.append(tid)
            else:
                # devops, git_setup, unknown — keep at front
                prefix.append(tid)

        # Interleave backend and frontend
        interleaved: List[str] = []
        bi, fi = 0, 0
        while bi < len(backend_queue) or fi < len(frontend_queue):
            if bi < len(backend_queue):
                interleaved.append(backend_queue[bi])
                bi += 1
            if fi < len(frontend_queue):
                interleaved.append(frontend_queue[fi])
                fi += 1

        result = prefix + interleaved
        if result != execution_order:
            logger.info(
                "Tech Lead: reordered execution to interleave backend/frontend: %s",
                result,
            )
        return result

    def _analyze_codebase(self, existing_codebase: str) -> str:
        """Step 1: Analyze the existing codebase to understand what already exists."""
        logger.info("Tech Lead: Step 1/3 - Analyzing existing codebase (%s chars)", len(existing_codebase))
        prompt = TECH_LEAD_ANALYZE_CODEBASE_PROMPT + "\n\n---\n\n**EXISTING CODEBASE:**\n" + existing_codebase
        data = self.llm.complete_json(prompt, temperature=0.1)
        # Return the full analysis as a formatted string for use in subsequent steps
        return json.dumps(data, indent=2)

    def _analyze_spec(self, spec_content: str, reqs) -> str:
        """Step 2: Deep analysis of the spec to extract every requirement."""
        logger.info("Tech Lead: Step 2/3 - Analyzing spec in depth (%s chars)", len(spec_content))
        context_parts = [
            f"**Product Title:** {reqs.title}",
            f"**Description:** {reqs.description}",
            "**Acceptance Criteria:**",
            *[f"- {c}" for c in reqs.acceptance_criteria],
            "**Constraints:**",
            *[f"- {c}" for c in reqs.constraints],
            f"**Priority:** {reqs.priority}",
            "",
            "**Full Specification:**",
            "---",
            spec_content,
            "---",
        ]
        prompt = TECH_LEAD_ANALYZE_SPEC_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.1)
        return json.dumps(data, indent=2)

    def run(self, input_data: TechLeadInput) -> TechLeadOutput:
        """
        Plan and assign tasks to the team using a multi-step approach:
        1. Analyze existing codebase (if provided)
        2. Deep-analyze the spec to extract all requirements
        3. Generate task plan using combined context
        4. Validate and retry if needed
        """
        logger.info("Tech Lead: beginning multi-step planning for %s", input_data.requirements.title)
        reqs = input_data.requirements
        arch = input_data.architecture

        spec_content = input_data.spec_content or ""
        arch_doc = (arch.architecture_document or "") if arch else ""
        existing_codebase = input_data.existing_codebase or ""

        # ── Step 1: Codebase analysis ──
        codebase_analysis = ""
        if existing_codebase:
            codebase_analysis = self._analyze_codebase(existing_codebase)
            logger.info("Tech Lead: codebase analysis complete (%s chars)", len(codebase_analysis))

        # ── Step 2: Spec analysis ──
        spec_analysis = ""
        if spec_content:
            spec_analysis = self._analyze_spec(spec_content, reqs)
            logger.info("Tech Lead: spec analysis complete (%s chars)", len(spec_analysis))

        # ── Step 3: Task generation with combined context ──
        logger.info("Tech Lead: Step 3/3 - Generating task plan from combined analysis")
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

        # Include the deep spec analysis from Step 2
        if spec_analysis:
            context_parts.extend([
                "",
                "**DEEP SPEC ANALYSIS (from prior analysis step - use this to ensure complete coverage):**",
                "---",
                spec_analysis,
                "---",
            ])

        # Also include the raw spec for reference
        if spec_content:
            context_parts.extend([
                "",
                "**Full initial_spec.md (use this to generate the complete build plan):**",
                "---",
                spec_content,
                "---",
            ])

        # Include the codebase analysis from Step 1
        if codebase_analysis:
            context_parts.extend([
                "",
                "**CODEBASE ANALYSIS (from prior analysis step - use this to avoid duplicating existing work):**",
                "---",
                codebase_analysis,
                "---",
            ])

        # Also include raw existing code for reference
        if existing_codebase:
            context_parts.extend([
                "",
                "**EXISTING CODE (raw - reference for understanding current state):**",
                "---",
                existing_codebase[:20000] + ("..." if len(existing_codebase) > 20000 else ""),
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
                arch_doc,
            ])

        base_prompt = TECH_LEAD_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        validation_feedback: str = ""

        # ── Step 4: Validation + retry ──
        for attempt in range(MAX_TECH_LEAD_RETRIES):
            prompt = base_prompt
            if validation_feedback:
                prompt += "\n\n**VALIDATION FAILED - Fix these issues before responding:**\n"
                prompt += validation_feedback

            data = self.llm.complete_json(prompt, temperature=0.2)

            # Check for spec clarification needed (alternative output mode)
            if data.get("spec_clarification_needed"):
                clarification_questions = data.get("clarification_questions") or []
                if not isinstance(clarification_questions, list):
                    clarification_questions = [str(clarification_questions)] if clarification_questions else []
                logger.warning("Tech Lead: spec is unclear, requesting clarification: %s", clarification_questions[:3])
                return TechLeadOutput(
                    assignment=None,
                    summary=data.get("summary", "Spec is incomplete or ambiguous."),
                    requirement_task_mapping=[],
                    spec_clarification_needed=True,
                    clarification_questions=clarification_questions,
                )

            assignment = self._parse_assignment_from_data(data)

            mapping = data.get("requirement_task_mapping") or []
            is_valid, errors = validate_assignment(assignment, reqs, mapping)
            if is_valid:
                logger.info("Tech Lead: assigned %s tasks in order %s", len(assignment.tasks), assignment.execution_order)
                return TechLeadOutput(
                    assignment=assignment,
                    summary=data.get("summary", ""),
                    requirement_task_mapping=mapping,
                    spec_clarification_needed=False,
                    clarification_questions=[],
                )

            validation_feedback = "\n".join(f"- {e}" for e in errors)
            logger.warning("Tech Lead validation failed (attempt %s/%s): %s", attempt + 1, MAX_TECH_LEAD_RETRIES, validation_feedback[:200])

        # Exhausted retries - return best effort
        logger.warning("Tech Lead: returning assignment after %s failed validation attempts", MAX_TECH_LEAD_RETRIES)
        return TechLeadOutput(
            assignment=assignment,
            summary=data.get("summary", "") + " [Validation incomplete - some issues may remain]",
            requirement_task_mapping=data.get("requirement_task_mapping") or [],
            spec_clarification_needed=False,
            clarification_questions=[],
        )

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
        logger.info("Tech Lead: refining task %s with %s clarification requests", task.id, len(clarification_requests))
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
            (spec_content or "")[:8000] + ("..." if len(spec_content or "") > 8000 else ""),
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
        logger.info("Tech Lead: evaluating QA feedback for task %s", task.id)
        qa_bugs = getattr(qa_result, "bugs_found", []) or []
        bugs_text = "\n".join(
            f"- [{getattr(b, 'severity', b.get('severity', 'medium'))}] {getattr(b, 'description', b.get('description', ''))} "
            f"(location: {getattr(b, 'location', b.get('location', ''))}) | "
            f"Recommendation: {getattr(b, 'recommendation', b.get('recommendation', ''))}"
            for b in qa_bugs
        )
        context_parts = [
            f"**Completed task:** id={task.id}, assignee={task.assignee}, description={task.description}",
            f"**QA approved:** {getattr(qa_result, 'approved', True)}",
            f"**QA bugs found ({len(qa_bugs)}):**",
            bugs_text or "None",
            "",
            "**Spec (excerpt):**",
            (spec_content or "")[:12000] + ("..." if len(spec_content or "") > 12000 else ""),
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
        logger.info("Tech Lead: evaluating if security review should run (%s completed code tasks)", len(completed_code_task_ids))
        context_parts = [
            "**Completed backend/frontend task IDs:**",
            ", ".join(completed_code_task_ids),
            "",
            "**Spec:**",
            (spec_content or "")[:8000] + ("..." if len(spec_content or "") > 8000 else ""),
            "",
            "**Requirement-task mapping:**",
            str(requirement_task_mapping)[:2000],
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
        Review completed work against the spec after receiving a task update from a specialist agent.
        Identifies gaps in spec coverage and creates new tasks to fill them.
        Called by the orchestrator after each specialist agent completes a task.

        Returns a list of new Task objects to enqueue (may be empty if no gaps found).
        """
        logger.info(
            "Tech Lead: reviewing progress after task %s (%s) - %s completed, %s remaining",
            task_update.task_id,
            task_update.agent_type,
            len(completed_tasks),
            len(remaining_tasks),
        )

        # Build context for the LLM
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

        if architecture:
            context_parts.extend([
                "",
                "**Architecture overview:**",
                architecture.overview,
            ])

        if codebase_summary:
            context_parts.extend([
                "",
                "**Current codebase state:**",
                codebase_summary[:8000] + ("..." if len(codebase_summary) > 8000 else ""),
            ])

        prompt = TECH_LEAD_REVIEW_PROGRESS_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        # Parse new tasks from the response
        new_tasks: List[Task] = []
        for t in data.get("tasks") or []:
            if isinstance(t, dict) and t.get("id"):
                assignee = t.get("assignee") or "backend"
                try:
                    task_type = TaskType(t.get("type", "backend"))
                except ValueError:
                    task_type = TaskType.BACKEND
                # Only allow coding tasks from progress review
                if task_type in (TaskType.SECURITY, TaskType.QA):
                    continue
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
        and if so, trigger the Documentation Agent to update README.md and CONTRIBUTORS.md.

        Preconditions:
            - doc_agent is a valid DocumentationAgent instance
            - repo_path is a valid git repository path
            - task_update contains details about the just-completed task

        Postconditions:
            - If docs need updating: Documentation Agent runs full workflow (branch, update, merge)
            - If docs don't need updating: only a log message is produced
            - Any failure is logged but does not raise (non-blocking)

        Invariants:
            - The repository is always left on the development branch
            - Documentation failures never block the main pipeline
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
            readme_missing_or_empty = (
                not readme_file.exists() or not readme_file.read_text(encoding="utf-8", errors="replace").strip()
            )
            force_docs_because_readme_empty = (
                readme_missing_or_empty
                and task_update.agent_type in ("backend", "frontend")
            )

            # Ask LLM if docs need updating (unless we already force due to empty README)
            should_update = force_docs_because_readme_empty
            rationale = ""
            if not force_docs_because_readme_empty:
                context_parts = [
                    f"**Task ID:** {task_update.task_id}",
                    f"**Agent type:** {task_update.agent_type}",
                    f"**Status:** {task_update.status}",
                    f"**Summary:** {task_update.summary}",
                    f"**Files changed:** {', '.join(task_update.files_changed) if task_update.files_changed else 'None reported'}",
                    "",
                    "**Current codebase state (excerpt):**",
                    (codebase_summary or "")[:4000] + ("..." if len(codebase_summary or "") > 4000 else ""),
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
                " (forced: README missing or empty)" if force_docs_because_readme_empty else "",
            )

            if not should_update:
                logger.info(
                    "Tech Lead: skipping documentation update for task %s (should_update_docs=false)",
                    task_update.task_id,
                )
                return

            # Trigger the Documentation Agent's full workflow
            logger.info(
                "Tech Lead: triggering Documentation Agent for task %s",
                task_update.task_id,
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
            # Non-blocking: documentation failure should never stop the pipeline
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
    ) -> bool:
        """
        Trigger the DevOps agent to add containerization and deployment for the backend repo.
        Writes Dockerfile, CI/CD, etc. into the backend repository.
        Returns True if run and write succeeded, False otherwise (non-blocking).
        """
        from pathlib import Path
        from devops_agent.models import DevOpsInput
        from shared.repo_writer import write_agent_output

        path = Path(repo_path).resolve()
        if not (path / ".git").exists():
            logger.warning("Tech Lead: skip DevOps for backend (not a git repo): %s", path)
            return False
        logger.info("Tech Lead: triggering DevOps for backend repo (containerize and deploy)")
        try:
            result = devops_agent.run(DevOpsInput(
                task_description="Add containerization and deployment for the backend application. Produce a Dockerfile and CI/CD so this repo can be built and deployed. Backend is Python/FastAPI.",
                requirements="Dockerfile for Python/FastAPI (pip install, uvicorn). CI/CD: install deps, run tests (pytest), build image. Make repo self-contained for build and deploy.",
                architecture=architecture,
                existing_pipeline=existing_pipeline if existing_pipeline and existing_pipeline != "# No code files found" else None,
                tech_stack=["Python", "FastAPI", "PostgreSQL", "Docker"],
                target_repo="backend",
            ))
            if result.needs_clarification and result.clarification_requests:
                logger.warning("Tech Lead: DevOps (backend) requested clarification (non-blocking): %s", result.clarification_requests[:1])
                return False
            ok, msg = write_agent_output(path, result, subdir="")
            if ok:
                logger.info("Tech Lead: DevOps for backend completed: %s", result.summary[:100] if result.summary else "ok")
            else:
                logger.warning("Tech Lead: DevOps for backend write failed: %s", msg)
            return ok
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
    ) -> bool:
        """
        Trigger the DevOps agent to add containerization and deployment for the frontend repo.
        Writes Dockerfile, CI/CD, etc. into the frontend repository.
        Returns True if run and write succeeded, False otherwise (non-blocking).
        """
        from pathlib import Path
        from devops_agent.models import DevOpsInput
        from shared.repo_writer import write_agent_output

        path = Path(repo_path).resolve()
        if not (path / ".git").exists():
            logger.warning("Tech Lead: skip DevOps for frontend (not a git repo): %s", path)
            return False
        logger.info("Tech Lead: triggering DevOps for frontend repo (containerize and deploy)")
        try:
            result = devops_agent.run(DevOpsInput(
                task_description="Add containerization and deployment for the frontend application. Produce a Dockerfile and CI/CD so this repo can be built and deployed. Frontend is Angular/Node.",
                requirements="Dockerfile: multi-stage build (npm ci, ng build; serve with nginx or Node). CI/CD: install deps, run tests, build image. Make repo self-contained for build and deploy.",
                architecture=architecture,
                existing_pipeline=existing_pipeline if existing_pipeline and existing_pipeline != "# No code files found" else None,
                tech_stack=["Angular", "Node", "Docker"],
                target_repo="frontend",
            ))
            if result.needs_clarification and result.clarification_requests:
                logger.warning("Tech Lead: DevOps (frontend) requested clarification (non-blocking): %s", result.clarification_requests[:1])
                return False
            ok, msg = write_agent_output(path, result, subdir="")
            if ok:
                logger.info("Tech Lead: DevOps for frontend completed: %s", result.summary[:100] if result.summary else "ok")
            else:
                logger.warning("Tech Lead: DevOps for frontend write failed: %s", msg)
            return ok
        except Exception as e:
            logger.warning("Tech Lead: DevOps for frontend failed (non-blocking): %s", e)
            return False
