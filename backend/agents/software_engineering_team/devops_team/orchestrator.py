"""DevOps team orchestrator (DevOpsTeamLeadAgent)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm_service import DummyLLMClient, LLMClient
from software_engineering_team.shared.branch_utils import make_branch_suffix
from software_engineering_team.shared.deliver_utils import DeliverGitOps, deliver_inline_merge
from software_engineering_team.shared.git_utils import (
    DEVELOPMENT_BRANCH,
    abort_merge,
    checkout_branch,
    commit_working_tree,
    create_feature_branch,
    delete_branch,
    ensure_development_branch,
    get_head_sha,
    merge_branch,
)
from software_engineering_team.shared.repo_writer import NO_FILES_TO_WRITE_MSG, write_agent_output
from software_engineering_team.shared.security_service import infra_gate_passed, run_policy_scan
from software_engineering_team.shared.team_lead_base import (
    BaseTeamLead,
    TeamLeadSharedState,
    build_team_failure_result,
)

from .change_review_agent import ChangeReviewAgent, ChangeReviewInput
from .cicd_pipeline_agent import CICDPipelineAgent
from .deployment_strategy_agent import DeploymentStrategyAgent
from .devsecops_review_agent import DevSecOpsReviewAgent, DevSecOpsReviewInput
from .doc_runbook_agent import DocumentationRunbookAgent, DocumentationRunbookInput
from .iac_agent import InfrastructureAsCodeAgent
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
from .phase2_graph import run_phase2_parallel

# Commit-message template for the shared deliver helper. ``deliver_inline_merge``
# calls ``template.format(scope=..., summary=...)``; only ``{summary}`` is used
# here (``str.format`` ignores the unreferenced ``scope`` kwarg).
DEVOPS_DELIVER_COMMIT_MSG_TEMPLATE = "feat(devops): {summary}"

# Static defaults for the legacy DevOpsTaskSpec adapter (_build_legacy_spec).
# Keep list values read-only — do not mutate them in the adapter.
_DEFAULT_LEGACY_CLOUD = "on-premises"
_DEFAULT_LEGACY_APP_REPO = "application"
_DEFAULT_LEGACY_INFRA_REPO = "platform-infra"
_DEFAULT_LEGACY_SECRETS_SOURCE = "managed_secret_store"

_DEFAULT_LEGACY_ACCEPTANCE_CRITERIA = [
    "CI/CD workflow exists and validates",
    "Deployment strategy and rollback documented",
    "Security and policy review executed",
]
_DEFAULT_LEGACY_ROLLBACK_REQUIREMENTS = [
    "Rollback to previous known good release",
]
_DEFAULT_LEGACY_SECURITY_CONSTRAINTS = [
    "No plaintext credentials",
    "Least privilege IAM",
]
_DEFAULT_LEGACY_COMPLIANCE_CONSTRAINTS = [
    "Audit trail required",
]


def _git_ops() -> DeliverGitOps:
    """Bundle this module's git callables for the shared deliver helper.

    Postconditions:
        - Returns a ``DeliverGitOps`` whose callables are the names bound in this
          module, so tests can monkeypatch the ``devops_team.orchestrator``
          boundary (e.g. ``merge_branch``) exactly as the v2 teams do.
    """
    return DeliverGitOps(
        abort_merge=abort_merge,
        checkout_branch=checkout_branch,
        commit_working_tree=commit_working_tree,
        create_feature_branch=create_feature_branch,
        delete_branch=delete_branch,
        merge_branch=merge_branch,
        write_agent_output=write_agent_output,
    )


def _criterion_traces_from_phase4(
    criteria: List[str],
    acceptance_trace: List[Dict[str, object]],
    artifact_keys: List[str],
) -> List[CriterionTrace]:
    """Map acceptance criteria onto Phase 4 validation evidence.

    Preconditions:
        - ``criteria`` is an iterable of criterion strings (may be empty).
        - ``acceptance_trace`` is an iterable of dict-like Phase 4 entries
          (may be empty); non-dict entries are ignored.
        - ``artifact_keys`` is an iterable of artifact path strings used as
          fallback ``implementation_refs`` when no Phase 4 match exists.

    Postconditions:
        - Returns one ``CriterionTrace`` per entry in ``criteria``, in order.
        - A Phase 4 match (first entry whose ``criterion`` string-equals the
          criterion) supplies coerced ``implementation_refs`` and ``tests``.
        - Unmatched criteria get ``implementation_refs=sorted(artifact_keys)``
          and ``tests=[]``.
        - Never invents a fabricated ``{"validation": "pass"}`` entry.
    """
    by_criterion: Dict[str, Dict[str, object]] = {}
    for entry in acceptance_trace:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("criterion", ""))
        if key and key not in by_criterion:
            by_criterion[key] = entry

    fallback_refs = sorted(artifact_keys)
    traces: List[CriterionTrace] = []
    for criterion in criteria:
        match = by_criterion.get(criterion)
        if match is None:
            traces.append(
                CriterionTrace(
                    criterion=criterion,
                    implementation_refs=list(fallback_refs),
                    tests=[],
                )
            )
            continue

        raw_refs = match.get("implementation_refs", [])
        refs = [str(r) for r in raw_refs] if isinstance(raw_refs, list) else []

        raw_tests = match.get("tests", [])
        tests: List[Dict[str, str]] = []
        if isinstance(raw_tests, list):
            for item in raw_tests:
                if isinstance(item, dict):
                    tests.append({str(k): str(v) for k, v in item.items()})

        traces.append(
            CriterionTrace(
                criterion=criterion,
                implementation_refs=refs,
                tests=tests,
            )
        )
    return traces


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
    PolicyAsCodeToolAgent,
    RepoNavigatorInput,
    RepoNavigatorToolAgent,
    TerraformExecutionInput,
    TerraformExecutionToolAgent,
)

logger = logging.getLogger(__name__)

# Bounded Phase 4.6 debug → patch → re-exec iterations for fixable infra failures.
MAX_INFRA_FIX_ITERATIONS = 3


@dataclass
class _DebugPatchState:
    """Mutable bag for one Phase 4.6 debug-patch retry session.

    Invariants: ``exec_failures`` is derived from ``exec_results`` (entries where
    ``success`` is falsy). ``exec_gate_map`` / ``exec_findings`` always mirror
    ``exec_results`` — established in ``__post_init__`` and refreshed via
    :meth:`refresh_aggregates` after each re-exec.
    """

    exec_results: List[Dict[str, Any]]
    exec_gate_map: Dict[str, str] = field(init=False, default_factory=dict)
    exec_findings: List[str] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.refresh_aggregates()

    @property
    def exec_failures(self) -> List[Dict[str, Any]]:
        """Failing execution-tool results derived from ``exec_results``."""
        return [er for er in self.exec_results if not er.get("success", True)]

    def refresh_aggregates(self) -> None:
        """Rebuild ``exec_gate_map`` / ``exec_findings`` from ``exec_results``.

        Preconditions: ``exec_results`` is the latest execution-tool output list.
        Postconditions: ``exec_gate_map`` and ``exec_findings`` mirror that list.
        """
        self.exec_gate_map = {}
        self.exec_findings = []
        for er in self.exec_results:
            self.exec_gate_map.update(er.get("checks", {}))
            self.exec_findings.extend(er.get("findings", []))


class DevOpsTeamLeadAgent(TeamLeadSharedState):
    """Coordinates specialized DevOps agents with hard gates.

    Inherits ``TeamLeadSharedState`` for the optional per-run status hook
    (``_report_status`` / ``_status_callback``). Pipeline phase status always
    emits INFO logs via :meth:`_log_pipeline_status`; the optional callback is
    a separate forward channel and may be set/cleared per run without losing
    historical log output.

    This class intentionally does **not** subclass ``BaseTeamLead``: that type's
    constructor and shared-state wiring differ from DevOps's
    ``TeamLeadSharedState`` setup. Instead, pure phase/retry helpers are aliased
    from ``BaseTeamLead`` as unbound methods on this class (see
    ``_run_gated_phases`` / ``_run_bounded_retry_loop`` below) so call sites can
    use ``self._run_*`` without inheriting ``BaseTeamLead`` instance state.

    Invariants: ``self.llm`` is the client passed to ``__init__``; specialist
    agents and tools are constructed once; ``_status_callback`` defaults to
    None (mixin default) and is independent of fallback logging.
    """

    # Unbound-method reuse (not inheritance): assign BaseTeamLead helpers onto
    # this class so ``self._run_gated_phases`` / ``self._run_bounded_retry_loop``
    # work without subclassing BaseTeamLead. The helpers only need ``self`` for
    # the Python method call signature — they do not read BaseTeamLead fields.
    # ``_run_phase_gates`` (called via BaseTeamLead._run_phase_gates) delegates
    # to ``self._run_gated_phases``, so that alias must exist on this class.
    _run_gated_phases = BaseTeamLead._run_gated_phases
    _run_bounded_retry_loop = BaseTeamLead._run_bounded_retry_loop

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        TeamLeadSharedState.__init__(
            self,
            llm_getter=lambda _agent_id: llm_client,
            shared_config={},
        )
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
    def _log_pipeline_status(
        *,
        phase: str,
        detail: str = "",
        progress: Optional[float] = None,
        **extra: Any,
    ) -> None:
        """Emit the historical pipeline status line at INFO.

        Preconditions: ``phase`` is a non-empty str (caller's responsibility;
          :meth:`_report_status` asserts this before delegating; direct callers
          of this staticmethod must satisfy it too — enforced below).
        Postconditions: logs ``detail`` when non-empty, otherwise logs
          ``DevOps team pipeline: {phase}``; ``progress`` and ``extra`` are ignored
          (reserved for external consumers). Never raises when preconditions hold.
        """
        assert isinstance(phase, str) and phase, "phase must be a non-empty str"
        if detail:
            logger.info("%s", detail)
        else:
            logger.info("DevOps team pipeline: %s", phase)

    def _report_status(
        self,
        phase: str,
        detail: str = "",
        progress: Optional[float] = None,
        **extra: Any,
    ) -> None:
        """Log phase status, then forward to the optional ``_status_callback``.

        Fallback INFO logging is independent of ``_status_callback`` so clearing
        the callback after an instrumented run (the shared per-run contract) does
        not silence later pipeline status on a reused lead.

        Preconditions: ``phase`` is a non-empty str.
        Postconditions: emits the historical INFO line via ``_log_pipeline_status``;
          then invokes ``TeamLeadSharedState._report_status`` (no-op when callback
          is None; forwards kwargs when set; swallows callback errors). Never
          raises when preconditions hold.
        """
        assert isinstance(phase, str) and phase, "phase must be a non-empty str"
        self._log_pipeline_status(phase=phase, detail=detail, progress=progress, **extra)
        TeamLeadSharedState._report_status(self, phase, detail=detail, progress=progress, **extra)

    @staticmethod
    def _build_legacy_spec(
        *,
        task_id: str,
        task_description: str,
        requirements: str,
        target_repo: Optional[Any] = None,
    ) -> DevOpsTaskSpec:
        repo_name = (
            target_repo.value
            if hasattr(target_repo, "value")
            else (str(target_repo) if target_repo else "")
        )
        combined_text = f"{task_description} {requirements}".lower()
        # Match explicit production intent; avoid false positives like "produce".
        env = "production" if re.search(r"\b(prod|production)\b", combined_text) else "staging"
        return DevOpsTaskSpec(
            task_id=task_id,
            title=task_description[:120] or task_id,
            platform_scope={"cloud": _DEFAULT_LEGACY_CLOUD, "environments": ["dev", env]},
            repo_context={
                "app_repo": repo_name or _DEFAULT_LEGACY_APP_REPO,
                "infra_repo": _DEFAULT_LEGACY_INFRA_REPO,
                "pipeline_repo": repo_name or _DEFAULT_LEGACY_APP_REPO,
            },
            goal={"summary": task_description},
            scope={"included": [requirements], "excluded": []},
            constraints={"secrets": {"source": _DEFAULT_LEGACY_SECRETS_SOURCE}},
            acceptance_criteria=_DEFAULT_LEGACY_ACCEPTANCE_CRITERIA,
            rollback_requirements=_DEFAULT_LEGACY_ROLLBACK_REQUIREMENTS,
            security_constraints=_DEFAULT_LEGACY_SECURITY_CONSTRAINTS,
            compliance_constraints=_DEFAULT_LEGACY_COMPLIANCE_CONSTRAINTS,
            environment=env,
        )

    def run(self, input_data: DevOpsTaskSpec) -> DevOpsCompletionPackage:
        """Execute a contract-first model run without orchestrator artifact writes.

        ``write_changes=False`` skips this team's ``write_agent_output`` / branch
        commits. Phase 4.5 execution tools (e.g. ``terraform init``, ``cdk synth``,
        ``helm lint``, ``docker-compose config``) may still write under the working
        directory as validation side effects.
        """
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
        _ = (
            architecture,
            existing_pipeline,
            tech_stack,
            max_iterations,
            devops_review_agent,
        )  # reserved for future routing
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
                r = self.terraform_exec_tool.run(
                    TerraformExecutionInput(
                        repo_path=repo_str,
                        command=cmd,
                    )
                )
                results.append(
                    {
                        "tool": "terraform",
                        "command": cmd,
                        "success": r.success,
                        "checks": r.checks,
                        "findings": r.findings,
                        "failure_class": r.failure_class,
                    }
                )
                if not r.success:
                    break

        if has_cdk:
            r = self.cdk_exec_tool.run(CDKExecutionInput(repo_path=repo_str, command="synth"))
            results.append(
                {
                    "tool": "cdk",
                    "command": "synth",
                    "success": r.success,
                    "checks": r.checks,
                    "findings": r.findings,
                    "failure_class": r.failure_class,
                }
            )

        if has_compose:
            r = self.compose_exec_tool.run(
                DockerComposeExecutionInput(
                    repo_path=repo_str,
                    command="config",
                )
            )
            results.append(
                {
                    "tool": "compose",
                    "command": "config",
                    "success": r.success,
                    "checks": r.checks,
                    "findings": r.findings,
                    "failure_class": r.failure_class,
                }
            )

        if has_chart:
            r = self.helm_exec_tool.run(HelmExecutionInput(repo_path=repo_str, command="lint"))
            results.append(
                {
                    "tool": "helm",
                    "command": "lint",
                    "success": r.success,
                    "checks": r.checks,
                    "findings": r.findings,
                    "failure_class": r.failure_class,
                }
            )

        return results

    def _debug_patch_once(
        self,
        fix_iter: int,
        *,
        state: _DebugPatchState,
        aggregated_artifacts: Dict[str, str],
        repo_path: Path,
        repo_str: str,
        write_changes: bool,
        subdir: str,
        max_iterations: int,
    ) -> Optional[_DebugPatchState]:
        """Run one infra debug → patch → re-exec iteration.

        Parameters:
          fix_iter: 0-based iteration index (status logging).
          state: mutable debug-patch state bag; ``exec_failures`` drives the
            iteration.
          aggregated_artifacts: mutable artifact path → content map; updated
            in place when a patch is applied.
          repo_path: repository path on disk (for optional writes).
          repo_str: string form of ``repo_path`` passed to tool agents.
          write_changes: when True, persist patched files via
            ``write_agent_output`` before re-exec.
          subdir: subdirectory scope for ``write_agent_output``.
          max_iterations: bound shown in status logs (enforced by
            ``_run_bounded_retry_loop``, not re-asserted here).

        Preconditions:
          - ``fix_iter`` is a 0-based index from the bounded-retry helper
          - ``state.exec_failures`` is expected to be non-empty when invoked by
            the helper; if empty, returns ``state`` unchanged
        Postconditions:
          - Empty ``state.exec_failures`` → return ``state`` unchanged
          - Soft abort (debug/patch exception, not fixable, or empty patches)
            → log and return ``None`` (retry helper stops; no further attempts)
          - Failed patch write → log a warning, still re-exec against the
            in-memory (and possibly on-disk) patch, then return ``state`` so
            validation is not skipped after a persistence failure
          - Successful debug/patch/re-exec that resolves all execution failures
            → ``state.exec_failures`` is cleared and ``state`` is returned
          - Partial success (some failures remain after re-exec) → return
            ``state`` with updated ``exec_failures`` so the retry helper can
            continue to the next iteration
        """
        if not state.exec_failures:
            return state

        self._report_status(
            "phase4.6",
            detail=(
                "DevOps team pipeline: phase 4.6 - debug-patch iteration "
                f"{fix_iter + 1}/{max_iterations} ({len(state.exec_failures)} failures)"
            ),
        )
        combined_output = "\n---\n".join(
            "\n".join(ef.get("findings", [])) for ef in state.exec_failures
        )
        first_tool = state.exec_failures[0].get("tool", "unknown")
        first_cmd = state.exec_failures[0].get("command", "unknown")
        try:
            debug_out = self.infra_debug_agent.run(
                IaCDebugInput(
                    execution_output=combined_output,
                    tool_name=first_tool,
                    command=first_cmd,
                    artifacts=aggregated_artifacts,
                )
            )
        except Exception as dbg_err:
            logger.warning("DevOps debug agent failed: %s", dbg_err)
            return None
        if not debug_out.fixable:
            logger.info("DevOps debug agent: errors are not fixable via code changes")
            return None
        try:
            patch_out = self.infra_patch_agent.run(
                IaCPatchInput(
                    debug_output=debug_out,
                    original_artifacts=aggregated_artifacts,
                    repo_path=repo_str,
                )
            )
        except Exception as patch_err:
            logger.warning("DevOps patch agent failed: %s", patch_err)
            return None
        if not patch_out.patched_artifacts:
            logger.info("DevOps patch agent returned no patches")
            return None
        aggregated_artifacts.update(patch_out.patched_artifacts)
        if write_changes:
            ok, msg = write_agent_output(
                repo_path=repo_path,
                output={
                    "files": patch_out.patched_artifacts,
                    "commit_message": f"fix(devops): patch iteration {fix_iter + 1}",
                },
                subdir=subdir,
            )
            # Persistence failure must not skip re-exec: patched content is
            # already in ``aggregated_artifacts`` and may already be on disk
            # (e.g. commit-hook reject after write). Soft-aborting here would
            # let Phase 5 commit unvalidated patches.
            if not ok and msg != NO_FILES_TO_WRITE_MSG:
                logger.warning(
                    "DevOps patch write failed (%s); continuing with re-exec validation",
                    msg,
                )
        state.exec_results = self._run_execution_tools(repo_str, aggregated_artifacts)
        state.refresh_aggregates()
        return state

    @staticmethod
    def _enforce_env_policy(task_spec: DevOpsTaskSpec) -> Optional[str]:
        """Return a blocking reason if environment policy is violated, else None.

        Preconditions:
            - ``task_spec`` is a fully populated ``DevOpsTaskSpec``.
            - ``task_spec.platform_scope.environments`` is iterable.
            - ``task_spec.scope.included`` is an iterable of strings.

        Postconditions:
            - Returns ``None`` if no configured environment policy is violated.
            - Returns a human-readable blocking reason string if any environment
              requires an approval gate or rollback requirements that are missing.

        Invariants:
            - The method does not mutate ``task_spec``.
        """
        for env in task_spec.platform_scope.environments:
            policy = ENV_POLICY.get(env)
            if policy is None:
                continue
            if policy["approval_required"] and not any(
                "approval" in item.lower() for item in task_spec.scope.included
            ):
                return (
                    f"Environment '{env}' requires explicit approval gate but none found in scope"
                )
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
        self._report_status(
            "start",
            detail=f"DevOps team pipeline: starting task {task_spec.task_id}",
        )

        # Phase outputs shared with Phase 4+ (set by the gated phase callables).
        iac_result: Any = None
        cicd_result: Any = None
        deploy_result: Any = None
        aggregated_artifacts: Dict[str, str] = {}
        quality_gates: Dict[str, str] = {}
        acceptance_trace: List[Dict[str, object]] = []
        completion: Any = None  # filled by Phase 5 on success

        def _phase1_intake_clarify() -> Optional[DevOpsTeamResult]:
            """Phase 1: environment policy + task clarification gates.

            Preconditions: ``task_spec`` is the pipeline input for this run.
            Postconditions: returns a failed ``DevOpsTeamResult`` on env-policy or
              clarifier rejection; otherwise builds subtask contracts, logs their
              count, and returns ``None`` so later phases run.
            """
            env_block = self._enforce_env_policy(task_spec)
            if env_block:
                return DevOpsTeamResult(
                    success=False, failure_reason=f"Environment policy violation: {env_block}"
                )

            clarifier = self.task_clarifier.run(DevOpsTaskClarifierInput(task_spec=task_spec))
            if not clarifier.approved_for_execution:
                return DevOpsTeamResult(
                    success=False,
                    failure_reason="Clarification required: "
                    + "; ".join(clarifier.clarification_requests[:3]),
                )

            subtask_contracts = self._build_subtask_contracts(task_spec)
            logger.info(
                "DevOps team pipeline: %d subtask contracts generated", len(subtask_contracts)
            )
            return None

        def _phase2_parallel_design() -> Optional[DevOpsTeamResult]:
            """Phase 2: change design / implementation (3-way parallel fan-out).

            Preconditions: Phase 1 returned ``None``.
            Postconditions: sets ``iac_result``, ``cicd_result``, ``deploy_result``,
              and ``aggregated_artifacts`` from the parallel fan-out; always returns
              ``None`` (this phase has no early-exit gate today).
            """
            nonlocal iac_result, cicd_result, deploy_result, aggregated_artifacts
            self._report_status(
                "phase2",
                detail="DevOps team pipeline: phase 2 - change design (parallel)",
            )
            repo_summary = self.repo_navigator_tool.run(
                RepoNavigatorInput(repo_path=str(repo_path))
            ).summary
            # Enable parallel execution unless the backing LLM client is a
            # DummyLLMClient (or subclass) — scripted test clients use a shared
            # sequential response list that breaks under concurrent access.
            use_parallel = not isinstance(self.llm, DummyLLMClient)
            phase2 = run_phase2_parallel(
                self.iac_agent,
                self.cicd_agent,
                self.deployment_agent,
                task_spec,
                repo_summary=repo_summary,
                parallel=use_parallel,
            )
            iac_result = phase2["iac_result"]
            cicd_result = phase2["cicd_result"]
            deploy_result = phase2["deploy_result"]
            aggregated_artifacts = phase2["aggregated_artifacts"]
            return None

        def _phase3_branch_write() -> Optional[DevOpsTeamResult]:
            """Phase 3: feature branch + artifact write gates.

            Preconditions: Phase 2 returned ``None`` (artifacts may be empty).
            Postconditions: when ``write_changes`` and artifacts are present, prepares
              the development branch, cuts a feature branch, and writes artifacts —
              returning a failed ``DevOpsTeamResult`` on any of those gates; otherwise
              reports phase-3 status and returns ``None``.
            """
            if write_changes and aggregated_artifacts:
                # Cut a feature branch from development up front (mirroring the
                # code-v2 teams) so every intermediate write/patch commit lands
                # on the branch and development stays clean until the reviewed
                # Phase 5 merge. Without this, writes would commit straight to
                # the checked-out development branch and the later merge would
                # be an empty no-op.
                dev_ok, dev_msg = ensure_development_branch(repo_path)
                if not dev_ok:
                    return DevOpsTeamResult(
                        success=False,
                        failure_reason=(f"Cannot prepare {DEVELOPMENT_BRANCH} branch: {dev_msg}"),
                    )
                branch_ok, branch_msg = create_feature_branch(
                    repo_path,
                    DEVELOPMENT_BRANCH,
                    make_branch_suffix(task_spec.task_id, task_spec.title),
                )
                if not branch_ok:
                    return DevOpsTeamResult(
                        success=False,
                        failure_reason=f"Cannot create feature branch: {branch_msg}",
                    )
                ok, msg = write_agent_output(
                    repo_path=repo_path,
                    output={
                        "files": aggregated_artifacts,
                        "commit_message": (f"feat(devops): implement task [{task_spec.task_id}]"),
                    },
                    subdir=subdir,
                )
                if not ok and msg != NO_FILES_TO_WRITE_MSG:
                    return DevOpsTeamResult(success=False, failure_reason=msg)

            self._report_status(
                "phase3",
                detail=(
                    "DevOps team pipeline: phase 3 - branch + implementation "
                    f"({len(aggregated_artifacts)} artifact files)"
                ),
            )
            return None

        def _phase4_validation_review() -> Optional[DevOpsTeamResult]:
            """Phase 4: tool validation, reviews, and early-exit gates.

            Preconditions: Phases 1–3 returned ``None`` (``aggregated_artifacts``
              may be empty).
            Postconditions: runs tool validation, execution verification, the
              debug-patch loop, and independent reviews; sets nonlocal
              ``quality_gates``. Returns a failed ``DevOpsTeamResult`` on quality-
              gate or build-verifier failure via ``_run_phase_gates``; otherwise
              returns ``None`` so Phase 5 runs.
            """
            nonlocal aggregated_artifacts, quality_gates, acceptance_trace

            # Phase 4: tool validation + independent reviews
            self._report_status(
                "phase4",
                detail="DevOps team pipeline: phase 4 - validation and review",
            )
            iac_checks = self.iac_validation_tool.run(IaCValidationInput(repo_path=str(repo_path)))
            policy_checks = run_policy_scan(str(repo_path), runner=self.policy_tool)
            cicd_checks = self.cicd_lint_tool.run(CICDLintInput(repo_path=str(repo_path)))
            dry_run_checks = self.deploy_dry_run_tool.run(
                DeploymentDryRunInput(repo_path=str(repo_path))
            )

            tool_gate_map: Dict[str, str] = {}
            tool_gate_map.update(iac_checks.checks)
            tool_gate_map.update(policy_checks.checks)
            tool_gate_map.update(cicd_checks.checks)
            tool_gate_map.update(dry_run_checks.checks)

            # Phase 4.5: Execution verification
            self._report_status(
                "phase4.5",
                detail="DevOps team pipeline: phase 4.5 - execution verification",
            )
            repo_str = str(repo_path)
            exec_results = self._run_execution_tools(repo_str, aggregated_artifacts)
            for er in exec_results:
                fc = er.get("failure_class", "")
                if fc:
                    logger.info(
                        "DevOps execution [%s %s]: failure_class=%s",
                        er.get("tool", "?"),
                        er.get("command", "?"),
                        fc,
                    )

            # Phase 4.6: Debug-patch loop for fixable execution failures.
            # Mutation contract: ``attempt`` / ``is_success`` share ``state`` and
            # ``aggregated_artifacts`` by reference (same as the former inline
            # locals). After the loop, ``state.exec_gate_map`` (aggregated
            # execution-tool check statuses) is merged into local
            # ``tool_gate_map``; remaining ``state.exec_failures`` do not
            # early-return a failed ``DevOpsTeamResult`` (pre-refactor behavior
            # preserved).
            state = _DebugPatchState(exec_results=exec_results)
            if state.exec_failures:
                self._run_bounded_retry_loop(
                    max_iterations=MAX_INFRA_FIX_ITERATIONS,
                    attempt=lambda i: self._debug_patch_once(
                        i,
                        state=state,
                        aggregated_artifacts=aggregated_artifacts,
                        repo_path=repo_path,
                        repo_str=repo_str,
                        write_changes=write_changes,
                        subdir=subdir,
                        max_iterations=MAX_INFRA_FIX_ITERATIONS,
                    ),
                    is_success=lambda s: not s.exec_failures,
                )

            tool_gate_map.update(state.exec_gate_map)

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
            acceptance_trace = list(val.acceptance_trace)

            quality_gates = dict(val.quality_gates)
            # The infra security gate routes both the DevSecOps LLM review and the
            # policy-as-code (checkov) scan through the unified infra decision. This
            # is force-assigned (not setdefault) so the authoritative DevSecOps +
            # policy result always wins — a validation-agent-supplied "pass" must
            # never mask a failing review or checkov scan.
            quality_gates["security_review"] = (
                "pass" if infra_gate_passed(devsec.approved, policy_checks.success) else "fail"
            )
            quality_gates.setdefault("change_review", "pass" if change_review.approved else "fail")

            def _quality_gates_check() -> Optional[DevOpsTeamResult]:
                """Fail the phase when any assembled quality gate is ``fail``.

                Preconditions: ``quality_gates``, ``devsec``, ``change_review``,
                  ``val``, ``task_spec``, and ``aggregated_artifacts`` are set by
                  Phase 4 setup above.
                Postconditions: returns the blocked ``DevOpsTeamResult`` with the
                  existing completion-package shape when any gate fails; otherwise
                  ``None``.
                """
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
                return None

            def _build_verifier_check() -> Optional[DevOpsTeamResult]:
                """Fail the phase when an injected build verifier rejects the repo.

                Preconditions: quality gates already passed (prior gate returned
                  ``None``); ``build_verifier`` may be ``None``.
                Postconditions: when ``build_verifier`` is set and returns a
                  failing result, returns ``DevOpsTeamResult(success=False, …)``
                  with the verifier error (or the default failure string);
                  otherwise ``None`` (including when verifier is absent).
                """
                if build_verifier is not None:
                    verify_ok, verify_err = build_verifier(repo_path, "devops", task_spec.task_id)
                    if not verify_ok:
                        return DevOpsTeamResult(
                            success=False,
                            failure_reason=verify_err or "Build verification failed",
                        )
                return None

            # Consume BaseTeamLead's intra-phase multi-gate hook without inheriting
            # the code-v2 BaseTeamLead constructor (DevOps uses TeamLeadSharedState).
            return BaseTeamLead._run_phase_gates(
                self,
                [_quality_gates_check, _build_verifier_check],
            )

        def _phase5_completion_deliver() -> Optional[DevOpsTeamResult]:
            """Phase 5: completion package assembly + deliver/merge.

            Preconditions: Phases 1–4 returned ``None``; ``quality_gates``,
              ``acceptance_trace``, ``aggregated_artifacts``, and Phase 2 results
              are set (artifacts / trace may be empty).
            Postconditions: on merge failure returns a failed ``DevOpsTeamResult``
              via ``build_team_failure_result`` with the blocked completion
              package; otherwise assigns nonlocal ``completion`` (completed status,
              git ops, handoff, quality gates) and returns ``None`` so the thin
              success envelope after the sequencer runs.
            """
            nonlocal completion, acceptance_trace

            # Phase 5: commit, merge, release readiness
            self._report_status(
                "phase5",
                detail="DevOps team pipeline: phase 5 - completion package assembly",
            )
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
            completion.acceptance_criteria_trace = _criterion_traces_from_phase4(
                list(task_spec.acceptance_criteria),
                acceptance_trace,
                list(aggregated_artifacts.keys()),
            )
            completion.release_readiness = ReleaseReadiness(
                deployment_strategy=deploy_result.strategy
                or task_spec.constraints.deployment.strategy
                or "rolling",
                rollback_available=bool(deploy_result.rollback_plan),
                alerting_configured=bool(deploy_result.alerting_configured),
                required_approvals=["manual_prod_approval"]
                if "production" in task_spec.platform_scope.environments
                else [],
                runtime_verification_checklist=[
                    "deployment_rollout_status",
                    "service_health",
                    "alert_health",
                ],
            )
            # Deliver the artifacts for real via the shared inline-merge helper and
            # report the actual outcome (real branch, commit SHA, merge status) rather
            # than fabricated placeholders. A model-only run (write_changes=False) does
            # no git work, so the neutral default honestly reports "nothing delivered".
            git_ops = GitOperationsMetadata()
            if write_changes and aggregated_artifacts:
                deliver_result = deliver_inline_merge(
                    task_id=task_spec.task_id,
                    repo_path=repo_path,
                    deliver_files=aggregated_artifacts,
                    summary=f"implement task [{task_spec.task_id}]",
                    task_title=task_spec.title,
                    commit_msg_template=DEVOPS_DELIVER_COMMIT_MSG_TEMPLATE,
                    ops=_git_ops(),
                    logger=logger,
                )
                # deliver_inline_merge leaves development checked out at the merged
                # commit. merge_branch fast-forwards (development never advanced since
                # the branch was cut), so this single HEAD SHA is the honest identifier
                # for both the delivered commit and the merge result.
                head_ok, head_sha = get_head_sha(repo_path)
                sha = head_sha if head_ok else ""
                commit_msg = (
                    deliver_result.commit_messages[0]
                    if deliver_result.commit_messages
                    else f"feat(devops): implement task [{task_spec.task_id}]"
                )
                if not deliver_result.merged:
                    return build_team_failure_result(
                        DevOpsTeamResult,
                        deliver_result.summary or "DevOps delivery merge failed",
                        completion_package=DevOpsCompletionPackage(
                            task_id=task_spec.task_id,
                            status="blocked",
                            files_changed=sorted(aggregated_artifacts.keys()),
                            quality_gates=quality_gates,
                            git_operations=GitOperationsMetadata(
                                branch_created=deliver_result.branch_name,
                                commits=[GitCommitMetadata(hash="", message=commit_msg)],
                                merge=GitMergeMetadata(
                                    target_branch=DEVELOPMENT_BRANCH,
                                    strategy="merge",
                                    merge_commit_hash="",
                                    status="failed",
                                ),
                            ),
                            notes=[deliver_result.summary],
                        ),
                    )
                git_ops = GitOperationsMetadata(
                    branch_created=deliver_result.branch_name,
                    commits=[GitCommitMetadata(hash=sha, message=commit_msg)],
                    merge=GitMergeMetadata(
                        target_branch=DEVELOPMENT_BRANCH,
                        strategy="merge",
                        merge_commit_hash=sha,
                        status="merged",
                    ),
                )
            completion.git_operations = git_ops
            completion.handoff = HandoffInfo(
                prod_approval_required="production" in task_spec.platform_scope.environments,
                runbook_updated=bool(doc.files),
            )
            completion.status = "completed"
            completion.quality_gates = quality_gates
            return None

        # Consume BaseTeamLead's gate-based phase sequencer without inheriting
        # the code-v2 BaseTeamLead constructor (DevOps uses TeamLeadSharedState).
        early_exit = BaseTeamLead._run_gated_phases(
            self,
            [
                _phase1_intake_clarify,
                _phase2_parallel_design,
                _phase3_branch_write,
                _phase4_validation_review,
                _phase5_completion_deliver,
            ],
        )
        if early_exit is not None:
            return early_exit

        assert completion is not None  # phase 5 success path always assigns it
        return DevOpsTeamResult(success=True, iterations=1, completion_package=completion)
