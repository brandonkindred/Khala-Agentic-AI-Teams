"""Helm execution tool agent -- safe read-only commands only."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.command_runner import run_command

logger = logging.getLogger(__name__)


class HelmExecutionInput(BaseModel):
    repo_path: str
    command: Literal["template", "lint"] = "lint"
    release_name: str = "release"
    values_file: Optional[str] = None


class HelmExecutionOutput(BaseModel):
    success: bool
    exit_code: int = 0
    checks: Dict[str, str] = Field(default_factory=dict)
    findings: List[str] = Field(default_factory=list)
    template_output: str = ""
    failure_class: str = ""


class HelmExecutionToolAgent:
    """Runs helm commands (read-only: template, lint) and returns structured results."""

    def run(self, input_data: HelmExecutionInput) -> HelmExecutionOutput:
        path = Path(input_data.repo_path).resolve()
        check_key = f"helm_{input_data.command}"

        has_chart = any(path.rglob("Chart.yaml"))
        if not has_chart:
            return HelmExecutionOutput(
                success=True,
                checks={check_key: "skipped"},
                findings=["No Chart.yaml found"],
            )

        cmd = ["helm", input_data.command]
        if input_data.command == "template":
            cmd.append(input_data.release_name)
        cmd.append(".")

        if input_data.values_file:
            cmd.extend(["-f", input_data.values_file])

        result = run_command(cmd, cwd=path, timeout=120)

        findings: List[str] = []
        if not result.success:
            findings.append(result.error_summary or result.stderr[:500])

        template_output = ""
        if input_data.command == "template" and result.success:
            template_output = result.stdout[-3000:] if result.stdout else ""

        return HelmExecutionOutput(
            success=result.success,
            exit_code=result.exit_code,
            checks={check_key: "pass" if result.success else "fail"},
            findings=findings,
            template_output=template_output,
            failure_class="" if result.success else "execution",
        )
