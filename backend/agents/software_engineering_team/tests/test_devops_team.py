"""Tests for the DevOps team orchestrator, models, agents, and tool agents."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from devops_team import DevOpsTaskSpec, DevOpsTeamLeadAgent, DevOpsTeamResult
from devops_team.models import (
    DevOpsCompletionPackage,
    DevOpsConstraints,
    PlatformScope,
    ReleaseReadiness,
    ReviewFinding,
    SubtaskContract,
)
from devops_team.orchestrator import DEVOPS_REQUIRED_GATE_NAMES, ENV_POLICY
from devops_team.task_clarifier import DevOpsTaskClarifierAgent, DevOpsTaskClarifierInput

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _mock_llm_for_happy_path() -> MagicMock:
    mock = MagicMock()
    mock.complete_json.side_effect = [
        {"approved_for_execution": True, "checklist": []},
        {
            "artifacts": {"infra/main.tf": "resource {}"},
            "summary": "iac ok",
            "destructive_changes_detected": False,
        },
        {
            "artifacts": {".github/workflows/ci.yml": "on: push"},
            "summary": "cicd ok",
            "required_gates_present": True,
        },
        {
            "artifacts": {"deploy/values.yaml": "replicas: 2"},
            "summary": "deploy ok",
            "strategy": "rolling",
            "rollback_plan": ["helm rollback"],
        },
        # Debug agent (execution tools fail because terraform CLI is not installed)
        {
            "errors": [{"error_type": "runtime", "error_message": "terraform not found"}],
            "summary": "cli missing",
            "fixable": False,
        },
        {"approved": True, "findings": [], "summary": "sec ok"},
        {"approved": True, "findings": [], "summary": "review ok"},
        {
            "approved": True,
            "quality_gates": {"iac_validate": "pass", "policy_checks": "pass"},
            "acceptance_trace": [],
            "summary": "validation ok",
        },
        {"files": {"docs/runbook.md": "# Runbook"}, "summary": "doc ok"},
    ]
    return mock


# ===========================================================================
# MODEL TESTS
# ===========================================================================


class TestDevOpsTaskSpec:
    def test_task_id_required(self) -> None:
        with pytest.raises(Exception):
            DevOpsTaskSpec(task_id="")

    def test_task_id_strips_whitespace(self) -> None:
        spec = DevOpsTaskSpec(task_id="  DO-123  ")
        assert spec.task_id == "DO-123"

    def test_priority_normalization(self) -> None:
        spec = DevOpsTaskSpec(task_id="t1", priority="p1")
        assert spec.priority == "high"

    def test_priority_passthrough(self) -> None:
        spec = DevOpsTaskSpec(task_id="t1", priority="medium")
        assert spec.priority == "medium"

    def test_environment_alias_normalization(self) -> None:
        spec = DevOpsTaskSpec(task_id="t1", environment="prod")
        assert spec.environment == "production"

    def test_environment_passthrough(self) -> None:
        spec = DevOpsTaskSpec(task_id="t1", environment="staging")
        assert spec.environment == "staging"

    def test_environments_dedup_and_lowercase(self) -> None:
        spec = DevOpsTaskSpec(
            task_id="t1",
            platform_scope={"environments": ["Dev", "dev", " STAGING ", ""]},
        )
        assert spec.platform_scope.environments == ["dev", "staging"]

    def test_acceptance_criteria_normalization(self) -> None:
        spec = DevOpsTaskSpec(
            task_id="t1",
            acceptance_criteria=["  a ", "", " b", ""],
        )
        assert spec.acceptance_criteria == ["a", "b"]

    def test_risk_flags_strip(self) -> None:
        spec = DevOpsTaskSpec(task_id="t1", risk_flags=["  prod  ", ""])
        assert spec.risk_flags == ["prod"]

    def test_rollback_strip(self) -> None:
        spec = DevOpsTaskSpec(task_id="t1", rollback_requirements=["  rollback  ", ""])
        assert spec.rollback_requirements == ["rollback"]

    def test_security_constraints_strip(self) -> None:
        spec = DevOpsTaskSpec(task_id="t1", security_constraints=["  no secrets  ", ""])
        assert spec.security_constraints == ["no secrets"]

    def test_default_risk_level(self) -> None:
        spec = DevOpsTaskSpec(task_id="t1")
        assert spec.risk_level == "medium"

    def test_full_spec_round_trip(self) -> None:
        spec = _base_task_spec()
        d = spec.model_dump()
        reconstructed = DevOpsTaskSpec(**d)
        assert reconstructed.task_id == spec.task_id
        assert reconstructed.platform_scope.environments == spec.platform_scope.environments


class TestSubtaskContract:
    def test_construction(self) -> None:
        c = SubtaskContract(subtask_id="T1", owner="IaC", objective="Do things")
        assert c.subtask_id == "T1"
        assert c.constraints == []


class TestReviewFinding:
    def test_default_severity(self) -> None:
        f = ReviewFinding(finding_id="F1")
        assert f.severity == "medium"
        assert not f.blocking

    def test_blocking_critical(self) -> None:
        f = ReviewFinding(finding_id="F1", severity="critical", blocking=True)
        assert f.blocking


class TestDevOpsCompletionPackage:
    def test_default_status_is_failed(self) -> None:
        pkg = DevOpsCompletionPackage(task_id="t1")
        assert pkg.status == "failed"

    def test_quality_gates_empty_by_default(self) -> None:
        pkg = DevOpsCompletionPackage(task_id="t1")
        assert pkg.quality_gates == {}


class TestGateStatusAndRiskLevel:
    def test_gate_status_literals(self) -> None:
        for val in ("pass", "fail", "skipped", "not_run"):
            pkg = DevOpsCompletionPackage(task_id="t1", quality_gates={"gate": val})
            assert pkg.quality_gates["gate"] == val

    def test_risk_level_literals(self) -> None:
        for val in ("low", "medium", "high", "critical"):
            spec = DevOpsTaskSpec(task_id="t1", risk_level=val)
            assert spec.risk_level == val


class TestNestedModels:
    def test_platform_scope_defaults(self) -> None:
        ps = PlatformScope()
        assert ps.cloud == ""
        assert ps.environments == []

    def test_constraints_defaults(self) -> None:
        c = DevOpsConstraints()
        assert c.iac.preferred == ""
        assert c.secrets.source == ""

    def test_release_readiness_defaults(self) -> None:
        rr = ReleaseReadiness()
        assert not rr.rollback_available
        assert rr.required_approvals == []


# ===========================================================================
# ENVIRONMENT POLICY TESTS
# ===========================================================================


class TestEnvPolicy:
    def test_dev_allows_auto_deploy(self) -> None:
        assert ENV_POLICY["dev"]["auto_deploy_allowed"] is True
        assert ENV_POLICY["dev"]["approval_required"] is False

    def test_staging_requires_rollback_test(self) -> None:
        assert ENV_POLICY["staging"]["rollback_test_required"] is True

    def test_production_requires_approval(self) -> None:
        assert ENV_POLICY["production"]["approval_required"] is True
        assert ENV_POLICY["production"]["auto_deploy_allowed"] is False


class TestEnforceEnvPolicy:
    def test_blocks_prod_without_approval(self) -> None:
        spec = _base_task_spec(scope={"included": ["build"], "excluded": []})
        reason = DevOpsTeamLeadAgent._enforce_env_policy(spec)
        assert reason is not None
        assert "approval" in reason.lower()

    def test_blocks_prod_without_rollback(self) -> None:
        spec = _base_task_spec(rollback_requirements=[])
        reason = DevOpsTeamLeadAgent._enforce_env_policy(spec)
        assert reason is not None
        assert "rollback" in reason.lower()

    def test_allows_dev_only(self) -> None:
        spec = _base_task_spec(
            platform_scope={"environments": ["dev"]},
            rollback_requirements=[],
        )
        reason = DevOpsTeamLeadAgent._enforce_env_policy(spec)
        assert reason is None

    def test_allows_full_spec(self) -> None:
        spec = _base_task_spec()
        reason = DevOpsTeamLeadAgent._enforce_env_policy(spec)
        assert reason is None


# ===========================================================================
# GATE NAME TESTS
# ===========================================================================


class TestGateNames:
    def test_required_gate_names_present(self) -> None:
        assert "iac_validate" in DEVOPS_REQUIRED_GATE_NAMES
        assert "security_review" in DEVOPS_REQUIRED_GATE_NAMES
        assert "change_review" in DEVOPS_REQUIRED_GATE_NAMES

    def test_required_gate_names_count(self) -> None:
        assert len(DEVOPS_REQUIRED_GATE_NAMES) >= 6


# ===========================================================================
# SUBTASK CONTRACT TESTS
# ===========================================================================


class TestSubtaskContractGeneration:
    def test_generates_three_contracts(self) -> None:
        spec = _base_task_spec()
        contracts = DevOpsTeamLeadAgent._build_subtask_contracts(spec)
        assert len(contracts) == 3

    def test_contract_owners(self) -> None:
        spec = _base_task_spec()
        contracts = DevOpsTeamLeadAgent._build_subtask_contracts(spec)
        owners = {c.owner for c in contracts}
        assert "InfrastructureAsCodeAgent" in owners
        assert "CICDPipelineAgent" in owners
        assert "DeploymentStrategyAgent" in owners

    def test_contract_ids_use_task_id(self) -> None:
        spec = _base_task_spec()
        contracts = DevOpsTeamLeadAgent._build_subtask_contracts(spec)
        for c in contracts:
            assert c.subtask_id.startswith("DO-2207")


# ===========================================================================
# TASK CLARIFIER TESTS
# ===========================================================================


class TestTaskClarifier:
    def _agent(self) -> DevOpsTaskClarifierAgent:
        return DevOpsTaskClarifierAgent(MagicMock())

    def test_blocks_missing_rollback_for_prod(self) -> None:
        spec = _base_task_spec(rollback_requirements=[])
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert not out.approved_for_execution
        assert any("Rollback" in r for r in out.clarification_requests)

    def test_blocks_missing_environments(self) -> None:
        spec = _base_task_spec(platform_scope={"environments": []})
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert not out.approved_for_execution
        assert any("environment" in r.lower() for r in out.clarification_requests)

    def test_blocks_missing_acceptance_criteria(self) -> None:
        spec = _base_task_spec(acceptance_criteria=[])
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert not out.approved_for_execution
        assert any("acceptance" in r.lower() for r in out.clarification_requests)

    def test_blocks_missing_secret_source(self) -> None:
        spec = _base_task_spec(
            constraints={
                "iac": {"preferred": "terraform"},
                "ci_cd": {"platform": "github_actions"},
                "deployment": {"strategy": "rolling"},
                "secrets": {"source": ""},
            }
        )
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert not out.approved_for_execution
        assert any("secret" in r.lower() for r in out.clarification_requests)

    def test_blocks_prod_without_approval_gate(self) -> None:
        spec = _base_task_spec(scope={"included": ["build image"], "excluded": []})
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert not out.approved_for_execution
        assert any("approval" in r.lower() for r in out.clarification_requests)

    def test_blocks_missing_goal(self) -> None:
        spec = _base_task_spec(goal={"summary": ""})
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert not out.approved_for_execution
        assert any("outcome" in r.lower() for r in out.clarification_requests)

    def test_approves_complete_spec(self) -> None:
        mock_llm = MagicMock()
        mock_llm.complete_json.return_value = {
            "approved_for_execution": True,
            "checklist": [],
            "gaps": [],
        }
        agent = DevOpsTaskClarifierAgent(mock_llm)
        spec = _base_task_spec()
        out = agent.run(DevOpsTaskClarifierInput(task_spec=spec))
        assert out.approved_for_execution

    def test_checklist_populated(self) -> None:
        spec = _base_task_spec(rollback_requirements=[])
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert len(out.checklist) >= 3


# ===========================================================================
# TOOL AGENT TESTS
# ===========================================================================


class TestRepoNavigatorToolAgent:
    def test_detects_terraform_files(self) -> None:
        from devops_team.tool_agents import RepoNavigatorInput, RepoNavigatorToolAgent

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "infra").mkdir()
            (Path(tmp) / "infra" / "main.tf").write_text("resource {}")
            out = RepoNavigatorToolAgent().run(RepoNavigatorInput(repo_path=tmp))
            assert any("main.tf" in p for p in out.detected_iac_paths)

    def test_detects_github_workflows(self) -> None:
        from devops_team.tool_agents import RepoNavigatorInput, RepoNavigatorToolAgent

        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = Path(tmp) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "ci.yml").write_text("on: push")
            out = RepoNavigatorToolAgent().run(RepoNavigatorInput(repo_path=tmp))
            assert any("ci.yml" in p for p in out.detected_pipeline_paths)

    def test_detects_helm_charts(self) -> None:
        from devops_team.tool_agents import RepoNavigatorInput, RepoNavigatorToolAgent

        with tempfile.TemporaryDirectory() as tmp:
            helm_dir = Path(tmp) / "deploy" / "helm" / "myapp"
            helm_dir.mkdir(parents=True)
            (helm_dir / "Chart.yaml").write_text("name: myapp")
            out = RepoNavigatorToolAgent().run(RepoNavigatorInput(repo_path=tmp))
            assert any("helm" in p.lower() for p in out.detected_deploy_paths)

    def test_empty_repo(self) -> None:
        from devops_team.tool_agents import RepoNavigatorInput, RepoNavigatorToolAgent

        with tempfile.TemporaryDirectory() as tmp:
            out = RepoNavigatorToolAgent().run(RepoNavigatorInput(repo_path=tmp))
            assert out.detected_iac_paths == []
            assert out.detected_pipeline_paths == []
            assert out.detected_deploy_paths == []


class TestIaCValidationToolAgent:
    def test_skipped_when_no_tf_files(self) -> None:
        from devops_team.tool_agents import IaCValidationInput, IaCValidationToolAgent

        with tempfile.TemporaryDirectory() as tmp:
            out = IaCValidationToolAgent().run(IaCValidationInput(repo_path=tmp))
            assert out.checks["iac_validate"] == "skipped"
            assert out.checks["iac_validate_fmt"] == "skipped"
            assert out.success is True


class TestPolicyAsCodeToolAgent:
    def test_skipped_when_checkov_missing(self) -> None:
        from devops_team.tool_agents import PolicyAsCodeInput, PolicyAsCodeToolAgent

        with tempfile.TemporaryDirectory() as tmp:
            out = PolicyAsCodeToolAgent().run(PolicyAsCodeInput(repo_path=tmp))
            assert out.checks["policy_checks"] == "skipped"
            assert out.success is True


class TestCICDLintToolAgent:
    def test_pass_valid_workflow(self) -> None:
        from devops_team.tool_agents import CICDLintInput, CICDLintPipelineValidationToolAgent

        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = Path(tmp) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "ci.yml").write_text(
                "on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []"
            )
            out = CICDLintPipelineValidationToolAgent().run(CICDLintInput(repo_path=tmp))
            assert out.checks["pipeline_lint"] == "pass"
            assert out.success is True

    def test_fail_missing_jobs(self) -> None:
        from devops_team.tool_agents import CICDLintInput, CICDLintPipelineValidationToolAgent

        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = Path(tmp) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "ci.yml").write_text("on: push\n")
            out = CICDLintPipelineValidationToolAgent().run(CICDLintInput(repo_path=tmp))
            assert out.checks["pipeline_lint"] == "fail"
            assert out.success is False

    def test_fail_prod_deploy_without_approval(self) -> None:
        from devops_team.tool_agents import CICDLintInput, CICDLintPipelineValidationToolAgent

        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = Path(tmp) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "deploy.yml").write_text(
                "on: push\njobs:\n  deploy:\n    runs-on: ubuntu-latest\n    steps: []\n# deploy to production"
            )
            out = CICDLintPipelineValidationToolAgent().run(CICDLintInput(repo_path=tmp))
            assert out.checks["pipeline_gate_check"] == "fail"

    def test_skipped_no_workflows(self) -> None:
        from devops_team.tool_agents import CICDLintInput, CICDLintPipelineValidationToolAgent

        with tempfile.TemporaryDirectory() as tmp:
            out = CICDLintPipelineValidationToolAgent().run(CICDLintInput(repo_path=tmp))
            assert out.checks["pipeline_lint"] == "skipped"
            assert out.success is True


class TestDeploymentDryRunToolAgent:
    def test_skipped_no_chart(self) -> None:
        from devops_team.tool_agents import DeploymentDryRunInput, DeploymentDryRunPlanToolAgent

        with tempfile.TemporaryDirectory() as tmp:
            out = DeploymentDryRunPlanToolAgent().run(DeploymentDryRunInput(repo_path=tmp))
            assert out.checks["deployment_dry_run"] == "skipped"
            assert out.success is True


# ===========================================================================
# CORE AGENT TESTS
# ===========================================================================


class TestInfrastructureAsCodeAgent:
    def test_run_returns_artifacts(self) -> None:
        from devops_team.iac_agent import IaCAgentInput, InfrastructureAsCodeAgent

        mock = MagicMock()
        mock.complete_json.return_value = {
            "artifacts": {"infra/main.tf": "resource {}"},
            "summary": "created main.tf",
            "destructive_changes_detected": False,
            "blast_radius_notes": [],
        }
        agent = InfrastructureAsCodeAgent(mock)
        out = agent.run(IaCAgentInput(task_spec=_base_task_spec()))
        assert "infra/main.tf" in out.artifacts
        assert not out.destructive_changes_detected

    def test_handles_destructive_flag(self) -> None:
        from devops_team.iac_agent import IaCAgentInput, InfrastructureAsCodeAgent

        mock = MagicMock()
        mock.complete_json.return_value = {
            "artifacts": {},
            "summary": "destructive",
            "destructive_changes_detected": True,
            "blast_radius_notes": ["Drops RDS instance"],
        }
        agent = InfrastructureAsCodeAgent(mock)
        out = agent.run(IaCAgentInput(task_spec=_base_task_spec()))
        assert out.destructive_changes_detected
        assert len(out.blast_radius_notes) == 1


class TestCICDPipelineAgent:
    def test_run_returns_artifacts(self) -> None:
        from devops_team.cicd_pipeline_agent import CICDPipelineAgent, CICDPipelineAgentInput

        mock = MagicMock()
        mock.complete_json.return_value = {
            "artifacts": {".github/workflows/ci.yml": "on: push"},
            "pipeline_job_graph_summary": "build -> test -> deploy",
            "required_gates_present": True,
            "summary": "pipeline created",
        }
        agent = CICDPipelineAgent(mock)
        out = agent.run(CICDPipelineAgentInput(task_spec=_base_task_spec()))
        assert ".github/workflows/ci.yml" in out.artifacts
        assert out.required_gates_present


class TestDeploymentStrategyAgent:
    def test_run_returns_strategy(self) -> None:
        from devops_team.deployment_strategy_agent import (
            DeploymentStrategyAgent,
            DeploymentStrategyAgentInput,
        )

        mock = MagicMock()
        mock.complete_json.return_value = {
            "artifacts": {"deploy/values.yaml": "replicas: 2"},
            "strategy": "rolling",
            "rollback_plan": ["helm rollback"],
            "health_checks": ["/healthz"],
            "rollout_timeout_minutes": 10,
            "summary": "deployment ok",
        }
        agent = DeploymentStrategyAgent(mock)
        out = agent.run(DeploymentStrategyAgentInput(task_spec=_base_task_spec()))
        assert out.strategy == "rolling"
        assert len(out.rollback_plan) == 1
        assert out.rollout_timeout_minutes == 10


class TestDevSecOpsReviewAgent:
    def test_blocks_on_high_severity(self) -> None:
        from devops_team.devsecops_review_agent import DevSecOpsReviewAgent, DevSecOpsReviewInput

        mock = MagicMock()
        mock.complete_json.return_value = {
            "approved": False,
            "findings": [
                {
                    "finding_id": "F1",
                    "severity": "high",
                    "area": "iam",
                    "issue": "wildcard",
                    "blocking": True,
                }
            ],
            "summary": "blocked",
        }
        agent = DevSecOpsReviewAgent(mock)
        out = agent.run(DevSecOpsReviewInput(task_description="test", artifacts={}))
        assert not out.approved
        assert len(out.findings) == 1
        assert out.findings[0].severity == "high"

    def test_approves_clean_artifacts(self) -> None:
        from devops_team.devsecops_review_agent import DevSecOpsReviewAgent, DevSecOpsReviewInput

        mock = MagicMock()
        mock.complete_json.return_value = {"approved": True, "findings": [], "summary": "all good"}
        agent = DevSecOpsReviewAgent(mock)
        out = agent.run(DevSecOpsReviewInput(task_description="test", artifacts={}))
        assert out.approved


class TestDevOpsTestValidationAgent:
    def test_aggregates_gates(self) -> None:
        from devops_team.test_validation_agent import (
            DevOpsTestValidationAgent,
            DevOpsTestValidationInput,
        )

        mock = MagicMock()
        mock.complete_json.return_value = {
            "approved": True,
            "quality_gates": {"iac_validate": "pass", "pipeline_lint": "pass"},
            "acceptance_trace": [],
            "summary": "ok",
        }
        agent = DevOpsTestValidationAgent(mock)
        out = agent.run(
            DevOpsTestValidationInput(
                acceptance_criteria=["test"],
                tool_results={"iac": {"iac_validate": "pass"}},
            )
        )
        assert out.approved
        assert out.quality_gates["iac_validate"] == "pass"

    def test_rejects_on_fail_gate(self) -> None:
        from devops_team.test_validation_agent import (
            DevOpsTestValidationAgent,
            DevOpsTestValidationInput,
        )

        mock = MagicMock()
        mock.complete_json.return_value = {
            "approved": True,
            "quality_gates": {"iac_validate": "fail"},
            "summary": "failed",
        }
        agent = DevOpsTestValidationAgent(mock)
        out = agent.run(DevOpsTestValidationInput(acceptance_criteria=[], tool_results={}))
        assert not out.approved


class TestChangeReviewAgent:
    def test_approves(self) -> None:
        from devops_team.change_review_agent import ChangeReviewAgent, ChangeReviewInput

        mock = MagicMock()
        mock.complete_json.return_value = {"approved": True, "findings": [], "summary": "ok"}
        agent = ChangeReviewAgent(mock)
        out = agent.run(ChangeReviewInput(task_description="test", artifacts={}))
        assert out.approved

    def test_blocks_on_finding(self) -> None:
        from devops_team.change_review_agent import ChangeReviewAgent, ChangeReviewInput

        mock = MagicMock()
        mock.complete_json.return_value = {
            "approved": True,
            "findings": [
                {"finding_id": "F1", "severity": "medium", "blocking": True, "issue": "brittle"}
            ],
            "summary": "blocked",
        }
        agent = ChangeReviewAgent(mock)
        out = agent.run(ChangeReviewInput(task_description="test", artifacts={}))
        assert not out.approved


class TestDocumentationRunbookAgent:
    def test_produces_completion_package(self) -> None:
        from devops_team.doc_runbook_agent import (
            DocumentationRunbookAgent,
            DocumentationRunbookInput,
        )

        mock = MagicMock()
        mock.complete_json.return_value = {
            "files": {"docs/runbook.md": "# Runbook"},
            "summary": "done",
        }
        agent = DocumentationRunbookAgent(mock)
        out = agent.run(
            DocumentationRunbookInput(
                task_id="DO-1",
                task_title="test",
                artifacts={"a.tf": "resource"},
                quality_gates={"iac_validate": "pass"},
            )
        )
        assert out.completion_package.task_id == "DO-1"
        assert "docs/runbook.md" in out.files


# ===========================================================================
# INTEGRATION TESTS -- ORCHESTRATOR
# ===========================================================================


class TestDevOpsTeamLeadAgentIntegration:
    def test_happy_path_run_workflow(self) -> None:
        mock_llm = _mock_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            subprocess.run(["git", "init"], cwd=path, capture_output=True, check=False)
            subprocess.run(
                ["git", "config", "user.email", "t@t.com"],
                cwd=path,
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["git", "config", "user.name", "T"], cwd=path, capture_output=True, check=False
            )
            subprocess.run(
                ["git", "config", "commit.gpgsign", "false"],
                cwd=path,
                capture_output=True,
                check=False,
            )
            result = agent.run_workflow(
                repo_path=path,
                task_description="Add backend deployment automation",
                requirements="Include prod approval gate and rollback plan",
                target_repo="backend",
                build_verifier=MagicMock(return_value=(True, "")),
                task_id="devops-backend",
            )
        assert result.success
        assert result.completion_package is not None
        assert result.completion_package.status == "completed"
        assert result.completion_package.task_id == "devops-backend"
        assert result.completion_package.git_operations.branch_created
        assert result.completion_package.handoff is not None

    def test_happy_path_direct_run(self) -> None:
        mock_llm = _mock_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert pkg.task_id == "DO-2207"
        assert pkg.status == "completed"
        assert len(pkg.acceptance_criteria_trace) == 2
        assert pkg.release_readiness.deployment_strategy == "rolling"

    def test_blocked_by_clarifier(self) -> None:
        mock_llm = MagicMock()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec(
            platform_scope={"environments": ["dev"]},
            acceptance_criteria=[],
            rollback_requirements=[],
        )
        with pytest.raises(ValueError, match="Clarification required|DevOps team run failed"):
            agent.run(spec)

    def test_blocked_by_env_policy(self) -> None:
        mock_llm = MagicMock()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec(
            rollback_requirements=[],
            scope={"included": ["build"], "excluded": []},
        )
        with pytest.raises(ValueError, match="policy violation|DevOps team run failed"):
            agent.run(spec)

    def test_blocked_by_security_review(self) -> None:
        mock_llm = MagicMock()
        mock_llm.complete_json.side_effect = [
            {"approved_for_execution": True},
            {"artifacts": {}, "summary": "iac"},
            {"artifacts": {}, "summary": "cicd", "required_gates_present": True},
            {"artifacts": {}, "summary": "deploy", "strategy": "rolling", "rollback_plan": ["rb"]},
            {
                "approved": False,
                "findings": [
                    {"finding_id": "F1", "severity": "high", "blocking": True, "issue": "bad iam"}
                ],
                "summary": "blocked",
            },
            {"approved": True, "findings": [], "summary": "ok"},
            {"approved": True, "quality_gates": {"iac_validate": "pass"}, "summary": "ok"},
        ]
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            result = agent.run_workflow(
                repo_path=Path(tmp),
                task_description="Deploy service",
                requirements="Include prod approval gate and rollback plan",
                task_id="devops-sec-block",
            )
        assert not result.success
        assert "Quality gates failed" in (result.failure_reason or "")
        assert result.completion_package is not None
        assert result.completion_package.status == "blocked"

    def test_completion_package_has_acceptance_trace(self) -> None:
        mock_llm = _mock_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert len(pkg.acceptance_criteria_trace) == len(spec.acceptance_criteria)
        for trace in pkg.acceptance_criteria_trace:
            assert trace.criterion
            assert len(trace.implementation_refs) > 0

    def test_completion_package_has_release_readiness(self) -> None:
        mock_llm = _mock_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert pkg.release_readiness.rollback_available
        assert "manual_prod_approval" in pkg.release_readiness.required_approvals

    def test_completion_package_has_git_operations(self) -> None:
        mock_llm = _mock_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert pkg.git_operations.branch_created.startswith("feature/")
        assert len(pkg.git_operations.commits) >= 1
        assert pkg.git_operations.merge is not None
        assert pkg.git_operations.merge.target_branch == "development"

    def test_completion_package_files_changed(self) -> None:
        mock_llm = _mock_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert len(pkg.files_changed) > 0

    def test_quality_gates_in_completion(self) -> None:
        mock_llm = _mock_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert "security_review" in pkg.quality_gates
        assert "change_review" in pkg.quality_gates

    def test_build_verifier_failure(self) -> None:
        mock_llm = _mock_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init"], cwd=tmp, capture_output=True, check=False)
            subprocess.run(
                ["git", "config", "user.email", "t@t.com"],
                cwd=tmp,
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["git", "config", "user.name", "T"], cwd=tmp, capture_output=True, check=False
            )
            subprocess.run(
                ["git", "config", "commit.gpgsign", "false"],
                cwd=tmp,
                capture_output=True,
                check=False,
            )
            result = agent.run_workflow(
                repo_path=Path(tmp),
                task_description="Deploy",
                requirements="Include prod approval and rollback plan",
                build_verifier=MagicMock(return_value=(False, "Docker build failed")),
                task_id="devops-bv-fail",
            )
        assert not result.success
        assert "Build verification failed" in (
            result.failure_reason or ""
        ) or "Docker build failed" in (result.failure_reason or "")


# ===========================================================================
# COMPATIBILITY / MIGRATION TESTS
# ===========================================================================


class TestBackwardCompatibility:
    def test_run_workflow_accepts_legacy_args(self) -> None:
        mock_llm = _mock_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init"], cwd=tmp, capture_output=True, check=False)
            subprocess.run(
                ["git", "config", "user.email", "t@t.com"],
                cwd=tmp,
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["git", "config", "user.name", "T"], cwd=tmp, capture_output=True, check=False
            )
            subprocess.run(
                ["git", "config", "commit.gpgsign", "false"],
                cwd=tmp,
                capture_output=True,
                check=False,
            )
            result = agent.run_workflow(
                repo_path=Path(tmp),
                task_description="Add CI/CD",
                requirements="Include prod approval gate and rollback plan",
                architecture=None,
                existing_pipeline=None,
                target_repo=None,
                tech_stack=["python"],
                build_verifier=MagicMock(return_value=(True, "")),
                task_id="devops-legacy",
                subdir="",
                max_iterations=1,
                devops_review_agent=None,
            )
        assert isinstance(result, DevOpsTeamResult)
        assert result.success

    def test_build_legacy_spec_prod_detection(self) -> None:
        spec = DevOpsTeamLeadAgent._build_legacy_spec(
            task_id="devops-1",
            task_description="Deploy to production",
            requirements="Prod pipeline needed",
        )
        assert spec.environment == "production"
        assert "production" in spec.platform_scope.environments

    def test_build_legacy_spec_staging_default(self) -> None:
        spec = DevOpsTeamLeadAgent._build_legacy_spec(
            task_id="devops-2",
            task_description="Set up CI",
            requirements="Run tests on push",
        )
        assert spec.environment == "staging"

    def test_build_legacy_spec_does_not_match_produce_as_prod(self) -> None:
        spec = DevOpsTeamLeadAgent._build_legacy_spec(
            task_id="devops-2b",
            task_description="Produce a Dockerfile and CI/CD",
            requirements="Build and deploy to staging",
        )
        assert spec.environment == "staging"

    def test_build_legacy_spec_always_has_rollback(self) -> None:
        spec = DevOpsTeamLeadAgent._build_legacy_spec(
            task_id="devops-3",
            task_description="Deploy",
            requirements="Ship it",
        )
        assert len(spec.rollback_requirements) > 0

    def test_build_legacy_spec_always_has_acceptance(self) -> None:
        spec = DevOpsTeamLeadAgent._build_legacy_spec(
            task_id="devops-4",
            task_description="Deploy",
            requirements="Ship it",
        )
        assert len(spec.acceptance_criteria) > 0


# ===========================================================================
# MAIN ORCHESTRATOR INTEGRATION
# ===========================================================================


class TestDevOpsTeamLeadAgentExecutionTools:
    """Verify execution tool agents are initialized on DevOpsTeamLeadAgent."""

    def test_init_has_execution_tools(self) -> None:
        mock_llm = MagicMock()
        mock_llm.complete_json.return_value = {}
        agent = DevOpsTeamLeadAgent(mock_llm)
        assert hasattr(agent, "terraform_exec_tool")
        assert hasattr(agent, "cdk_exec_tool")
        assert hasattr(agent, "compose_exec_tool")
        assert hasattr(agent, "helm_exec_tool")
        assert hasattr(agent, "infra_debug_agent")
        assert hasattr(agent, "infra_patch_agent")

    def test_run_execution_tools_returns_empty_for_no_artifacts(self) -> None:
        mock_llm = MagicMock()
        mock_llm.complete_json.return_value = {}
        agent = DevOpsTeamLeadAgent(mock_llm)
        results = agent._run_execution_tools("/tmp/nonexistent", {})
        assert results == []


class TestMainOrchestratorRegistration:
    def test_devops_team_lead_registered(self) -> None:
        """Verify the main orchestrator registers DevOpsTeamLeadAgent."""
        import importlib

        mod = importlib.import_module("orchestrator")
        source = Path(mod.__file__).read_text()
        assert "DevOpsTeamLeadAgent" in source
        assert "devops_team" in source

    def test_build_fix_specialist_registered(self) -> None:
        """Verify the main orchestrator registers BuildFixSpecialistAgent."""
        import importlib

        mod = importlib.import_module("orchestrator")
        source = Path(mod.__file__).read_text()
        assert "build_fix_specialist" in source
        assert "BuildFixSpecialistAgent" in source
