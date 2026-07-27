"""Unit tests for devops_team.intake_design (Phase 1 clarification + Phase 2
design fan-out), exercised directly against a duck-typed mock agent at the
module boundary rather than only incidentally through the full
DevOpsTeamLeadAgent pipeline — mirrors the ``TestToolDispatchRun*`` pattern
in test_devops_team.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.devops_team import intake_design
from software_engineering_team.devops_team.cicd_pipeline_agent.models import (
    CICDPipelineAgentOutput,
)
from software_engineering_team.devops_team.deployment_strategy_agent.models import (
    DeploymentStrategyAgentOutput,
)
from software_engineering_team.devops_team.iac_agent.models import IaCAgentOutput
from software_engineering_team.devops_team.models import DevOpsTaskSpec
from software_engineering_team.devops_team.orchestrator import DevOpsTeamLeadAgent


def _task_spec(**overrides) -> DevOpsTaskSpec:
    defaults = dict(task_id="DO-9001", title="Add pipeline")
    defaults.update(overrides)
    return DevOpsTaskSpec(**defaults)


class TestRunIntakeClarification:
    """Exercise intake_design.run_intake_clarification directly."""

    def _agent(self) -> MagicMock:
        agent = MagicMock()
        # Bind the real (pure, static) policy/contract helpers so these tests
        # exercise the actual gate logic, not a mocked stand-in for it.
        agent._enforce_env_policy = DevOpsTeamLeadAgent._enforce_env_policy
        agent._build_subtask_contracts = DevOpsTeamLeadAgent._build_subtask_contracts
        return agent

    def test_env_policy_violation_blocks_before_clarifier_runs(self) -> None:
        agent = self._agent()
        task_spec = _task_spec(platform_scope={"environments": ["production"]})

        result = intake_design.run_intake_clarification(agent, task_spec)

        assert result is not None
        assert result.success is False
        assert "Environment policy violation" in result.failure_reason
        agent.task_clarifier.run.assert_not_called()

    def test_clarifier_rejection_returns_failed_result_with_requests(self) -> None:
        agent = self._agent()
        agent.task_clarifier.run.return_value = MagicMock(
            approved_for_execution=False,
            clarification_requests=["Missing rollback plan", "Missing acceptance criteria"],
        )
        task_spec = _task_spec()

        result = intake_design.run_intake_clarification(agent, task_spec)

        assert result is not None
        assert result.success is False
        assert "Clarification required" in result.failure_reason
        assert "Missing rollback plan" in result.failure_reason

    def test_approved_clarification_returns_none_and_builds_contracts(self) -> None:
        agent = self._agent()
        agent.task_clarifier.run.return_value = MagicMock(
            approved_for_execution=True, clarification_requests=[]
        )
        task_spec = _task_spec()

        result = intake_design.run_intake_clarification(agent, task_spec)

        assert result is None
        agent.task_clarifier.run.assert_called_once()
        clarifier_input = agent.task_clarifier.run.call_args.args[0]
        assert clarifier_input.task_spec.task_id == task_spec.task_id


class TestRunDesignFanOut:
    """Exercise intake_design.run_design_fan_out directly."""

    def _agent(self) -> MagicMock:
        agent = MagicMock()
        # A real DummyLLMClient (not a MagicMock) forces run_phase2_parallel's
        # sequential fallback path, keeping these tests deterministic.
        agent.llm = DummyLLMClient()
        agent.repo_navigator_tool.run.return_value = MagicMock(summary="repo summary")
        return agent

    def test_returns_design_fan_out_state_with_merged_artifacts(self) -> None:
        agent = self._agent()
        agent.iac_agent.run.return_value = IaCAgentOutput(
            artifacts={"infra/main.tf": "resource {}"}, summary="iac ok"
        )
        agent.cicd_agent.run.return_value = CICDPipelineAgentOutput(
            artifacts={".github/workflows/ci.yml": "on: push"}, summary="cicd ok"
        )
        agent.deployment_agent.run.return_value = DeploymentStrategyAgentOutput(
            artifacts={"deploy/values.yaml": "replicas: 2"}, summary="deploy ok"
        )

        state = intake_design.run_design_fan_out(agent, _task_spec(), Path("/tmp/x"))

        assert isinstance(state, intake_design.DesignFanOutState)
        assert state.iac_result.summary == "iac ok"
        assert state.cicd_result.summary == "cicd ok"
        assert state.deploy_result.summary == "deploy ok"
        assert state.aggregated_artifacts == {
            "infra/main.tf": "resource {}",
            ".github/workflows/ci.yml": "on: push",
            "deploy/values.yaml": "replicas: 2",
        }
        agent._report_status.assert_called_once()
        agent.repo_navigator_tool.run.assert_called_once()

    def test_no_artifacts_from_any_agent_produces_empty_aggregate(self) -> None:
        agent = self._agent()
        agent.iac_agent.run.return_value = IaCAgentOutput()
        agent.cicd_agent.run.return_value = CICDPipelineAgentOutput()
        agent.deployment_agent.run.return_value = DeploymentStrategyAgentOutput()

        state = intake_design.run_design_fan_out(agent, _task_spec(), Path("/tmp/x"))

        assert state.aggregated_artifacts == {}
