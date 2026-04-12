"""Phase 2 Change Design — parallel fan-out.

Replaces the sequential ``iac_agent.run → cicd_agent.run → deployment_agent.run``
calls in ``DevOpsTeamLeadAgent._run_pipeline`` with concurrent execution via
``concurrent.futures.ThreadPoolExecutor``. All three design agents produce
disjoint artifact files and have no cross-dependencies, so they can run
concurrently. Wall-clock latency drops from ``sum(iac, cicd, deploy)`` to
``max(iac, cicd, deploy)``.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Optional

from .cicd_pipeline_agent import CICDPipelineAgent, CICDPipelineAgentInput
from .cicd_pipeline_agent.models import CICDPipelineAgentOutput
from .deployment_strategy_agent import DeploymentStrategyAgent, DeploymentStrategyAgentInput
from .deployment_strategy_agent.models import DeploymentStrategyAgentOutput
from .iac_agent import IaCAgentInput, InfrastructureAsCodeAgent
from .iac_agent.models import IaCAgentOutput
from .models import DevOpsTaskSpec

logger = logging.getLogger(__name__)


def run_phase2_parallel(
    iac_agent: InfrastructureAsCodeAgent,
    cicd_agent: CICDPipelineAgent,
    deployment_agent: DeploymentStrategyAgent,
    task_spec: DevOpsTaskSpec,
    repo_summary: str = "",
    *,
    parallel: bool = True,
) -> Dict[str, Any]:
    """Run Phase 2 design agents, returning merged artifacts.

    When ``parallel=True`` (the default), the three independent agents
    run simultaneously in a ``ThreadPoolExecutor``, reducing wall-clock
    latency from ``sum(iac, cicd, deploy)`` to ``max(iac, cicd, deploy)``.

    Set ``parallel=False`` for deterministic execution order — this is
    needed when the backing LLM client is a ``_ScriptedClient`` with a
    shared sequential response list, because the thread pool would cause
    agents to consume responses non-deterministically.

    Args:
        iac_agent: The IaC agent instance (from the orchestrator).
        cicd_agent: The CI/CD pipeline agent instance.
        deployment_agent: The deployment strategy agent instance.
        task_spec: The validated DevOps task specification.
        repo_summary: Summary of the repo structure from the repo navigator tool.
        parallel: If True, run agents concurrently; if False, sequentially.

    Returns:
        Dict with keys:
        - ``aggregated_artifacts``: merged file dict from all 3 agents.
        - ``iac_result``: ``IaCAgentOutput`` (with empty defaults on failure).
        - ``cicd_result``: ``CICDPipelineAgentOutput``.
        - ``deploy_result``: ``DeploymentStrategyAgentOutput``.
    """
    logger.info(
        "DevOps Phase 2: starting parallel fan-out for task %s",
        task_spec.task_id,
    )

    iac_result: Optional[IaCAgentOutput] = None
    cicd_result: Optional[CICDPipelineAgentOutput] = None
    deploy_result: Optional[DeploymentStrategyAgentOutput] = None

    def _run_iac() -> IaCAgentOutput:
        return iac_agent.run(IaCAgentInput(task_spec=task_spec, repo_summary=repo_summary))

    def _run_cicd() -> CICDPipelineAgentOutput:
        return cicd_agent.run(CICDPipelineAgentInput(task_spec=task_spec))

    def _run_deploy() -> DeploymentStrategyAgentOutput:
        return deployment_agent.run(DeploymentStrategyAgentInput(task_spec=task_spec))

    if parallel:
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="devops_phase2") as pool:
            futures = {
                pool.submit(_run_iac): "iac",
                pool.submit(_run_cicd): "cicd",
                pool.submit(_run_deploy): "deploy",
            }
            for future in as_completed(futures):
                agent_name = futures[future]
                try:
                    result = future.result()
                    if agent_name == "iac":
                        iac_result = result
                    elif agent_name == "cicd":
                        cicd_result = result
                    elif agent_name == "deploy":
                        deploy_result = result
                except Exception as exc:
                    logger.warning("DevOps Phase 2: %s agent failed: %s", agent_name, exc)
    else:
        # Sequential fallback — deterministic ordering for scripted test clients.
        try:
            iac_result = _run_iac()
        except Exception as exc:
            logger.warning("DevOps Phase 2: iac agent failed: %s", exc)
        try:
            cicd_result = _run_cicd()
        except Exception as exc:
            logger.warning("DevOps Phase 2: cicd agent failed: %s", exc)
        try:
            deploy_result = _run_deploy()
        except Exception as exc:
            logger.warning("DevOps Phase 2: deploy agent failed: %s", exc)

    # Build defaults for any agents that failed
    if iac_result is None:
        iac_result = IaCAgentOutput(summary="IaC agent failed during Phase 2")
    if cicd_result is None:
        cicd_result = CICDPipelineAgentOutput(summary="CICD agent failed during Phase 2")
    if deploy_result is None:
        deploy_result = DeploymentStrategyAgentOutput(
            summary="Deployment agent failed during Phase 2"
        )

    aggregated: Dict[str, str] = {}
    aggregated.update(iac_result.artifacts)
    aggregated.update(cicd_result.artifacts)
    aggregated.update(deploy_result.artifacts)

    logger.info(
        "DevOps Phase 2: completed — %d artifacts, iac=%s cicd=%s deploy=%s",
        len(aggregated),
        bool(iac_result.artifacts),
        bool(cicd_result.artifacts),
        bool(deploy_result.artifacts),
    )

    return {
        "aggregated_artifacts": aggregated,
        "iac_result": iac_result,
        "cicd_result": cicd_result,
        "deploy_result": deploy_result,
    }
