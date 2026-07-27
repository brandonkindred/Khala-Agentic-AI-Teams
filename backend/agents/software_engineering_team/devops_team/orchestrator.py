"""DevOps team orchestrator (DevOpsTeamLeadAgent)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm_service import DummyLLMClient, LLMClient
from software_engineering_team.shared.deliver_utils import DeliverGitOps, deliver_inline_merge
from software_engineering_team.shared.git_utils import (
    abort_merge,
    checkout_branch,
    commit_working_tree,
    create_feature_branch,
    delete_branch,
    ensure_development_branch,
    get_head_sha,
    merge_branch,
)
from software_engineering_team.shared.repo_writer import write_agent_output
from software_engineering_team.shared.team_lead_base import BaseTeamLead, TeamLeadSharedState

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
    HandoffInfo,
    ReleaseReadiness,
    SubtaskContract,
)
from .phases import (
    assemble_quality_gates,
    deliver_and_merge,
    run_phase1_intake_clarify,
    run_phase2_design_fanout,
)

# Commit-message template for the shared deliver helper. ``deliver_inline_merge``
# calls ``template.format(scope=..., summary=...)``; only ``{summary}`` is used
# here (``str.format`` ignores the unreferenced ``scope`` kwarg).
DEVOPS_DELIVER_COMMIT_MSG_TEMPLATE = "feat(devops): {summary}"

# Fallback runtime verification checklist used when the deployment-strategy
# agent's output carries no health checks of its own; the required-approval
# name for production deploys. Named so Phase 5's ReleaseReadiness assembly
# has a single, reusable source for these defaults instead of inline literals.
DEFAULT_RUNTIME_CHECKS = [
    "deployment_rollout_status",
    "service_health",
    "alert_health",
]
PROD_APPROVAL = "manual_prod_approval"

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
from . import tool_dispatch  # noqa: E402
from .infra_debug_agent import InfraDebugAgent  # noqa: E402
from .infra_patch_agent import InfraPatchAgent  # noqa: E402
from .task_clarifier import DevOpsTaskClarifierAgent  # noqa: E402
from .test_validation_agent import (  # noqa: E402
    DevOpsTestValidationAgent,
    DevOpsTestValidationInput,
)
from .tool_agents import (  # noqa: E402
    CDKExecutionToolAgent,
    CICDLintPipelineValidationToolAgent,
    DeploymentDryRunPlanToolAgent,
    DockerComposeExecutionToolAgent,
    HelmExecutionToolAgent,
    IaCValidationToolAgent,
    PolicyAsCodeToolAgent,
    RepoNavigatorToolAgent,
    TerraformExecutionToolAgent,
)

logger = logging.getLogger(__name__)

from . import debug_patch  # noqa: E402
from .debug_patch import MAX_INFRA_FIX_ITERATIONS, _DebugPatchState  # noqa: E402,F401


class DevOpsTeamLeadAgent(BaseTeamLead):
    """Coordinates specialized DevOps agents with hard gates.

    Inherits ``BaseTeamLead`` (and, transitively, ``TeamLeadSharedState``) for
    LLM resolution and the optional per-run status hook (``_report_status`` /
    ``_status_callback``). Pipeline phase status always emits INFO logs via
    :meth:`_log_pipeline_status`; the optional callback is a separate forward
    channel and may be set/cleared per run without losing historical log
    output. DevOps does not use ``BaseTeamLead``'s per-repo briefing cache
    (:meth:`BaseTeamLead._repo_context_cache_for`), so ``__init__`` passes
    empty extension/exclude-dir sets and a zero char budget for that unused
    feature.

    Invariants: ``self.llm`` is the client passed to ``__init__``; specialist
    agents and tools are constructed once; ``_status_callback`` defaults to
    None (mixin default) and is independent of fallback logging.
    """

    # Tool-dispatch logic lives in ``tool_dispatch.py``; aliased here so
    # ``self._run_execution_tools(...)`` keeps its existing bound-method call
    # shape (see devops_team/tool_dispatch.py for the implementation).
    _run_execution_tools = tool_dispatch.run_execution_tools

    # Debug-patch retry logic lives in ``debug_patch.py``; aliased here so
    # ``self._debug_patch_once(...)`` keeps its existing bound-method call
    # shape (see devops_team/debug_patch.py for the implementation).
    _debug_patch_once = debug_patch.debug_patch_once

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        BaseTeamLead.__init__(
            self,
            llm_client,
            extensions=frozenset(),
            exclude_dirs=frozenset(),
            max_chars=0,
        )
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
        task_spec = DevOpsTeamLeadAgent._build_legacy_spec(
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
        # Phase 4.6 debug-patch attempts consumed; 1 = no retry needed.
        infra_fix_iterations = 1

        def _phase1_intake_clarify() -> Optional[DevOpsTeamResult]:
            """Phase 1: environment policy + task clarification gates.

            Preconditions: ``task_spec`` is the pipeline input for this run.
            Postconditions: returns a failed ``DevOpsTeamResult`` on env-policy or
              clarifier rejection; otherwise builds subtask contracts, logs their
              count, and returns ``None`` so later phases run.

            Thin wrapper around the standalone ``run_phase1_intake_clarify``;
            converts its typed result into this pipeline's gate contract.
            """
            result = run_phase1_intake_clarify(
                task_spec=task_spec,
                task_clarifier=self.task_clarifier,
                enforce_env_policy=self._enforce_env_policy,
                build_subtask_contracts=self._build_subtask_contracts,
            )
            if result.blocked_reason:
                return DevOpsTeamResult(success=False, failure_reason=result.blocked_reason)

            logger.info(
                "DevOps team pipeline: %d subtask contracts generated",
                len(result.subtask_contracts),
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
            # Enable parallel execution unless the backing LLM client is a
            # DummyLLMClient (or subclass) — scripted test clients use a shared
            # sequential response list that breaks under concurrent access.
            use_parallel = not isinstance(self.llm, DummyLLMClient)
            phase2 = run_phase2_design_fanout(
                task_spec=task_spec,
                repo_path=repo_path,
                iac_agent=self.iac_agent,
                cicd_agent=self.cicd_agent,
                deployment_agent=self.deployment_agent,
                repo_navigator_tool=self.repo_navigator_tool,
                parallel=use_parallel,
            )
            iac_result = phase2.iac_result
            cicd_result = phase2.cicd_result
            deploy_result = phase2.deploy_result
            aggregated_artifacts = phase2.aggregated_artifacts
            return None

        def _phase3_branch_write() -> Optional[DevOpsTeamResult]:
            """Phase 3: feature branch + artifact write gates.

            Delegates to :func:`debug_patch.run_phase3_branch_write`; passes
            ``ensure_development_branch``/``create_feature_branch`` through
            from this module's globals so existing test monkeypatches on
            those names keep working.
            """
            return debug_patch.run_phase3_branch_write(
                write_changes=write_changes,
                aggregated_artifacts=aggregated_artifacts,
                repo_path=repo_path,
                task_spec=task_spec,
                subdir=subdir,
                ensure_development_branch=ensure_development_branch,
                create_feature_branch=create_feature_branch,
                report_status=self._report_status,
            )

        def _phase4_validation_review() -> Optional[DevOpsTeamResult]:
            """Phase 4: tool validation, reviews, and early-exit gates.

            Preconditions: Phases 1–3 returned ``None`` (``aggregated_artifacts``
              may be empty).
            Postconditions: runs tool validation, execution verification, the
              debug-patch loop, and independent reviews; sets nonlocal
              ``quality_gates`` and ``infra_fix_iterations`` (Phase 4.6 attempts
              consumed; stays 1 when no retry was needed). Returns a failed
              ``DevOpsTeamResult`` on quality-gate or build-verifier failure via
              ``_run_phase_gates``; otherwise returns ``None`` so Phase 5 runs.
            """
            nonlocal aggregated_artifacts, quality_gates, acceptance_trace, infra_fix_iterations

            # Phase 4: tool validation + independent reviews
            self._report_status(
                "phase4",
                detail="DevOps team pipeline: phase 4 - validation and review",
            )
            vt = tool_dispatch.run_validation_tools(self, repo_path)
            iac_checks, policy_checks = vt.iac_checks, vt.policy_checks
            cicd_checks, dry_run_checks = vt.cicd_checks, vt.dry_run_checks
            tool_gate_map: Dict[str, str] = dict(vt.tool_gate_map)

            # Phase 4.5: Execution verification
            self._report_status(
                "phase4.5",
                detail="DevOps team pipeline: phase 4.5 - execution verification",
            )
            repo_str = str(repo_path)
            exec_results = self._run_execution_tools(repo_str, aggregated_artifacts)
            for er in exec_results:
                if not isinstance(er, dict):
                    logger.warning("DevOps execution result is not a dict: %r", er)
                    continue
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

                def _debug_patch_attempt(i: int) -> Optional[_DebugPatchState]:
                    nonlocal infra_fix_iterations
                    infra_fix_iterations = i + 1
                    return self._debug_patch_once(
                        i,
                        state=state,
                        aggregated_artifacts=aggregated_artifacts,
                        repo_path=repo_path,
                        repo_str=repo_str,
                        write_changes=write_changes,
                        subdir=subdir,
                        max_iterations=MAX_INFRA_FIX_ITERATIONS,
                    )

                self._run_bounded_retry_loop(
                    max_iterations=MAX_INFRA_FIX_ITERATIONS,
                    attempt=_debug_patch_attempt,
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

            qg = assemble_quality_gates(
                task_spec=task_spec,
                val=val,
                devsec=devsec,
                policy_checks=policy_checks,
                change_review=change_review,
                aggregated_artifacts=aggregated_artifacts,
            )
            quality_gates = qg.quality_gates

            def _quality_gates_check() -> Optional[DevOpsTeamResult]:
                """Fail the phase when any assembled quality gate is ``fail``.

                Preconditions: ``qg`` is set by Phase 4 setup above.
                Postconditions: returns the blocked ``DevOpsTeamResult`` with the
                  existing completion-package shape when any gate fails; otherwise
                  ``None``.
                """
                return qg.gate_result

            def _build_verifier_check() -> Optional[DevOpsTeamResult]:
                """Fail the phase when an injected build verifier rejects the repo.

                Preconditions: quality gates already passed (prior gate returned
                  ``None``); ``build_verifier`` may be ``None``; ``repo_path`` and
                  ``task_spec`` are set by Phase 4 setup above.
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

            return self._run_phase_gates([_quality_gates_check, _build_verifier_check])

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
                required_approvals=[PROD_APPROVAL]
                if "production" in task_spec.platform_scope.environments
                else [],
                runtime_verification_checklist=list(getattr(deploy_result, "health_checks", []))
                or DEFAULT_RUNTIME_CHECKS,
            )
            # Deliver the artifacts for real via the shared inline-merge helper and
            # report the actual outcome (real branch, commit SHA, merge status) rather
            # than fabricated placeholders. A model-only run (write_changes=False) does
            # no git work, so the neutral default honestly reports "nothing delivered".
            delivery = deliver_and_merge(
                task_spec=task_spec,
                repo_path=repo_path,
                aggregated_artifacts=aggregated_artifacts,
                write_changes=write_changes,
                quality_gates=quality_gates,
                commit_msg_template=DEVOPS_DELIVER_COMMIT_MSG_TEMPLATE,
                ops=_git_ops(),
                deliver_inline_merge=deliver_inline_merge,
                get_head_sha=get_head_sha,
                logger=logger,
            )
            if delivery.failure_result is not None:
                return delivery.failure_result
            completion.notes.extend(delivery.notes)
            completion.git_operations = delivery.git_ops
            completion.handoff = HandoffInfo(
                prod_approval_required="production" in task_spec.platform_scope.environments,
                runbook_updated=bool(doc.files),
            )
            completion.status = "completed"
            completion.quality_gates = quality_gates
            return None

        early_exit = self._run_gated_phases(
            [
                _phase1_intake_clarify,
                _phase2_parallel_design,
                _phase3_branch_write,
                _phase4_validation_review,
                _phase5_completion_deliver,
            ]
        )
        if early_exit is not None:
            return early_exit

        assert completion is not None  # phase 5 success path always assigns it
        return DevOpsTeamResult(
            success=True, iterations=infra_fix_iterations, completion_package=completion
        )
