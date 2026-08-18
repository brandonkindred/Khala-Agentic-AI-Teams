"""AI Agent Development Team orchestrator."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, Optional

from llm_service import LLMClient
from shared.dev_models.models import Task
from software_engineering_team.shared.team_lead_base import BaseTeamLead

from .models import (
    AIAgentDevelopmentWorkflowResult,
    ExecutionResult,
    IntakeResult,
    Phase,
    PlanningResult,
    ReviewResult,
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
from .tool_agents.agent_runtime import AgentRuntimeToolAgent
from .tool_agents.evaluation_harness import EvaluationHarnessToolAgent
from .tool_agents.mcp_server_connectivity import MCPServerConnectivityToolAgent
from .tool_agents.memory_rag import MemoryRagToolAgent
from .tool_agents.prompt_engineering import PromptEngineeringToolAgent
from .tool_agents.safety_governance import SafetyGovernanceToolAgent

logger = logging.getLogger(__name__)
# Default bound for the review/problem-solving gate. Kept as a module constant
# (not env/settings) so tests can assert exhaustion against a stable value;
# subclasses may override via the class attribute ``max_review_iterations``
# (which defaults to this module constant).
MAX_REVIEW_ITERATIONS = 15
REPO_EXTENSIONS = frozenset({".py", ".md", ".yaml", ".yml", ".json", ".toml"})
REPO_EXCLUDE_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv"})
REPO_MAX_CHARS = 20_000


class AIAgentDevelopmentTeamLead(BaseTeamLead):
    """Orchestrates intake -> planning -> execution -> review -> problem-solving -> deliver.

    Five gated phases are sequenced via ``BaseTeamLead._run_gated_phases``
    (mirroring ``devops_team``): intake, planning, execution, review/fix, and
    deliver. Review and problem-solving are combined into a single gated phase
    that uses ``BaseTeamLead._run_bounded_retry_loop`` internally. Phase
    callables (``run_intake`` / ``run_planning`` / ``run_execution`` /
    ``run_review`` / ``run_problem_solving`` / ``run_deliver``) and the
    artifact-substring review check / placeholder-file fix logic itself remain
    team-specific: this team's phase shape (a whole-result review/problem-
    solving retry loop with no LLM-based gates and no git/branch concerns)
    does not fit the code-v2 teams' ``BaseV2DevelopmentAgent`` per-microtask
    gated state machine, mirroring why ``devops_team`` also does not use it.
    Reuses :meth:`BaseTeamLead._repo_context_cache_for` with the briefing
    contract above so multi-task jobs re-read only changed files.
    """

    max_review_iterations = MAX_REVIEW_ITERATIONS

    def __init__(self, llm_client: LLMClient) -> None:
        BaseTeamLead.__init__(
            self,
            llm_client,
            extensions=REPO_EXTENSIONS,
            exclude_dirs=REPO_EXCLUDE_DIRS,
            max_chars=REPO_MAX_CHARS,
        )

    def _build_tool_runners(
        self,
    ) -> Dict[ToolAgentKind, Callable[[ToolAgentInput], ToolAgentOutput]]:
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

    def _read_repo_code(self, repo_path: Path) -> str:
        """Return the budgeted repo briefing via the per-repo incremental cache.

        Preconditions: ``repo_path`` is an existing directory.
        Postconditions: returns the same briefing ``RepoContextCache.read`` would for
          this team's extensions / exclude dirs / char budget; the cache instance for
          ``repo_path`` is reused across calls on this lead.
        """
        return self._repo_context_cache_for(repo_path).read(repo_path)

    def run_workflow(
        self,
        *,
        repo_path: Path,
        task: Task,
        spec_content: str = "",
        job_updater: Optional[Callable[..., None]] = None,
    ) -> AIAgentDevelopmentWorkflowResult:
        """Run the full AI-agent development workflow for a task.

        Preconditions: ``task.id`` is set; ``repo_path`` points to an existing
          repository root when execution needs to read source files.
        Postconditions: returns an ``AIAgentDevelopmentWorkflowResult`` whose
          ``success``, ``current_phase``, ``failure_reason``, ``summary``, and
          phase results are populated. Trace events are appended to
          ``result.trace`` and, when ``job_updater`` is provided, forwarded to it.
          Exceptions are caught and recorded in the result with
          ``needs_followup=True``.
        """
        result = AIAgentDevelopmentWorkflowResult(task_id=task.id, current_phase=Phase.INTAKE)

        def _trace(phase: Phase, message: str) -> None:
            result.trace.append(WorkflowTraceEvent(phase=phase, message=message))
            if job_updater:
                try:
                    job_updater(task_id=task.id, phase=phase.value, message=message)
                except Exception:
                    logger.debug("job_updater failed", exc_info=True)

        logger.info("[%s] AI Agent Development workflow started", task.id)

        # Phase outputs shared across gated-phase closures (set by earlier gates).
        intake: Optional[IntakeResult] = None
        planning: Optional[PlanningResult] = None
        execution: Optional[ExecutionResult] = None

        def _phase_intake() -> Optional[AIAgentDevelopmentWorkflowResult]:
            """Intake gate: normalize mission/constraints via run_intake.

            Preconditions: none (first phase).
            Postconditions: sets ``result.current_phase`` to ``Phase.INTAKE``,
              nonlocal ``intake``, and ``result.intake_result``; always returns
              None today (run_intake has no soft-failure branch — exceptions
              propagate to run_workflow's outer try/except unchanged).
            """
            nonlocal intake
            result.current_phase = Phase.INTAKE
            _trace(Phase.INTAKE, "Starting intake")
            intake = run_intake(llm=self.llm, task=task, spec_content=spec_content)
            result.intake_result = intake
            return None

        def _phase_planning() -> Optional[AIAgentDevelopmentWorkflowResult]:
            """Planning gate: derive microtasks via run_planning.

            Preconditions: ``_phase_intake`` has run (``intake`` is set).
            Postconditions: sets nonlocal ``planning`` and ``result.planning_result``;
              always returns None because ``run_planning`` has no soft-failure
              branch and exceptions propagate to ``run_workflow``'s outer
              try/except.
            """
            nonlocal planning
            result.current_phase = Phase.PLANNING
            _trace(Phase.PLANNING, "Planning microtasks")
            planning = run_planning(
                llm=self.llm, task=task, intake_result=intake, spec_content=spec_content
            )
            result.planning_result = planning
            return None

        def _phase_execution() -> Optional[AIAgentDevelopmentWorkflowResult]:
            """Execution gate: run planned microtasks via run_execution.

            Preconditions: ``_phase_planning`` has run (``planning`` is set).
            Postconditions: sets nonlocal ``execution`` and
              ``result.execution_result``; always returns None today
              (``run_execution`` has no soft-failure branch — exceptions
              propagate to ``run_workflow``'s outer try/except).
            """
            nonlocal execution
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
            return None

        def _phase_review_fix() -> Optional[AIAgentDevelopmentWorkflowResult]:
            """Review/problem-solving gate via ``_run_bounded_retry_loop``.

            Preconditions: ``_phase_execution`` has run (``execution`` is set).
            Postconditions: always sets ``result.final_files`` to the last
              attempted file set (including problem-solving patches). On review
              success returns None so deliver runs. On abort or exhausted
              retries, populates ``failure_reason``/``summary``/``needs_followup``
              (abort path may already have set them) and returns ``result``.
            """
            assert execution is not None, "execution must be set by _phase_execution"

            def _attempt_review_cycle(i: int) -> Optional[ReviewResult]:
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
                  ``None`` (abort signal for ``_run_bounded_retry_loop``; the
                  helper only invokes ``is_success`` on non-``None`` values)
                  when problem-solving could not resolve the issues, after
                  populating ``result.failure_reason``, ``result.summary``, and
                  ``result.needs_followup``.
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
                # Intentionally return the pre-fix (non-passing) review: the
                # helper treats ``r.passed`` as False and continues, so the next
                # iteration re-runs ``run_review`` against the patched files.
                return review

            # ``None`` from attempt is the explicit abort signal;
            # ``is_success`` is only invoked on non-``None`` values.
            succeeded, _final_review = self._run_bounded_retry_loop(
                max_iterations=self.max_review_iterations,
                attempt=_attempt_review_cycle,
                is_success=lambda r: r.passed,
            )
            # Last attempted file set (see ``final_files`` Field description):
            # includes problem-solving patches even on abort/exhausted paths.
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
            return None

        def _phase_deliver() -> Optional[AIAgentDevelopmentWorkflowResult]:
            """Deliver gate: package handoff summary via run_deliver.

            Preconditions: ``_phase_review_fix`` returned None (review passed);
              ``execution`` and ``result.review_result`` are set.
            Postconditions: sets ``result.deliver_result``, ``result.success``,
              and ``result.summary``; always returns None today
              (``run_deliver`` has no soft-failure branch — exceptions
              propagate to ``run_workflow``'s outer try/except).
            """
            assert execution is not None, "execution must be set by _phase_execution"
            assert result.review_result is not None, "review_result must be set"

            result.current_phase = Phase.DELIVER
            _trace(Phase.DELIVER, "Preparing handoff package")
            deliver = run_deliver(
                llm=self.llm, execution_result=execution, review_result=result.review_result
            )
            result.deliver_result = deliver
            result.success = True
            result.summary = deliver.summary or "AI agent system blueprint generated."
            return None

        try:
            gate_failure = self._run_gated_phases(
                [
                    _phase_intake,
                    _phase_planning,
                    _phase_execution,
                    _phase_review_fix,
                    _phase_deliver,
                ]
            )
            if gate_failure is not None:
                return gate_failure
            return result
        except Exception as exc:
            logger.exception("[%s] AI Agent Development workflow failed", task.id)
            result.failure_reason = str(exc)
            result.summary = "Workflow failed."
            result.needs_followup = True
            return result
