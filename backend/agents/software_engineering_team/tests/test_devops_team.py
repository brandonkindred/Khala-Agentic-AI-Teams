"""Tests for the DevOps team orchestrator, models, agents, and tool agents."""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from llm_service.clients.dummy import DummyLLMClient
from shared.git.git_utils import initialize_new_repo
from software_engineering_team.devops_team import (
    DevOpsTaskSpec,
    DevOpsTeamLeadAgent,
    tool_dispatch,
)
from software_engineering_team.devops_team.iac_agent import (
    IaCAgentInput,
    InfrastructureAsCodeAgent,
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
from software_engineering_team.devops_team.phases.quality_gate import (
    _describe_task_with_exclusions,
    run_phase4_quality_gate,
)
from software_engineering_team.devops_team.task_clarifier import (
    DevOpsTaskClarifierAgent,
    DevOpsTaskClarifierInput,
)
from software_engineering_team.devops_team.tool_agents import (
    CDKExecutionOutput,
    CDKExecutionToolAgent,
    CICDLintInput,
    CICDLintOutput,
    CICDLintPipelineValidationToolAgent,
    DeploymentDryRunInput,
    DeploymentDryRunOutput,
    DeploymentDryRunPlanToolAgent,
    DockerComposeExecutionOutput,
    DockerComposeExecutionToolAgent,
    HelmExecutionOutput,
    HelmExecutionToolAgent,
    IaCValidationInput,
    IaCValidationOutput,
    IaCValidationToolAgent,
    PolicyAsCodeInput,
    PolicyAsCodeOutput,
    PolicyAsCodeToolAgent,
    RepoNavigatorInput,
    RepoNavigatorToolAgent,
    TerraformExecutionOutput,
    TerraformExecutionToolAgent,
)
from software_engineering_team.tests.conftest import (
    _patch_fenced_response,
    _strands_model_double,
)


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

    def __init__(
        self,
        responses: List[Dict[str, Any]],
        *,
        default_factory: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0
        self._default_factory = default_factory

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
        # After the scripted list is exhausted, use the caller-supplied
        # neutral default when given (so drift onto this fallback is an
        # obviously-generic response, not a real step's payload silently
        # overloaded with extra fields); otherwise fall back to the last
        # entry so extra pipeline steps don't crash the test.
        if self._default_factory is not None:
            return self._default_factory()
        return self._responses[-1] if self._responses else {}

    @property
    def responses(self) -> List[Dict[str, Any]]:
        """Copy of the scripted response list (safe to mutate by callers)."""
        return list(self._responses)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Shared prefix for the security-blocking pipeline script: task_clarifier →
# iac → cicd → deployment → devsecops (blocked). Tests that need this
# five-step blocked-by-security-review sequence append their own differing
# tail entries rather than re-inlining the prefix.
_SECURITY_BLOCKING_SCRIPT_PREFIX: List[Dict[str, Any]] = [
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
]


def _base_task_spec(**overrides) -> DevOpsTaskSpec:
    """Return a baseline ``DevOpsTaskSpec`` for tests, with optional field overrides."""
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
    devsecops, change_review, qa_agent, doc_runbook).

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
        ],
        # Once the scripted list is exhausted, any further call (e.g. an
        # unrelated upstream corrective retry -- a chunk-review bisection,
        # say -- consuming an extra slot and shifting a later named step
        # onto an overrun call) gets this DevSecOps-shaped clean-approval
        # fallback instead of the real (and now schema-validated)
        # doc_runbook payload silently overloaded with extra fields. This
        # keeps DevSecOpsReviewAgent from crashing the test on such drift
        # while still making non-DevSecOps drift landing here fail schema
        # validation loudly, rather than masking every step's drift equally.
        default_factory=lambda: {"approved": True, "findings": [], "summary": "fallback"},
    )


# ===========================================================================
# MODEL TESTS
# ===========================================================================


class TestDevOpsTaskSpec:
    def test_task_id_required(self) -> None:
        with pytest.raises(ValidationError):
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
    """Verify ENV_POLICY defines the expected auto-deploy, approval, and rollback rules per environment."""

    def test_dev_allows_auto_deploy(self) -> None:
        """Dev allows auto-deploy and does not require approval."""
        assert ENV_POLICY["dev"]["auto_deploy_allowed"] is True
        assert ENV_POLICY["dev"]["approval_required"] is False

    def test_staging_requires_rollback_test(self) -> None:
        """Staging requires a rollback test before deploy."""
        assert ENV_POLICY["staging"]["rollback_test_required"] is True

    def test_production_requires_approval(self) -> None:
        """Production requires approval and disallows auto-deploy."""
        assert ENV_POLICY["production"]["approval_required"] is True
        assert ENV_POLICY["production"]["auto_deploy_allowed"] is False


class TestEnforceEnvPolicy:
    """Verify DevOpsTeamLeadAgent._enforce_env_policy blocks task specs that violate ENV_POLICY."""

    def test_blocks_prod_without_approval(self) -> None:
        """A production-scope spec without an approval step is blocked with an approval-related reason."""
        spec = _base_task_spec(scope={"included": ["build"], "excluded": []})
        reason = DevOpsTeamLeadAgent._enforce_env_policy(spec)
        assert reason is not None
        assert "approval" in reason.lower()

    def test_blocks_prod_without_rollback(self) -> None:
        """A production-scope spec without rollback requirements is blocked with a rollback-related reason."""
        spec = _base_task_spec(rollback_requirements=[])
        reason = DevOpsTeamLeadAgent._enforce_env_policy(spec)
        assert reason is not None
        assert "rollback" in reason.lower()

    def test_allows_dev_only(self) -> None:
        """A dev-only spec with no rollback requirements passes the policy check."""
        spec = _base_task_spec(
            platform_scope={"environments": ["dev"]},
            rollback_requirements=[],
        )
        reason = DevOpsTeamLeadAgent._enforce_env_policy(spec)
        assert reason is None

    def test_allows_full_spec(self) -> None:
        """A fully-specified spec satisfying all environment policies passes the check."""
        spec = _base_task_spec()
        reason = DevOpsTeamLeadAgent._enforce_env_policy(spec)
        assert reason is None

    def test_rejects_plain_string_included(self) -> None:
        """``scope.included`` as a single string (not an iterable of strings) is rejected."""
        task_spec = SimpleNamespace(
            platform_scope=SimpleNamespace(environments=["production"]),
            scope=SimpleNamespace(included="approval gate"),
            rollback_requirements=["Rollback"],
        )
        with pytest.raises(AssertionError, match="not a single string"):
            DevOpsTeamLeadAgent._enforce_env_policy(task_spec)  # type: ignore[arg-type]

    def test_rejects_negated_approval_mention(self) -> None:
        """A negated approval mention ("no approval required") does not satisfy the approval gate."""
        spec = _base_task_spec(
            platform_scope={"environments": ["production"]},
            scope={"included": ["no approval required"], "excluded": []},
            rollback_requirements=["Rollback"],
        )
        reason = DevOpsTeamLeadAgent._enforce_env_policy(spec)
        assert reason is not None
        assert "approval" in reason.lower()

    def test_rejects_intervening_negated_approval(self) -> None:
        """A negation separated from "approval" by an intervening filler word still counts as negated."""
        spec = _base_task_spec(
            platform_scope={"environments": ["production"]},
            scope={"included": ["no formal approval"], "excluded": []},
            rollback_requirements=["Rollback"],
        )
        reason = DevOpsTeamLeadAgent._enforce_env_policy(spec)
        assert reason is not None
        assert "approval" in reason.lower()

    def test_rejects_string_environments(self) -> None:
        """``platform_scope.environments`` as a single string (not an iterable of strings) is rejected."""
        task_spec = SimpleNamespace(
            platform_scope=SimpleNamespace(environments="production"),
            scope=SimpleNamespace(included=["prod approval"]),
            rollback_requirements=["Rollback"],
        )
        with pytest.raises(AssertionError, match="not a string"):
            DevOpsTeamLeadAgent._enforce_env_policy(task_spec)  # type: ignore[arg-type]

    def test_rejects_non_string_included(self) -> None:
        """A non-string element in ``scope.included`` is rejected."""
        task_spec = SimpleNamespace(
            platform_scope=SimpleNamespace(environments=["production"]),
            scope=SimpleNamespace(included=[None]),
            rollback_requirements=["Rollback"],
        )
        with pytest.raises(AssertionError, match="scope.included"):
            DevOpsTeamLeadAgent._enforce_env_policy(task_spec)  # type: ignore[arg-type]


# ===========================================================================
# GATE NAME TESTS
# ===========================================================================


class TestGateNames:
    """Verify the required DevOps quality-gate name list is complete and stable."""

    def test_required_gate_names_present(self) -> None:
        """The iac_validate, security_review, and change_review gates are required."""
        assert "iac_validate" in DEVOPS_REQUIRED_GATE_NAMES
        assert "security_review" in DEVOPS_REQUIRED_GATE_NAMES
        assert "change_review" in DEVOPS_REQUIRED_GATE_NAMES

    def test_required_gate_names_count(self) -> None:
        """The full required-gate tuple matches its expected order and membership."""
        assert DEVOPS_REQUIRED_GATE_NAMES == (
            "iac_validate",
            "iac_validate_fmt",
            "policy_checks",
            "pipeline_lint",
            "pipeline_gate_check",
            "deployment_dry_run",
            "security_review",
            "change_review",
        )


# ===========================================================================
# SUBTASK CONTRACT TESTS
# ===========================================================================


class TestSubtaskContractGeneration:
    """Verify DevOpsTeamLeadAgent._build_subtask_contracts fans a task spec out
    into per-owner subtask contracts with stable, task-scoped IDs."""

    def test_generates_three_contracts(self) -> None:
        """A task spec always yields exactly three subtask contracts."""
        spec = _base_task_spec()
        contracts = DevOpsTeamLeadAgent._build_subtask_contracts(spec)
        assert len(contracts) == 3

    def test_contract_owners(self) -> None:
        """The three contracts are owned by IaC, CI/CD, and deployment agents."""
        spec = _base_task_spec()
        contracts = DevOpsTeamLeadAgent._build_subtask_contracts(spec)
        owners = {c.owner for c in contracts}
        assert "InfrastructureAsCodeAgent" in owners
        assert "CICDPipelineAgent" in owners
        assert "DeploymentStrategyAgent" in owners

    def test_contract_ids_use_task_id(self) -> None:
        """Every contract's subtask_id is prefixed with the originating task_id."""
        spec = _base_task_spec()
        contracts = DevOpsTeamLeadAgent._build_subtask_contracts(spec)
        for c in contracts:
            assert c.subtask_id.startswith("DO-2207")


# ===========================================================================
# TASK CLARIFIER TESTS
# ===========================================================================


class TestTaskClarifier:
    """Verify DevOpsTaskClarifierAgent rejects incomplete or unsafe task specs
    and only approves a spec once all required fields are satisfied."""

    def _agent(self) -> DevOpsTaskClarifierAgent:
        """Build a clarifier agent backed by a mock LLM client (unused by the
        rule-based blocking checks under test)."""
        return DevOpsTaskClarifierAgent(MagicMock())

    def test_blocks_missing_rollback_for_prod(self) -> None:
        """A spec with no rollback requirements is rejected with a rollback request."""
        spec = _base_task_spec(rollback_requirements=[])
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert not out.approved_for_execution
        assert any("Rollback" in r for r in out.clarification_requests)

    def test_blocks_missing_environments(self) -> None:
        """A spec with no target environments is rejected with an environment request."""
        spec = _base_task_spec(platform_scope={"environments": []})
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert not out.approved_for_execution
        assert any("environment" in r.lower() for r in out.clarification_requests)

    def test_blocks_missing_acceptance_criteria(self) -> None:
        """A spec with no acceptance criteria is rejected with an acceptance request."""
        spec = _base_task_spec(acceptance_criteria=[])
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert not out.approved_for_execution
        assert any("acceptance" in r.lower() for r in out.clarification_requests)

    def test_blocks_missing_secret_source(self) -> None:
        """A spec with an empty secrets source is rejected with a secret request."""
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
        """Production-scope work without an approval gate is rejected."""
        spec = _base_task_spec(scope={"included": ["build image"], "excluded": []})
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert not out.approved_for_execution
        assert any("approval" in r.lower() for r in out.clarification_requests)

    def test_blocks_prod_when_approval_only_appears_as_embedded_substring(self) -> None:
        """Scope items like "disapproval"/"preapproval" must not satisfy the approval
        gate via substring match -- "approval" has to appear as its own word."""
        spec = _base_task_spec(
            scope={
                "included": ["build image", "disapproval workflow", "preapproval script"],
                "excluded": [],
            }
        )
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert not out.approved_for_execution
        assert any("approval" in r.lower() for r in out.clarification_requests)

    def test_blocks_missing_goal(self) -> None:
        """A spec with an empty goal summary is rejected with an outcome request."""
        spec = _base_task_spec(goal={"summary": ""})
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert not out.approved_for_execution
        assert any("outcome" in r.lower() for r in out.clarification_requests)

    def test_approves_complete_spec(self) -> None:
        """A fully-specified spec, once the LLM confirms it, is approved for execution."""
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
        """A blocked spec still returns a non-trivial pre-execution checklist."""
        spec = _base_task_spec(rollback_requirements=[])
        out = self._agent().run(DevOpsTaskClarifierInput(task_spec=spec))
        assert len(out.checklist) >= 3


# ===========================================================================
# TOOL AGENT TESTS
# ===========================================================================


class TestRepoNavigatorToolAgent:
    """Verify RepoNavigatorToolAgent detects IaC, pipeline, and deploy paths
    by scanning a repo's file layout for well-known markers."""

    def test_detects_terraform_files(self) -> None:
        """A ``.tf`` file under ``infra/`` is reported as a detected IaC path."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "infra").mkdir()
            (Path(tmp) / "infra" / "main.tf").write_text("resource {}")
            out = RepoNavigatorToolAgent().run(RepoNavigatorInput(repo_path=tmp))
            assert any("main.tf" in p for p in out.detected_iac_paths)

    def test_detects_github_workflows(self) -> None:
        """A workflow file under ``.github/workflows/`` is a detected pipeline path."""
        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = Path(tmp) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "ci.yml").write_text("on: push")
            out = RepoNavigatorToolAgent().run(RepoNavigatorInput(repo_path=tmp))
            assert any("ci.yml" in p for p in out.detected_pipeline_paths)

    def test_detects_helm_charts(self) -> None:
        """A Helm ``Chart.yaml`` under ``deploy/helm/`` is a detected deploy path."""
        with tempfile.TemporaryDirectory() as tmp:
            helm_dir = Path(tmp) / "deploy" / "helm" / "myapp"
            helm_dir.mkdir(parents=True)
            (helm_dir / "Chart.yaml").write_text("name: myapp")
            out = RepoNavigatorToolAgent().run(RepoNavigatorInput(repo_path=tmp))
            assert any("helm" in p.lower() for p in out.detected_deploy_paths)

    def test_empty_repo(self) -> None:
        """A repo with none of the known markers reports no detected paths."""
        with tempfile.TemporaryDirectory() as tmp:
            out = RepoNavigatorToolAgent().run(RepoNavigatorInput(repo_path=tmp))
            assert out.detected_iac_paths == []
            assert out.detected_pipeline_paths == []
            assert out.detected_deploy_paths == []


class TestIaCValidationToolAgent:
    """Verify IaCValidationToolAgent skips Terraform checks when no .tf files exist."""

    def test_skipped_when_no_tf_files(self) -> None:
        """When the repo contains no Terraform files, both iac_validate and iac_validate_fmt are skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            out = IaCValidationToolAgent().run(IaCValidationInput(repo_path=tmp))
            assert out.checks["iac_validate"] == "skipped"
            assert out.checks["iac_validate_fmt"] == "skipped"
            assert out.success is True


class TestPolicyAsCodeToolAgent:
    """Verify PolicyAsCodeToolAgent skips policy-as-code checks when checkov is unavailable."""

    def test_skipped_when_checkov_missing(self) -> None:
        """When checkov is not installed, the policy_checks gate is reported as skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            out = PolicyAsCodeToolAgent().run(PolicyAsCodeInput(repo_path=tmp))
            assert out.checks["policy_checks"] == "skipped"
            assert out.success is True


class TestCICDLintToolAgent:
    """Verify CICDLintPipelineValidationToolAgent lints GitHub Actions workflows for
    structure and production-deploy safeguards."""

    def test_pass_valid_workflow(self) -> None:
        """A workflow with a valid job definition passes the pipeline_lint gate."""
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
        """A workflow missing a jobs section fails the pipeline_lint gate."""
        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = Path(tmp) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "ci.yml").write_text("on: push\n")
            out = CICDLintPipelineValidationToolAgent().run(CICDLintInput(repo_path=tmp))
            assert out.checks["pipeline_lint"] == "fail"
            assert out.success is False

    def test_fail_prod_deploy_without_approval(self) -> None:
        """A workflow that deploys to production without an approval step fails the pipeline_gate_check gate."""
        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = Path(tmp) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "deploy.yml").write_text(
                "on: push\njobs:\n  deploy:\n    runs-on: ubuntu-latest\n    steps: []\n# deploy to production"
            )
            out = CICDLintPipelineValidationToolAgent().run(CICDLintInput(repo_path=tmp))
            assert out.checks["pipeline_gate_check"] == "fail"
            assert out.success is False

    def test_skipped_no_workflows(self) -> None:
        """When no workflow files exist, the pipeline_lint gate is reported as skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            out = CICDLintPipelineValidationToolAgent().run(CICDLintInput(repo_path=tmp))
            assert out.checks["pipeline_lint"] == "skipped"
            assert out.success is True


class TestDeploymentDryRunToolAgent:
    """Verify DeploymentDryRunPlanToolAgent skips the dry-run gate when no Helm chart is present."""

    def test_skipped_no_chart(self) -> None:
        """When the repo contains no Helm chart, the deployment_dry_run gate is reported as skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            out = DeploymentDryRunPlanToolAgent().run(DeploymentDryRunInput(repo_path=tmp))
            assert out.checks["deployment_dry_run"] == "skipped"
            assert out.success is True


def test_devops_env_policy_and_tool_agent_test_classes_have_docstrings() -> None:
    """Docstring-coverage guard for the DevOps env-policy and tool-agent test classes."""
    target_classes = [
        TestEnvPolicy,
        TestEnforceEnvPolicy,
        TestIaCValidationToolAgent,
        TestPolicyAsCodeToolAgent,
        TestCICDLintToolAgent,
        TestDeploymentDryRunToolAgent,
    ]
    for cls in target_classes:
        assert cls.__doc__, f"{cls.__name__} is missing a class docstring"
        for name, member in vars(cls).items():
            if name.startswith("test_"):
                assert member.__doc__, f"{cls.__name__}.{name} is missing a docstring"


# ===========================================================================
# CORE AGENT TESTS
# ===========================================================================


class TestInfrastructureAsCodeAgent:
    """Verify InfrastructureAsCodeAgent surfaces generated IaC artifacts and
    flags destructive-change warnings from the LLM response."""

    def test_run_returns_artifacts(self) -> None:
        """A non-destructive response yields the generated artifact and no warnings."""
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
        """A destructive response is passed through with its blast-radius notes intact."""
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

    def test_build_context_surfaces_scope_exclusions(self) -> None:
        """This agent never reads task_spec.scope directly (only
        InfrastructureAsCodeAgent does), so an explicit exclusion must reach it
        through the prompt context instead, or a generated pipeline could
        violate it with the agent never having been told about it."""
        from software_engineering_team.devops_team.cicd_pipeline_agent import (
            CICDPipelineAgent,
            CICDPipelineAgentInput,
        )

        agent = CICDPipelineAgent(_StubClient({}))
        context = agent.build_context(CICDPipelineAgentInput(task_spec=_base_task_spec()))
        assert "cluster provisioning" in context

    def test_build_output_required_gates_present_missing_defaults_false(self) -> None:
        from software_engineering_team.devops_team.cicd_pipeline_agent import (
            CICDPipelineAgent,
            CICDPipelineAgentInput,
        )

        agent = CICDPipelineAgent(_StubClient({}))
        out = agent.build_output(
            CICDPipelineAgentInput(task_spec=_base_task_spec()),
            {"summary": "ok"},
        )
        assert out.required_gates_present is False

    def test_build_output_required_gates_present_false(self) -> None:
        from software_engineering_team.devops_team.cicd_pipeline_agent import (
            CICDPipelineAgent,
            CICDPipelineAgentInput,
        )

        agent = CICDPipelineAgent(_StubClient({}))
        out = agent.build_output(
            CICDPipelineAgentInput(task_spec=_base_task_spec()),
            {"required_gates_present": False},
        )
        assert out.required_gates_present is False

    def test_build_output_required_gates_present_true(self) -> None:
        from software_engineering_team.devops_team.cicd_pipeline_agent import (
            CICDPipelineAgent,
            CICDPipelineAgentInput,
        )

        agent = CICDPipelineAgent(_StubClient({}))
        out = agent.build_output(
            CICDPipelineAgentInput(task_spec=_base_task_spec()),
            {"required_gates_present": True},
        )
        assert out.required_gates_present is True

    def test_build_output_required_gates_present_string_false_is_false(self) -> None:
        """Schema-drift ``\"false\"`` must not become True via Python truthiness."""
        from software_engineering_team.devops_team.cicd_pipeline_agent import (
            CICDPipelineAgent,
            CICDPipelineAgentInput,
        )

        agent = CICDPipelineAgent(_StubClient({}))
        out = agent.build_output(
            CICDPipelineAgentInput(task_spec=_base_task_spec()),
            {"required_gates_present": "false"},
        )
        assert out.required_gates_present is False

    def test_build_output_required_gates_present_string_true_is_true(self) -> None:
        from software_engineering_team.devops_team.cicd_pipeline_agent import (
            CICDPipelineAgent,
            CICDPipelineAgentInput,
        )

        agent = CICDPipelineAgent(_StubClient({}))
        out = agent.build_output(
            CICDPipelineAgentInput(task_spec=_base_task_spec()),
            {"required_gates_present": "TRUE"},
        )
        assert out.required_gates_present is True


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

    def test_build_context_surfaces_scope_exclusions(self) -> None:
        """This agent never reads task_spec.scope directly (only
        InfrastructureAsCodeAgent does) and doesn't even read title, so an
        explicit exclusion must reach it through the prompt context instead,
        or a chosen rollout strategy could violate it with the agent never
        having been told about it."""
        from software_engineering_team.devops_team.deployment_strategy_agent import (
            DeploymentStrategyAgent,
            DeploymentStrategyAgentInput,
        )

        agent = DeploymentStrategyAgent(_StubClient({}))
        context = agent.build_context(DeploymentStrategyAgentInput(task_spec=_base_task_spec()))
        assert "cluster provisioning" in context


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

    def test_recovers_from_malformed_first_response(self) -> None:
        """A schema-invalid first reply (missing the required ``summary``
        key) drives ``run_single_shot_review``'s corrective retry; a valid
        second reply is used instead of falling back."""
        from software_engineering_team.devops_team.devsecops_review_agent import (
            DevSecOpsReviewAgent,
            DevSecOpsReviewInput,
        )

        client = _ScriptedClient(
            [
                {"findings": []},  # missing required "summary" -- schema-invalid
                {"approved": True, "findings": [], "summary": "recovered on retry"},
            ]
        )
        agent = DevSecOpsReviewAgent(client)
        out = agent.run(DevSecOpsReviewInput(task_description="test", artifacts={}))
        assert out.approved is True
        assert out.summary == "recovered on retry"
        assert client._idx == 2

    def test_recovers_from_malformed_finding(self) -> None:
        """A finding dict missing its required ``finding_id`` used to crash
        ``run()`` via a bare ``ReviewFinding(**f)`` call -- it now fails
        schema validation and drives a corrective retry instead."""
        from software_engineering_team.devops_team.devsecops_review_agent import (
            DevSecOpsReviewAgent,
            DevSecOpsReviewInput,
        )

        client = _ScriptedClient(
            [
                {
                    "approved": False,
                    "findings": [{"severity": "high", "issue": "no finding_id here"}],
                    "summary": "malformed finding",
                },
                {"approved": True, "findings": [], "summary": "clean on retry"},
            ]
        )
        agent = DevSecOpsReviewAgent(client)
        out = agent.run(DevSecOpsReviewInput(task_description="test", artifacts={}))
        assert out.approved is True
        assert out.findings == []
        assert out.summary == "clean on retry"
        assert client._idx == 2

    def test_falls_back_when_retries_exhausted(self) -> None:
        """A reply that stays schema-invalid across the corrective retry
        still yields the safe fallback -- never raises out of ``run()``."""
        from software_engineering_team.devops_team.devsecops_review_agent import (
            DevSecOpsReviewAgent,
            DevSecOpsReviewInput,
        )

        client = _StubClient({"findings": []})  # missing required "summary" every call
        agent = DevSecOpsReviewAgent(client)
        out = agent.run(DevSecOpsReviewInput(task_description="test", artifacts={}))
        assert out.approved is False
        assert out.findings == []
        assert "DevSecOps review failed" in out.summary


class TestGateStatusCoercion:
    def test_unknown_gate_status_coerced_to_not_run(self) -> None:
        from software_engineering_team.devops_team.models import coerce_gate_status

        assert coerce_gate_status("flaky") == "not_run"

    def test_known_gate_status_passes_through_after_normalization(self) -> None:
        from software_engineering_team.devops_team.models import coerce_gate_status

        assert coerce_gate_status(" FAIL ") == "fail"


class TestChangeReviewAgent:
    # The gate now routes through the shared code-review engine with the
    # ``devops_maintainability`` profile, so stubs return the engine's flat
    # issue shape ({approved, issues, summary, spec_compliance_notes}); the adapter maps those issues
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
        """A high-severity engine issue maps to a blocking ReviewFinding.

        This used to stub a deliberately buggy engine reply (``approved=True``
        alongside a high-severity issue) to prove the gate's own blocking rule
        overrides the engine's flag. ``ChunkReviewLLMResponse``'s consistency
        validator now rejects that exact contradiction at the schema layer, so
        it can no longer be produced through the stub at all -- the reply must
        be schema-valid (``approved=False``) here. The gate's blocking rule is
        still exercised: it is what turns the high-severity finding into a
        blocking ``ReviewFinding`` and an unapproved verdict.
        """
        from software_engineering_team.devops_team.change_review_agent import (
            ChangeReviewAgent,
            ChangeReviewInput,
        )

        client = _StubClient(
            {
                "approved": False,
                "issues": [
                    {
                        "severity": "high",
                        "category": "maintainability",
                        "file_path": "Dockerfile",
                        "description": "latest tag pinned nowhere",
                        "suggestion": "pin a digest",
                    }
                ],
                "summary": "blocked",
                "spec_compliance_notes": "",
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
            "software_engineering_team.devops_team.change_review_agent.agent.CodeReviewAgent",
            _RaisingEngine(),
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

    def test_severity_map_derives_from_engine_type(self) -> None:
        """_SEVERITY_MAP's keys are exactly code_review_agent's severity values,
        and every one maps to a valid ReviewFinding severity -- proving the map
        is derived from the shared type, not hand-copied."""
        from typing import get_args

        from software_engineering_team.code_review_agent.models import CodeReviewIssueSeverity
        from software_engineering_team.devops_team.change_review_agent.agent import (
            _REVIEW_FINDING_SEVERITIES,
            _SEVERITY_MAP,
        )

        engine_severities = set(get_args(CodeReviewIssueSeverity))
        assert set(_SEVERITY_MAP) == engine_severities
        assert all(v in _REVIEW_FINDING_SEVERITIES for v in _SEVERITY_MAP.values())

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
                "spec_compliance_notes": "",
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


class TestDevOpsTeamLeadAgentModelRouting:
    def test_qa_agent_preserves_devops_model_routing_key(self, monkeypatch) -> None:
        """A non-Strands client resolves the DevOps-facing QA agent's model
        under the 'devops' routing key, not QAExpertAgent's own default 'qa'."""
        from software_engineering_team.devops_team import orchestrator as orchestrator_mod

        captured: Dict[str, Any] = {}

        def _fake_get_strands_model(key: str, **_kwargs: Any) -> Any:
            captured["key"] = key
            return DummyLLMClient()  # a Strands Model — used directly downstream

        monkeypatch.setattr(orchestrator_mod, "get_strands_model", _fake_get_strands_model)
        DevOpsTeamLeadAgent(object())  # non-None, non-Strands -> resolves via key
        assert captured["key"] == "devops"


# ===========================================================================
# UNIT TESTS -- PHASE 4 REVIEW-INPUT SCOPE EXCLUSIONS
# ===========================================================================


class TestDescribeTaskWithExclusions:
    """Unit tests for the Phase 4 devsecops/change-review exclusion propagation.

    Neither DevSecOpsReviewInput.requirements (task_spec.goal.summary) nor
    ChangeReviewInput.task_description (task_spec.title) otherwise carries
    task_spec.scope.excluded, so an explicit exclusion could be violated by
    generated artifacts with neither reviewer ever having been told about it.
    """

    def test_appends_exclusions_when_present(self) -> None:
        spec = _base_task_spec(
            scope={"included": ["build"], "excluded": ["legacy Jenkins pipeline"]}
        )
        result = _describe_task_with_exclusions(spec, "base text")
        assert "base text" in result
        assert "legacy Jenkins pipeline" in result

    def test_returns_base_unchanged_when_no_exclusions(self) -> None:
        spec = _base_task_spec(scope={"included": ["build"], "excluded": []})
        result = _describe_task_with_exclusions(spec, "base text")
        assert result == "base text"

    def test_joins_multiple_exclusions(self) -> None:
        spec = _base_task_spec(
            scope={
                "included": ["build"],
                "excluded": ["legacy Jenkins pipeline", "blue-green rollout"],
            }
        )
        result = _describe_task_with_exclusions(spec, "base text")
        assert "legacy Jenkins pipeline" in result
        assert "blue-green rollout" in result


# ===========================================================================
# INTEGRATION TESTS -- ORCHESTRATOR
# ===========================================================================


class TestDevOpsTeamLeadAgentIntegration:
    def test_happy_path_run_task(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            init_ok, _ = initialize_new_repo(path)
            assert init_ok
            result = agent.run_task(
                _base_task_spec(task_id="devops-backend"),
                repo_path=path,
                build_verifier=MagicMock(return_value=(True, "")),
            )
            rev = subprocess.run(
                ["git", "rev-parse", "development"],
                cwd=path,
                capture_output=True,
                text=True,
                check=True,
            )
            dev_head = rev.stdout.strip()
            assert dev_head
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
        assert gitops.merge.merge_commit_hash == dev_head
        assert any(c.hash == gitops.merge.merge_commit_hash for c in gitops.commits)

    def test_happy_path_direct_run(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert pkg.task_id == "DO-2207"
        assert pkg.status == "completed"
        assert len(pkg.acceptance_criteria_trace) == 2
        assert pkg.release_readiness.deployment_strategy == "rolling"

    def test_multiple_sequential_runs_on_same_lead_agent(self, monkeypatch) -> None:
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
        happy_path = _scripted_llm_for_happy_path()
        per_run = list(happy_path.responses)
        assert len(per_run) == 9
        chained = _ScriptedClient(per_run + per_run, default_factory=happy_path._default_factory)
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

        monkeypatch.setattr(agent, "_run_execution_tools", _failing_exec)
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
        with pytest.raises(ValueError, match="Clarification required"):
            agent.run(spec)

    def test_blocked_by_env_policy(self) -> None:
        mock_llm = MagicMock()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec(
            rollback_requirements=[],
            scope={"included": ["build"], "excluded": []},
        )
        with pytest.raises(ValueError, match="[Pp]olicy violation"):
            agent.run(spec)

    def test_blocked_by_security_review(self) -> None:
        mock_llm = _ScriptedClient(
            _SECURITY_BLOCKING_SCRIPT_PREFIX
            + [
                {"approved": True, "findings": [], "summary": "ok"},
                {"approved": True, "quality_gates": {"iac_validate": "pass"}, "summary": "ok"},
            ]
        )
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            result = agent.run_task(
                _base_task_spec(task_id="devops-sec-block"),
                repo_path=Path(tmp),
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
            _SECURITY_BLOCKING_SCRIPT_PREFIX
            + [
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
            result = agent.run_task(
                _base_task_spec(task_id="devops-sec-mask"),
                repo_path=Path(tmp),
            )
        assert not result.success
        assert result.completion_package is not None
        assert result.completion_package.quality_gates["security_review"] == "fail"

    def test_completion_package_has_acceptance_trace(self) -> None:
        from software_engineering_team.qa_agent.models import QAOutput

        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        # Stub Phase 4 validation output directly. The DummyLLM + Strands
        # structured-output path reuses the prior change-review payload for the
        # QA acceptance_evidence call, so a scripted ``acceptance_trace`` on the
        # LLM client does not reach the orchestrator. This test targets Phase 5
        # wiring, not that adapter quirk.
        agent.qa_agent.run = (  # type: ignore[method-assign]
            lambda _inp: QAOutput(
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
        assert any(path.endswith((".tf", ".yml", ".yaml", ".md")) for path in pkg.files_changed)

    def test_quality_gates_in_completion(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec()
        pkg = agent.run(spec)
        assert pkg.quality_gates.get("security_review") == "pass"
        assert pkg.quality_gates.get("change_review") == "pass"

    def test_build_verifier_failure(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            init_ok, _ = initialize_new_repo(Path(tmp))
            assert init_ok
            result = agent.run_task(
                _base_task_spec(task_id="devops-bv-fail"),
                repo_path=Path(tmp),
                build_verifier=MagicMock(return_value=(False, "Docker build failed")),
            )
        assert not result.success
        assert result.failure_reason == "Docker build failed"

    def test_completion_package_git_operations_real_merge(self) -> None:
        """A real ``run_task`` delivers the artifacts by cutting a feature
        branch, merging it into development, and deleting it — the reported
        metadata reflects the actual git state (real SHA equal to development's
        HEAD), not fabricated placeholders."""
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            init_ok, _ = initialize_new_repo(path)
            assert init_ok
            result = agent.run_task(
                _base_task_spec(task_id="devops-real-merge"),
                repo_path=path,
                build_verifier=MagicMock(return_value=(True, "")),
            )
            branches = subprocess.run(
                ["git", "branch"], cwd=path, capture_output=True, text=True, check=True
            ).stdout
            rev = subprocess.run(
                ["git", "rev-parse", "development"],
                cwd=path,
                capture_output=True,
                text=True,
                check=True,
            )
            dev_head = rev.stdout.strip()
            assert dev_head
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
            result = agent.run_task(
                _base_task_spec(task_id="devops-head-sha-unknown"),
                repo_path=Path(tmp),
                build_verifier=MagicMock(return_value=(True, "")),
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
            result = agent.run_task(
                _base_task_spec(task_id="devops-merge-fail"),
                repo_path=Path(tmp),
                build_verifier=MagicMock(return_value=(True, "")),
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
            result = agent.run_task(
                _base_task_spec(task_id="devops-dev-branch-fail"),
                repo_path=Path(tmp),
                build_verifier=MagicMock(return_value=(True, "")),
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
            result = agent.run_task(
                _base_task_spec(task_id="devops-feat-branch-fail"),
                repo_path=Path(tmp),
                build_verifier=MagicMock(return_value=(True, "")),
            )
        assert not result.success
        assert "feature branch" in (result.failure_reason or "")


# ===========================================================================
# STRUCTURED, WRITE-CAPABLE ENTRY POINT -- run_task
# ===========================================================================


class TestRunTaskStructuredEntrypoint:
    """``run_task`` is the structured, write-capable entry point the
    coding-team's devops worker adapter uses."""

    def test_run_task_accepts_structured_spec_and_merges(self) -> None:
        """Default ``merge_to_development=True`` cuts a feature branch,
        merges it into development, and deletes it."""
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec(task_id="devops-run-task-merge")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            init_ok, _ = initialize_new_repo(path)
            assert init_ok
            result = agent.run_task(
                spec, repo_path=path, build_verifier=MagicMock(return_value=(True, ""))
            )
            branches = subprocess.run(
                ["git", "branch"], cwd=path, capture_output=True, text=True, check=True
            ).stdout
            rev = subprocess.run(
                ["git", "rev-parse", "development"],
                cwd=path,
                capture_output=True,
                text=True,
                check=True,
            )
            dev_head = rev.stdout.strip()
        assert result.success
        assert result.completion_package is not None
        assert result.completion_package.status == "completed"
        gitops = result.completion_package.git_operations
        assert gitops.branch_created.startswith("feature/")
        assert gitops.merge is not None
        assert gitops.merge.status == "merged"
        assert gitops.merge.merge_commit_hash == dev_head
        assert "feature/" not in branches

    def test_run_task_handoff_mode_leaves_branch_unmerged(self) -> None:
        """``merge_to_development=False`` commits the feature branch and
        leaves it in place — the mode the coding-team worker uses — instead
        of merging/deleting it."""
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec(task_id="devops-run-task-handoff")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            init_ok, _ = initialize_new_repo(path)
            assert init_ok
            rev_before = subprocess.run(
                ["git", "rev-parse", "development"],
                cwd=path,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            result = agent.run_task(spec, repo_path=path, merge_to_development=False)
            branches = subprocess.run(
                ["git", "branch"], cwd=path, capture_output=True, text=True, check=True
            ).stdout
            rev_after = subprocess.run(
                ["git", "rev-parse", "development"],
                cwd=path,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        assert result.success
        assert result.completion_package is not None
        assert result.completion_package.status == "completed"
        gitops = result.completion_package.git_operations
        assert gitops.branch_created.startswith("feature/")
        assert gitops.merge is None
        assert len(result.completion_package.files_changed) > 0
        # The branch is still there, and development never advanced.
        assert gitops.branch_created in branches
        assert rev_after == rev_before

    def test_run_task_handoff_branch_matches_make_branch_suffix(self) -> None:
        """The branch name the pipeline actually cuts must match what a
        caller (the coding-team devops worker) independently computes via
        ``make_branch_suffix`` — otherwise the Tech Lead review diffs the
        wrong branch."""
        from shared.git.branch_utils import make_branch_suffix

        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec(task_id="devops-branch-name", title="Add deploy workflow")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            init_ok, _ = initialize_new_repo(path)
            assert init_ok
            result = agent.run_task(spec, repo_path=path, merge_to_development=False)
        expected = f"feature/{make_branch_suffix(spec.task_id, spec.title)}"
        assert result.completion_package.git_operations.branch_created == expected

    def test_run_task_reports_delivery_failure_in_handoff_mode(self, monkeypatch) -> None:
        """A commit failure in handoff mode still reports a blocked package
        with ``merge=None`` (no merge was even attempted)."""
        import software_engineering_team.devops_team.orchestrator as orch

        monkeypatch.setattr(orch, "commit_working_tree", lambda *a, **k: (False, "boom"))
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec(task_id="devops-handoff-fail")
        with tempfile.TemporaryDirectory() as tmp:
            init_ok, _ = initialize_new_repo(Path(tmp))
            assert init_ok
            result = agent.run_task(spec, repo_path=Path(tmp), merge_to_development=False)
        assert not result.success
        assert result.completion_package is not None
        assert result.completion_package.status == "blocked"
        assert result.completion_package.git_operations.merge is None

    def test_run_task_rejects_missing_repo_path(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec(task_id="devops-missing-repo")
        with pytest.raises(AssertionError):
            agent.run_task(spec, repo_path=Path("/nonexistent/does/not/exist"))

    def test_run_task_requires_task_spec(self) -> None:
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(AssertionError):
                agent.run_task(None, repo_path=Path(tmp))

    def test_run_task_cleans_untracked_validation_leftovers_before_delivering(
        self, monkeypatch
    ) -> None:
        """A Phase 4/4.5 validation tool (e.g. `terraform init` leaving
        `.terraform.lock.hcl`) can leave untracked files in the working tree
        AFTER Phase 3 has already written+committed the generated artifacts.
        deliver_inline_merge/prepare_handoff_branch both commit via `git add
        -A`, which would otherwise sweep those files into the delivered
        commit even though they were never part of the generated artifact
        map. Simulate that leftover as a Phase 4 validation-tool side effect
        (planting it before Phase 3 runs would just get it committed by
        Phase 3's own git-add-A, which doesn't exercise this fix) and prove
        it never reaches the delivered branch."""
        import software_engineering_team.devops_team.tool_dispatch as tool_dispatch_mod

        real_run_validation_tools = tool_dispatch_mod.run_validation_tools

        def _run_validation_tools_with_leftover(agent, repo_path):
            (Path(repo_path) / "untracked.leftover").write_text(
                "validation tool side effect", encoding="utf-8"
            )
            return real_run_validation_tools(agent, repo_path)

        monkeypatch.setattr(
            tool_dispatch_mod, "run_validation_tools", _run_validation_tools_with_leftover
        )
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec(task_id="devops-clean-untracked")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            init_ok, _ = initialize_new_repo(path)
            assert init_ok

            result = agent.run_task(spec, repo_path=path, merge_to_development=False)

            assert not (path / "untracked.leftover").exists()
        assert result.success
        assert "untracked.leftover" not in result.completion_package.files_changed

    def test_run_task_blocks_delivery_when_untracked_cleanup_fails(self, monkeypatch) -> None:
        """If the pre-delivery reset_hard_to(repo_path, "HEAD") itself fails (e.g. a
        permissions error), delivery must block rather than merely warn and proceed:
        continuing into deliver_inline_merge/prepare_handoff_branch would let exactly
        the leftover this reset exists to catch slip into the commit via git add -A
        unreviewed."""
        import software_engineering_team.devops_team.phases.deliver_merge as deliver_merge_mod

        monkeypatch.setattr(
            deliver_merge_mod,
            "reset_hard_to",
            lambda repo_path, ref: (False, "permission denied"),
        )
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec(task_id="devops-cleanup-fail")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            init_ok, _ = initialize_new_repo(path)
            assert init_ok
            result = agent.run_task(spec, repo_path=path, merge_to_development=False)

        assert not result.success
        assert result.completion_package is not None
        assert result.completion_package.status == "blocked"
        assert "reset" in (result.failure_reason or "").lower()

    def test_run_task_reverts_tracked_validation_side_effects_before_delivering(
        self, monkeypatch
    ) -> None:
        """A validator can MODIFY an already-tracked file (e.g. terraform init
        updating a committed .terraform.lock.hcl after provider constraints change)
        -- git clean alone would not touch that, only reset_hard_to would. Simulate
        that as a Phase 4 validation-tool side effect and confirm the modification
        never reaches the delivered branch."""
        import software_engineering_team.devops_team.tool_dispatch as tool_dispatch_mod

        real_run_validation_tools = tool_dispatch_mod.run_validation_tools

        def _run_validation_tools_with_tracked_mutation(agent, repo_path):
            tracked = Path(repo_path) / "tracked.lock"
            if tracked.exists():
                tracked.write_text("mutated by a validation tool", encoding="utf-8")
            return real_run_validation_tools(agent, repo_path)

        monkeypatch.setattr(
            tool_dispatch_mod, "run_validation_tools", _run_validation_tools_with_tracked_mutation
        )
        mock_llm = _scripted_llm_for_happy_path()
        agent = DevOpsTeamLeadAgent(mock_llm)
        spec = _base_task_spec(task_id="devops-revert-tracked")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            init_ok, _ = initialize_new_repo(path)
            assert init_ok
            (path / "tracked.lock").write_text("committed content", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True, check=True)
            subprocess.run(
                ["git", "commit", "-m", "seed tracked.lock"],
                cwd=path,
                capture_output=True,
                check=True,
            )

            result = agent.run_task(spec, repo_path=path, merge_to_development=False)

            assert (path / "tracked.lock").read_text(encoding="utf-8") == "committed content"
        assert result.success
        assert "tracked.lock" not in result.completion_package.files_changed


# ===========================================================================
# COMPATIBILITY / MIGRATION TESTS
# ===========================================================================


class TestRunPipelineCompletionGuard:
    def test_run_pipeline_raises_if_completion_not_assigned(self, monkeypatch) -> None:
        agent = DevOpsTeamLeadAgent(MagicMock())
        spec = _base_task_spec(task_id="completion-guard")
        monkeypatch.setattr(agent, "_run_gated_phases", lambda phases: None)
        with pytest.raises(RuntimeError, match="Phase 5 did not assign a completion package"):
            agent._run_pipeline(
                repo_path=Path("/tmp"),
                task_spec=spec,
                build_verifier=None,
                write_changes=False,
                subdir="",
            )


# ===========================================================================
# MAIN ORCHESTRATOR INTEGRATION
# ===========================================================================


class TestDevOpsTeamLeadAgentExecutionTools:
    """Verify execution tool agents are initialized on DevOpsTeamLeadAgent."""

    def test_init_has_execution_tools(self) -> None:
        mock_llm = MagicMock()
        mock_llm.complete_json.return_value = {}
        agent = DevOpsTeamLeadAgent(mock_llm)
        assert isinstance(agent.terraform_exec_tool, TerraformExecutionToolAgent)
        assert isinstance(agent.cdk_exec_tool, CDKExecutionToolAgent)
        assert isinstance(agent.compose_exec_tool, DockerComposeExecutionToolAgent)
        assert isinstance(agent.helm_exec_tool, HelmExecutionToolAgent)
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

    def test_run_validation_tools_executes_the_four_tool_calls_concurrently(self) -> None:
        """Perf-guard: proves the 4 tool calls run concurrently, not sequentially.

        Each call sleeps ~0.12s (simulating subprocess/I-O-bound work).
        Sequential execution would take >= 4 * 0.12s == 0.48s; concurrent
        execution across 4 workers should take roughly one sleep interval.
        The 2x-one-interval bound sits well below the sequential floor and
        well above the concurrent expectation, so it isn't flaky under
        ordinary CI scheduling noise while still failing hard if the four
        calls ever became sequential again.
        """
        sleep_s = 0.12

        def _slow(output: Any) -> Callable[[Any], Any]:
            def _run(_input: Any) -> Any:
                time.sleep(sleep_s)
                return output

            return _run

        agent = MagicMock()
        agent.iac_validation_tool.run.side_effect = _slow(
            IaCValidationOutput(success=True, checks={"iac_validate": "pass"})
        )
        agent.policy_tool.run.side_effect = _slow(
            PolicyAsCodeOutput(success=True, checks={"policy_checks": "pass"})
        )
        agent.cicd_lint_tool.run.side_effect = _slow(
            CICDLintOutput(success=True, checks={"pipeline_lint": "pass"})
        )
        agent.deploy_dry_run_tool.run.side_effect = _slow(
            DeploymentDryRunOutput(success=True, checks={"deployment_dry_run": "pass"})
        )

        start = time.perf_counter()
        vt = tool_dispatch.run_validation_tools(agent, Path("/tmp/x"))
        elapsed = time.perf_counter() - start

        assert vt.tool_gate_map == {
            "iac_validate": "pass",
            "policy_checks": "pass",
            "pipeline_lint": "pass",
            "deployment_dry_run": "pass",
        }
        assert elapsed < 2 * sleep_s, (
            f"run_validation_tools took {elapsed:.3f}s, expected well under "
            f"{2 * sleep_s:.3f}s if the 4 tool calls run concurrently"
        )


class TestPhase4QualityGateReviewCallsConcurrency:
    """Perf-guard for the parallel_map refactor of run_phase4_quality_gate's 3 review calls."""

    def _mock_agent(self) -> MagicMock:
        agent = MagicMock()
        agent.llm = object()  # not a DummyLLMClient -> takes the parallel_map branch
        agent.iac_validation_tool.run.return_value = IaCValidationOutput(success=True, checks={})
        agent.policy_tool.run.return_value = PolicyAsCodeOutput(success=True, checks={})
        agent.cicd_lint_tool.run.return_value = CICDLintOutput(success=True, checks={})
        agent.deploy_dry_run_tool.run.return_value = DeploymentDryRunOutput(success=True, checks={})
        agent._run_execution_tools.return_value = []
        return agent

    def test_run_phase4_quality_gate_executes_the_three_review_calls_concurrently(self) -> None:
        """Each review call sleeps ~0.12s; sequential would take >= 3 * 0.12s == 0.36s.

        Concurrent execution across 3 workers should take roughly one sleep
        interval, so a 2x-one-interval bound is unreachable under sequential
        execution while staying well above ordinary CI scheduling noise.
        """
        from software_engineering_team.devops_team.change_review_agent import ChangeReviewOutput
        from software_engineering_team.devops_team.devsecops_review_agent import (
            DevSecOpsReviewOutput,
        )
        from software_engineering_team.qa_agent.models import QAOutput

        sleep_s = 0.12

        def _slow(output: Any) -> Callable[[Any], Any]:
            def _run(_input: Any) -> Any:
                time.sleep(sleep_s)
                return output

            return _run

        agent = self._mock_agent()
        agent.devsecops_review_agent.run.side_effect = _slow(DevSecOpsReviewOutput())
        agent.change_review_agent.run.side_effect = _slow(ChangeReviewOutput())
        agent.qa_agent.run.side_effect = _slow(
            QAOutput(approved=True, quality_gates={"acceptance_evidence": "pass"}, summary="ok")
        )

        spec = _base_task_spec()
        start = time.perf_counter()
        result = run_phase4_quality_gate(
            agent,
            task_spec=spec,
            repo_path=Path("/tmp/x"),
            aggregated_artifacts={},
            write_changes=False,
            subdir="",
            build_verifier=None,
        )
        elapsed = time.perf_counter() - start

        assert result.blocked_result is None
        assert result.quality_gates["security_review"] == "pass"
        assert result.quality_gates["change_review"] == "pass"
        assert elapsed < 2 * sleep_s, (
            f"run_phase4_quality_gate took {elapsed:.3f}s, expected well under "
            f"{2 * sleep_s:.3f}s if the 3 review calls run concurrently"
        )

    def test_run_phase4_quality_gate_runs_sequentially_for_dummy_llm_client(self) -> None:
        """A DummyLLMClient double (matching _ScriptedClient's isinstance check) takes the
        sequential branch, so scripted integration tests keep their deterministic call order.
        """
        agent = self._mock_agent()
        agent.llm = DummyLLMClient()

        from software_engineering_team.devops_team.change_review_agent import ChangeReviewOutput
        from software_engineering_team.devops_team.devsecops_review_agent import (
            DevSecOpsReviewOutput,
        )
        from software_engineering_team.qa_agent.models import QAOutput

        call_order: List[str] = []

        def _record(name: str, output: Any) -> Callable[[Any], Any]:
            def _run(_input: Any) -> Any:
                call_order.append(name)
                return output

            return _run

        agent.devsecops_review_agent.run.side_effect = _record("devsec", DevSecOpsReviewOutput())
        agent.change_review_agent.run.side_effect = _record("change_review", ChangeReviewOutput())
        agent.qa_agent.run.side_effect = _record(
            "qa",
            QAOutput(approved=True, quality_gates={"acceptance_evidence": "pass"}, summary="ok"),
        )

        run_phase4_quality_gate(
            agent,
            task_spec=_base_task_spec(),
            repo_path=Path("/tmp/x"),
            aggregated_artifacts={},
            write_changes=False,
            subdir="",
            build_verifier=None,
        )

        assert call_order == ["devsec", "change_review", "qa"]


class TestMainOrchestratorRegistration:
    def test_devops_team_lead_registered(self) -> None:
        """Verify DevOpsTeamLeadAgent constructs from its definition module."""
        agent = DevOpsTeamLeadAgent(DummyLLMClient())
        assert isinstance(agent, DevOpsTeamLeadAgent)

    def test_build_fix_specialist_registered(self) -> None:
        """Verify BuildFixSpecialistAgent constructs from its definition module."""
        from software_engineering_team.build_fix_specialist import BuildFixSpecialistAgent

        agent = BuildFixSpecialistAgent(DummyLLMClient())
        assert isinstance(agent, BuildFixSpecialistAgent)


# ===========================================================================
# FENCE-RECOVERY REGRESSION TESTS
#
# Each of these 7 agents now routes its raw LLM completion through
# complete_json_with_continuation() instead of a bare json.loads(). These
# tests exercise the real recovery path (llm_mod.Agent is mocked at the
# shared/llm.py level, not at complete_json_with_continuation itself) to
# prove a markdown-fenced response no longer crashes the agent.
#
# devsecops_review is deliberately absent here: it was retrofitted onto
# software_engineering_team.shared.single_shot_review.run_single_shot_review
# (a raw LLMClient.complete_json call), which no longer goes through a
# Strands Agent or complete_json_with_continuation at all, so this
# fence-recovery mechanism doesn't apply to it. Fenced/markdown-wrapped
# replies are the underlying LLMClient implementation's responsibility now,
# same as every other complete_json-based caller (e.g. code_review_agent).
# ===========================================================================


def _fenced_iac_case():
    from software_engineering_team.devops_team.iac_agent import (
        IaCAgentInput,
        InfrastructureAsCodeAgent,
    )

    return (
        InfrastructureAsCodeAgent(_strands_model_double()),
        IaCAgentInput(task_spec=_base_task_spec()),
    )


def _fenced_cicd_case():
    from software_engineering_team.devops_team.cicd_pipeline_agent import (
        CICDPipelineAgent,
        CICDPipelineAgentInput,
    )

    return (
        CICDPipelineAgent(_strands_model_double()),
        CICDPipelineAgentInput(task_spec=_base_task_spec()),
    )


def _fenced_deploy_case():
    from software_engineering_team.devops_team.deployment_strategy_agent import (
        DeploymentStrategyAgent,
        DeploymentStrategyAgentInput,
    )

    return (
        DeploymentStrategyAgent(_strands_model_double()),
        DeploymentStrategyAgentInput(task_spec=_base_task_spec()),
    )


def _fenced_infra_debug_case():
    from software_engineering_team.devops_team.infra_debug_agent import (
        IaCDebugInput,
        InfraDebugAgent,
    )

    return (
        InfraDebugAgent(_strands_model_double()),
        IaCDebugInput(
            execution_output="Error: bad hcl",
            tool_name="terraform",
            command="plan",
            artifacts={"main.tf": "resource {}"},
        ),
    )


def _fenced_doc_runbook_case():
    from software_engineering_team.devops_team.doc_runbook_agent import (
        DocumentationRunbookAgent,
        DocumentationRunbookInput,
    )

    return (
        DocumentationRunbookAgent(_strands_model_double()),
        DocumentationRunbookInput(
            task_id="DO-1",
            task_title="test",
            artifacts={"a.tf": "resource"},
            quality_gates={"iac_validate": "pass"},
        ),
    )


def _fenced_infra_patch_case():
    from software_engineering_team.devops_team.infra_debug_agent.models import (
        IaCDebugOutput,
        IaCExecutionError,
    )
    from software_engineering_team.devops_team.infra_patch_agent import (
        IaCPatchInput,
        InfraPatchAgent,
    )

    debug_output = IaCDebugOutput(
        errors=[IaCExecutionError(error_type="syntax", error_message="bad hcl")],
        summary="debug",
        fixable=True,
    )
    return (
        InfraPatchAgent(_strands_model_double()),
        IaCPatchInput(
            debug_output=debug_output,
            original_artifacts={"main.tf": "resource {}"},
        ),
    )


def _fenced_task_clarifier_case():
    return (
        DevOpsTaskClarifierAgent(_strands_model_double()),
        DevOpsTaskClarifierInput(task_spec=_base_task_spec()),
    )


def _check_fenced_iac(out) -> None:
    assert "infra/main.tf" in out.artifacts
    assert out.summary == "fenced iac ok"


def _check_fenced_cicd(out) -> None:
    assert ".github/workflows/ci.yml" in out.artifacts


def _check_fenced_deploy(out) -> None:
    assert out.strategy == "rolling"


def _check_fenced_infra_debug(out) -> None:
    assert out.summary == "fenced debug ok"
    assert out.fixable is True


def _check_fenced_doc_runbook(out) -> None:
    assert "docs/runbook.md" in out.files


def _check_fenced_infra_patch(out) -> None:
    assert "main.tf" in out.patched_artifacts


def _check_fenced_task_clarifier(out) -> None:
    assert out.approved_for_execution is True


# DevSecOpsReviewAgent is deliberately absent from this list: it no longer
# builds a Strands Agent or calls complete_json_with_continuation (see
# devsecops_review_agent/agent.py), so this Strands-Agent-double-based
# fenced-markdown-recovery harness no longer applies to it. Fenced-JSON
# recovery for its new run_single_shot_review/complete_json call path is
# each concrete LLMClient's own extract_json_from_response usage, already
# covered by llm_service's own tests -- not a per-agent concern here.
_FENCED_RECOVERY_CASES = [
    (
        "iac",
        _fenced_iac_case,
        {
            "artifacts": {"infra/main.tf": "resource {}"},
            "summary": "fenced iac ok",
            "destructive_changes_detected": False,
            "blast_radius_notes": [],
        },
        _check_fenced_iac,
    ),
    (
        "cicd_pipeline",
        _fenced_cicd_case,
        {
            "artifacts": {".github/workflows/ci.yml": "on: push"},
            "pipeline_job_graph_summary": "build -> test",
            "required_gates_present": True,
            "summary": "fenced cicd ok",
        },
        _check_fenced_cicd,
    ),
    (
        "deployment_strategy",
        _fenced_deploy_case,
        {
            "artifacts": {"deploy/values.yaml": "replicas: 2"},
            "strategy": "rolling",
            "rollback_plan": ["helm rollback"],
            "health_checks": ["/healthz"],
            "rollout_timeout_minutes": 10,
            "summary": "fenced deploy ok",
        },
        _check_fenced_deploy,
    ),
    (
        "infra_debug",
        _fenced_infra_debug_case,
        {
            "errors": [{"error_type": "syntax", "error_message": "bad hcl"}],
            "summary": "fenced debug ok",
            "fixable": True,
        },
        _check_fenced_infra_debug,
    ),
    (
        "doc_runbook",
        _fenced_doc_runbook_case,
        {
            "files": {"docs/runbook.md": "# Runbook"},
            "summary": "fenced doc ok",
        },
        _check_fenced_doc_runbook,
    ),
    (
        "infra_patch",
        _fenced_infra_patch_case,
        {
            "patched_artifacts": {"main.tf": "resource {} # fixed"},
            "summary": "fenced patch ok",
            "edits_applied": 1,
        },
        _check_fenced_infra_patch,
    ),
    (
        "task_clarifier",
        _fenced_task_clarifier_case,
        {
            "approved_for_execution": True,
            "checklist": ["done"],
            "gaps": [],
            "clarification_requests": [],
        },
        _check_fenced_task_clarifier,
    ),
]


class TestDevOpsAgentsRecoverFencedJson:
    """Markdown-fenced LLM JSON must recover for each DevOps LLM agent that
    still uses ``complete_json_with_continuation``."""

    @pytest.mark.parametrize(
        "build,payload,check",
        [(case[1], case[2], case[3]) for case in _FENCED_RECOVERY_CASES],
        ids=[case[0] for case in _FENCED_RECOVERY_CASES],
    )
    def test_recovers_fenced_json(self, monkeypatch, build, payload, check) -> None:
        """Each remaining DevOps LLM agent recovers markdown-fenced JSON via complete_json_with_continuation."""
        _patch_fenced_response(monkeypatch, payload)
        agent, inp = build()
        check(agent.run(inp))
