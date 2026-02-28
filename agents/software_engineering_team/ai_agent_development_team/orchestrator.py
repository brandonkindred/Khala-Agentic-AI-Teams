"""AI Agent Development Team orchestrator."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, Optional

from shared.llm import LLMClient
from shared.models import Task

from .models import (
    AIAgentDevelopmentWorkflowResult,
    Phase,
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
MAX_REVIEW_ITERATIONS = 3


class AIAgentDevelopmentTeamLead:
    """Orchestrates intake -> planning -> execution -> review -> problem-solving -> deliver."""

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm = llm_client

    def _build_tool_runners(self) -> Dict[ToolAgentKind, Callable[[ToolAgentInput], ToolAgentOutput]]:
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
        exts = {".py", ".md", ".yaml", ".yml", ".json", ".toml"}
        chunks = []
        total = 0
        for file_path in sorted(repo_path.rglob("*")):
            if not file_path.is_file() or file_path.suffix not in exts:
                continue
            if any(skip in file_path.parts for skip in (".git", "node_modules", "__pycache__", ".venv", "venv")):
                continue
            text = file_path.read_text(encoding="utf-8", errors="replace")
            chunk = f"--- {file_path.relative_to(repo_path)} ---\n{text}\n"
            if total + len(chunk) > max_chars:
                break
            chunks.append(chunk)
            total += len(chunk)
        return "\n".join(chunks)

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

        try:
            _trace(Phase.INTAKE, "Starting intake")
            intake = run_intake(llm=self.llm, task=task, spec_content=spec_content)
            result.intake_result = intake

            result.current_phase = Phase.PLANNING
            _trace(Phase.PLANNING, "Planning microtasks")
            planning = run_planning(llm=self.llm, task=task, intake_result=intake, spec_content=spec_content)
            result.planning_result = planning

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
            result.final_files = execution.files

            for i in range(1, MAX_REVIEW_ITERATIONS + 1):
                result.current_phase = Phase.REVIEW
                result.iterations_used = i
                _trace(Phase.REVIEW, f"Review iteration {i}")
                review = run_review(execution_result=execution)
                result.review_result = review

                if review.passed:
                    break

                result.current_phase = Phase.PROBLEM_SOLVING
                _trace(Phase.PROBLEM_SOLVING, f"Problem-solving iteration {i}")
                problem_solving = run_problem_solving(execution_result=execution, review_result=review)
                result.problem_solving_result = problem_solving

                if not problem_solving.resolved:
                    result.failure_reason = "Review failed and no deterministic fix was available."
                    result.summary = review.summary
                    result.needs_followup = True
                    return result

                execution.files = problem_solving.files
                execution.summary = f"{execution.summary} | {problem_solving.summary}"

            if not result.review_result or not result.review_result.passed:
                result.failure_reason = "Review did not pass after max iterations."
                result.summary = result.review_result.summary if result.review_result else "Review failed"
                result.needs_followup = True
                return result

            result.current_phase = Phase.DELIVER
            _trace(Phase.DELIVER, "Preparing handoff package")
            deliver = run_deliver(llm=self.llm, execution_result=execution, review_result=result.review_result)
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
