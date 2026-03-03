"""Deployment dry-run and plan tool agent."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, Field

from software_engineering_team.shared.command_runner import run_command


class DeploymentDryRunInput(BaseModel):
    repo_path: str


class DeploymentDryRunOutput(BaseModel):
    success: bool
    checks: Dict[str, str] = Field(default_factory=dict)
    findings: List[str] = Field(default_factory=list)


class DeploymentDryRunPlanToolAgent:
    """Runs deployment dry-run checks (Helm lint/template if available)."""

    def run(self, input_data: DeploymentDryRunInput) -> DeploymentDryRunOutput:
        path = Path(input_data.repo_path).resolve()
        checks: Dict[str, str] = {
            "deployment_dry_run": "skipped",
        }
        findings: List[str] = []

        has_chart = any(path.rglob("Chart.yaml"))
        if has_chart:
            lint = run_command(["helm", "lint", "."], cwd=path, timeout=120)
            if lint.exit_code == 127:
                checks["deployment_dry_run"] = "skipped"
            else:
                checks["deployment_dry_run"] = "pass" if lint.success else "fail"
                if not lint.success:
                    findings.append(lint.error_summary or lint.stderr[:1000])

        return DeploymentDryRunOutput(
            success=checks["deployment_dry_run"] != "fail",
            checks=checks,
            findings=findings,
        )
