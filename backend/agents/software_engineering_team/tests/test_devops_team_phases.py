"""Tests for the standalone devops_team phase functions (Phase 1 clarification,
Phase 2 design fan-out) extracted from ``DevOpsTeamLeadAgent._run_pipeline``."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import MagicMock

import pytest

from software_engineering_team.devops_team.change_review_agent import ChangeReviewOutput
from software_engineering_team.devops_team.cicd_pipeline_agent.models import (
    CICDPipelineAgentOutput,
)
from software_engineering_team.devops_team.deployment_strategy_agent.models import (
    DeploymentStrategyAgentOutput,
)
from software_engineering_team.devops_team.devsecops_review_agent import DevSecOpsReviewOutput
from software_engineering_team.devops_team.iac_agent.models import IaCAgentOutput
from software_engineering_team.devops_team.models import DevOpsTaskSpec, SubtaskContract
from software_engineering_team.devops_team.phase2_graph import run_phase2_parallel
from software_engineering_team.devops_team.phases import (
    Phase1ClarifyResult,
    Phase2DesignResult,
    Phase4QualityGateResult,
    run_phase1_intake_clarify,
    run_phase2_design_fanout,
    run_phase4_quality_gate,
)
from software_engineering_team.devops_team.task_clarifier import DevOpsTaskClarifierOutput
from software_engineering_team.devops_team.tool_agents import (
    CICDLintOutput,
    DeploymentDryRunOutput,
    IaCValidationOutput,
    PolicyAsCodeOutput,
)
from software_engineering_team.qa_agent import QAOutput


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


def _mock_phase4_agent(
    *,
    devsec_approved: bool = True,
    change_approved: bool = True,
    quality_gates: Optional[Dict[str, str]] = None,
) -> MagicMock:
    """A MagicMock agent covering everything run_phase4_quality_gate reaches
    for an ``aggregated_artifacts``/``exec_results``-empty run: the 4
    validation tools (so Phase 4.6's debug-patch loop is never entered) plus
    the 3 review agents."""
    agent = MagicMock()
    agent._run_execution_tools.return_value = []
    agent.iac_validation_tool.run.return_value = IaCValidationOutput(
        success=True, checks={"iac_validate": "pass"}
    )
    agent.policy_tool.run.return_value = PolicyAsCodeOutput(
        success=True, checks={"policy_checks": "pass"}
    )
    agent.cicd_lint_tool.run.return_value = CICDLintOutput(
        success=True, checks={"pipeline_lint": "pass"}
    )
    agent.deploy_dry_run_tool.run.return_value = DeploymentDryRunOutput(
        success=True, checks={"deployment_dry_run": "pass"}
    )
    agent.devsecops_review_agent.run.return_value = DevSecOpsReviewOutput(
        approved=devsec_approved, findings=[], summary="devsec summary"
    )
    agent.change_review_agent.run.return_value = ChangeReviewOutput(
        approved=change_approved, findings=[], summary="change summary"
    )
    agent.qa_agent.run.return_value = QAOutput(
        approved=True,
        quality_gates=dict(quality_gates or {}),
        acceptance_trace=[{"criterion": "c1", "status": "met"}],
        summary="qa summary",
    )
    return agent


class TestRunPhase4QualityGate:
    """Exercise run_phase4_quality_gate's 3-way review fan-out (DevSecOps,
    change review, QA acceptance-evidence) directly against a mock agent."""

    def test_parallel_executes_review_calls_concurrently(self) -> None:
        """Perf-guard: proves the 3 review calls run concurrently, not
        sequentially.

        Each call sleeps ~0.12s (simulating an LLM round-trip). Sequential
        execution would take >= 3 * 0.12s == 0.36s; concurrent execution
        across 3 workers should take roughly one sleep interval. The
        2x-one-interval bound sits well below the sequential floor and well
        above the concurrent expectation, so it isn't flaky under ordinary CI
        scheduling noise while still failing hard if the three calls ever
        became sequential again (mirrors
        ``TestToolDispatchRunValidationTools.test_run_validation_tools_executes_the_four_tool_calls_concurrently``
        in ``test_devops_team.py``).
        """
        sleep_s = 0.12

        def _slow(output):
            def _run(_input):
                time.sleep(sleep_s)
                return output

            return _run

        agent = _mock_phase4_agent()
        agent.devsecops_review_agent.run.side_effect = _slow(
            DevSecOpsReviewOutput(approved=True, findings=[], summary="devsec ok")
        )
        agent.change_review_agent.run.side_effect = _slow(
            ChangeReviewOutput(approved=True, findings=[], summary="change ok")
        )
        agent.qa_agent.run.side_effect = _slow(
            QAOutput(approved=True, quality_gates={}, acceptance_trace=[], summary="qa ok")
        )
        spec = _base_task_spec()

        start = time.perf_counter()
        result = run_phase4_quality_gate(
            agent,
            task_spec=spec,
            repo_path=Path("/tmp/repo"),
            aggregated_artifacts={},
            write_changes=False,
            subdir="devops",
            build_verifier=None,
            parallel=True,
        )
        elapsed = time.perf_counter() - start

        assert isinstance(result, Phase4QualityGateResult)
        assert result.blocked_result is None
        assert elapsed < 2 * sleep_s, (
            f"run_phase4_quality_gate took {elapsed:.3f}s, expected well under "
            f"{2 * sleep_s:.3f}s if the 3 review calls run concurrently"
        )

    def test_parallel_and_sequential_agree_on_all_pass(self) -> None:
        spec = _base_task_spec()

        def _run(parallel: bool) -> Phase4QualityGateResult:
            agent = _mock_phase4_agent(quality_gates={"tests": "pass"})
            return run_phase4_quality_gate(
                agent,
                task_spec=spec,
                repo_path=Path("/tmp/repo"),
                aggregated_artifacts={"infra/main.tf": "resource {}"},
                write_changes=False,
                subdir="devops",
                build_verifier=None,
                parallel=parallel,
            )

        parallel_result = _run(True)
        sequential_result = _run(False)

        assert parallel_result.quality_gates == sequential_result.quality_gates
        assert parallel_result.quality_gates["security_review"] == "pass"
        assert parallel_result.quality_gates["change_review"] == "pass"
        assert parallel_result.acceptance_trace == sequential_result.acceptance_trace
        assert parallel_result.blocked_result is None
        assert sequential_result.blocked_result is None

    def test_parallel_and_sequential_agree_on_security_review_failure(self) -> None:
        """Mirrors test_devops_team.py's test_blocked_by_security_review /
        test_security_gate_not_masked_by_stale_validation_pass: the
        force-assigned ``security_review`` gate and resulting
        ``blocked_result`` must be identical whether the 3 reviews ran in
        parallel or sequentially."""
        spec = _base_task_spec()

        def _run(parallel: bool) -> Phase4QualityGateResult:
            agent = _mock_phase4_agent(devsec_approved=False, quality_gates={})
            return run_phase4_quality_gate(
                agent,
                task_spec=spec,
                repo_path=Path("/tmp/repo"),
                aggregated_artifacts={},
                write_changes=False,
                subdir="devops",
                build_verifier=None,
                parallel=parallel,
            )

        parallel_result = _run(True)
        sequential_result = _run(False)

        assert parallel_result.quality_gates["security_review"] == "fail"
        assert sequential_result.quality_gates["security_review"] == "fail"
        assert parallel_result.blocked_result is not None
        assert sequential_result.blocked_result is not None
        assert parallel_result.blocked_result.failure_reason == "Quality gates failed"
        assert sequential_result.blocked_result.failure_reason == "Quality gates failed"
        assert (
            parallel_result.blocked_result.completion_package.quality_gates
            == sequential_result.blocked_result.completion_package.quality_gates
        )

    def test_parallel_propagates_exception_uncaught(self) -> None:
        """Pins the no-silent-default failure policy: unlike
        ``run_phase2_parallel``'s deliberate catch-and-degrade-to-default
        policy, a failing review call must still crash
        ``run_phase4_quality_gate`` under ``parallel=True`` -- the same as
        today's unguarded sequential calls -- so the aggregate pass/fail
        semantics stay unchanged from the sequential version."""
        agent = _mock_phase4_agent()
        agent.change_review_agent.run.side_effect = RuntimeError("boom")
        spec = _base_task_spec()

        with pytest.raises(RuntimeError, match="boom"):
            run_phase4_quality_gate(
                agent,
                task_spec=spec,
                repo_path=Path("/tmp/repo"),
                aggregated_artifacts={},
                write_changes=False,
                subdir="devops",
                build_verifier=None,
                parallel=True,
            )
