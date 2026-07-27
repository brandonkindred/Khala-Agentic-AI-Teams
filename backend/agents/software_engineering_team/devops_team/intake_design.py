"""DevOps team Phase 1 (intake/clarification) and Phase 2 (design fan-out).

Split out of ``orchestrator.py``'s ``_run_pipeline`` nested closures so the
task-clarification gate and the 3-way parallel design fan-out (IaC + CI/CD +
deployment strategy) live in a dedicated module instead of nonlocal-capturing
closures. Phase 2's result is returned as an explicit ``DesignFanOutState``
object rather than mutated via ``nonlocal``. Functions here take the owning
``DevOpsTeamLeadAgent`` instance (duck-typed as ``agent``) so they can reach
its constructed specialist agents without each becoming a class method — same
pattern as ``tool_dispatch.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from llm_service import DummyLLMClient

from .models import DevOpsTaskSpec, DevOpsTeamResult
from .phase2_graph import run_phase2_parallel
from .task_clarifier import DevOpsTaskClarifierInput
from .tool_agents import RepoNavigatorInput

logger = logging.getLogger(__name__)


def run_intake_clarification(agent: Any, task_spec: DevOpsTaskSpec) -> Optional[DevOpsTeamResult]:
    """Phase 1: environment policy + task clarification gates.

    Preconditions:
        - ``agent`` duck-types ``DevOpsTeamLeadAgent``: it exposes
          ``_enforce_env_policy`` (staticmethod), ``task_clarifier``, and
          ``_build_subtask_contracts`` (staticmethod).
        - ``task_spec`` is the pipeline's validated input for this run.

    Postconditions:
        - Returns a failed ``DevOpsTeamResult`` when the environment policy is
          violated or the task clarifier withholds execution approval.
        - Otherwise builds and logs the subtask contracts (parity with the
          pre-extraction behavior) and returns ``None`` so later phases run.
    """
    env_block = agent._enforce_env_policy(task_spec)
    if env_block:
        return DevOpsTeamResult(
            success=False, failure_reason=f"Environment policy violation: {env_block}"
        )

    clarifier = agent.task_clarifier.run(DevOpsTaskClarifierInput(task_spec=task_spec))
    if not clarifier.approved_for_execution:
        return DevOpsTeamResult(
            success=False,
            failure_reason="Clarification required: "
            + "; ".join(clarifier.clarification_requests[:3]),
        )

    subtask_contracts = agent._build_subtask_contracts(task_spec)
    logger.info("DevOps team pipeline: %d subtask contracts generated", len(subtask_contracts))
    return None


@dataclass
class DesignFanOutState:
    """Explicit state produced by Phase 2's 3-way parallel design fan-out.

    Invariants:
        - ``aggregated_artifacts`` is the union of the three agents'
          ``artifacts`` mappings, as merged by ``run_phase2_parallel``.
    """

    iac_result: Any = None
    cicd_result: Any = None
    deploy_result: Any = None
    aggregated_artifacts: Dict[str, str] = field(default_factory=dict)


def run_design_fan_out(agent: Any, task_spec: DevOpsTaskSpec, repo_path: Path) -> DesignFanOutState:
    """Phase 2: change design / implementation (3-way parallel fan-out).

    Preconditions:
        - ``agent`` duck-types ``DevOpsTeamLeadAgent``: it exposes
          ``_report_status``, ``repo_navigator_tool``, ``llm``, ``iac_agent``,
          ``cicd_agent``, and ``deployment_agent``.
        - Phase 1 (:func:`run_intake_clarification`) returned ``None``.

    Postconditions:
        - Returns a ``DesignFanOutState`` carrying ``iac_result``,
          ``cicd_result``, ``deploy_result``, and the merged
          ``aggregated_artifacts`` from the parallel fan-out. Never returns
          ``None`` — this phase has no early-exit gate today.
    """
    agent._report_status(
        "phase2",
        detail="DevOps team pipeline: phase 2 - change design (parallel)",
    )
    repo_summary = agent.repo_navigator_tool.run(
        RepoNavigatorInput(repo_path=str(repo_path))
    ).summary
    # Enable parallel execution unless the backing LLM client is a
    # DummyLLMClient (or subclass) — scripted test clients use a shared
    # sequential response list that breaks under concurrent access.
    use_parallel = not isinstance(agent.llm, DummyLLMClient)
    phase2 = run_phase2_parallel(
        agent.iac_agent,
        agent.cicd_agent,
        agent.deployment_agent,
        task_spec,
        repo_summary=repo_summary,
        parallel=use_parallel,
    )
    return DesignFanOutState(
        iac_result=phase2["iac_result"],
        cicd_result=phase2["cicd_result"],
        deploy_result=phase2["deploy_result"],
        aggregated_artifacts=phase2["aggregated_artifacts"],
    )
