"""Docker Compose execution tool agent -- safe non-destructive commands only."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Literal

from pydantic import BaseModel, Field

from software_engineering_team.shared.command_runner import run_command

logger = logging.getLogger(__name__)


class DockerComposeExecutionInput(BaseModel):
    repo_path: str
    command: Literal["config", "build", "ps", "logs"] = "config"
    services: List[str] = Field(default_factory=list)


class DockerComposeExecutionOutput(BaseModel):
    success: bool
    exit_code: int = 0
    checks: Dict[str, str] = Field(default_factory=dict)
    findings: List[str] = Field(default_factory=list)
    failure_class: str = ""


class DockerComposeExecutionToolAgent:
    """Runs docker compose commands (non-destructive) and returns structured results."""

    _COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

    def run(self, input_data: DockerComposeExecutionInput) -> DockerComposeExecutionOutput:
        path = Path(input_data.repo_path).resolve()
        check_key = f"compose_{input_data.command}"

        has_compose = any((path / f).exists() for f in self._COMPOSE_FILES)
        if not has_compose:
            return DockerComposeExecutionOutput(
                success=True,
                checks={check_key: "skipped"},
                findings=["No docker-compose/compose file found"],
            )

        cmd = ["docker", "compose", input_data.command]
        if input_data.services:
            cmd.extend(input_data.services)

        result = run_command(cmd, cwd=path, timeout=120)

        findings: List[str] = []
        if not result.success:
            findings.append(result.error_summary or result.stderr[:500])

        return DockerComposeExecutionOutput(
            success=result.success,
            exit_code=result.exit_code,
            checks={check_key: "pass" if result.success else "fail"},
            findings=findings,
            failure_class="" if result.success else "execution",
        )
