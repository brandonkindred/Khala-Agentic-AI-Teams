"""Phase 4 quality-gate assembly (devops_team)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from software_engineering_team.shared.security_service import infra_gate_passed

from ..change_review_agent import ChangeReviewOutput
from ..devsecops_review_agent import DevSecOpsReviewOutput
from ..models import DevOpsCompletionPackage, DevOpsTaskSpec, DevOpsTeamResult
from ..test_validation_agent import DevOpsTestValidationOutput


@dataclass(frozen=True)
class QualityGateAssemblyResult:
    """Outcome of Phase 4 quality-gate assembly.

    Invariants: ``gate_result is None`` implies every gate in
      ``quality_gates`` is ``"pass"``; a non-``None`` ``gate_result`` implies
      at least one gate is ``"fail"`` and carries the blocked
      ``DevOpsTeamResult``.
    """

    quality_gates: Dict[str, str]
    gate_result: Optional[DevOpsTeamResult] = None


def assemble_quality_gates(
    *,
    task_spec: DevOpsTaskSpec,
    val: DevOpsTestValidationOutput,
    devsec: DevSecOpsReviewOutput,
    policy_checks: Any,
    change_review: ChangeReviewOutput,
    aggregated_artifacts: Dict[str, str],
) -> QualityGateAssemblyResult:
    """Assemble quality gates and evaluate the pass/fail gate check.

    Preconditions: ``val``/``devsec``/``change_review``/``policy_checks`` are
      the Phase 4 agent/tool results already produced this run;
      ``task_spec``/``aggregated_artifacts`` are the pipeline's own inputs.
    Postconditions: returns a ``QualityGateAssemblyResult`` whose
      ``quality_gates`` seeds from ``val.quality_gates``, force-assigns
      ``security_review`` from the unified infra decision (DevSecOps review +
      policy-as-code scan — this must never be masked by a validation-agent
      "pass"), and defaults ``change_review``. ``gate_result`` is the blocked
      ``DevOpsTeamResult`` (status "blocked", existing completion-package
      shape) when any gate is "fail", else ``None``.
    """
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

    gate_result: Optional[DevOpsTeamResult] = None
    if any(v == "fail" for v in quality_gates.values()):
        gate_result = DevOpsTeamResult(
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

    return QualityGateAssemblyResult(quality_gates=quality_gates, gate_result=gate_result)
