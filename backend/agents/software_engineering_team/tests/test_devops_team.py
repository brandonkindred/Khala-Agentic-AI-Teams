"""Tests for the DevOps team orchestrator, models, agents, and tool agents."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.devops_team import (
    DevOpsTaskSpec,
    DevOpsTeamLeadAgent,
    DevOpsTeamResult,
    tool_dispatch,
)
from software_engineering_team.devops_team.models import (
    CriterionTrace,
    DevOpsCompletionPackage,
    DevOpsConstraints,
    PlatformScope,
    ReleaseReadiness,
    ReviewFinding,
    SubtaskContract,
)
from software_engineering_team.devops_team.orchestrator import (
    DEVOPS_REQUIRED_GATE_NAMES,
    ENV_POLICY,
    criterion_traces_from_phase4,
)
from software_engineering_team.devops_team.task_clarifier import (
    DevOpsTaskClarifierAgent,
    DevOpsTaskClarifierInput,
)
from software_engineering_team.devops_team.tool_agents import (
    CDKExecutionOutput,
    CICDLintOutput,
    DeploymentDryRunOutput,
    DockerComposeExecutionOutput,
    HelmExecutionOutput,
    IaCValidationOutput,
    PolicyAsCodeInput,
    PolicyAsCodeOutput,
    TerraformExecutionOutput,
)
from software_engineering_team.shared.git_utils import initialize_new_repo


class _StubClient(DummyLLMClient):
    """DummyLLMClient subclass returning a single canned response for every
    ``complete_json`` call. Routes transparently through the Strands adapter
    path (``chat_json_round`` → ``StructuredOutputTool`` detection → the
    complete_json override below)."""

    def __init__(self, response: Dict[str, Any]) -> None:
        super().__init__()
        self._response = response

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return self._response


class _ScriptedClient(DummyLLMClient):
    """DummyLLMClient subclass returning a different canned response on each
    ``complete_json`` call. Replaces the Wave 1/2/3 pre-migration pattern of
    ``mock.complete_json.side_effect = [...]`` for scripted DevOps pipelines."""

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        # After the scripted list is exhausted, fall back to the last entry
        # so extra pipeline steps don't crash the test.
        return self._responses[-1] if self._responses else {}


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


def _scripted_llm_for_happy_path(*, alerting_configured: bool = True) -> _ScriptedClient:
    """Script a full DevOps pipeline run: one response per sub-agent call in
    orchestrator order (task_clarifier, iac, cicd, deployment, infra_debug,
    devsecops, change_review, test_validation, doc_runbook).

    ``alerting_configured`` is set only on the deployment-strategy response.
    The doc_runbook LLM payload intentionally omits it — the doc agent uses a
    Python-side ``False`` placeholder that Phase 5 overwrites from deploy output.
    """
    return _ScriptedClient(
        [
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
                "alerting_configured": alerting_configured,
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
                "acceptance_trace": [
                    {
                        "criterion": "Pipeline runs tests and scan before deploy",
                        "implementation_refs": ["infra/main.tf"],
                        "tests": [{"iac_validate": "pass"}],
                    }
                ],
                "summary": "validation ok",
            },
            {"files": {"docs/runbook.md": "# Runbook"}, "summary": "doc ok"},
        ]
    )


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
        client = _StubClient(
            {
                "approved_for_execution": True,
                "checklist": [],
                "gaps": [],
            }
        )
        agent = DevOpsTaskClarifierAgent(client)
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
        from software_engineering_team.devops_team.tool_agents import (
            RepoNavigatorInput,
            RepoNavigatorToolAgent,
        )

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "infra").mkdir()
            (Path(tmp) / "infra" / "main.tf").write_text("resource {}")
            out = RepoNavigatorToolAgent().run(RepoNavigatorInput(repo_path=tmp))
            assert any("main.tf" in p for p in out.detected_iac_paths)

    def test_detects_github_workflows(self) -> None:
        from software_engineering_team.devops_team.tool_agents import (
            RepoNavigatorInput,
            RepoNavigatorToolAgent,
        )

        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = Path(tmp) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "ci.yml").write_text("on: push")
            out = RepoNavigatorToolAgent().run(RepoNavigatorInput(repo_path=tmp))
            assert any("ci.yml" in p for p in out.detected_pipeline_paths)

    def test_detects_helm_charts(self) -> None:
        from software_engineering_team.devops_team.tool_agents import (
            RepoNavigatorInput,
            RepoNavigatorToolAgent,
        )

        with tempfile.TemporaryDirectory() as tmp:
            helm_dir = Path(tmp) / "deploy" / "helm" / "myapp"
            helm_dir.mkdir(parents=True)
            (helm_dir / "Chart.yaml").write_text("name: myapp")
            out = RepoNavigatorToolAgent().run(RepoNavigatorInput(repo_path=tmp))
            assert any("helm" in p.lower() for p in out.detected_deploy_paths)

    def test_empty_repo(self) -> None:
        from software_engineering_team.devops_team.tool_agents import (
            RepoNavigatorInput,
            RepoNavigatorToolAgent,
        )

        with tempfile.TemporaryDirectory() as tmp:
            out = RepoNavigatorToolAgent().run(RepoNavigatorInput(repo_path=tmp))
            assert out.detected_iac_paths == []
            assert out.detected_pipeline_paths == []
            assert out.detected_deploy_paths == []


class TestIaCValidationToolAgent:
    def test_skipped_when_no_tf_files(self) -> None:
        from software_engineering_team.devops_team.tool_agents import (
            IaCValidationInput,
            IaCValidationToolAgent,
        )

        with tempfile.TemporaryDirectory() as tmp:
            out = IaCValidationToolAgent().run(IaCValidationInput(repo_path=tmp))
            assert out.checks["iac_validate"] == "skipped"
            assert out.checks["iac_validate_fmt"] == "skipped"
            assert out.success is True


class TestPolicyAsCodeToolAgent:
    def test_skipped_when_checkov_missing(self) -> None:
        from software_engineering_team.devops_team.tool_agents import (
            PolicyAsCodeInput,
            PolicyAsCodeToolAgent,
        )

        with tempfile.TemporaryDirectory() as tmp:
            out = PolicyAsCodeToolAgent().run(PolicyAsCodeInput(repo_path=tmp))
            assert out.checks["policy_checks"] == "skipped"
            assert out.success is True


class TestCICDLintToolAgent:
    def test_pass_valid_workflow(self) -> None:
        from software_engineering_team.devops_team.tool_agents import (
            CICDLintInput,
            CICDLintPipelineValidationToolAgent,
        )

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
        from software_engineering_team.devops_team.tool_agents import (
            CICDLintInput,
            CICDLintPipelineValidationToolAgent,
        )

        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = Path(tmp) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "ci.yml").write_text("on: push\n")
            out = CICDLintPipelineValidationToolAgent().run(CICDLintInput(repo_path=tmp))
            assert out.checks["pipeline_lint"] == "fail"
            assert out.success is False

    def test_fail_prod_deploy_without_approval(self) -> None:
        from software_engineering_team.devops_team.tool_agents import (
            CICDLintInput,
            CICDLintPipelineValidationToolAgent,
        )

        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = Path(tmp) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "deploy.yml").write_text(
                "on: push\njobs:\n  deploy:\n    runs-on: ubuntu-latest\n    steps: []\n# deploy to production"
            )
            out = CICDLintPipelineValidationToolAgent().run(CICDLintInput(repo_path=tmp))
            assert out.checks["pipeline_gate_check"] == "fail"

    def test_skipped_no_workflows(self) -> None:
        from software_engineering_team.devops_team.tool_agents import (
            CICDLintInput,
            CICDLintPipelineValidationToolAgent,
        )

        with tempfile.TemporaryDirectory() as tmp:
            out = CICDLintPipelineValidationToolAgent().run(CICDLintInput(repo_path=tmp))
            assert out.checks["pipeline_lint"] == "skipped"
            assert out.success is True


class TestDeploymentDryRunToolAgent:
    def test_skipped_no_chart(self) -> None:
        from software_engineering_team.devops_team.tool_agents import (
            DeploymentDryRunInput,
            DeploymentDryRunPlanToolAgent,
        )

        with tempfile.TemporaryDirectory() as tmp:
            out = DeploymentDryRunPlanToolAgent().run(DeploymentDryRunInput(repo_path=tmp))
            assert out.checks["deployment_dry_run"] == "skipped"
            assert out.success is True


# ===========================================================================
# CORE AGENT TESTS
# ===========================================================================


class TestInfrastructureAsCodeAgent:
    def test_run_returns_artifacts(self) -> None:
        from software_engineering_team.devops_team.iac_agent import (
            IaCAgentInput,
            InfrastructureAsCodeAgent,
        )

        client = _StubClient(
            {
                "artifacts": {"infra/main.tf": "resource {}"},
                "summary": "created main.tf",
                "destructive_changes_detected": False,
                "blast_radius_notes": [],
            }
        )
        agent = InfrastructureAsCodeAgent(client)
        out = agent.run(IaCAgentInput(task_spec=_base_task_spec()))
        assert "infra/main.tf" in out.artifacts
        assert not out.destructive_changes_detected

    def test_handles_destructive_flag(self) -> None:
        from software_engineering_team.devops_team.iac_agent import (
            IaCAgentInput,
            InfrastructureAsCodeAgent,
        )

        client = _StubClient(
            {
                "artifacts": {},
                "summary": "destructive",
                "destructive_changes_detected": True,
                "blast_radius_notes": ["Drops RDS instance"],
            }
        )
        agent = InfrastructureAsCodeAgent(client)
        out = agent.run(IaCAgentInput(task_spec=_base_task_spec()))
        assert out.destructive_changes_detected
        assert len(out.blast_radius_notes) == 1


class TestCICDPipelineAgent:
    def test_run_returns_artifacts(self) -> None:
        from software_engineering_team.devops_team.cicd_pipeline_agent import (
            CICDPipelineAgent,
            CICDPipelineAgentInput,
        )

        client = _StubClient(
            {
                "artifacts": {".github/workflows/ci.yml": "on: push"},
                "pipeline_job_graph_summary": "build -> test -> deploy",
                "required_gates_present": True,
                "summary": "pipeline created",
            }
        )
        agent = CICDPipelineAgent(client)
        out = agent.run(CICDPipelineAgentInput(task_spec=_base_task_spec()))
        assert ".github/workflows/ci.yml" in out.artifacts
        assert out.required_gates_present

    def test_run_surfaces_frontend_pipeline_artifact(self) -> None:
        """As the single CI/CD owner, the agent carries frontend workflow artifacts too."""
        from software_engineering_team.devops_team.cicd_pipeline_agent import (
            CICDPipelineAgent,
            CICDPipelineAgentInput,
        )

        client = _StubClient(
            {
                "artifacts": {".github/workflows/frontend.yml": "on: pull_request"},
                "pipeline_job_graph_summary": "install -> lint -> build -> test -> preview",
                "required_gates_present": True,
                "summary": "frontend pipeline created",
            }
        )
        agent = CICDPipelineAgent(client)
        out = agent.run(CICDPipelineAgentInput(task_spec=_base_task_spec()))
        assert ".github/workflows/frontend.yml" in out.artifacts

    def test_prompt_covers_frontend_concerns(self) -> None:
        """The merged prompt owns frontend CI/CD (preview env, bundle, source maps)."""
        from software_engineering_team.devops_team.cicd_pipeline_agent.prompts import (
            CICD_PIPELINE_PROMPT,
        )

        lowered = CICD_PIPELINE_PROMPT.lower()
        assert "preview environment" in lowered
        assert "bundle-size" in lowered or "bundle size" in lowered
        assert "source map" in lowered
        assert "frontend.yml" in lowered


class TestDeploymentStrategyAgent:
    def test_run_returns_strategy(self) -> None:
        from software_engineering_team.devops_team.deployment_strategy_agent import (
            DeploymentStrategyAgent,
            DeploymentStrategyAgentInput,
        )

        client = _StubClient(
            {
                "artifacts": {"deploy/values.yaml": "replicas: 2"},
                "strategy": "rolling",
                "rollback_plan": ["helm rollback"],
                "health_checks": ["/healthz"],
                "rollout_timeout_minutes": 10,
                "summary": "deployment ok",
            }
        )
        agent = DeploymentStrategyAgent(client)
        out = agent.run(DeploymentStrategyAgentInput(task_spec=_base_task_spec()))
        assert out.strategy == "rolling"
        assert len(out.rollback_plan) == 1
        assert out.rollout_timeout_minutes == 10
        assert out.alerting_configured is False

    def test_build_output_alerting_configured_missing_defaults_false(self) -> None:
        from software_engineering_team.devops_team.deployment_strategy_agent import (
            DeploymentStrategyAgent,
            DeploymentStrategyAgentInput,
        )

        agent = DeploymentStrategyAgent(_StubClient({}))
        out = agent.build_output(
            DeploymentStrategyAgentInput(task_spec=_base_task_spec()),
            {"strategy": "rolling", "summary": "ok"},
        )
        assert out.alerting_configured is False

    def test_build_output_alerting_configured_false(self) -> None:
        from software_engineering_team.devops_team.deployment_strategy_agent import (
            DeploymentStrategyAgent,
            DeploymentStrategyAgentInput,
        )

        agent = DeploymentStrategyAgent(_StubClient({}))
        out = agent.build_output(
            DeploymentStrategyAgentInput(task_spec=_base_task_spec()),
            {"alerting_configured": False},
        )
        assert out.alerting_configured is False

    def test_build_output_alerting_configured_true(self) -> None:
        from software_engineering_team.devops_team.deployment_strategy_agent import (
            DeploymentStrategyAgent,
            DeploymentStrategyAgentInput,
        )

        agent = DeploymentStrategyAgent(_StubClient({}))
        out = agent.build_output(
            DeploymentStrategyAgentInput(task_spec=_base_task_spec()),
            {"alerting_configured": True},
        )
        assert out.alerting_configured is True

    def test_build_output_alerting_configured_string_false_is_false(self) -> None:
        """Schema-drift ``\"false\"`` must not become True via Python truthiness."""
        from software_engineering_team.devops_team.deployment_strategy_agent import (
            DeploymentStrategyAgent,
            DeploymentStrategyAgentInput,
        )

        agent = DeploymentStrategyAgent(_StubClient({}))
        out = agent.build_output(
            DeploymentStrategyAgentInput(task_spec=_base_task_spec()),
            {"alerting_configured": "false"},
        )
        assert out.alerting_configured is False

    def test_build_output_alerting_configured_string_true_is_true(self) -> None:
        from software_engineering_team.devops_team.deployment_strategy_agent import (
            DeploymentStrategyAgent,
            DeploymentStrategyAgentInput,
        )

        agent = DeploymentStrategyAgent(_StubClient({}))
        out = agent.build_output(
            DeploymentStrategyAgentInput(task_spec=_base_task_spec()),
            {"alerting_configured": "TRUE"},
        )
        assert out.alerting_configured is True


class TestDevSecOpsReviewAgent:
    def test_blocks_on_high_severity(self) -> None:
        from software_engineering_team.devops_team.devsecops_review_agent import (
            DevSecOpsReviewAgent,
            DevSecOpsReviewInput,
        )

        client = _StubClient(
            {
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
        )
        agent = DevSecOpsReviewAgent(client)
        out = agent.run(DevSecOpsReviewInput(task_description="test", artifacts={}))
        assert not out.approved
        assert len(out.findings) == 1
        assert out.findings[0].severity == "high"

    def test_approves_clean_artifacts(self) -> None:
        from software_engineering_team.devops_team.devsecops_review_agent import (
            DevSecOpsReviewAgent,
            DevSecOpsReviewInput,
        )

        client = _StubClient({"approved": True, "findings": [], "summary": "all good"})
        agent = DevSecOpsReviewAgent(client)
        out = agent.run(DevSecOpsReviewInput(task_description="test", artifacts={}))
        assert out.approved

    def test_explicit_null_approved_fails_closed(self) -> None:
        """A present-but-null ``approved`` is an explicit non-approval (fail
        closed), even with no blocking findings — matching legacy semantics."""
        from software_engineering_team.devops_team.devsecops_review_agent import (
            DevSecOpsReviewAgent,
            DevSecOpsReviewInput,
        )

        client = _StubClient({"approved": None, "findings": [], "summary": "unsure"})
        agent = DevSecOpsReviewAgent(client)
        out = agent.run(DevSecOpsReviewInput(task_description="test", artifacts={}))
        assert not out.approved

    def test_absent_approved_defers_to_findings(self) -> None:
        """An absent ``approved`` key defers to the finding-derived default:
        no blocking findings -> approved."""
        from software_engineering_team.devops_team.devsecops_review_agent import (
            DevSecOpsReviewAgent,
            DevSecOpsReviewInput,
        )

        client = _StubClient({"findings": [], "summary": "no opinion"})
        agent = DevSecOpsReviewAgent(client)
        out = agent.run(DevSecOpsReviewInput(task_description="test", artifacts={}))
        assert out.approved


class TestDevOpsTestValidationAgent:
    def test_aggregates_gates(self) -> None:
        from software_engineering_team.devops_team.test_validation_agent import (
            DevOpsTestValidationAgent,
            DevOpsTestValidationInput,
        )

        client = _StubClient(
            {
                "approved": True,
                "quality_gates": {"iac_validate": "pass", "pipeline_lint": "pass"},
                "acceptance_trace": [],
                "summary": "ok",
            }
        )
        agent = DevOpsTestValidationAgent(client)
        out = agent.run(
            DevOpsTestValidationInput(
                acceptance_criteria=["test"],
                tool_results={"iac": {"iac_validate": "pass"}},
            )
        )
        assert out.approved
        assert out.quality_gates["iac_validate"] == "pass"

    def test_rejects_on_fail_gate(self) -> None:
        from software_engineering_team.devops_team.test_validation_agent import (
            DevOpsTestValidationAgent,
            DevOpsTestValidationInput,
        )

        client = _StubClient(
            {
                "approved": True,
                "quality_gates": {"iac_validate": "fail"},
                "summary": "failed",
            }
        )
        agent = DevOpsTestValidationAgent(client)
        out = agent.run(DevOpsTestValidationInput(acceptance_criteria=[], tool_results={}))
        assert not out.approved

    def test_delegates_to_unified_qa_agent(self) -> None:
        from software_engineering_team.devops_team.test_validation_agent import (
            DevOpsTestValidationAgent,
        )
        from software_engineering_team.qa_agent import QAExpertAgent

        agent = DevOpsTestValidationAgent(_StubClient({"approved": True}))
        assert isinstance(agent._qa, QAExpertAgent)

    def test_preserves_devops_model_routing_key(self, monkeypatch) -> None:
        """A non-Strands client resolves the model under the 'devops' routing
        key (the pre-refactor key), not the QA agent's default 'qa'."""
        from software_engineering_team.devops_team.test_validation_agent import (
            DevOpsTestValidationAgent,
        )
        from software_engineering_team.devops_team.test_validation_agent import agent as agent_mod

        captured: Dict[str, Any] = {}

        def _fake_get_strands_model(key: str, **_kwargs: Any) -> Any:
            captured["key"] = key
            return DummyLLMClient()  # a Strands Model — used directly downstream

        monkeypatch.setattr(agent_mod, "get_strands_model", _fake_get_strands_model)
        DevOpsTestValidationAgent(object())  # non-None, non-Strands -> resolves via key
        assert captured["key"] == "devops"

    def test_qa_delegation_exception_fails_closed(self) -> None:
        """If the delegated QA agent raises, the shim returns a fail-closed
        result instead of propagating the exception to the orchestrator."""
        from software_engineering_team.devops_team.test_validation_agent import (
            DevOpsTestValidationAgent,
            DevOpsTestValidationInput,
        )

        agent = DevOpsTestValidationAgent(_StubClient({"approved": True}))

        def _boom(_inp: Any) -> Any:
            raise RuntimeError("LLM unavailable")

        agent._qa.run = _boom  # type: ignore[assignment]
        out = agent.run(DevOpsTestValidationInput(acceptance_criteria=["c1"], tool_results={}))
        assert out.approved is False
        assert out.quality_gates.get("test_validation") == "fail"
        assert "LLM unavailable" in out.summary

    def test_maps_evidence_and_trace_through(self) -> None:
        from software_engineering_team.devops_team.test_validation_agent import (
            DevOpsTestValidationAgent,
            DevOpsTestValidationInput,
        )

        client = _StubClient(
            {
                "approved": True,
                "quality_gates": {"unit_tests": "pass"},
                "acceptance_trace": [
                    {"criterion": "c1", "implementation_refs": ["app.py"], "tests": []}
                ],
                "validation_evidence": [
                    {"gate": "unit_tests", "status": "pass", "detail": "12 passed"}
                ],
                "summary": "ok",
            }
        )
        out = DevOpsTestValidationAgent(client).run(
            DevOpsTestValidationInput(acceptance_criteria=["c1"], tool_results={})
        )
        assert out.acceptance_trace and out.acceptance_trace[0]["criterion"] == "c1"
        assert out.evidence and out.evidence[0].gate == "unit_tests"
        assert out.evidence[0].status == "pass"

    def test_unknown_gate_status_coerced_to_not_run(self) -> None:
        from software_engineering_team.devops_team.test_validation_agent import (
            DevOpsTestValidationAgent,
            DevOpsTestValidationInput,
        )

        client = _StubClient(
            {
                "approved": True,
                "quality_gates": {"unit_tests": "flaky"},  # not a valid GateStatus
                "summary": "ok",
            }
        )
        out = DevOpsTestValidationAgent(client).run(
            DevOpsTestValidationInput(acceptance_criteria=[], tool_results={})
        )
        assert out.quality_gates["unit_tests"] == "not_run"

    def test_unapproved_without_fail_gate_fails_closed(self) -> None:
        """An unapproved validation with no failing gate must synthesize one so
        the gate-only DevOps pipeline still blocks (fail closed)."""
        from software_engineering_team.devops_team.test_validation_agent import (
            DevOpsTestValidationAgent,
            DevOpsTestValidationInput,
        )

        client = _StubClient(
            {
                "approved": False,  # unapproved but no "fail" gate present
                "quality_gates": {"unit_tests": "not_run"},
                "summary": "could not validate",
            }
        )
        out = DevOpsTestValidationAgent(client).run(
            DevOpsTestValidationInput(acceptance_criteria=["c1"], tool_results={})
        )
        assert not out.approved
        assert any(v == "fail" for v in out.quality_gates.values())

    def test_approved_does_not_synthesize_fail_gate(self) -> None:
        from software_engineering_team.devops_team.test_validation_agent import (
            DevOpsTestValidationAgent,
            DevOpsTestValidationInput,
        )

        client = _StubClient(
            {
                "approved": True,
                "quality_gates": {"unit_tests": "pass"},
                "summary": "ok",
            }
        )
        out = DevOpsTestValidationAgent(client).run(
            DevOpsTestValidationInput(acceptance_criteria=["c1"], tool_results={})
        )
        assert out.approved
        assert "test_validation" not in out.quality_gates


class TestChangeReviewAgent:
    # The gate now routes through the shared code-review engine with the
    # ``devops_maintainability`` profile, so stubs return the engine's flat
    # issue shape ({approved, issues, summary}); the adapter maps those issues
    # to ``ReviewFinding`` and re-derives approval from blocking severities.

    def test_requires_client(self) -> None:
        """Constructing with a None client fails fast via an explicit ValueError."""
        from software_engineering_team.devops_team.change_review_agent import ChangeReviewAgent

        with pytest.raises(ValueError):
            ChangeReviewAgent(None)

    def test_empty_artifacts_approve_without_engine(self) -> None:
        """No artifacts => nothing to block on and the engine is never invoked.

        Uses a tripwire client that raises if the engine touches it, so the
        short-circuit is verified rather than merely tolerated.
        """
        from software_engineering_team.devops_team.change_review_agent import (
            ChangeReviewAgent,
            ChangeReviewInput,
        )

        class _TripWireClient(DummyLLMClient):
            def complete_json(self, *a, **kw):  # type: ignore[override]
                raise AssertionError("engine must not be called when artifacts are empty")

            def chat_json_round(self, *a, **kw):  # type: ignore[override]
                raise AssertionError("engine must not be called when artifacts are empty")

        agent = ChangeReviewAgent(_TripWireClient())
        out = agent.run(ChangeReviewInput(task_description="test", artifacts={}))
        assert out.approved
        assert out.findings == []

    def test_approves_when_engine_finds_nothing(self) -> None:
        """A clean engine result yields approval with no findings."""
        from software_engineering_team.devops_team.change_review_agent import (
            ChangeReviewAgent,
            ChangeReviewInput,
        )

        client = _StubClient({"approved": True, "issues": [], "summary": "ok"})
        agent = ChangeReviewAgent(client)
        out = agent.run(
            ChangeReviewInput(task_description="test", artifacts={"Dockerfile": "FROM x\n"})
        )
        assert out.approved
        assert out.findings == []

    def test_blocks_on_high_severity_finding(self) -> None:
        """A high-severity engine issue maps to a blocking ReviewFinding and the
        blocking rule overrides the engine's approved flag."""
        from software_engineering_team.devops_team.change_review_agent import (
            ChangeReviewAgent,
            ChangeReviewInput,
        )

        client = _StubClient(
            {
                "approved": True,  # engine flag is overridden by the blocking rule
                "issues": [
                    {
                        "severity": "high",
                        "category": "brittle-automation",
                        "file_path": "Dockerfile",
                        "description": "latest tag pinned nowhere",
                        "suggestion": "pin a digest",
                    }
                ],
                "summary": "blocked",
            }
        )
        agent = ChangeReviewAgent(client)
        out = agent.run(
            ChangeReviewInput(task_description="test", artifacts={"Dockerfile": "FROM x:latest\n"})
        )
        assert not out.approved
        assert len(out.findings) == 1
        assert out.findings[0].blocking
        assert out.findings[0].severity == "high"

    def test_engine_unavailable_degrades_to_approved(self, monkeypatch) -> None:
        """A CodeReviewUnavailableError degrades the gate to approved (no findings)
        rather than crashing the DevOps pipeline."""
        from software_engineering_team.code_review_agent import CodeReviewUnavailableError
        from software_engineering_team.devops_team.change_review_agent import (
            ChangeReviewAgent,
            ChangeReviewInput,
        )

        class _RaisingEngine:
            def __init__(self, exc):
                self._exc = exc

            def __call__(self, _llm):
                return self

            def run(self, _input):
                raise self._exc

        monkeypatch.setattr(
            "software_engineering_team.devops_team.change_review_agent.agent.CodeReviewAgent",
            _RaisingEngine(CodeReviewUnavailableError("engine down")),
        )
        agent = ChangeReviewAgent(_StubClient({}))
        out = agent.run(
            ChangeReviewInput(task_description="test", artifacts={"Dockerfile": "FROM x\n"})
        )
        assert out.approved
        assert out.findings == []
        assert "unavailable" in out.summary.lower()

    def test_engine_programming_error_propagates(self, monkeypatch) -> None:
        """An unexpected engine error (e.g. TypeError) is not masked — it propagates."""
        from software_engineering_team.devops_team.change_review_agent import (
            ChangeReviewAgent,
            ChangeReviewInput,
        )

        class _RaisingEngine:
            def __call__(self, _llm):
                return self

            def run(self, _input):
                raise TypeError("boom")

        monkeypatch.setattr(
            "software_engineering_team.devops_team.change_review_agent.agent.CodeReviewAgent", _RaisingEngine()
        )
        agent = ChangeReviewAgent(_StubClient({}))
        with pytest.raises(TypeError):
            agent.run(
                ChangeReviewInput(task_description="test", artifacts={"Dockerfile": "FROM x\n"})
            )

    def test_unrecognized_severity_maps_to_low_with_warning(self, caplog) -> None:
        """_normalize_severity maps an unrecognized value to 'low' and warns.

        (The engine sanitizes severities to its known set, so this defensive
        branch is exercised at the helper level rather than through ``run``.)"""
        import logging

        from software_engineering_team.devops_team.change_review_agent.agent import (
            _normalize_severity,
        )

        with caplog.at_level(logging.WARNING):
            assert _normalize_severity("catastrophic") == "low"
        assert "unrecognized severity" in caplog.text.lower()

    def test_info_severity_maps_to_low_and_does_not_block(self) -> None:
        """An engine 'info' severity maps to ReviewFinding 'low' and does not block."""
        from software_engineering_team.devops_team.change_review_agent import (
            ChangeReviewAgent,
            ChangeReviewInput,
        )

        client = _StubClient(
            {
                "approved": True,
                "issues": [
                    {
                        "severity": "info",
                        "category": "maintainability",
                        "file_path": "Dockerfile",
                        "description": "consider a comment",
                        "suggestion": "add one",
                    }
                ],
                "summary": "fyi",
            }
        )
        agent = ChangeReviewAgent(client)
        out = agent.run(
            ChangeReviewInput(task_description="test", artifacts={"Dockerfile": "FROM x\n"})
        )
        assert out.approved
        assert out.findings[0].severity == "low"
        assert not out.findings[0].blocking


class TestDocumentationRunbookAgent:
    def test_produces_completion_package(self) -> None:
        from software_engineering_team.devops_team.doc_runbook_agent import (
            DocumentationRunbookAgent,
            DocumentationRunbookInput,
        )

        client = _StubClient(
            {
                "files": {"docs/runbook.md": "# Runbook"},
                "summary": "done",
            }
        )
        agent = DocumentationRunbookAgent(client)
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
        assert out.completion_package.release_readiness.alerting_configured is False


# ===========================================================================
# UNIT TESTS -- PHASE 4 CRITERION TRACE MAPPER
# ===========================================================================


class TestCriterionTracesFromPhase4:
    """Unit tests for the Phase 4 → CriterionTrace mapper."""

    def test_match_uses_phase4_entry(self) -> None:
        traces = criterion_traces_from_phase4(
            criteria=["c1", "c2"],
            acceptance_trace=[
                {
                    "criterion": "c1",
                    "implementation_refs": ["infra/main.tf"],
                    "tests": [{"iac_validate": "pass"}],
                }
            ],
            artifact_keys=["infra/main.tf", "deploy/values.yaml"],
        )
        assert len(traces) == 2
        assert traces[0].criterion == "c1"
        assert traces[0].implementation_refs == ["infra/main.tf"]
        assert traces[0].tests == [{"iac_validate": "pass"}]
        assert traces[1].criterion == "c2"
        assert traces[1].tests == []
        assert traces[1].implementation_refs == [
            "deploy/values.yaml",
            "infra/main.tf",
        ]

    def test_no_match_uses_empty_tests_and_artifact_keys(self) -> None:
        traces = criterion_traces_from_phase4(
            criteria=["lonely"],
            acceptance_trace=[],
            artifact_keys=["a.py"],
        )
        assert traces == [
            CriterionTrace(
                criterion="lonely",
                implementation_refs=["a.py"],
                tests=[],
            )
        ]

    def test_coerces_bad_shapes(self) -> None:
        traces = criterion_traces_from_phase4(
            criteria=["c1"],
            acceptance_trace=[
                {
                    "criterion": "c1",
                    "implementation_refs": "not-a-list",
                    "tests": [{"ok": 1}, "skip-me", {"gate": True}],
                }
            ],
            artifact_keys=["fallback.py"],
        )
        assert traces[0].implementation_refs == []
        assert traces[0].tests == [{"ok": "1"}, {"gate": "True"}]

    def test_never_invents_validation_pass(self) -> None:
        traces = criterion_traces_from_phase4(
            criteria=["c1"],
            acceptance_trace=[],
            artifact_keys=[],
        )
        assert traces[0].tests == []
        assert {"validation": "pass"} not in traces[0].tests


# ===========================================================================
# INTEGRATION TESTS -- ORCHESTRATOR
# ===========================================================================


class TestDevOpsTeamLeadAgentIntegration:
    def test_happy_path_run_workflow(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            init_ok, _ = initialize_new_repo(path)
            assert init_ok
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
        assert result.completion_package.handoff is not None
        # Real delivery: a feature branch was actually created and merged into
        # development, with real SHAs rather than fabricated placeholders.
        gitops = result.completion_package.git_operations
        assert gitops.branch_created.startswith("feature/")
        assert gitops.merge is not None
        assert gitops.merge.status == "merged"
        assert gitops.merge.target_branch == "development"
        assert len(gitops.merge.merge_commit_hash) == 40
        assert gitops.commits and gitops.commits[0].hash == gitops.merge.merge_commit_hash

    def test_happy_path_direct_run(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert pkg.task_id == "DO-2207"
        assert pkg.status == "completed"
        assert len(pkg.acceptance_criteria_trace) == 2
        assert pkg.release_readiness.deployment_strategy == "rolling"

    def test_multiple_sequential_runs_on_same_lead_agent(self) -> None:
        """Regression guard for the fresh-Strands-Agent-per-call fix.

        A single ``DevOpsTeamLeadAgent`` instance constructs all 10 DevOps
        sub-agents in ``__init__`` and reuses them across pipeline runs.
        An earlier migration bug cached the Strands ``Agent`` instance on
        each sub-agent and reused it across calls, which broke
        ``structured_output_model`` forced-tool-choice on the second call.
        This test runs the full pipeline twice on the same lead-agent
        instance — the second run exercises every cached sub-agent for a
        second time, which is exactly the failure mode that bug caused."""
        # Pin Phase 4.5 so infra_debug is always invoked (fixable=False soft-abort).
        # Without this, hosts with a working terraform CLI skip debug and consume
        # 8 LLM calls while hosts without it consume 9 — which desynchronizes a
        # chained two-run script. Use the full 9-response happy-path script twice.
        per_run = list(_scripted_llm_for_happy_path()._responses)
        assert len(per_run) == 9
        chained = _ScriptedClient(per_run + per_run)
        agent = DevOpsTeamLeadAgent(chained)

        def _failing_exec(_repo: str, _artifacts: dict) -> list:
            return [
                {
                    "tool": "terraform",
                    "command": "validate",
                    "success": False,
                    "checks": {"terraform_validate": "fail"},
                    "findings": ["terraform not found"],
                    "failure_class": "execution",
                }
            ]

        agent._run_execution_tools = _failing_exec  # type: ignore[method-assign]
        for i in range(2):
            pkg = agent.run(_base_task_spec())
            assert pkg.status == "completed", f"iter {i}: {pkg.status}"
            assert pkg.task_id == "DO-2207"

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
        mock_llm = _ScriptedClient(
            [
                {"approved_for_execution": True},
                {"artifacts": {}, "summary": "iac"},
                {"artifacts": {}, "summary": "cicd", "required_gates_present": True},
                {
                    "artifacts": {},
                    "summary": "deploy",
                    "strategy": "rolling",
                    "rollback_plan": ["rb"],
                },
                {
                    "approved": False,
                    "findings": [
                        {
                            "finding_id": "F1",
                            "severity": "high",
                            "blocking": True,
                            "issue": "bad iam",
                        }
                    ],
                    "summary": "blocked",
                },
                {"approved": True, "findings": [], "summary": "ok"},
                {"approved": True, "quality_gates": {"iac_validate": "pass"}, "summary": "ok"},
            ]
        )
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

    def test_security_gate_not_masked_by_stale_validation_pass(self) -> None:
        """A validation-agent-supplied ``security_review: "pass"`` must not mask
        a blocking DevSecOps review: the gate is force-assigned from the
        DevSecOps + policy result, not preserved via setdefault."""
        mock_llm = _ScriptedClient(
            [
                {"approved_for_execution": True},
                {"artifacts": {}, "summary": "iac"},
                {"artifacts": {}, "summary": "cicd", "required_gates_present": True},
                {
                    "artifacts": {},
                    "summary": "deploy",
                    "strategy": "rolling",
                    "rollback_plan": ["rb"],
                },
                {
                    "approved": False,
                    "findings": [
                        {
                            "finding_id": "F1",
                            "severity": "high",
                            "blocking": True,
                            "issue": "bad iam",
                        }
                    ],
                    "summary": "blocked",
                },
                {"approved": True, "findings": [], "summary": "ok"},
                # Validation agent wrongly reports the security gate as passing.
                {
                    "approved": True,
                    "quality_gates": {"iac_validate": "pass", "security_review": "pass"},
                    "summary": "ok",
                },
            ]
        )
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            result = agent.run_workflow(
                repo_path=Path(tmp),
                task_description="Deploy service",
                requirements="Include prod approval gate and rollback plan",
                task_id="devops-sec-mask",
            )
        assert not result.success
        assert result.completion_package is not None
        assert result.completion_package.quality_gates["security_review"] == "fail"

    def test_completion_package_has_acceptance_trace(self) -> None:
        from software_engineering_team.devops_team.test_validation_agent.models import (
            DevOpsTestValidationOutput,
        )

        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        # Stub Phase 4 validation output directly. The DummyLLM + Strands
        # structured-output path reuses the prior change-review payload for the
        # QA acceptance_evidence call, so a scripted ``acceptance_trace`` on the
        # LLM client does not reach the orchestrator. This test targets Phase 5
        # wiring, not that adapter quirk.
        agent.test_validation_agent.run = (  # type: ignore[method-assign]
            lambda _inp: DevOpsTestValidationOutput(
                approved=True,
                quality_gates={"iac_validate": "pass", "policy_checks": "pass"},
                acceptance_trace=[
                    {
                        "criterion": "Pipeline runs tests and scan before deploy",
                        "implementation_refs": ["infra/main.tf"],
                        "tests": [{"iac_validate": "pass"}],
                    }
                ],
                summary="validation ok",
            )
        )
        pkg = agent.run(spec)
        assert len(pkg.acceptance_criteria_trace) == len(spec.acceptance_criteria)

        by_criterion = {t.criterion: t for t in pkg.acceptance_criteria_trace}
        matched = by_criterion["Pipeline runs tests and scan before deploy"]
        assert matched.implementation_refs == ["infra/main.tf"]
        assert matched.tests == [{"iac_validate": "pass"}]

        unmatched = by_criterion["Prod deploy requires explicit approval"]
        assert unmatched.tests == []
        assert len(unmatched.implementation_refs) > 0

        for trace in pkg.acceptance_criteria_trace:
            assert {"validation": "pass"} not in trace.tests

    def test_completion_package_has_release_readiness(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert pkg.release_readiness.rollback_available
        assert "manual_prod_approval" in pkg.release_readiness.required_approvals
        assert pkg.release_readiness.alerting_configured is True

    @pytest.mark.parametrize("alerting_configured", [True, False])
    def test_release_readiness_alerting_follows_deploy_result(
        self, alerting_configured: bool
    ) -> None:
        """Phase 5 copies deploy output, overwriting the doc-agent False placeholder."""
        mock_llm = _scripted_llm_for_happy_path(alerting_configured=alerting_configured)
        agent = DevOpsTeamLeadAgent(mock_llm)
        pkg = agent.run(_base_task_spec())
        assert pkg.release_readiness.alerting_configured is alerting_configured

    def test_completion_package_has_git_operations(self) -> None:
        # A model-only run (``run`` → write_changes=False) performs no git work,
        # so the completion package honestly reports an empty git-operations record
        # instead of fabricating a branch/commit/merge that never happened.
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert pkg.git_operations.branch_created == ""
        assert pkg.git_operations.commits == []
        assert pkg.git_operations.merge is None

    def test_completion_package_files_changed(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert len(pkg.files_changed) > 0

    def test_quality_gates_in_completion(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert "security_review" in pkg.quality_gates
        assert "change_review" in pkg.quality_gates

    def test_build_verifier_failure(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            init_ok, _ = initialize_new_repo(Path(tmp))
            assert init_ok
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

    def test_completion_package_git_operations_real_merge(self) -> None:
        """A real ``run_workflow`` delivers the artifacts by cutting a feature
        branch, merging it into development, and deleting it — the reported
        metadata reflects the actual git state (real SHA equal to development's
        HEAD), not fabricated placeholders."""
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            init_ok, _ = initialize_new_repo(path)
            assert init_ok
            result = agent.run_workflow(
                repo_path=path,
                task_description="Add backend deployment automation",
                requirements="Include prod approval gate and rollback plan",
                build_verifier=MagicMock(return_value=(True, "")),
                task_id="devops-real-merge",
            )
            branches = subprocess.run(
                ["git", "branch"], cwd=path, capture_output=True, text=True, check=False
            ).stdout
            dev_head = subprocess.run(
                ["git", "rev-parse", "development"],
                cwd=path,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        assert result.success
        gitops = result.completion_package.git_operations
        assert gitops.branch_created.startswith("feature/")
        assert gitops.merge is not None and gitops.merge.status == "merged"
        # The reported merge SHA is exactly what development now points at.
        assert gitops.merge.merge_commit_hash == dev_head
        # The feature branch was cleaned up after the merge.
        assert "feature/" not in branches

    def test_completion_package_merge_sha_unknown_on_head_read_failure(self, monkeypatch) -> None:
        """When the merge succeeds but the post-merge HEAD read fails, the
        pipeline must not report ``status == "merged"`` with an empty SHA — it
        reports ``merged_sha_unknown`` and notes the failure instead of
        fabricating a successful-looking record."""
        import software_engineering_team.devops_team.orchestrator as orch

        monkeypatch.setattr(orch, "get_head_sha", lambda *a, **k: (False, ""))
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            init_ok, _ = initialize_new_repo(Path(tmp))
            assert init_ok
            result = agent.run_workflow(
                repo_path=Path(tmp),
                task_description="Add backend deployment automation",
                requirements="Include prod approval gate and rollback plan",
                build_verifier=MagicMock(return_value=(True, "")),
                task_id="devops-head-sha-unknown",
            )
        assert result.success
        gitops = result.completion_package.git_operations
        assert gitops.merge is not None
        assert gitops.merge.status == "merged_sha_unknown"
        assert gitops.merge.merge_commit_hash == ""
        assert any("HEAD SHA" in note for note in result.completion_package.notes)

    def test_delivery_merge_failure_blocks(self, monkeypatch) -> None:
        """When the real merge fails, the pipeline reports failure and a blocked
        completion package with an honest ``merge.status == "failed"`` — it does
        not claim success on a merge that never landed."""
        import software_engineering_team.devops_team.orchestrator as orch

        monkeypatch.setattr(orch, "merge_branch", lambda *a, **k: (False, "merge conflict"))
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            init_ok, _ = initialize_new_repo(Path(tmp))
            assert init_ok
            result = agent.run_workflow(
                repo_path=Path(tmp),
                task_description="Add backend deployment automation",
                requirements="Include prod approval gate and rollback plan",
                build_verifier=MagicMock(return_value=(True, "")),
                task_id="devops-merge-fail",
            )
        assert not result.success
        assert "merge" in (result.failure_reason or "").lower()
        assert result.completion_package is not None
        assert result.completion_package.status == "blocked"
        assert result.completion_package.git_operations.merge is not None
        assert result.completion_package.git_operations.merge.status == "failed"

    def test_delivery_development_branch_failure(self, monkeypatch) -> None:
        """A failure preparing the development branch aborts the run with an
        honest failure reason rather than committing to the wrong branch."""
        import software_engineering_team.devops_team.orchestrator as orch

        monkeypatch.setattr(orch, "ensure_development_branch", lambda *a, **k: (False, "no base"))
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            init_ok, _ = initialize_new_repo(Path(tmp))
            assert init_ok
            result = agent.run_workflow(
                repo_path=Path(tmp),
                task_description="Add backend deployment automation",
                requirements="Include prod approval gate and rollback plan",
                build_verifier=MagicMock(return_value=(True, "")),
                task_id="devops-dev-branch-fail",
            )
        assert not result.success
        assert "development" in (result.failure_reason or "")

    def test_delivery_feature_branch_failure(self, monkeypatch) -> None:
        """A failure creating the feature branch aborts the run rather than
        writing changes onto whatever branch happens to be checked out."""
        import software_engineering_team.devops_team.orchestrator as orch

        monkeypatch.setattr(orch, "create_feature_branch", lambda *a, **k: (False, "boom"))
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            init_ok, _ = initialize_new_repo(Path(tmp))
            assert init_ok
            result = agent.run_workflow(
                repo_path=Path(tmp),
                task_description="Add backend deployment automation",
                requirements="Include prod approval gate and rollback plan",
                build_verifier=MagicMock(return_value=(True, "")),
                task_id="devops-feat-branch-fail",
            )
        assert not result.success
        assert "feature branch" in (result.failure_reason or "")


# ===========================================================================
# COMPATIBILITY / MIGRATION TESTS
# ===========================================================================


class TestBackwardCompatibility:
    def test_run_workflow_accepts_legacy_args(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            init_ok, _ = initialize_new_repo(Path(tmp))
            assert init_ok
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


_RESULT_KEYS = {"tool", "command", "success", "checks", "findings", "failure_class"}


class TestToolDispatchRunExecutionTools:
    """Exercise tool_dispatch.run_execution_tools directly against a mock agent,
    at the new module boundary rather than only incidentally through the
    full DevOpsTeamLeadAgent pipeline."""

    def test_returns_empty_list_for_no_artifacts(self) -> None:
        assert tool_dispatch.run_execution_tools(MagicMock(), "/tmp/x", {}) == []

    def test_terraform_only_runs_init_validate_plan_on_success(self) -> None:
        agent = MagicMock()
        agent.terraform_exec_tool.run.side_effect = [
            TerraformExecutionOutput(success=True, checks={"terraform_init": "pass"}),
            TerraformExecutionOutput(success=True, checks={"terraform_validate": "pass"}),
            TerraformExecutionOutput(success=True, checks={"terraform_plan": "pass"}),
        ]
        results = tool_dispatch.run_execution_tools(
            agent, "/tmp/x", {"infra/main.tf": "resource {}"}
        )
        assert [r["command"] for r in results] == ["init", "validate", "plan"]
        for r in results:
            assert set(r.keys()) == _RESULT_KEYS
            assert r["tool"] == "terraform"
            assert r["success"] is True

    def test_terraform_breaks_on_first_failure(self) -> None:
        agent = MagicMock()
        agent.terraform_exec_tool.run.return_value = TerraformExecutionOutput(
            success=False, checks={}, failure_class="init_failed"
        )
        results = tool_dispatch.run_execution_tools(
            agent, "/tmp/x", {"infra/main.tf": "resource {}"}
        )
        assert len(results) == 1
        assert results[0]["command"] == "init"
        assert results[0]["success"] is False
        assert agent.terraform_exec_tool.run.call_count == 1

    def test_cdk_artifact_triggers_cdk_synth(self) -> None:
        agent = MagicMock()
        agent.cdk_exec_tool.run.return_value = CDKExecutionOutput(
            success=True, checks={"cdk_synth": "pass"}
        )
        results = tool_dispatch.run_execution_tools(agent, "/tmp/x", {"cdk.json": "{}"})
        assert len(results) == 1
        assert results[0]["tool"] == "cdk"
        assert results[0]["command"] == "synth"

    @pytest.mark.parametrize(
        "compose_file",
        ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"],
    )
    def test_compose_artifact_triggers_compose_config(self, compose_file: str) -> None:
        agent = MagicMock()
        agent.compose_exec_tool.run.return_value = DockerComposeExecutionOutput(
            success=True, checks={"compose_config": "pass"}
        )
        results = tool_dispatch.run_execution_tools(agent, "/tmp/x", {compose_file: "services: {}"})
        assert len(results) == 1
        assert results[0]["tool"] == "compose"
        assert results[0]["command"] == "config"

    def test_chart_artifact_triggers_helm_lint(self) -> None:
        agent = MagicMock()
        agent.helm_exec_tool.run.return_value = HelmExecutionOutput(
            success=True, checks={"helm_lint": "pass"}
        )
        results = tool_dispatch.run_execution_tools(
            agent, "/tmp/x", {"charts/app/Chart.yaml": "name: app"}
        )
        assert len(results) == 1
        assert results[0]["tool"] == "helm"
        assert results[0]["command"] == "lint"

    def test_multiple_simultaneous_triggers_accumulate_all_results(self) -> None:
        agent = MagicMock()
        agent.terraform_exec_tool.run.return_value = TerraformExecutionOutput(
            success=True, checks={}
        )
        agent.helm_exec_tool.run.return_value = HelmExecutionOutput(success=True, checks={})
        results = tool_dispatch.run_execution_tools(
            agent, "/tmp/x", {"infra/main.tf": "resource {}", "charts/app/Chart.yaml": "name: app"}
        )
        assert len(results) == 4
        assert [r["tool"] for r in results] == ["terraform", "terraform", "terraform", "helm"]


class TestToolDispatchRunValidationTools:
    """Exercise tool_dispatch.run_validation_tools directly against a mock agent."""

    def _mock_agent(self) -> MagicMock:
        agent = MagicMock()
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
        return agent

    def test_returns_validation_tool_results_with_merged_tool_gate_map(self) -> None:
        agent = self._mock_agent()
        vt = tool_dispatch.run_validation_tools(agent, Path("/tmp/x"))
        assert isinstance(vt, tool_dispatch.ValidationToolResults)
        assert vt.iac_checks is agent.iac_validation_tool.run.return_value
        assert vt.policy_checks is agent.policy_tool.run.return_value
        assert vt.cicd_checks is agent.cicd_lint_tool.run.return_value
        assert vt.dry_run_checks is agent.deploy_dry_run_tool.run.return_value
        assert vt.tool_gate_map == {
            "iac_validate": "pass",
            "policy_checks": "pass",
            "pipeline_lint": "pass",
            "deployment_dry_run": "pass",
        }

    def test_tool_gate_map_merge_later_dict_wins_on_key_collision(self) -> None:
        agent = MagicMock()
        agent.iac_validation_tool.run.return_value = IaCValidationOutput(
            success=True, checks={"shared_key": "iac"}
        )
        agent.policy_tool.run.return_value = PolicyAsCodeOutput(
            success=True, checks={"shared_key": "policy"}
        )
        agent.cicd_lint_tool.run.return_value = CICDLintOutput(
            success=True, checks={"shared_key": "cicd"}
        )
        agent.deploy_dry_run_tool.run.return_value = DeploymentDryRunOutput(
            success=True, checks={"shared_key": "dryrun"}
        )
        vt = tool_dispatch.run_validation_tools(agent, Path("/tmp/x"))
        assert vt.tool_gate_map["shared_key"] == "dryrun"

    def test_policy_scan_invoked_with_agent_policy_tool_as_runner(self) -> None:
        agent = self._mock_agent()
        repo_path = Path("/tmp/x")
        tool_dispatch.run_validation_tools(agent, repo_path)
        assert agent.policy_tool.run.call_count == 1
        (call_arg,) = agent.policy_tool.run.call_args.args
        assert call_arg == PolicyAsCodeInput(repo_path=str(repo_path))


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


# ===========================================================================
# FENCE-RECOVERY REGRESSION TESTS
#
# Each of these 8 agents now routes its raw LLM completion through
# complete_json_with_continuation() instead of a bare json.loads(). These
# tests exercise the real recovery path (llm_mod.Agent is mocked at the
# shared/llm.py level, not at complete_json_with_continuation itself) to
# prove a markdown-fenced response no longer crashes the agent.
# ===========================================================================


from software_engineering_team.tests.conftest import (  # noqa: E402
    _patch_fenced_response,
    _strands_model_double,
)


class TestDevOpsAgentsRecoverFencedJson:
    def test_iac_agent_recovers_fenced_response(self, monkeypatch) -> None:
        from software_engineering_team.devops_team.iac_agent import (
            IaCAgentInput,
            InfrastructureAsCodeAgent,
        )

        _patch_fenced_response(
            monkeypatch,
            {
                "artifacts": {"infra/main.tf": "resource {}"},
                "summary": "fenced iac ok",
                "destructive_changes_detected": False,
                "blast_radius_notes": [],
            },
        )
        agent = InfrastructureAsCodeAgent(_strands_model_double())
        out = agent.run(IaCAgentInput(task_spec=_base_task_spec()))
        assert "infra/main.tf" in out.artifacts
        assert out.summary == "fenced iac ok"

    def test_cicd_pipeline_agent_recovers_fenced_response(self, monkeypatch) -> None:
        from software_engineering_team.devops_team.cicd_pipeline_agent import (
            CICDPipelineAgent,
            CICDPipelineAgentInput,
        )

        _patch_fenced_response(
            monkeypatch,
            {
                "artifacts": {".github/workflows/ci.yml": "on: push"},
                "pipeline_job_graph_summary": "build -> test",
                "required_gates_present": True,
                "summary": "fenced cicd ok",
            },
        )
        agent = CICDPipelineAgent(_strands_model_double())
        out = agent.run(CICDPipelineAgentInput(task_spec=_base_task_spec()))
        assert ".github/workflows/ci.yml" in out.artifacts

    def test_deployment_strategy_agent_recovers_fenced_response(self, monkeypatch) -> None:
        from software_engineering_team.devops_team.deployment_strategy_agent import (
            DeploymentStrategyAgent,
            DeploymentStrategyAgentInput,
        )

        _patch_fenced_response(
            monkeypatch,
            {
                "artifacts": {"deploy/values.yaml": "replicas: 2"},
                "strategy": "rolling",
                "rollback_plan": ["helm rollback"],
                "health_checks": ["/healthz"],
                "rollout_timeout_minutes": 10,
                "summary": "fenced deploy ok",
            },
        )
        agent = DeploymentStrategyAgent(_strands_model_double())
        out = agent.run(DeploymentStrategyAgentInput(task_spec=_base_task_spec()))
        assert out.strategy == "rolling"

    def test_infra_debug_agent_recovers_fenced_response(self, monkeypatch) -> None:
        from software_engineering_team.devops_team.infra_debug_agent import (
            IaCDebugInput,
            InfraDebugAgent,
        )

        _patch_fenced_response(
            monkeypatch,
            {
                "errors": [{"error_type": "syntax", "error_message": "bad hcl"}],
                "summary": "fenced debug ok",
                "fixable": True,
            },
        )
        agent = InfraDebugAgent(_strands_model_double())
        out = agent.run(
            IaCDebugInput(
                execution_output="Error: bad hcl",
                tool_name="terraform",
                command="plan",
                artifacts={"main.tf": "resource {}"},
            )
        )
        assert out.summary == "fenced debug ok"
        assert out.fixable is True

    def test_doc_runbook_agent_recovers_fenced_response(self, monkeypatch) -> None:
        from software_engineering_team.devops_team.doc_runbook_agent import (
            DocumentationRunbookAgent,
            DocumentationRunbookInput,
        )

        _patch_fenced_response(
            monkeypatch,
            {
                "files": {"docs/runbook.md": "# Runbook"},
                "summary": "fenced doc ok",
            },
        )
        agent = DocumentationRunbookAgent(_strands_model_double())
        out = agent.run(
            DocumentationRunbookInput(
                task_id="DO-1",
                task_title="test",
                artifacts={"a.tf": "resource"},
                quality_gates={"iac_validate": "pass"},
            )
        )
        assert "docs/runbook.md" in out.files

    def test_infra_patch_agent_recovers_fenced_response(self, monkeypatch) -> None:
        from software_engineering_team.devops_team.infra_debug_agent.models import (
            IaCDebugOutput,
            IaCExecutionError,
        )
        from software_engineering_team.devops_team.infra_patch_agent import (
            IaCPatchInput,
            InfraPatchAgent,
        )

        _patch_fenced_response(
            monkeypatch,
            {
                "patched_artifacts": {"main.tf": "resource {} # fixed"},
                "summary": "fenced patch ok",
                "edits_applied": 1,
            },
        )
        agent = InfraPatchAgent(_strands_model_double())
        debug_output = IaCDebugOutput(
            errors=[IaCExecutionError(error_type="syntax", error_message="bad hcl")],
            summary="debug",
            fixable=True,
        )
        out = agent.run(
            IaCPatchInput(
                debug_output=debug_output,
                original_artifacts={"main.tf": "resource {}"},
            )
        )
        assert "main.tf" in out.patched_artifacts

    def test_devsecops_review_agent_recovers_fenced_response(self, monkeypatch) -> None:
        from software_engineering_team.devops_team.devsecops_review_agent import (
            DevSecOpsReviewAgent,
            DevSecOpsReviewInput,
        )

        _patch_fenced_response(
            monkeypatch,
            {"approved": True, "findings": [], "summary": "fenced sec ok"},
        )
        agent = DevSecOpsReviewAgent(_strands_model_double())
        out = agent.run(DevSecOpsReviewInput(task_description="test", artifacts={}))
        assert out.approved

    def test_task_clarifier_recovers_fenced_response(self, monkeypatch) -> None:
        from software_engineering_team.devops_team.task_clarifier import (
            DevOpsTaskClarifierAgent,
            DevOpsTaskClarifierInput,
        )

        _patch_fenced_response(
            monkeypatch,
            {
                "approved_for_execution": True,
                "checklist": ["done"],
                "gaps": [],
                "clarification_requests": [],
            },
        )
        agent = DevOpsTaskClarifierAgent(_strands_model_double())
        out = agent.run(DevOpsTaskClarifierInput(task_spec=_base_task_spec()))
        assert out.approved_for_execution is True
