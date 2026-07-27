"""Phase 1: environment policy + task clarification gates (devops_team)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from ..models import DevOpsTaskSpec, SubtaskContract
from ..task_clarifier import DevOpsTaskClarifierAgent, DevOpsTaskClarifierInput


@dataclass(frozen=True)
class Phase1ClarifyResult:
    """Outcome of Phase 1 (env-policy + clarification gates).

    Invariants: ``blocked_reason is None`` implies the gates passed and
      ``subtask_contracts`` was generated; a non-``None`` ``blocked_reason``
      implies ``subtask_contracts`` is empty.
    """

    blocked_reason: Optional[str] = None
    subtask_contracts: List[SubtaskContract] = field(default_factory=list)


def run_phase1_intake_clarify(
    *,
    task_spec: DevOpsTaskSpec,
    task_clarifier: DevOpsTaskClarifierAgent,
    enforce_env_policy: Callable[[DevOpsTaskSpec], Optional[str]],
    build_subtask_contracts: Callable[[DevOpsTaskSpec], List[SubtaskContract]],
) -> Phase1ClarifyResult:
    """Enforce environment policy, run the clarifier, and build subtask contracts.

    Preconditions: ``task_spec`` is the pipeline input for this run;
      ``task_clarifier`` is a constructed ``DevOpsTaskClarifierAgent``;
      ``enforce_env_policy``/``build_subtask_contracts`` are the team's own
      static helpers, injected so this function stays free of ``self``.
    Postconditions: returns a ``Phase1ClarifyResult`` with ``blocked_reason``
      set to a human-readable message on env-policy or clarifier rejection
      (``subtask_contracts`` empty in that case); otherwise ``blocked_reason``
      is ``None`` and ``subtask_contracts`` holds the generated contracts.
    """
    env_block = enforce_env_policy(task_spec)
    if env_block:
        return Phase1ClarifyResult(blocked_reason=f"Environment policy violation: {env_block}")

    clarifier = task_clarifier.run(DevOpsTaskClarifierInput(task_spec=task_spec))
    if not clarifier.approved_for_execution:
        return Phase1ClarifyResult(
            blocked_reason="Clarification required: "
            + "; ".join(clarifier.clarification_requests[:3])
        )

    subtask_contracts = build_subtask_contracts(task_spec)
    return Phase1ClarifyResult(subtask_contracts=subtask_contracts)
