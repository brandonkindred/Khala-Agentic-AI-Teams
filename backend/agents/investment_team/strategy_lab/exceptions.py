"""Strategy Lab exceptions.

Kept in a standalone module so the orchestrator and the refinement agent
can both raise/catch ``SpecImplementabilityError`` without pulling each
other in via a circular import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..models import StrategySpec


class SpecImplementabilityError(Exception):
    """Raised when the refinement loop cannot make the spec implementable.

    The orchestrator catches this and re-enters ideation with ``evidence``
    appended to the convergence directives, bounded by
    ``MAX_DESIGN_REENTRIES``.  ``failure_phase`` (when set) identifies the
    refinement sub-phase that triggered the trip — one of ``"validation"``,
    ``"execution"``, or ``"evaluation"``.

    ``last_spec`` / ``last_code`` carry the just-pre-mutation spec and
    code so the outer loop can build a useful short-circuit record on
    re-entry exhaustion without re-running ideation. Raisers MUST set
    both — ``run_cycle`` relies on them when building the
    ``failed: spec_unimplementable`` record.
    """

    def __init__(
        self,
        evidence: str,
        *,
        failure_phase: Optional[str],
        last_spec: "StrategySpec",
        last_code: str,
    ) -> None:
        super().__init__(evidence)
        self.evidence = evidence
        self.failure_phase = failure_phase
        self.last_spec = last_spec
        self.last_code = last_code
