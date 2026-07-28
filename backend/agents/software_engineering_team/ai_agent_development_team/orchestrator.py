"""AI Agent Development Team orchestrator."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, Optional

from llm_service import LLMClient
from shared.repo_context import read_repo_code_budgeted
from software_engineering_team.shared.models import Task
from software_engineering_team.shared.team_lead_base import BaseTeamLead

from .models import (
    AIAgentDevelopmentWorkflowResult,
    IntakeResult,
    Phase,
    PlanningResult,
    ToolAgentInput,
    ToolAgentKind,
    ToolAgentOutput,
    WorkflowTraceEvent,
)
from .phases.deliver import run_deliver
from .phases.execution import run_execution
from .phases.intake import run_intake
from .phases.planning import run_planning
from .phases.problem_solving import run_problem_solving
from .phases.review import run_review

logger = logging.getLogger(__name__)
MAX_REVIEW_ITERATIONS = 15


class AIAgentDevelopmentTeamLead(BaseTeamLead):
    """Orchestrates intake -> planning -> execution -> review -> problem-solving -> deliver.

    Intake and planning run as gated phases via ``BaseTeamLead._run_gated_phases``;
    the review/problem-solving retry loop runs via ``BaseTeamLead._run_bounded_retry_loop``.
    ``run_execution``/``run_deliver`` and the artifact-substring review check /
    placeholder-file fix logic itself remain team-specific: this team's phase
    shape (a whole-result review/problem-solving retry loop with no LLM-based
    gates and no git/branch concerns) does not fit the code-v2 teams'
    ``BaseV2DevelopmentAgent`` per-microtask gated state machine, mirroring why
    ``devops_team`` also does not use it. Does not use ``BaseTeamLead``'s
    per-repo briefing cache (:meth:`BaseTeamLead._repo_context_cache_for`), so
    ``__init__`` passes empty extension/exclude-dir sets and a zero char budget
    for that unused feature.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        BaseTeamLead.__init__(
            self,
            llm_client,
            extensions=frozenset(),
            exclude_dirs=frozenset(),
            max_chars=0,
        )

    def _build_tool_runners(
        self,
    ) -> Dict[ToolAgentKind, Callable[[ToolAgentInput], ToolAgentOutput]]:
        from .tool_agents.agent_runtime import AgentRuntimeToolAgent
        from .tool_agents.evaluation_harness import EvaluationHarnessToolAgent
        from .tool_agents.mcp_server_connectivity import MCPServerConnectivityToolAgent
        from .tool_agents.memory_rag import MemoryRagToolAgent
        from .tool_agents.prompt_engineering import PromptEngineeringToolAgent
        from .tool_agents.safety_governance import SafetyGovernanceToolAgent

        prompt = PromptEngineeringToolAgent(self.llm)
        memory = MemoryRagToolAgent(self.llm)
        safety = SafetyGovernanceToolAgent(self.llm)
        eval_h = EvaluationHarnessToolAgent(self.llm)
        runtime = AgentRuntimeToolAgent(self.llm)
        mcp = MCPServerConnectivityToolAgent(self.llm)

        return {
            ToolAgentKind.GENERAL: prompt.run,
            ToolAgentKind.PROMPT_ENGINEERING: prompt.run,
            ToolAgentKind.MEMORY_RAG: memory.run,
            ToolAgentKind.SAFETY_GOVERNANCE: safety.run,
            ToolAgentKind.EVALUATION_HARNESS: eval_h.run,
            ToolAgentKind.AGENT_RUNTIME: runtime.run,
            ToolAgentKind.MCP_SERVER_CONNECTIVITY: mcp.run,
        }

    @staticmethod
    def _read_repo_code(repo_path: Path, max_chars: int = 20_000) -> str:
        """Read source files into a single string via the shared budgeted scanner."""
        return read_repo_code_budgeted(
            repo_path,
            extensions={".py", ".md", ".yaml", ".yml", ".json", ".toml"},
            exclude_dirs={".git", "node_modules", "__pycache__", ".venv", "venv"},
            max_chars=max_chars,
            empty="",
        )

    def run_workflow(
        self,
        *,
        repo_path: Path,
        task: Task,
        spec_content: str = "",
        job_updater: Optional[Callable[..., None]] = None,
    ) -> AIAgentDevelopmentWorkflowResult:
        result = AIAgentDevelopmentWorkflowResult(task_id=task.id, current_phase=Phase.INTAKE)

        def _trace(phase: Phase, message: str) -> None:
            result.trace.append(WorkflowTraceEvent(phase=phase, message=message))
            if job_updater:
                try:
                    job_updater(task_id=task.id, phase=phase.value, message=message)
                except Exception:
                    logger.debug("job_updater failed", exc_info=True)

        logger.info("[%s] AI Agent Development workflow started", task.id)

        # Phase outputs shared with the still-hand-rolled execution/review/deliver
        # tail below (set by the gated phase callables).
        intake: Optional[IntakeResult] = None
        planning: Optional[PlanningResult] = None

        def _phase_intake() -> Optional[AIAgentDevelopmentWorkflowResult]:
            """Intake gate: normalize mission/constraints via run_intake.

            Preconditions: none (first phase).
            Postconditions: sets nonlocal ``intake`` and ``result.intake_result``;
              always returns None today (run_intake has no soft-failure branch —
              exceptions propagate to run_workflow's outer try/except unchanged).
            """
            nonlocal intake
            _trace(Phase.INTAKE, "Starting intake")
            intake = run_intake(llm=self.llm, task=task, spec_content=spec_content)
            result.intake_result = intake
            return None

        def _phase_planning() -> Optional[AIAgentDevelopmentWorkflowResult]:
            """Planning gate: derive microtasks via run_planning.

            Preconditions: ``_phase_intake`` has run (``intake`` is set).
            Postconditions: sets nonlocal ``planning`` and ``result.planning_result``;
              always returns None today (see _phase_intake docstring).
            """
            nonlocal planning
            result.current_phase = Phase.PLANNING
            _trace(Phase.PLANNING, "Planning microtasks")
            planning = run_planning(
                llm=self.llm, task=task, intake_result=intake, spec_content=spec_content
            )
            result.planning_result = planning
            return None

        try:
            gate_failure = self._run_gated_phases([_phase_intake, _phase_planning])
            if gate_failure is not None:
                return gate_failure

            result.current_phase = Phase.EXECUTION
            _trace(Phase.EXECUTION, "Executing microtasks")
            execution = run_execution(
                planning_result=planning,
                repo_path=str(repo_path),
                spec_context=spec_content,
                existing_code=self._read_repo_code(repo_path),
                tool_runners=self._build_tool_runners(),
            )
            result.execution_result = execution

            def _attempt_review_cycle(i: int):
                """One review/problem-solving iteration for ``_run_bounded_retry_loop``.

                Preconditions: ``execution`` (nonlocal) holds the current
                  ``ExecutionResult``; ``i`` is the 0-based iteration index
                  supplied by ``_run_bounded_retry_loop``.
                Postconditions: always sets ``result.review_result`` and
                  ``result.iterations_used`` (1-based). Returns the passing
                  ``ReviewResult`` when review passes; returns a non-passing
                  ``ReviewResult`` when problem-solving resolved the issues
                  (and mutates ``execution.files``/``.summary`` in place so
                  the next iteration re-reviews the patched files); returns
                  ``None`` (abort signal) when problem-solving could not
                  resolve the issues, after populating ``result.failure_reason``,
                  ``result.summary``, and ``result.needs_followup``.
                """
                iteration = i + 1
                result.current_phase = Phase.REVIEW
                result.iterations_used = iteration
                _trace(Phase.REVIEW, f"Review iteration {iteration}")
                review = run_review(execution_result=execution)
                result.review_result = review

                if review.passed:
                    return review

                result.current_phase = Phase.PROBLEM_SOLVING
                _trace(Phase.PROBLEM_SOLVING, f"Problem-solving iteration {iteration}")
                problem_solving = run_problem_solving(
                    execution_result=execution, review_result=review
                )
                result.problem_solving_result = problem_solving

                if not problem_solving.resolved:
                    result.failure_reason = "Review failed and no deterministic fix was available."
                    result.summary = review.summary
                    result.needs_followup = True
                    return None

                execution.files = problem_solving.files
                execution.summary = f"{execution.summary} | {problem_solving.summary}"
                return review

            succeeded, _final_review = self._run_bounded_retry_loop(
                max_iterations=MAX_REVIEW_ITERATIONS,
                attempt=_attempt_review_cycle,
                is_success=lambda r: r.passed,
            )
            # Reflect any problem-solving patches applied during the loop
            # (including on abort/exhausted paths, where partial progress is
            # still useful to callers inspecting final_files).
            result.final_files = execution.files
            if not succeeded:
                if result.needs_followup:
                    # Abort path: _attempt_review_cycle already populated
                    # failure_reason/summary/needs_followup before returning None.
                    return result
                result.failure_reason = "Review did not pass after max iterations."
                result.summary = (
                    result.review_result.summary if result.review_result else "Review failed"
                )
                result.needs_followup = True
                return result

            result.current_phase = Phase.DELIVER
            _trace(Phase.DELIVER, "Preparing handoff package")
            deliver = run_deliver(
                llm=self.llm, execution_result=execution, review_result=result.review_result
            )
            result.deliver_result = deliver
            result.success = True
            result.summary = deliver.summary or "AI agent system blueprint generated."
            return result
        except Exception as exc:
            logger.exception("[%s] AI Agent Development workflow failed", task.id)
            result.failure_reason = str(exc)
            result.summary = "Workflow failed."
            result.needs_followup = True
            return result
