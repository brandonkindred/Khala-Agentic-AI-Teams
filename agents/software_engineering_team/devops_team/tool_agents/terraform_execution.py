"""Terraform execution tool agent -- safe wrapper around terraform CLI."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.command_runner import run_command

logger = logging.getLogger(__name__)


class TerraformExecutionInput(BaseModel):
    repo_path: str
    command: Literal["init", "validate", "plan", "apply", "fmt"] = "plan"
    auto_approve: bool = False
    vars_file: Optional[str] = None


class TerraformExecutionOutput(BaseModel):
    success: bool
    exit_code: int = 0
    checks: Dict[str, str] = Field(default_factory=dict)
    findings: List[str] = Field(default_factory=list)
    plan_summary: str = ""
    failure_class: str = ""


class TerraformExecutionToolAgent:
    """Runs terraform commands and returns structured results."""

    def run(self, input_data: TerraformExecutionInput) -> TerraformExecutionOutput:
        path = Path(input_data.repo_path).resolve()

        has_tf = any(path.rglob("*.tf"))
        if not has_tf:
            return TerraformExecutionOutput(
                success=True,
                checks={f"terraform_{input_data.command}": "skipped"},
                findings=["No .tf files found"],
            )

        if input_data.command == "apply" and not input_data.auto_approve:
            return TerraformExecutionOutput(
                success=False,
                checks={"terraform_apply": "blocked"},
                findings=["terraform apply requires auto_approve=True"],
                failure_class="safety_blocked",
            )

        cmd: List[str] = ["terraform"]
        check_key = f"terraform_{input_data.command}"

        if input_data.command == "init":
            cmd += ["init", "-backend=false"]
        elif input_data.command == "fmt":
            cmd += ["fmt", "-check"]
        elif input_data.command == "apply":
            cmd += ["apply", "-auto-approve"]
            if input_data.vars_file:
                cmd += ["-var-file", input_data.vars_file]
        elif input_data.command == "plan":
            cmd += ["plan"]
            if input_data.vars_file:
                cmd += ["-var-file", input_data.vars_file]
        else:
            cmd.append(input_data.command)

        result = run_command(cmd, cwd=path, timeout=180)

        findings: List[str] = []
        if not result.success:
            findings.append(result.error_summary or result.stderr[:500])

        plan_summary = ""
        if input_data.command == "plan" and result.success:
            plan_summary = result.stdout[-2000:] if result.stdout else ""

        return TerraformExecutionOutput(
            success=result.success,
            exit_code=result.exit_code,
            checks={check_key: "pass" if result.success else "fail"},
            findings=findings,
            plan_summary=plan_summary,
            failure_class="" if result.success else "execution",
        )
