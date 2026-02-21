"""Tech Lead agent: orchestrates tasks from product requirements and architecture."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from planning_team.planning_graph import (
    PlanningDomain,
    PlanningGraph,
    PlanningNode,
    PlanningNodeKind,
    compile_planning_graph_to_task_assignment,
)
from planning_team.spec_analysis_merger import SpecAnalysisMerger, SpecAnalysisMergerInput
from shared.context_sizing import compute_spec_chunk_chars
from planning_team.spec_chunk_analyzer import SpecChunkAnalyzer, SpecChunkAnalyzerInput
from planning_team.spec_chunking import chunk_spec_by_size
from planning_team.task_generator_agent import TaskGeneratorAgent, TaskGeneratorInput
from shared.llm import LLMClient
from shared.models import Task, TaskAssignment, TaskStatus, TaskType, TaskUpdate
from shared.task_parsing import parse_assignment_from_data
from shared.task_validation import validate_assignment

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

MAX_TECH_LEAD_RETRIES = 6


class TechLeadAgent:
    """
    Staff-level Tech Lead that bridges product management and engineering.
    Uses product requirements and system architecture to plan and distribute
    tasks amongst DevOps, Security, Backend, Frontend, and QA agents.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def _analyze_codebase(self, existing_codebase: str) -> str:
        """Step 1: Analyze the existing codebase to understand what already exists."""
        logger.info("Tech Lead: Step 1/3 - Analyzing existing codebase (%s chars)", len(existing_codebase))
        prompt = TECH_LEAD_ANALYZE_CODEBASE_PROMPT + "\n\n---\n\n**EXISTING CODEBASE:**\n" + existing_codebase
        data = self.llm.complete_json(prompt, temperature=0.1)
        # Return the full analysis as a formatted string for use in subsequent steps
        return json.dumps(data, indent=2)

    def _analyze_spec_chunked(self, spec_content: str, reqs) -> str:
        """Step 2: Chunk spec, analyze each chunk, merge results."""
        max_chunk_chars = compute_spec_chunk_chars(self.llm)
        logger.info(
            "Tech Lead: Step 2/3 - Analyzing spec in chunks (%s chars, max %s chars/chunk)",
            len(spec_content),
            max_chunk_chars,
        )
        chunks = chunk_spec_by_size(spec_content, max_chars=max_chunk_chars, overlap=500)
        if not chunks:
            return "{}"

        chunk_analyzer = SpecChunkAnalyzer(self.llm)
        header = {
            "title": reqs.title,
            "description": reqs.description,
            "acceptance_criteria": reqs.acceptance_criteria,
            "constraints": reqs.constraints,
            "priority": reqs.priority,
        }

        def _analyze_chunk(args):
            i, chunk = args
            analysis = chunk_analyzer.run(
                SpecChunkAnalyzerInput(
                    spec_chunk=chunk,
                    chunk_index=i + 1,
                    total_chunks=len(chunks),
                    requirements_header=header,
                )
            )
            return i, analysis.model_dump() if hasattr(analysis, "model_dump") else analysis.dict()

        max_workers = min(4, len(chunks))
        chunk_results = [None] * len(chunks)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_analyze_chunk, (i, ch)): i for i, ch in enumerate(chunks)}
            for future in as_completed(futures):
                idx, result = future.result()
                chunk_results[idx] = result

        merger = SpecAnalysisMerger(self.llm)
        merged = merger.run(SpecAnalysisMergerInput(chunk_results=chunk_results))
        return json.dumps(
            merged.model_dump() if hasattr(merged, "model_dump") else merged.dict(),
            indent=2,
        )

    def _run_planning_pipeline(
        self,
        input_data: TechLeadInput,
        codebase_analysis: str,
        spec_analysis: str,
    ) -> Optional[TechLeadOutput]:
        """
        Run the multi-agent planning pipeline (Backend + Frontend planners).
        Returns TechLeadOutput if successful and valid, else None.
        """
        from planning_team.backend_planning_agent import BackendPlanningAgent, BackendPlanningInput
        from planning_team.frontend_planning_agent import FrontendPlanningAgent, FrontendPlanningInput

        reqs = input_data.requirements
        arch = input_data.architecture
        project_overview = getattr(input_data, "project_overview", None)

        if not arch:
            logger.info("Tech Lead: skipping planning pipeline (no architecture)")
            return None

        logger.info("Tech Lead: running planning pipeline (Backend + Frontend planners in parallel)")
        backend_planner = BackendPlanningAgent(self.llm)
        frontend_planner = FrontendPlanningAgent(self.llm)

        backend_input = BackendPlanningInput(
            requirements=reqs,
            architecture=arch,
            spec_content=input_data.spec_content or "",
            project_overview=project_overview,
            codebase_analysis=codebase_analysis or None,
            spec_analysis=spec_analysis or None,
        )
        frontend_input = FrontendPlanningInput(
            requirements=reqs,
            architecture=arch,
            spec_content=input_data.spec_content or "",
            project_overview=project_overview,
            codebase_analysis=codebase_analysis or None,
            spec_analysis=spec_analysis or None,
            backend_planning_summary="",  # Run in parallel; alignment via merged graph
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            backend_future = executor.submit(backend_planner.run, backend_input)
            frontend_future = executor.submit(frontend_planner.run, frontend_input)
            backend_output = backend_future.result()
            frontend_output = frontend_future.result()

        merged = PlanningGraph()

        # Seed graph with high-level EPIC/FEATURE nodes derived from architecture components
        if arch and arch.components:
            for comp in arch.components:
                domain = PlanningDomain.BACKEND
                if comp.type in ("frontend", "ui", "client"):
                    domain = PlanningDomain.FRONTEND
                elif comp.type in ("devops", "ci", "cicd"):
                    domain = PlanningDomain.DEVOPS
                elif comp.type in ("database", "data"):
                    domain = PlanningDomain.DATA
                node_id = f"arch-{domain.value}-{comp.name.replace(' ', '-').lower()}"
                merged.add_node(
                    PlanningNode(
                        id=node_id,
                        domain=domain,
                        kind=PlanningNodeKind.FEATURE,
                        summary=f"{comp.name} ({comp.type})",
                        details=comp.description or f"Architecture component {comp.name} of type {comp.type}.",
                        metadata={"component_name": comp.name},
                    )
                )

        merged.merge(backend_output.planning_graph)
        merged.merge(frontend_output.planning_graph)

        # Data planner (optional)
        try:
            from planning_team.data_planning_agent import DataPlanningAgent, DataPlanningInput
            data_planner = DataPlanningAgent(self.llm)
            data_output = data_planner.run(DataPlanningInput(
                requirements=reqs,
                architecture=arch,
                spec_content=input_data.spec_content or "",
                project_overview=project_overview,
            ))
            if data_output.planning_graph.nodes:
                merged.merge(data_output.planning_graph)
        except Exception as e:
            logger.debug("Data planner skipped: %s", e)

        # Test planner
        executable_ids = [
            nid for nid, n in merged.nodes.items()
            if n.kind in (PlanningNodeKind.TASK, PlanningNodeKind.SUBTASK)
            and n.domain in (PlanningDomain.BACKEND, PlanningDomain.FRONTEND)
        ]
        try:
            from planning_team.test_planning_agent import TestPlanningAgent, TestPlanningInput
            test_planner = TestPlanningAgent(self.llm)
            test_output = test_planner.run(TestPlanningInput(
                requirements=reqs,
                architecture=arch,
                spec_content=input_data.spec_content or "",
                project_overview=project_overview,
                existing_task_ids=executable_ids[:15],
            ))
            if test_output.planning_graph.nodes:
                merged.merge(test_output.planning_graph)
        except Exception as e:
            logger.debug("Test planner skipped: %s", e)

        # Performance planner (apply node_budgets to merged nodes)
        try:
            from planning_team.performance_planning_agent import PerformancePlanningAgent, PerformancePlanningInput
            perf_planner = PerformancePlanningAgent(self.llm)
            perf_output = perf_planner.run(PerformancePlanningInput(
                requirements=reqs,
                architecture=arch,
                spec_content=input_data.spec_content or "",
                project_overview=project_overview,
                existing_node_ids=executable_ids[:20],
            ))
            for nid, budget in perf_output.node_budgets.items():
                if nid in merged.nodes:
                    node = merged.nodes[nid]
                    merged.nodes[nid] = node.model_copy(update={"performance_budget": budget})
            if perf_output.planning_graph.nodes:
                merged.merge(perf_output.planning_graph)
        except Exception as e:
            logger.debug("Performance planner skipped: %s", e)

        # Documentation planner
        try:
            from planning_team.documentation_planning_agent import DocumentationPlanningAgent, DocumentationPlanningInput
            doc_planner = DocumentationPlanningAgent(self.llm)
            doc_output = doc_planner.run(DocumentationPlanningInput(
                requirements=reqs,
                architecture=arch,
                spec_content=input_data.spec_content or "",
                project_overview=project_overview,
                existing_task_ids=executable_ids[:10],
            ))
            if doc_output.planning_graph.nodes:
                merged.merge(doc_output.planning_graph)
        except Exception as e:
            logger.debug("Documentation planner skipped: %s", e)

        # Quality gate planner (apply quality_gates to merged nodes)
        try:
            from planning_team.quality_gate_planning_agent import QualityGatePlanningAgent, QualityGatePlanningInput
            qg_planner = QualityGatePlanningAgent(self.llm)
            qg_output = qg_planner.run(QualityGatePlanningInput(
                task_ids=executable_ids,
                project_overview=project_overview,
                delivery_strategy=project_overview.get("delivery_strategy", "") if project_overview else "",
            ))
            for nid, gates in qg_output.node_quality_gates.items():
                if nid in merged.nodes:
                    node = merged.nodes[nid]
                    merged.nodes[nid] = node.model_copy(update={"quality_gates": gates})
        except Exception as e:
            logger.debug("Quality gate planner skipped: %s", e)

        # Ensure git_setup exists when we have backend/frontend tasks
        has_backend = any(n.domain.value == "backend" for n in merged.nodes.values())
        has_frontend = any(n.domain.value == "frontend" for n in merged.nodes.values())
        has_git_setup = any(n.domain == PlanningDomain.GIT_SETUP for n in merged.nodes.values())
        if (has_backend or has_frontend) and not has_git_setup:
            git_node = PlanningNode(
                id="git-setup-repos",
                domain=PlanningDomain.GIT_SETUP,
                kind=PlanningNodeKind.TASK,
                summary="Initialize git repositories for backend and frontend",
                details="Create backend/ and frontend/ directories with git init. Set up development branch.",
                acceptance_criteria=[
                    "Backend repo initialized at work_path/backend",
                    "Frontend repo initialized at work_path/frontend",
                    "Development branch created in both repos",
                ],
            )
            merged.add_node(git_node)
            from planning_team.planning_graph import EdgeType, PlanningEdge
            for nid, node in list(merged.nodes.items()):
                if node.kind in (PlanningNodeKind.TASK, PlanningNodeKind.SUBTASK) and node.domain != PlanningDomain.GIT_SETUP:
                    merged.add_edge(PlanningEdge(from_id="git-setup-repos", to_id=nid, type=EdgeType.BLOCKS))

        assignment = compile_planning_graph_to_task_assignment(
            merged,
            rationale=f"Planning pipeline: {backend_output.summary}; {frontend_output.summary}",
        )

        # Run planning graph validation and build report
        from planning_team.validation import format_validation_report, validate_planning_graph
        is_valid, val_errors = validate_planning_graph(merged, requirement_count=len(reqs.acceptance_criteria or []))
        domain_counts = {}
        for n in merged.nodes.values():
            d = n.domain.value
            domain_counts[d] = domain_counts.get(d, 0) + 1
        validation_report = format_validation_report(
            is_valid, val_errors,
            total_nodes=len(merged.nodes),
            total_edges=len(merged.edges),
            domain_counts=domain_counts,
        )

        mapping = []
        for ac in reqs.acceptance_criteria or []:
            task_ids = [t.id for t in assignment.tasks if ac[:30].lower() in (t.description or "").lower() or (t.title or "").lower() in ac[:30].lower()]
            if not task_ids:
                task_ids = [t.id for t in assignment.tasks[:2]]
            mapping.append({"spec_item": ac[:80], "task_ids": task_ids[:3]})

        is_valid, errors = validate_assignment(assignment, reqs, mapping)
        if not is_valid:
            logger.warning("Tech Lead planning pipeline validation failed: %s", errors[:3])
            return None

        coding_count = sum(1 for t in assignment.tasks if t.type.value in ("backend", "frontend", "devops"))
        if coding_count < 4:
            logger.info("Tech Lead: planning pipeline produced too few coding tasks (%s), falling back to monolithic", coding_count)
            return None

        logger.info("Tech Lead: planning pipeline produced %s tasks", len(assignment.tasks))
        return TechLeadOutput(
            assignment=assignment,
            summary=f"Planning pipeline: {backend_output.summary}. {frontend_output.summary}",
            requirement_task_mapping=mapping,
            spec_clarification_needed=False,
            clarification_questions=[],
            validation_report=validation_report,
        )

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

        # ── Step 1 & 2: Codebase and spec analysis (parallel when both needed) ──
        codebase_analysis = ""
        spec_analysis = ""
        needs_codebase = bool(existing_codebase)
        needs_spec = bool(spec_content)
        if needs_codebase and needs_spec:
            with ThreadPoolExecutor(max_workers=2) as executor:
                cb_future = executor.submit(self._analyze_codebase, existing_codebase)
                spec_future = executor.submit(self._analyze_spec_chunked, spec_content, reqs)
                codebase_analysis = cb_future.result()
                spec_analysis = spec_future.result()
            logger.info(
                "Tech Lead: codebase (%s chars) and spec analysis (%s chars) complete (parallel)",
                len(codebase_analysis),
                len(spec_analysis),
            )
        elif needs_codebase:
            codebase_analysis = self._analyze_codebase(existing_codebase)
            logger.info("Tech Lead: codebase analysis complete (%s chars)", len(codebase_analysis))
        elif needs_spec:
            spec_analysis = self._analyze_spec_chunked(spec_content, reqs)
            logger.info("Tech Lead: spec analysis complete (%s chars)", len(spec_analysis))

        # ── Step 2b: Try planning pipeline (Backend + Frontend planners) unless minimal_planning ──
        if not getattr(input_data, "minimal_planning", False):
            pipeline_output = self._run_planning_pipeline(input_data, codebase_analysis, spec_analysis)
            if pipeline_output is not None:
                return pipeline_output
        else:
            logger.info("Tech Lead: minimal_planning=True, skipping planning pipeline")

        # ── Step 3: Task generation via TaskGeneratorAgent (fallback with model-based limits) ──
        logger.info("Tech Lead: Step 3/3 - Generating task plan via TaskGeneratorAgent")
        from shared.context_sizing import (
            compute_task_generator_existing_chars,
            compute_task_generator_features_chars,
            compute_task_generator_spec_chars,
        )

        po = getattr(input_data, "project_overview", None) or {}
        max_features = compute_task_generator_features_chars(self.llm)
        features_doc_raw = po.get("features_and_functionality_doc") or ""
        features_doc = features_doc_raw[:max_features] + ("..." if len(features_doc_raw) > max_features else "")

        max_spec = compute_task_generator_spec_chars(self.llm)
        max_existing = compute_task_generator_existing_chars(self.llm)
        task_gen = TaskGeneratorAgent(self.llm)
        task_gen_input = TaskGeneratorInput(
            requirements=reqs,
            merged_spec_analysis=spec_analysis,
            codebase_analysis=codebase_analysis,
            spec_content_truncated=spec_content[:max_spec],
            existing_codebase=existing_codebase[:max_existing],
            project_overview=po if po else None,
            features_doc=features_doc,
            architecture=arch,
            alignment_feedback=getattr(input_data, "alignment_feedback", None),
            conformance_issues=getattr(input_data, "conformance_issues", None),
            repo_path=input_data.repo_path or "",
            open_questions=getattr(input_data, "open_questions", None),
            assumptions=getattr(input_data, "assumptions", None),
            resolved_questions=getattr(input_data, "resolved_questions", None),
        )
        data = task_gen.run(task_gen_input)

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

        assignment = parse_assignment_from_data(data)

        # Resilience: when 0 tasks (e.g. LLM returned raw content wrapper), try to recover
        if not assignment.tasks and data.get("content"):
            from shared.llm_response_utils import extract_task_assignment_from_content
            recovered = extract_task_assignment_from_content(data["content"])
            if recovered:
                logger.info("Tech Lead: recovered task assignment from raw content")
                data = recovered
                assignment = parse_assignment_from_data(data)

        # Retry task generation once when we still have 0 tasks
        if not assignment.tasks:
            logger.warning("Tech Lead: got 0 tasks from Task Generator, retrying once")
            data = task_gen.run(task_gen_input)
            if not data.get("spec_clarification_needed"):
                assignment = parse_assignment_from_data(data)
                if not assignment.tasks and data.get("content"):
                    from shared.llm_response_utils import extract_task_assignment_from_content
                    recovered = extract_task_assignment_from_content(data["content"])
                    if recovered:
                        logger.info("Tech Lead: recovered task assignment from raw content on retry")
                        assignment = parse_assignment_from_data(recovered)

        mapping = data.get("requirement_task_mapping") or []
        logger.info("Tech Lead: assigned %s tasks in order %s", len(assignment.tasks), assignment.execution_order)
        return TechLeadOutput(
            assignment=assignment,
            summary=data.get("summary", ""),
            requirement_task_mapping=mapping,
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
            readme_content = (
                readme_file.read_text(encoding="utf-8", errors="replace").strip()
                if readme_file.exists()
                else ""
            )
            # Force docs update when README is missing, empty, or minimal (< 100 chars)
            readme_missing_or_empty = (
                not readme_file.exists() or not readme_content or len(readme_content) < 100
            )
            force_docs_because_readme_empty = (
                readme_missing_or_empty
                and task_update.agent_type in ("backend", "frontend")
            )

            # Ask LLM if docs need updating (unless we already force due to empty README)
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
                logger.info(
                    "Tech Lead: skipping documentation update for task %s (should_update_docs=false, "
                    "README exists=%s, README len=%d chars)",
                    task_update.task_id,
                    readme_file.exists(),
                    len(readme_content),
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
        build_verifier=None,
    ) -> bool:
        """
        Trigger the DevOps agent to add containerization and deployment for the backend repo.
        Writes Dockerfile, CI/CD, etc. into the backend repository.
        When build_verifier is provided, uses run_workflow (plan -> generate -> verify -> fix loop).
        Returns True if run and write succeeded, False otherwise (non-blocking).
        """
        from pathlib import Path
        from devops_agent.models import DevOpsInput, TargetRepo
        from shared.repo_writer import write_agent_output

        path = Path(repo_path).resolve()
        if not (path / ".git").exists():
            logger.warning("Tech Lead: skip DevOps for backend (not a git repo): %s", path)
            return False
        logger.info("Tech Lead: triggering DevOps for backend repo (containerize and deploy)")
        try:
            if build_verifier:
                workflow_result = devops_agent.run_workflow(
                    repo_path=path,
                    task_description="Add containerization and deployment for the backend application. Produce a Dockerfile and CI/CD so this repo can be built and deployed. Backend is Python/FastAPI.",
                    requirements="Dockerfile for Python/FastAPI (pip install, uvicorn). CI/CD: install deps, run tests (pytest), build image. Make repo self-contained for build and deploy.",
                    architecture=architecture,
                    existing_pipeline=existing_pipeline if existing_pipeline and existing_pipeline != "# No code files found" else None,
                    tech_stack=["Python", "FastAPI", "PostgreSQL", "Docker"],
                    target_repo=TargetRepo.BACKEND,
                    build_verifier=build_verifier,
                    task_id="devops-backend",
                    subdir="",
                )
                if workflow_result.success:
                    logger.info("Tech Lead: DevOps for backend completed (workflow)")
                else:
                    logger.warning("Tech Lead: DevOps for backend workflow failed: %s", workflow_result.failure_reason)
                return workflow_result.success
            result = devops_agent.run(DevOpsInput(
                task_description="Add containerization and deployment for the backend application. Produce a Dockerfile and CI/CD so this repo can be built and deployed. Backend is Python/FastAPI.",
                requirements="Dockerfile for Python/FastAPI (pip install, uvicorn). CI/CD: install deps, run tests (pytest), build image. Make repo self-contained for build and deploy.",
                architecture=architecture,
                existing_pipeline=existing_pipeline if existing_pipeline and existing_pipeline != "# No code files found" else None,
                tech_stack=["Python", "FastAPI", "PostgreSQL", "Docker"],
                target_repo=TargetRepo.BACKEND,
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
        build_verifier=None,
    ) -> bool:
        """
        Trigger the DevOps agent to add containerization and deployment for the frontend repo.
        Writes Dockerfile, CI/CD, etc. into the frontend repository.
        When build_verifier is provided, uses run_workflow (plan -> generate -> verify -> fix loop).
        Returns True if run and write succeeded, False otherwise (non-blocking).
        """
        from pathlib import Path
        from devops_agent.models import DevOpsInput, TargetRepo
        from shared.repo_writer import write_agent_output

        path = Path(repo_path).resolve()
        if not (path / ".git").exists():
            logger.warning("Tech Lead: skip DevOps for frontend (not a git repo): %s", path)
            return False
        logger.info("Tech Lead: triggering DevOps for frontend repo (containerize and deploy)")
        try:
            if build_verifier:
                workflow_result = devops_agent.run_workflow(
                    repo_path=path,
                    task_description="Add containerization and deployment for the frontend application. Produce a Dockerfile and CI/CD so this repo can be built and deployed. Frontend is Angular/Node.",
                    requirements="Dockerfile: multi-stage build (npm ci, ng build; serve with nginx or Node). CI/CD: install deps, run tests, build image. Make repo self-contained for build and deploy.",
                    architecture=architecture,
                    existing_pipeline=existing_pipeline if existing_pipeline and existing_pipeline != "# No code files found" else None,
                    tech_stack=["Angular", "Node", "Docker"],
                    target_repo=TargetRepo.FRONTEND,
                    build_verifier=build_verifier,
                    task_id="devops-frontend",
                    subdir="",
                )
                if workflow_result.success:
                    logger.info("Tech Lead: DevOps for frontend completed (workflow)")
                else:
                    logger.warning("Tech Lead: DevOps for frontend workflow failed: %s", workflow_result.failure_reason)
                return workflow_result.success
            result = devops_agent.run(DevOpsInput(
                task_description="Add containerization and deployment for the frontend application. Produce a Dockerfile and CI/CD so this repo can be built and deployed. Frontend is Angular/Node.",
                requirements="Dockerfile: multi-stage build (npm ci, ng build; serve with nginx or Node). CI/CD: install deps, run tests, build image. Make repo self-contained for build and deploy.",
                architecture=architecture,
                existing_pipeline=existing_pipeline if existing_pipeline and existing_pipeline != "# No code files found" else None,
                tech_stack=["Angular", "Node", "Docker"],
                target_repo=TargetRepo.FRONTEND,
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
