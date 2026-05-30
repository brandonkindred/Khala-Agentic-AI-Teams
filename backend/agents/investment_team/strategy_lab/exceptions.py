"""Strategy Lab exceptions.

Kept in a standalone module so the orchestrator and the refinement agent
can both raise/catch ``SpecImplementabilityError`` without pulling each
other in via a circular import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from llm_service.interface import LLMTemporaryError

if TYPE_CHECKING:
    from ..models import StrategySpec


class StrategyLabLLMError(LLMTemporaryError):
    """Raised by the strategy-lab LLM envelope when a call cannot be completed.

    Produced by :func:`agents._llm_envelope.invoke_agent` after retries or the
    total wall-time budget are exhausted, or immediately when the underlying
    exception classifies as fatal (4xx / auth / malformed). Subclasses
    :class:`LLMTemporaryError` so existing broad ``except Exception`` handlers
    keep their fail-closed contract, and so any future job-pause logic keyed on
    ``LLMTemporaryError`` treats an unreachable lab LLM the same way.

    Preconditions:
      Raised only by the envelope; ``attempts >= 1`` when set.
    Postconditions:
      ``agent_key`` / ``phase`` / ``attempts`` / ``last_error_class`` /
      ``outcome`` expose the failure context; ``outcome`` is one of
      ``"fatal"`` | ``"exhausted"`` | ``"budget_exhausted"``.
    """

    def __init__(
        self,
        message: str,
        *,
        agent_key: Optional[str] = None,
        phase: Optional[str] = None,
        attempts: Optional[int] = None,
        last_error_class: Optional[str] = None,
        outcome: Optional[str] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.agent_key = agent_key
        self.phase = phase
        self.attempts = attempts
        self.last_error_class = last_error_class
        self.outcome = outcome


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

    ``drift_collector`` (optional) carries the accumulated spec/code
    revision history so the short-circuit record preserves drift
    observability even on failure paths.
    """

    def __init__(
        self,
        evidence: str,
        *,
        failure_phase: Optional[str],
        last_spec: "StrategySpec",
        last_code: str,
        drift_collector: Optional[Any] = None,
    ) -> None:
        super().__init__(evidence)
        self.evidence = evidence
        self.failure_phase = failure_phase
        self.last_spec = last_spec
        self.last_code = last_code
        self.drift_collector = drift_collector
