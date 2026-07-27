"""Phase 2: change design / implementation (3-way parallel fan-out, devops_team)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from ..cicd_pipeline_agent import CICDPipelineAgent
from ..cicd_pipeline_agent.models import CICDPipelineAgentOutput
from ..deployment_strategy_agent import DeploymentStrategyAgent
from ..deployment_strategy_agent.models import DeploymentStrategyAgentOutput
from ..iac_agent import InfrastructureAsCodeAgent
from ..iac_agent.models import IaCAgentOutput
from ..models import DevOpsTaskSpec
from ..phase2_graph import run_phase2_parallel
from ..tool_agents import RepoNavigatorInput, RepoNavigatorToolAgent


@dataclass(frozen=True)
class Phase2DesignResult:
    """Outcome of Phase 2 (3-way parallel design fan-out)."""

    iac_result: IaCAgentOutput
    cicd_result: CICDPipelineAgentOutput
    deploy_result: DeploymentStrategyAgentOutput
    aggregated_artifacts: Dict[str, str]


def run_phase2_design_fanout(
    *,
    task_spec: DevOpsTaskSpec,
    repo_path: Path,
    iac_agent: InfrastructureAsCodeAgent,
    cicd_agent: CICDPipelineAgent,
    deployment_agent: DeploymentStrategyAgent,
    repo_navigator_tool: RepoNavigatorToolAgent,
    parallel: bool,
) -> Phase2DesignResult:
    """Resolve the repo summary, then run the 3-way parallel design fan-out.

    Preconditions: Phase 1 returned a non-blocked result; ``repo_path`` is an
      existing repo directory; ``parallel`` is precomputed by the caller (this
      function does not decide it, since that decision depends on the
      caller's LLM client).
    Postconditions: returns a ``Phase2DesignResult`` with ``iac_result``,
      ``cicd_result``, ``deploy_result``, and ``aggregated_artifacts``
      populated exactly as ``run_phase2_parallel`` produces them.
    """
    repo_summary = repo_navigator_tool.run(RepoNavigatorInput(repo_path=str(repo_path))).summary
    phase2 = run_phase2_parallel(
        iac_agent,
        cicd_agent,
        deployment_agent,
        task_spec,
        repo_summary=repo_summary,
        parallel=parallel,
    )
    return Phase2DesignResult(
        iac_result=phase2["iac_result"],
        cicd_result=phase2["cicd_result"],
        deploy_result=phase2["deploy_result"],
        aggregated_artifacts=phase2["aggregated_artifacts"],
    )
