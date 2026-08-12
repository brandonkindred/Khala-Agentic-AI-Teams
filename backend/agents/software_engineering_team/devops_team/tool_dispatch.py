"""DevOps team tool-dispatch helpers (execution + validation tools).

Split out of ``orchestrator.py`` to separate low-level tool invocation from
pipeline coordination. Functions here take the owning ``DevOpsTeamLeadAgent``
instance (duck-typed as ``agent``) so they can reach its constructed tool
agents without each becoming a class method.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple

from shared.concurrency import parallel_map
from software_engineering_team.shared.security_service import run_policy_scan

from .tool_agents import (
    CDKExecutionInput,
    CICDLintInput,
    DeploymentDryRunInput,
    DockerComposeExecutionInput,
    HelmExecutionInput,
    IaCValidationInput,
    TerraformExecutionInput,
)


def run_execution_tools(
    agent: Any, repo_str: str, artifacts: Dict[str, str]
) -> List[Dict[str, Any]]:
    """Run applicable execution tools and return list of result dicts."""
    results: List[Dict[str, Any]] = []
    has_tf = any(k.endswith(".tf") for k in artifacts)
    has_cdk = "cdk.json" in artifacts
    has_compose = any(
        k in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
        for k in artifacts
    )
    has_chart = any(k.endswith("Chart.yaml") for k in artifacts)

    if has_tf:
        for cmd in ("init", "validate", "plan"):
            r = agent.terraform_exec_tool.run(
                TerraformExecutionInput(
                    repo_path=repo_str,
                    command=cmd,
                )
            )
            results.append(
                {
                    "tool": "terraform",
                    "command": cmd,
                    "success": r.success,
                    "checks": r.checks,
                    "findings": r.findings,
                    "failure_class": r.failure_class,
                }
            )
            if not r.success:
                break

    if has_cdk:
        r = agent.cdk_exec_tool.run(CDKExecutionInput(repo_path=repo_str, command="synth"))
        results.append(
            {
                "tool": "cdk",
                "command": "synth",
                "success": r.success,
                "checks": r.checks,
                "findings": r.findings,
                "failure_class": r.failure_class,
            }
        )

    if has_compose:
        r = agent.compose_exec_tool.run(
            DockerComposeExecutionInput(
                repo_path=repo_str,
                command="config",
            )
        )
        results.append(
            {
                "tool": "compose",
                "command": "config",
                "success": r.success,
                "checks": r.checks,
                "findings": r.findings,
                "failure_class": r.failure_class,
            }
        )

    if has_chart:
        r = agent.helm_exec_tool.run(HelmExecutionInput(repo_path=repo_str, command="lint"))
        results.append(
            {
                "tool": "helm",
                "command": "lint",
                "success": r.success,
                "checks": r.checks,
                "findings": r.findings,
                "failure_class": r.failure_class,
            }
        )

    return results


class ValidationToolResults(NamedTuple):
    """Phase 4 tool-validation results, packaged for the coordinator."""

    iac_checks: Any
    policy_checks: Any
    cicd_checks: Any
    dry_run_checks: Any
    tool_gate_map: Dict[str, str]


def run_validation_tools(agent: Any, repo_path: Path) -> ValidationToolResults:
    """Run Phase 4 IaC/policy/CICD/dry-run validation tools against ``repo_path``.

    The four tool calls are independent and I/O-bound (subprocess-backed), so
    they run concurrently via ``parallel_map``. ``preserve_order=True`` (the
    default) guarantees the unpacked results line up with ``calls`` regardless
    of which one finishes first, so the ``tool_gate_map`` merge below stays
    deterministic (iac -> policy -> cicd -> dry_run, last write wins) exactly
    as when the calls ran sequentially.
    """
    repo_str = str(repo_path)
    calls: List[Callable[[], Any]] = [
        lambda: agent.iac_validation_tool.run(IaCValidationInput(repo_path=repo_str)),
        lambda: run_policy_scan(repo_str, runner=agent.policy_tool),
        lambda: agent.cicd_lint_tool.run(CICDLintInput(repo_path=repo_str)),
        lambda: agent.deploy_dry_run_tool.run(DeploymentDryRunInput(repo_path=repo_str)),
    ]
    iac_checks, policy_checks, cicd_checks, dry_run_checks = parallel_map(
        calls, lambda fn: fn(), max_workers=len(calls), skip_none=False
    )

    tool_gate_map: Dict[str, str] = {}
    tool_gate_map.update(iac_checks.checks)
    tool_gate_map.update(policy_checks.checks)
    tool_gate_map.update(cicd_checks.checks)
    tool_gate_map.update(dry_run_checks.checks)

    return ValidationToolResults(
        iac_checks=iac_checks,
        policy_checks=policy_checks,
        cicd_checks=cicd_checks,
        dry_run_checks=dry_run_checks,
        tool_gate_map=tool_gate_map,
    )
