"""Tests for DevOps execution tool agents (Phase 4)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from devops_team.tool_agents.cdk_execution import (
    CDKExecutionInput,
    CDKExecutionToolAgent,
)
from devops_team.tool_agents.docker_compose_execution import (
    DockerComposeExecutionInput,
    DockerComposeExecutionToolAgent,
)
from devops_team.tool_agents.helm_execution import (
    HelmExecutionInput,
    HelmExecutionToolAgent,
)
from devops_team.tool_agents.terraform_execution import (
    TerraformExecutionInput,
    TerraformExecutionToolAgent,
)


def _cmd_result(success: bool = True, exit_code: int = 0, stdout: str = "", stderr: str = ""):
    return MagicMock(
        success=success,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        error_summary=stderr[:500] if stderr else "",
    )


# ---------------------------------------------------------------------------
# Terraform
# ---------------------------------------------------------------------------


class TestTerraformExecutionToolAgent:
    def test_plan_pass(self, tmp_path: Path) -> None:
        (tmp_path / "main.tf").write_text("resource {}", encoding="utf-8")
        agent = TerraformExecutionToolAgent()
        with patch(
            "devops_team.tool_agents.terraform_execution.run_command",
            return_value=_cmd_result(True, stdout="Plan: 1 to add"),
        ):
            result = agent.run(TerraformExecutionInput(repo_path=str(tmp_path), command="plan"))
        assert result.success
        assert result.checks["terraform_plan"] == "pass"

    def test_validate_fail(self, tmp_path: Path) -> None:
        (tmp_path / "main.tf").write_text("bad", encoding="utf-8")
        agent = TerraformExecutionToolAgent()
        with patch(
            "devops_team.tool_agents.terraform_execution.run_command",
            return_value=_cmd_result(False, 1, stderr="Error: invalid"),
        ):
            result = agent.run(TerraformExecutionInput(repo_path=str(tmp_path), command="validate"))
        assert not result.success
        assert result.checks["terraform_validate"] == "fail"
        assert result.failure_class == "execution"

    def test_skipped_no_tf_files(self, tmp_path: Path) -> None:
        agent = TerraformExecutionToolAgent()
        result = agent.run(TerraformExecutionInput(repo_path=str(tmp_path), command="plan"))
        assert result.success
        assert result.checks["terraform_plan"] == "skipped"

    def test_apply_blocked_without_auto_approve(self, tmp_path: Path) -> None:
        (tmp_path / "main.tf").write_text("resource {}", encoding="utf-8")
        agent = TerraformExecutionToolAgent()
        result = agent.run(
            TerraformExecutionInput(repo_path=str(tmp_path), command="apply", auto_approve=False)
        )
        assert not result.success
        assert result.checks["terraform_apply"] == "blocked"
        assert result.failure_class == "safety_blocked"

    def test_apply_allowed_with_auto_approve(self, tmp_path: Path) -> None:
        (tmp_path / "main.tf").write_text("resource {}", encoding="utf-8")
        agent = TerraformExecutionToolAgent()
        with patch(
            "devops_team.tool_agents.terraform_execution.run_command",
            return_value=_cmd_result(True),
        ):
            result = agent.run(
                TerraformExecutionInput(repo_path=str(tmp_path), command="apply", auto_approve=True)
            )
        assert result.success


# ---------------------------------------------------------------------------
# CDK
# ---------------------------------------------------------------------------


class TestCDKExecutionToolAgent:
    def test_synth_pass(self, tmp_path: Path) -> None:
        (tmp_path / "cdk.json").write_text("{}", encoding="utf-8")
        agent = CDKExecutionToolAgent()
        with patch(
            "devops_team.tool_agents.cdk_execution.run_command", return_value=_cmd_result(True)
        ):
            result = agent.run(CDKExecutionInput(repo_path=str(tmp_path), command="synth"))
        assert result.success
        assert result.checks["cdk_synth"] == "pass"

    def test_skipped_no_cdk_json(self, tmp_path: Path) -> None:
        agent = CDKExecutionToolAgent()
        result = agent.run(CDKExecutionInput(repo_path=str(tmp_path), command="synth"))
        assert result.success
        assert result.checks["cdk_synth"] == "skipped"

    def test_synth_fail(self, tmp_path: Path) -> None:
        (tmp_path / "cdk.json").write_text("{}", encoding="utf-8")
        agent = CDKExecutionToolAgent()
        with patch(
            "devops_team.tool_agents.cdk_execution.run_command",
            return_value=_cmd_result(False, 1, stderr="Synthesis error"),
        ):
            result = agent.run(CDKExecutionInput(repo_path=str(tmp_path), command="synth"))
        assert not result.success
        assert result.failure_class == "execution"


# ---------------------------------------------------------------------------
# Docker Compose
# ---------------------------------------------------------------------------


class TestDockerComposeExecutionToolAgent:
    def test_config_pass(self, tmp_path: Path) -> None:
        (tmp_path / "docker-compose.yml").write_text("version: '3'", encoding="utf-8")
        agent = DockerComposeExecutionToolAgent()
        with patch(
            "devops_team.tool_agents.docker_compose_execution.run_command",
            return_value=_cmd_result(True),
        ):
            result = agent.run(
                DockerComposeExecutionInput(repo_path=str(tmp_path), command="config")
            )
        assert result.success
        assert result.checks["compose_config"] == "pass"

    def test_skipped_no_compose_file(self, tmp_path: Path) -> None:
        agent = DockerComposeExecutionToolAgent()
        result = agent.run(DockerComposeExecutionInput(repo_path=str(tmp_path), command="config"))
        assert result.success
        assert result.checks["compose_config"] == "skipped"

    def test_config_fail(self, tmp_path: Path) -> None:
        (tmp_path / "compose.yaml").write_text("bad", encoding="utf-8")
        agent = DockerComposeExecutionToolAgent()
        with patch(
            "devops_team.tool_agents.docker_compose_execution.run_command",
            return_value=_cmd_result(False, 1, stderr="parse error"),
        ):
            result = agent.run(
                DockerComposeExecutionInput(repo_path=str(tmp_path), command="config")
            )
        assert not result.success


# ---------------------------------------------------------------------------
# Helm
# ---------------------------------------------------------------------------


class TestHelmExecutionToolAgent:
    def test_template_pass(self, tmp_path: Path) -> None:
        (tmp_path / "Chart.yaml").write_text("apiVersion: v2\nname: test", encoding="utf-8")
        agent = HelmExecutionToolAgent()
        with patch(
            "devops_team.tool_agents.helm_execution.run_command",
            return_value=_cmd_result(True, stdout="---\napiVersion: v1"),
        ):
            result = agent.run(HelmExecutionInput(repo_path=str(tmp_path), command="template"))
        assert result.success
        assert result.checks["helm_template"] == "pass"
        assert result.template_output

    def test_skipped_no_chart(self, tmp_path: Path) -> None:
        agent = HelmExecutionToolAgent()
        result = agent.run(HelmExecutionInput(repo_path=str(tmp_path), command="lint"))
        assert result.success
        assert result.checks["helm_lint"] == "skipped"

    def test_lint_fail(self, tmp_path: Path) -> None:
        (tmp_path / "Chart.yaml").write_text("apiVersion: v2", encoding="utf-8")
        agent = HelmExecutionToolAgent()
        with patch(
            "devops_team.tool_agents.helm_execution.run_command",
            return_value=_cmd_result(False, 1, stderr="Error: chart metadata missing"),
        ):
            result = agent.run(HelmExecutionInput(repo_path=str(tmp_path), command="lint"))
        assert not result.success
        assert result.failure_class == "execution"
