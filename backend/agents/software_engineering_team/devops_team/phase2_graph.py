"""Phase 2 Change Design — parallel fan-out.

Replaces the sequential ``iac_agent.run → cicd_agent.run → deployment_agent.run``
calls in ``DevOpsTeamLeadAgent._run_pipeline`` with concurrent execution via
``shared.concurrency.parallel_map``. All three design agents produce
disjoint artifact files and have no cross-dependencies, so they can run
concurrently. Wall-clock latency drops from ``sum(iac, cicd, deploy)`` to
``max(iac, cicd, deploy)``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from shared.concurrency import parallel_map

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

    When ``parallel=True`` (the default), the three independent agents run
    simultaneously via ``shared.concurrency.parallel_map``, reducing
    wall-clock latency from ``sum(iac, cicd, deploy)`` to
    ``max(iac, cicd, deploy)``.

    Set ``parallel=False`` for deterministic execution order — this is
    needed when the backing LLM client is a ``_ScriptedClient`` with a
    shared sequential response list, because concurrent execution would
    cause agents to consume responses non-deterministically.

    Failure policy — deliberately silent-default, not fast-fail: this
    function, not the individual agents, owns catch/fallback policy for
    the three Phase 2 design agents (see
    ``docs/LLM_CALLING_PATTERN_DECISION.md`` Pattern 3 and
    ``docs/LLM_CALLING_PATTERNS_AUDIT.md``). Each agent's ``DevOpsSingleShotAgent``
    base intentionally lets LLM/parse errors propagate unchanged out of
    ``run()``, on the assumption the caller handles them. ``parallel_map``
    itself is fast-fail (an unhandled exception aborts the whole batch), so
    each agent call below is individually wrapped to catch its own
    ``Exception``, log a warning, and degrade to ``None`` instead of
    propagating — one agent failing must not abort the other two or the
    aggregation step. A ``None`` result is then replaced with that agent's
    typed empty-summary default below.

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

    def _guarded(named_fn: tuple[str, Callable[[], Any]]) -> Optional[Any]:
        agent_name, fn = named_fn
        try:
            return fn()
        except Exception as exc:
            logger.warning("DevOps Phase 2: %s agent failed: %s", agent_name, exc)
            return None

    if parallel:  # pragma: no cover  # integration-only: parallel_map fan-out
        tasks = [("iac", _run_iac), ("cicd", _run_cicd), ("deploy", _run_deploy)]
        iac_result, cicd_result, deploy_result = parallel_map(
            tasks, _guarded, max_workers=3, skip_none=False
        )
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
