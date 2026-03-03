"""Policy-as-code validation tool agent."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, Field

from software_engineering_team.shared.command_runner import run_command


class PolicyAsCodeInput(BaseModel):
    repo_path: str


class PolicyAsCodeOutput(BaseModel):
    success: bool
    checks: Dict[str, str] = Field(default_factory=dict)
    findings: List[str] = Field(default_factory=list)


class PolicyAsCodeToolAgent:
    """Runs policy scanners like checkov/tfsec when available."""

    def run(self, input_data: PolicyAsCodeInput) -> PolicyAsCodeOutput:
        path = Path(input_data.repo_path).resolve()
        checks: Dict[str, str] = {}
        findings: List[str] = []

        checkov = run_command(["checkov", "-d", str(path)], cwd=path, timeout=180)
        if checkov.exit_code in (127, -1) or "Command not found" in (checkov.stderr or ""):
            checks["policy_checks"] = "skipped"
        else:
            checks["policy_checks"] = "pass" if checkov.success else "fail"
            if not checkov.success:
                findings.append((checkov.stderr or checkov.stdout)[:1000])

        return PolicyAsCodeOutput(
            success=not any(v == "fail" for v in checks.values()),
            checks=checks,
            findings=findings,
        )
