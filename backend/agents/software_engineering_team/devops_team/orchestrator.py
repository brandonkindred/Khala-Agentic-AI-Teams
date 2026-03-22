"""DevOps team orchestrator (DevOpsTeamLeadAgent)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm_service import LLMClient
from software_engineering_team.shared.repo_writer import NO_FILES_TO_WRITE_MSG, write_agent_output

from .change_review_agent import ChangeReviewAgent, ChangeReviewInput
from .cicd_pipeline_agent import CICDPipelineAgent, CICDPipelineAgentInput
from .deployment_strategy_agent import DeploymentStrategyAgent, DeploymentStrategyAgentInput
from .devsecops_review_agent import DevSecOpsReviewAgent, DevSecOpsReviewInput
from .doc_runbook_agent import DocumentationRunbookAgent, DocumentationRunbookInput
from .iac_agent import IaCAgentInput, InfrastructureAsCodeAgent
from .models import (
    CriterionTrace,
    DevOpsCompletionPackage,
    DevOpsTaskSpec,
    DevOpsTeamResult,
    GitCommitMetadata,
    GitMergeMetadata,
    GitOperationsMetadata,
    HandoffInfo,
    ReleaseReadiness,
    SubtaskContract,
)

DEVOPS_REQUIRED_GATE_NAMES = [
    "iac_validate",
    "iac_validate_fmt",
    "policy_checks",
    "pipeline_lint",
    "pipeline_gate_check",
    "deployment_dry_run",
    "security_review",
    "change_review",
]

ENV_POLICY = {
    "dev": {
        "auto_deploy_allowed": True,
        "approval_required": False,
        "rollback_test_required": False,
        "policy_strictness": "low",
    },
    "staging": {
        "auto_deploy_allowed": True,
        "approval_required": False,
        "rollback_test_required": True,
        "policy_strictness": "medium",
    },
    "production": {
        "auto_deploy_allowed": False,
        "approval_required": True,
        "rollback_test_required": True,
        "policy_strictness": "high",
    },
}
from .infra_debug_agent import IaCDebugInput, InfraDebugAgent  # noqa: E402
from .infra_patch_agent import IaCPatchInput, InfraPatchAgent  # noqa: E402
from .task_clarifier import DevOpsTaskClarifierAgent, DevOpsTaskClarifierInput  # noqa: E402
from .test_validation_agent import (  # noqa: E402
    DevOpsTestValidationAgent,
    DevOpsTestValidationInput,
)
from .tool_agents import (  # noqa: E402
    CDKExecutionInput,
    CDKExecutionToolAgent,
    CICDLintInput,
    CICDLintPipelineValidationToolAgent,
    DeploymentDryRunInput,
    DeploymentDryRunPlanToolAgent,
    DockerComposeExecutionInput,
    DockerComposeExecutionToolAgent,
    HelmExecutionInput,
    HelmExecutionToolAgent,
    IaCValidationInput,
    IaCValidationToolAgent,
    PolicyAsCodeInput,
    PolicyAsCodeToolAgent,
    RepoNavigatorInput,
    RepoNavigatorToolAgent,
    TerraformExecutionInput,
    TerraformExecutionToolAgent,
)

logger = logging.getLogger(__name__)


class DevOpsTeamLeadAgent:
    """Coordinates specialized DevOps agents with hard gates."""

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self.task_clarifier = DevOpsTaskClarifierAgent(llm_client)
        self.iac_agent = InfrastructureAsCodeAgent(llm_client)
        self.cicd_agent = CICDPipelineAgent(llm_client)
        self.deployment_agent = DeploymentStrategyAgent(llm_client)
        self.devsecops_review_agent = DevSecOpsReviewAgent(llm_client)
        self.test_validation_agent = DevOpsTestValidationAgent(llm_client)
        self.change_review_agent = ChangeReviewAgent(llm_client)
        self.doc_runbook_agent = DocumentationRunbookAgent(llm_client)

        self.repo_navigator_tool = RepoNavigatorToolAgent()
        self.iac_validation_tool = IaCValidationToolAgent()
        self.policy_tool = PolicyAsCodeToolAgent()
        self.cicd_lint_tool = CICDLintPipelineValidationToolAgent()
        self.deploy_dry_run_tool = DeploymentDryRunPlanToolAgent()

        self.terraform_exec_tool = TerraformExecutionToolAgent()
        self.cdk_exec_tool = CDKExecutionToolAgent()
        self.compose_exec_tool = DockerComposeExecutionToolAgent()
        self.helm_exec_tool = HelmExecutionToolAgent()
        self.infra_debug_agent = InfraDebugAgent(llm_client)
        self.infra_patch_agent = InfraPatchAgent(llm_client)

    @staticmethod
    def _build_legacy_spec(
        *,
        task_id: str,
        task_description: str,
        requirements: str,
        target_repo: Optional[Any] = None,
    ) -> DevOpsTaskSpec:
        repo_name = target_repo.value if hasattr(target_repo, "value") else (str(target_repo) if target_repo else "")
        combined_text = f"{task_description} {requirements}".lower()
        # Match explicit production intent; avoid false positives like "produce".
        env = "production" if re.search(r"\b(prod|production)\b", combined_text) else "staging"
        return DevOpsTaskSpec(
            task_id=task_id,
            title=task_description[:120] or task_id,
            platform_scope={"cloud": "on-premises", "environments": ["dev", env]},
            repo_context={"app_repo": repo_name or "application", "infra_repo": "platform-infra", "pipeline_repo": repo_name or "application"},
            goal={"summary": task_description},
            scope={"included": [requirements], "excluded": []},
            constraints={"secrets": {"source": "managed_secret_store"}},
            acceptance_criteria=[
                "CI/CD workflow exists and validates",
                "Deployment strategy and rollback documented",
                "Security and policy review executed",
            ],
            rollback_requirements=["Rollback to previous known good release"],
            security_constraints=["No plaintext credentials", "Least privilege IAM"],
            compliance_constraints=["Audit trail required"],
            environment=env,
        )

    def run(self, input_data: DevOpsTaskSpec) -> DevOpsCompletionPackage:
        """Execute model-only run for contract-first input (no repo writes)."""
        result = self._run_pipeline(
            repo_path=Path("."),
            task_spec=input_data,
            build_verifier=None,
            write_changes=False,
        )
        if result.completion_package is None:
            raise ValueError(result.failure_reason or "DevOps team run failed")
        return result.completion_package

    def run_workflow(
        self,
        *,
        repo_path: Path,
        task_description: str,
        requirements: str,
        architecture: Optional[Any] = None,
        existing_pipeline: Optional[str] = None,
        target_repo: Optional[Any] = None,
        tech_stack: Optional[List[str]] = None,
        build_verifier: Optional[Any] = None,
        task_id: str = "devops",
        subdir: str = "",
        max_iterations: int = 1,
        devops_review_agent: Optional[Any] = None,
    ) -> DevOpsTeamResult:
        """Compatibility workflow adapter for existing orchestrator/tech lead calls."""
        _ = architecture, existing_pipeline, tech_stack, max_iterations, devops_review_agent  # reserved for future routing
        task_spec = self._build_legacy_spec(
            task_id=task_id,
            task_description=task_description,
            requirements=requirements,
            target_repo=target_repo,
        )
        return self._run_pipeline(
            repo_path=Path(repo_path).resolve(),
            task_spec=task_spec,
            build_verifier=build_verifier,
            write_changes=True,
            subdir=subdir,
        )

    @staticmethod
    def _build_subtask_contracts(task_spec: DevOpsTaskSpec) -> List[SubtaskContract]:
        return [
            SubtaskContract(
                subtask_id=f"{task_spec.task_id}-T1",
                owner="InfrastructureAsCodeAgent",
                objective="Implement IaC changes for task scope",
                inputs=["validated_task_spec", "repo_context"],
                constraints=["no destructive changes without approval", "no secrets in code"],
                expected_artifact=["iac_files"],
                completion_criteria=["IaC validates", "no wildcard IAM"],
            ),
            SubtaskContract(
                subtask_id=f"{task_spec.task_id}-T2",
                owner="CICDPipelineAgent",
                objective="Create CI/CD workflow with gates",
                inputs=["validated_task_spec", "repo_context", "deployment_strategy_spec"],
                constraints=["OIDC preferred", "no prod deploy without approval gate"],
                expected_artifact=["workflow_file", "pipeline_job_graph_summary"],
                completion_criteria=["workflow syntax valid", "required gates present"],
            ),
            SubtaskContract(
                subtask_id=f"{task_spec.task_id}-T3",
                owner="DeploymentStrategyAgent",
                objective="Define rollout and rollback mechanics",
                inputs=["validated_task_spec"],
                constraints=["health checks required", "rollback path defined"],
                expected_artifact=["deploy_manifests", "rollback_plan"],
                completion_criteria=["strategy defined", "rollback steps documented"],
            ),
        ]

    def _run_execution_tools(
        self, repo_str: str, artifacts: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        """Run applicable execution tools and return list of result dicts."""
        results: List[Dict[str, Any]] = []
        has_tf = any(k.endswith(".tf") for k in artifacts)
        has_cdk = "cdk.json" in artifacts
        has_compose = any(
            k in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
            for k in artifacts
        )
        has_chart = any(k.endswith("Chart.yaml") or k == "Chart.yaml" for k in artifacts)

        if has_tf:
            for cmd in ("init", "validate", "plan"):
                r = self.terraform_exec_tool.run(TerraformExecutionInput(
                    repo_path=repo_str, command=cmd,
                ))
                results.append({
                    "tool": "terraform", "command": cmd,
                    "success": r.success, "checks": r.checks,
                    "findings": r.findings, "failure_class": r.failure_class,
                })
                if not r.success:
                    break

        if has_cdk:
            r = self.cdk_exec_tool.run(CDKExecutionInput(repo_path=repo_str, command="synth"))
            results.append({
                "tool": "cdk", "command": "synth",
                "success": r.success, "checks": r.checks,
                "findings": r.findings, "failure_class": r.failure_class,
            })

        if has_compose:
            r = self.compose_exec_tool.run(DockerComposeExecutionInput(
                repo_path=repo_str, command="config",
            ))
            results.append({
                "tool": "compose", "command": "config",
                "success": r.success, "checks": r.checks,
                "findings": r.findings, "failure_class": r.failure_class,
            })

        if has_chart:
            r = self.helm_exec_tool.run(HelmExecutionInput(repo_path=repo_str, command="lint"))
            results.append({
                "tool": "helm", "command": "lint",
                "success": r.success, "checks": r.checks,
                "findings": r.findings, "failure_class": r.failure_class,
            })

        return results

    @staticmethod
    def _enforce_env_policy(task_spec: DevOpsTaskSpec) -> Optional[str]:
        """Return a blocking reason if environment policy is violated, else None."""
        for env in task_spec.platform_scope.environments:
            policy = ENV_POLICY.get(env)
            if policy is None:
                continue
            if policy["approval_required"] and not any(
                "approval" in item.lower() for item in task_spec.scope.included
            ):
                return f"Environment '{env}' requires explicit approval gate but none found in scope"
            if policy["rollback_test_required"] and not task_spec.rollback_requirements:
                return f"Environment '{env}' requires rollback requirements but none specified"
        return None

    def _run_pipeline(
        self,
        *,
        repo_path: Path,
        task_spec: DevOpsTaskSpec,
        build_verifier: Optional[Any],
        write_changes: bool,
        subdir: str = "",
    ) -> DevOpsTeamResult:
        logger.info("DevOps team pipeline: starting task %s", task_spec.task_id)

        # Phase 1: intake + clarification
        env_block = self._enforce_env_policy(task_spec)
        if env_block:
            return DevOpsTeamResult(success=False, failure_reason=f"Environment policy violation: {env_block}")

        clarifier = self.task_clarifier.run(DevOpsTaskClarifierInput(task_spec=task_spec))
        if not clarifier.approved_for_execution:
            return DevOpsTeamResult(
                success=False,
                failure_reason="Clarification required: " + "; ".join(clarifier.clarification_requests[:3]),
            )

        subtask_contracts = self._build_subtask_contracts(task_spec)
        logger.info("DevOps team pipeline: %d subtask contracts generated", len(subtask_contracts))

        # Phase 2: change design / implementation
        logger.info("DevOps team pipeline: phase 2 - change design")
        repo_summary = self.repo_navigator_tool.run(RepoNavigatorInput(repo_path=str(repo_path))).summary
        iac_result = self.iac_agent.run(IaCAgentInput(task_spec=task_spec, repo_summary=repo_summary))
        cicd_result = self.cicd_agent.run(CICDPipelineAgentInput(task_spec=task_spec))
        deploy_result = self.deployment_agent.run(DeploymentStrategyAgentInput(task_spec=task_spec))

        aggregated_artifacts: Dict[str, str] = {}
        aggregated_artifacts.update(iac_result.artifacts)
        aggregated_artifacts.update(cicd_result.artifacts)
        aggregated_artifacts.update(deploy_result.artifacts)

        if write_changes and aggregated_artifacts:
            ok, msg = write_agent_output(
                repo_path=repo_path,
                output={"files": aggregated_artifacts, "commit_message": f"feat(devops): implement task [{task_spec.task_id}]"},
                subdir=subdir,
            )
            if not ok and msg != NO_FILES_TO_WRITE_MSG:
                return DevOpsTeamResult(success=False, failure_reason=msg)

        # Phase 3: write changes (branch + implementation)
        logger.info("DevOps team pipeline: phase 3 - branch + implementation (%d artifact files)", len(aggregated_artifacts))

        # Phase 4: tool validation + independent reviews
        logger.info("DevOps team pipeline: phase 4 - validation and review")
        iac_checks = self.iac_validation_tool.run(IaCValidationInput(repo_path=str(repo_path)))
        policy_checks = self.policy_tool.run(PolicyAsCodeInput(repo_path=str(repo_path)))
        cicd_checks = self.cicd_lint_tool.run(CICDLintInput(repo_path=str(repo_path)))
        dry_run_checks = self.deploy_dry_run_tool.run(DeploymentDryRunInput(repo_path=str(repo_path)))

        tool_gate_map: Dict[str, str] = {}
        tool_gate_map.update(iac_checks.checks)
        tool_gate_map.update(policy_checks.checks)
        tool_gate_map.update(cicd_checks.checks)
        tool_gate_map.update(dry_run_checks.checks)

        # Phase 4.5: Execution verification
        logger.info("DevOps team pipeline: phase 4.5 - execution verification")
        repo_str = str(repo_path)
        exec_results = self._run_execution_tools(repo_str, aggregated_artifacts)
        exec_gate_map: Dict[str, str] = {}
        exec_findings: List[str] = []
        for er in exec_results:
            exec_gate_map.update(er.get("checks", {}))
            exec_findings.extend(er.get("findings", []))
            fc = er.get("failure_class", "")
            if fc:
                logger.info(
                    "DevOps execution [%s %s]: failure_class=%s",
                    er.get("tool", "?"), er.get("command", "?"), fc,
                )

        # Phase 4.6: Debug-patch loop for fixable execution failures
        MAX_INFRA_FIX_ITERATIONS = 3
        exec_failures = [er for er in exec_results if not er.get("success", True)]
        for fix_iter in range(MAX_INFRA_FIX_ITERATIONS):
            if not exec_failures:
                break
            logger.info(
                "DevOps team pipeline: phase 4.6 - debug-patch iteration %d/%d (%d failures)",
                fix_iter + 1, MAX_INFRA_FIX_ITERATIONS, len(exec_failures),
            )
            combined_output = "\n---\n".join(
                "\n".join(ef.get("findings", [])) for ef in exec_failures
            )
            first_tool = exec_failures[0].get("tool", "unknown")
            first_cmd = exec_failures[0].get("command", "unknown")
            try:
                debug_out = self.infra_debug_agent.run(IaCDebugInput(
                    execution_output=combined_output[:4000],
                    tool_name=first_tool,
                    command=first_cmd,
                    artifacts=aggregated_artifacts,
                ))
            except Exception as dbg_err:
                logger.warning("DevOps debug agent failed: %s", dbg_err)
                break
            if not debug_out.fixable:
                logger.info("DevOps debug agent: errors are not fixable via code changes")
                break
            try:
                patch_out = self.infra_patch_agent.run(IaCPatchInput(
                    debug_output=debug_out,
                    original_artifacts=aggregated_artifacts,
                    repo_path=repo_str,
                ))
            except Exception as patch_err:
                logger.warning("DevOps patch agent failed: %s", patch_err)
                break
            if not patch_out.patched_artifacts:
                logger.info("DevOps patch agent returned no patches")
                break
            aggregated_artifacts.update(patch_out.patched_artifacts)
            if write_changes:
                write_agent_output(
                    repo_path=repo_path,
                    output={"files": patch_out.patched_artifacts, "commit_message": f"fix(devops): patch iteration {fix_iter + 1}"},
                    subdir=subdir,
                )
            exec_results = self._run_execution_tools(repo_str, aggregated_artifacts)
            exec_failures = [er for er in exec_results if not er.get("success", True)]
            exec_gate_map = {}
            exec_findings = []
            for er in exec_results:
                exec_gate_map.update(er.get("checks", {}))
                exec_findings.extend(er.get("findings", []))

        tool_gate_map.update(exec_gate_map)

        devsec = self.devsecops_review_agent.run(
            DevSecOpsReviewInput(
                task_description=task_spec.title,
                requirements=task_spec.goal.summary,
                artifacts=aggregated_artifacts,
            )
        )
        change_review = self.change_review_agent.run(
            ChangeReviewInput(task_description=task_spec.title, artifacts=aggregated_artifacts)
        )

        val = self.test_validation_agent.run(
            DevOpsTestValidationInput(
                acceptance_criteria=task_spec.acceptance_criteria,
                tool_results={
                    "iac": iac_checks.checks,
                    "policy": policy_checks.checks,
                    "cicd": cicd_checks.checks,
                    "deploy_dry_run": dry_run_checks.checks,
                },
            )
        )

        quality_gates = dict(val.quality_gates)
        quality_gates.setdefault("security_review", "pass" if devsec.approved else "fail")
        quality_gates.setdefault("change_review", "pass" if change_review.approved else "fail")

        if any(v == "fail" for v in quality_gates.values()):
            return DevOpsTeamResult(
                success=False,
                failure_reason="Quality gates failed",
                completion_package=DevOpsCompletionPackage(
                    task_id=task_spec.task_id,
                    status="blocked",
                    files_changed=sorted(aggregated_artifacts.keys()),
                    quality_gates=quality_gates,
                    notes=[devsec.summary, change_review.summary, val.summary],
                    risks_remaining=[f.issue for f in devsec.findings if f.blocking],
                ),
            )

        if build_verifier is not None:
            verify_ok, verify_err = build_verifier(repo_path, "devops", task_spec.task_id)
            if not verify_ok:
                return DevOpsTeamResult(success=False, failure_reason=verify_err or "Build verification failed")

        # Phase 5: commit, merge, release readiness
        logger.info("DevOps team pipeline: phase 5 - completion package assembly")
        doc = self.doc_runbook_agent.run(
            DocumentationRunbookInput(
                task_id=task_spec.task_id,
                task_title=task_spec.title,
                artifacts=aggregated_artifacts,
                quality_gates=quality_gates,
                notes=[iac_result.summary, cicd_result.summary, deploy_result.summary],
            )
        )

        completion = doc.completion_package
        completion.acceptance_criteria_trace = [
            CriterionTrace(criterion=c, implementation_refs=sorted(aggregated_artifacts.keys()), tests=[{"validation": "pass"}])
            for c in task_spec.acceptance_criteria
        ]
        completion.release_readiness = ReleaseReadiness(
            deployment_strategy=deploy_result.strategy or task_spec.constraints.deployment.strategy or "rolling",
            rollback_available=bool(deploy_result.rollback_plan),
            alerting_configured=True,
            required_approvals=["manual_prod_approval"] if "production" in task_spec.platform_scope.environments else [],
            runtime_verification_checklist=["deployment_rollout_status", "service_health", "alert_health"],
        )
        completion.git_operations = GitOperationsMetadata(
            branch_created=f"feature/{task_spec.task_id.lower()}",
            commits=[GitCommitMetadata(hash="", message=f"feat(devops): implement task [{task_spec.task_id}]")],
            merge=GitMergeMetadata(target_branch="development", strategy="squash", merge_commit_hash="", status="pending"),
        )
        completion.handoff = HandoffInfo(
            prod_approval_required="production" in task_spec.platform_scope.environments,
            runbook_updated=bool(doc.files),
        )
        completion.status = "completed"
        completion.quality_gates = quality_gates

        return DevOpsTeamResult(success=True, iterations=1, completion_package=completion)

