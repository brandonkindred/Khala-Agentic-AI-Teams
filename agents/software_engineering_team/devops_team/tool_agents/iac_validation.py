"""IaC validation tool agent."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, Field

from software_engineering_team.shared.command_runner import run_command


class IaCValidationInput(BaseModel):
    repo_path: str


class IaCValidationOutput(BaseModel):
    success: bool
    checks: Dict[str, str] = Field(default_factory=dict)
    findings: List[str] = Field(default_factory=list)


class IaCValidationToolAgent:
    """Runs IaC validation checks and emits structured findings."""

    def run(self, input_data: IaCValidationInput) -> IaCValidationOutput:
        path = Path(input_data.repo_path).resolve()
        checks: Dict[str, str] = {}
        findings: List[str] = []

        has_tf = any(path.rglob("*.tf"))
        if has_tf:
            fmt = run_command(["terraform", "fmt", "-check"], cwd=path, timeout=120)
            checks["iac_validate_fmt"] = "pass" if fmt.success else "fail"
            if not fmt.success:
                findings.append(fmt.error_summary or fmt.stderr[:500])
            validate = run_command(["terraform", "validate"], cwd=path, timeout=120)
            checks["iac_validate"] = "pass" if validate.success else "fail"
            if not validate.success:
                findings.append(validate.error_summary or validate.stderr[:500])
        else:
            checks["iac_validate_fmt"] = "skipped"
            checks["iac_validate"] = "skipped"

        success = not any(v == "fail" for v in checks.values())
        return IaCValidationOutput(success=success, checks=checks, findings=findings)
