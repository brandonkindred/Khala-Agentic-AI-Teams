"""AWS CDK execution tool agent -- safe wrapper around cdk CLI."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.command_runner import run_command

logger = logging.getLogger(__name__)


class CDKExecutionInput(BaseModel):
    repo_path: str
    command: Literal["synth", "diff"] = "synth"
    stack_name: Optional[str] = None


class CDKExecutionOutput(BaseModel):
    success: bool
    exit_code: int = 0
    checks: Dict[str, str] = Field(default_factory=dict)
    findings: List[str] = Field(default_factory=list)
    failure_class: str = ""


class CDKExecutionToolAgent:
    """Runs AWS CDK commands (read-only: synth, diff) and returns structured results."""

    def run(self, input_data: CDKExecutionInput) -> CDKExecutionOutput:
        path = Path(input_data.repo_path).resolve()
        check_key = f"cdk_{input_data.command}"

        if not (path / "cdk.json").exists():
            return CDKExecutionOutput(
                success=True,
                checks={check_key: "skipped"},
                findings=["No cdk.json found"],
            )

        cmd = ["npx", "cdk", input_data.command]
        if input_data.stack_name:
            cmd.append(input_data.stack_name)

        result = run_command(cmd, cwd=path, timeout=180)

        findings: List[str] = []
        if not result.success:
            findings.append(result.error_summary or result.stderr[:500])

        return CDKExecutionOutput(
            success=result.success,
            exit_code=result.exit_code,
            checks={check_key: "pass" if result.success else "fail"},
            findings=findings,
            failure_class="" if result.success else "execution",
        )
