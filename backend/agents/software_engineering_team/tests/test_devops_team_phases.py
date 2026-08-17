"""Tests for the standalone devops_team phase functions (Phase 1 clarification,
Phase 2 design fan-out) extracted from ``DevOpsTeamLeadAgent._run_pipeline``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from software_engineering_team.devops_team.cicd_pipeline_agent.models import (
    CICDPipelineAgentOutput,
)
from software_engineering_team.devops_team.deployment_strategy_agent.models import (
    DeploymentStrategyAgentOutput,
)
from software_engineering_team.devops_team.iac_agent.models import IaCAgentOutput
from software_engineering_team.devops_team.models import DevOpsTaskSpec, SubtaskContract
from software_engineering_team.devops_team.phase2_graph import run_phase2_parallel
from software_engineering_team.devops_team.phases import (
    Phase1ClarifyResult,
    Phase2DesignResult,
    run_phase1_intake_clarify,
    run_phase2_design_fanout,
)
from software_engineering_team.devops_team.task_clarifier import DevOpsTaskClarifierOutput


def _base_task_spec(**overrides) -> DevOpsTaskSpec:
    defaults = dict(
        task_id="DO-2207",
        title="Add CI/CD pipeline and deployment flow",
        platform_scope={
            "cloud": "aws",
            "runtime": "eks",
            "environments": ["dev", "staging", "production"],
        },
        repo_context={
            "app_repo": "billing-service",
            "infra_repo": "platform-infra",
            "pipeline_repo": "billing-service",
        },
        goal={
            "summary": "Build secure CI/CD workflow for build/test/scan/deploy with staged promotion."
        },
        scope={
            "included": ["build image", "deploy staging", "prod approval"],
            "excluded": ["cluster provisioning"],
        },
        constraints={
            "iac": {"preferred": "terraform"},
            "ci_cd": {"platform": "github_actions"},
            "deployment": {"strategy": "rolling", "tooling": "helm"},
            "secrets": {"source": "aws_secrets_manager"},
        },
        acceptance_criteria=[
            "Pipeline runs tests and scan before deploy",
            "Prod deploy requires explicit approval",
        ],
        rollback_requirements=["Rollback to previous helm release"],
    )
    defaults.update(overrides)
    return DevOpsTaskSpec(**defaults)


class TestRunPhase1IntakeClarify:
    def test_env_policy_violation_blocks(self) -> None:
        spec = _base_task_spec()
        task_clarifier = MagicMock()
        result = run_phase1_intake_clarify(
            task_spec=spec,
            task_clarifier=task_clarifier,
            enforce_env_policy=lambda _spec: "prod requires approval",
            build_subtask_contracts=MagicMock(),
        )

        assert isinstance(result, Phase1ClarifyResult)
        assert result.blocked_reason == "Environment policy violation: prod requires approval"
        assert result.subtask_contracts == []
        task_clarifier.run.assert_not_called()

    def test_clarifier_rejection_blocks(self) -> None:
        spec = _base_task_spec()
        task_clarifier = MagicMock()
        task_clarifier.run.return_value = DevOpsTaskClarifierOutput(
            approved_for_execution=False,
            clarification_requests=["missing rollback plan", "missing goal"],
            checklist=[],
        )
        build_subtask_contracts = MagicMock()

        result = run_phase1_intake_clarify(
            task_spec=spec,
            task_clarifier=task_clarifier,
            enforce_env_policy=lambda _spec: None,
            build_subtask_contracts=build_subtask_contracts,
        )

        assert result.blocked_reason == (
            "Clarification required: missing rollback plan; missing goal"
        )
        assert result.subtask_contracts == []
        build_subtask_contracts.assert_not_called()

    def test_approved_returns_none_reason_and_contracts(self) -> None:
        spec = _base_task_spec()
        task_clarifier = MagicMock()
        task_clarifier.run.return_value = DevOpsTaskClarifierOutput(
            approved_for_execution=True,
            clarification_requests=[],
            checklist=[],
        )
        contracts = [
            SubtaskContract(
                subtask_id="DO-2207-T1",
                owner="InfrastructureAsCodeAgent",
                objective="Implement IaC changes",
                inputs=["validated_task_spec"],
                constraints=["no secrets in code"],
                expected_artifact=["iac_files"],
                completion_criteria=["IaC validates"],
            )
        ]

        result = run_phase1_intake_clarify(
            task_spec=spec,
            task_clarifier=task_clarifier,
            enforce_env_policy=lambda _spec: None,
            build_subtask_contracts=lambda _spec: contracts,
        )

        assert result.blocked_reason is None
        assert result.subtask_contracts == contracts


class TestRunPhase2DesignFanout:
    def test_sequential_fallback_aggregates_all_three_agents(self) -> None:
        spec = _base_task_spec()
        iac_agent = MagicMock()
        iac_agent.run.return_value = IaCAgentOutput(
            artifacts={"infra/main.tf": "resource {}"}, summary="iac ok"
        )
        cicd_agent = MagicMock()
        cicd_agent.run.return_value = CICDPipelineAgentOutput(
            artifacts={".github/workflows/ci.yml": "on: push"}, summary="cicd ok"
        )
        deployment_agent = MagicMock()
        deployment_agent.run.return_value = DeploymentStrategyAgentOutput(
            artifacts={"deploy/values.yaml": "replicas: 2"}, summary="deploy ok"
        )
        repo_navigator_tool = MagicMock()
        repo_navigator_tool.run.return_value = MagicMock(summary="a small repo")

        result = run_phase2_design_fanout(
            task_spec=spec,
            repo_path=Path("/tmp/repo"),
            iac_agent=iac_agent,
            cicd_agent=cicd_agent,
            deployment_agent=deployment_agent,
            repo_navigator_tool=repo_navigator_tool,
            parallel=False,
        )

        assert isinstance(result, Phase2DesignResult)
        assert result.iac_result.summary == "iac ok"
        assert result.cicd_result.summary == "cicd ok"
        assert result.deploy_result.summary == "deploy ok"
        assert result.aggregated_artifacts == {
            "infra/main.tf": "resource {}",
            ".github/workflows/ci.yml": "on: push",
            "deploy/values.yaml": "replicas: 2",
        }

    def test_repo_summary_passed_through_to_agents(self) -> None:
        spec = _base_task_spec()
        iac_agent = MagicMock()
        iac_agent.run.return_value = IaCAgentOutput(summary="iac ok")
        cicd_agent = MagicMock()
        cicd_agent.run.return_value = CICDPipelineAgentOutput(summary="cicd ok")
        deployment_agent = MagicMock()
        deployment_agent.run.return_value = DeploymentStrategyAgentOutput(summary="deploy ok")
        repo_navigator_tool = MagicMock()
        repo_navigator_tool.run.return_value = MagicMock(summary="repo summary text")

        run_phase2_design_fanout(
            task_spec=spec,
            repo_path=Path("/tmp/repo"),
            iac_agent=iac_agent,
            cicd_agent=cicd_agent,
            deployment_agent=deployment_agent,
            repo_navigator_tool=repo_navigator_tool,
            parallel=False,
        )

        iac_call_input = iac_agent.run.call_args[0][0]
        assert iac_call_input.repo_summary == "repo summary text"


class TestRunPhase2ParallelFailurePolicy:
    """``run_phase2_parallel``'s ``parallel=True`` path now runs through
    ``shared.concurrency.parallel_map``; these tests pin the documented
    silent-default failure policy so the migration didn't quietly turn it
    into fast-fail."""

    def test_one_agent_failure_degrades_to_default_others_unaffected(self) -> None:
        spec = _base_task_spec()
        iac_agent = MagicMock()
        iac_agent.run.side_effect = RuntimeError("boom")
        cicd_agent = MagicMock()
        cicd_agent.run.return_value = CICDPipelineAgentOutput(
            artifacts={".github/workflows/ci.yml": "on: push"}, summary="cicd ok"
        )
        deployment_agent = MagicMock()
        deployment_agent.run.return_value = DeploymentStrategyAgentOutput(
            artifacts={"deploy/values.yaml": "replicas: 2"}, summary="deploy ok"
        )

        result = run_phase2_parallel(
            iac_agent,
            cicd_agent,
            deployment_agent,
            spec,
            repo_summary="a small repo",
            parallel=True,
        )

        assert result["iac_result"].summary == "IaC agent failed during Phase 2"
        assert result["iac_result"].artifacts == {}
        assert result["cicd_result"].summary == "cicd ok"
        assert result["deploy_result"].summary == "deploy ok"
        assert result["aggregated_artifacts"] == {
            ".github/workflows/ci.yml": "on: push",
            "deploy/values.yaml": "replicas: 2",
        }

    def test_all_agents_succeed(self) -> None:
        spec = _base_task_spec()
        iac_agent = MagicMock()
        iac_agent.run.return_value = IaCAgentOutput(
            artifacts={"infra/main.tf": "resource {}"}, summary="iac ok"
        )
        cicd_agent = MagicMock()
        cicd_agent.run.return_value = CICDPipelineAgentOutput(
            artifacts={".github/workflows/ci.yml": "on: push"}, summary="cicd ok"
        )
        deployment_agent = MagicMock()
        deployment_agent.run.return_value = DeploymentStrategyAgentOutput(
            artifacts={"deploy/values.yaml": "replicas: 2"}, summary="deploy ok"
        )

        result = run_phase2_parallel(
            iac_agent,
            cicd_agent,
            deployment_agent,
            spec,
            repo_summary="a small repo",
            parallel=True,
        )

        assert result["iac_result"].summary == "iac ok"
        assert result["cicd_result"].summary == "cicd ok"
        assert result["deploy_result"].summary == "deploy ok"
        assert result["aggregated_artifacts"] == {
            "infra/main.tf": "resource {}",
            ".github/workflows/ci.yml": "on: push",
            "deploy/values.yaml": "replicas: 2",
        }
