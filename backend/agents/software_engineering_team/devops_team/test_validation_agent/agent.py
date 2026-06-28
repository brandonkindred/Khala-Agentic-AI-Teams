"""DevOps test and validation agent.

This is now a thin delegating shim over the unified QA agent
(:class:`qa_agent.QAExpertAgent`). The distinctive *evidence -> acceptance-
criteria mapping* logic lives in that agent's ``acceptance_evidence`` mode; this
class preserves the DevOps-facing name, constructor signature, and I/O models so
the DevOps orchestrator and existing tests are unaffected.
"""

from __future__ import annotations

from typing import get_args

from devops_team.models import GateStatus
from qa_agent import QAExpertAgent, QAInput

from llm_service import LLMClient

from .models import (
    DevOpsTestValidationInput,
    DevOpsTestValidationOutput,
    ValidationEvidence,
)

_VALID_GATE_STATUSES = frozenset(get_args(GateStatus))


def _coerce_gate_status(value: object) -> GateStatus:
    """Map an arbitrary status string onto the ``GateStatus`` literal.

    Preconditions: ``value`` is any object (typically a str from the QA agent).
    Postconditions: returns a member of ``GateStatus``; unrecognized values
    collapse to ``"not_run"`` so the output never violates the literal contract.
    """
    text = str(value).strip().lower()
    return text if text in _VALID_GATE_STATUSES else "not_run"  # type: ignore[return-value]


class DevOpsTestValidationAgent:
    """Validate tool results against acceptance criteria via the unified QA agent.

    Invariants:
        - Delegates all reasoning to a single wrapped :class:`QAExpertAgent`.
        - The output shape (:class:`DevOpsTestValidationOutput`) and the
          gate-fail blocking rule are identical to the pre-refactor agent.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        """Build the shim.

        Preconditions: ``llm_client`` is not ``None``.
        Postconditions: holds a ``QAExpertAgent`` constructed from the same client.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._qa = QAExpertAgent(llm_client)

    def run(self, input_data: DevOpsTestValidationInput) -> DevOpsTestValidationOutput:
        """Map tool/test evidence to acceptance criteria.

        Preconditions: ``input_data`` is a valid ``DevOpsTestValidationInput``.
        Postconditions: returns a ``DevOpsTestValidationOutput`` whose
        ``quality_gates`` values are valid ``GateStatus`` members and whose
        ``approved`` is ``False`` whenever any gate failed (the unified QA agent
        already applies this rule in ``acceptance_evidence`` mode).
        """
        qa_out = self._qa.run(
            QAInput(
                code="",
                request_mode="acceptance_evidence",
                acceptance_criteria=input_data.acceptance_criteria,
                tool_results=input_data.tool_results,
            )
        )
        gates = {k: _coerce_gate_status(v) for k, v in qa_out.quality_gates.items()}
        return DevOpsTestValidationOutput(
            approved=qa_out.approved,
            quality_gates=gates,
            acceptance_trace=list(qa_out.acceptance_trace),
            evidence=[
                ValidationEvidence(
                    gate=str(e.get("gate", "")),
                    status=_coerce_gate_status(e.get("status", "")),
                    detail=str(e.get("detail", "")),
                )
                for e in qa_out.validation_evidence
                if isinstance(e, dict)
            ],
            summary=qa_out.summary,
        )
