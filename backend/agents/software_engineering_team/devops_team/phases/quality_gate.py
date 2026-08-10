"""Phase 4: tool validation, reviews, and quality-gate assembly (devops_team).

Split out of ``orchestrator.py``. ``run_phase4_quality_gate`` takes the owning
``DevOpsTeamLeadAgent`` instance (duck-typed as ``agent``), following the
``tool_dispatch.py`` / ``debug_patch.py`` convention, because this phase needs
live access to several agent-owned collaborators invoked multiple times
(``agent._run_execution_tools``, ``agent._debug_patch_once``,
``agent._run_bounded_retry_loop``, ``agent._report_status``, plus the
DevSecOps/change-review/QA agents).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from software_engineering_team.qa_agent import QAInput
from software_engineering_team.shared.security_service import infra_gate_passed

from .. import tool_dispatch
from ..change_review_agent import ChangeReviewInput
from ..debug_patch import MAX_INFRA_FIX_ITERATIONS, _DebugPatchState
from ..devsecops_review_agent import DevSecOpsReviewInput
from ..models import (
    DevOpsCompletionPackage,
    DevOpsTaskSpec,
    DevOpsTeamResult,
    GateStatus,
    coerce_gate_status,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Phase4QualityGateResult:
    """Outcome of Phase 4 (tool validation, reviews, quality-gate assembly)."""

    quality_gates: Dict[str, GateStatus] = field(default_factory=dict)
    acceptance_trace: List[Dict[str, object]] = field(default_factory=list)
    tool_gate_map: Dict[str, str] = field(default_factory=dict)
    infra_fix_iterations: int = 1
    blocked_result: Optional[DevOpsTeamResult] = None


def run_phase4_quality_gate(
    agent: Any,
    *,
    task_spec: DevOpsTaskSpec,
    repo_path: Path,
    aggregated_artifacts: Dict[str, str],
    write_changes: bool,
    subdir: str,
    build_verifier: Optional[Any],
) -> Phase4QualityGateResult:
    """Phase 4: tool validation, execution verification, reviews, and gates.

    Preconditions: Phases 1-3 returned ``None`` (``aggregated_artifacts`` may
      be empty); ``agent`` provides ``_report_status``,
      ``_run_execution_tools``, ``_debug_patch_once``,
      ``_run_bounded_retry_loop``, ``devsecops_review_agent``,
      ``change_review_agent``, and ``qa_agent``.
    Postconditions: runs tool validation, execution verification, the
      debug-patch loop, and independent reviews, returning the assembled
      ``quality_gates`` (``agent.qa_agent.run(..., request_mode="acceptance_evidence")``'s
      ``quality_gates`` coerced to ``GateStatus`` via ``coerce_gate_status``, then
      augmented with ``security_review`` and ``change_review``), ``acceptance_trace``,
      ``tool_gate_map``, and ``infra_fix_iterations`` (Phase 4.6 attempts consumed;
      stays 1 when no retry was needed). ``blocked_result`` is set to a failed
      ``DevOpsTeamResult`` when any quality gate fails or the injected
      ``build_verifier`` rejects the repo; otherwise ``None`` so Phase 5 runs.
    """
    agent._report_status(
        "phase4",
        detail="DevOps team pipeline: phase 4 - validation and review",
    )
    vt = tool_dispatch.run_validation_tools(agent, repo_path)
    iac_checks, policy_checks = vt.iac_checks, vt.policy_checks
    cicd_checks, dry_run_checks = vt.cicd_checks, vt.dry_run_checks
    tool_gate_map: Dict[str, str] = dict(vt.tool_gate_map)

    agent._report_status(
        "phase4.5",
        detail="DevOps team pipeline: phase 4.5 - execution verification",
    )
    repo_str = str(repo_path)
    exec_results = agent._run_execution_tools(repo_str, aggregated_artifacts)
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
    # execution-tool check statuses) is merged into local ``tool_gate_map``;
    # remaining ``state.exec_failures`` do not early-return a failed
    # ``DevOpsTeamResult`` (pre-refactor behavior preserved).
    state = _DebugPatchState(exec_results=exec_results)
    infra_fix_iterations = 1
    if state.exec_failures:

        def _debug_patch_attempt(i: int) -> Optional[_DebugPatchState]:
            nonlocal infra_fix_iterations
            infra_fix_iterations = i + 1
            return agent._debug_patch_once(
                i,
                state=state,
                aggregated_artifacts=aggregated_artifacts,
                repo_path=repo_path,
                repo_str=repo_str,
                write_changes=write_changes,
                subdir=subdir,
                max_iterations=MAX_INFRA_FIX_ITERATIONS,
            )

        agent._run_bounded_retry_loop(
            max_iterations=MAX_INFRA_FIX_ITERATIONS,
            attempt=_debug_patch_attempt,
            is_success=lambda s: s is not None and not s.exec_failures,
        )

    tool_gate_map.update(state.exec_gate_map)

    devsec = agent.devsecops_review_agent.run(
        DevSecOpsReviewInput(
            task_description=task_spec.title,
            requirements=task_spec.goal.summary,
            artifacts=aggregated_artifacts,
        )
    )
    change_review = agent.change_review_agent.run(
        ChangeReviewInput(task_description=task_spec.title, artifacts=aggregated_artifacts)
    )

    qa_val = agent.qa_agent.run(
        QAInput(
            code="",
            request_mode="acceptance_evidence",
            acceptance_criteria=task_spec.acceptance_criteria,
            tool_results={
                "iac": iac_checks.checks,
                "policy": policy_checks.checks,
                "cicd": cicd_checks.checks,
                "deploy_dry_run": dry_run_checks.checks,
            },
        )
    )
    acceptance_trace = list(qa_val.acceptance_trace)

    quality_gates = {k: coerce_gate_status(v) for k, v in qa_val.quality_gates.items()}
    # The infra security gate routes both the DevSecOps LLM review and the
    # policy-as-code (checkov) scan through the unified infra decision. This
    # is force-assigned (not setdefault) so the authoritative DevSecOps +
    # policy result always wins - a validation-agent-supplied "pass" must
    # never mask a failing review or checkov scan.
    quality_gates["security_review"] = (
        "pass" if infra_gate_passed(devsec.approved, policy_checks.success) else "fail"
    )
    quality_gates.setdefault("change_review", "pass" if change_review.approved else "fail")

    if any(v == "fail" for v in quality_gates.values()):
        return Phase4QualityGateResult(
            quality_gates=quality_gates,
            acceptance_trace=acceptance_trace,
            tool_gate_map=tool_gate_map,
            infra_fix_iterations=infra_fix_iterations,
            blocked_result=DevOpsTeamResult(
                success=False,
                failure_reason="Quality gates failed",
                completion_package=DevOpsCompletionPackage(
                    task_id=task_spec.task_id,
                    status="blocked",
                    files_changed=sorted(aggregated_artifacts.keys()),
                    quality_gates=quality_gates,
                    notes=[devsec.summary, change_review.summary, qa_val.summary],
                    risks_remaining=[f.issue for f in devsec.findings if f.blocking],
                ),
            ),
        )

    if build_verifier is not None:
        verify_ok, verify_err = build_verifier(repo_path, "devops", task_spec.task_id)
        if not verify_ok:
            return Phase4QualityGateResult(
                quality_gates=quality_gates,
                acceptance_trace=acceptance_trace,
                tool_gate_map=tool_gate_map,
                infra_fix_iterations=infra_fix_iterations,
                blocked_result=DevOpsTeamResult(
                    success=False,
                    failure_reason=verify_err or "Build verification failed",
                ),
            )

    return Phase4QualityGateResult(
        quality_gates=quality_gates,
        acceptance_trace=acceptance_trace,
        tool_gate_map=tool_gate_map,
        infra_fix_iterations=infra_fix_iterations,
        blocked_result=None,
    )
