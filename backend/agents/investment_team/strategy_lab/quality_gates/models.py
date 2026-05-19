"""Shared models for quality gate results."""

from __future__ import annotations

from contextlib import contextmanager
from typing import ClassVar, Iterator, Literal, Optional

from pydantic import BaseModel

StrategyLabPhase = Literal["design", "design_review", "synthesis", "verification"]

_VALID_PHASES: frozenset[str] = frozenset(StrategyLabPhase.__args__)  # type: ignore[attr-defined]


class QualityGateResult(BaseModel):
    """Result of a single quality gate check."""

    gate_name: str
    passed: bool
    details: str
    severity: Literal["info", "warning", "critical"]
    phase: StrategyLabPhase
    refinement_round: int = 0


class GateResultsMixin:
    """Mixin that fills in ``gate_name`` and ``phase`` automatically.

    Subclasses declare ``GATE`` (the gate name stamped on every result) and
    wrap their public ``check()`` / ``validate()`` body in
    ``with self._using_phase(phase): ...``. Helpers (``_critical`` /
    ``_warning`` / ``_info``) then read the gate name and phase from
    instance state.

    Contract:
      Pre: ``GATE`` is non-empty; ``_using_phase`` is active when any
      helper is invoked.
      Post: every emitted ``QualityGateResult`` carries the gate's
      ``GATE`` string and the phase the context manager declared.
      The context manager restores the previous ``_phase`` on exit
      (including the unwound state after an exception), so a rule that
      raises mid-``check`` cannot leak a stale phase into a subsequent
      call. The slot is still a single attribute on ``self``, so the
      gate must not be shared across concurrent ``check()`` calls —
      Strategy Lab orchestration is sequential.
    """

    GATE: ClassVar[str] = ""
    _phase: Optional[StrategyLabPhase] = None

    @contextmanager
    def _using_phase(self, phase: StrategyLabPhase) -> Iterator[None]:
        # Pre: phase is one of the four valid labels.
        assert phase in _VALID_PHASES, f"invalid phase: {phase!r}"
        # Pre: subclass must declare GATE.
        assert self.GATE, f"{type(self).__name__} must declare a non-empty GATE constant"
        prev = self._phase
        self._phase = phase
        try:
            yield
        finally:
            # Post: phase slot reverts even when the body raises, so a rule
            # that throws never leaves stale state visible to the next caller.
            self._phase = prev

    def _critical(self, details: str) -> QualityGateResult:
        return self._emit(passed=False, severity="critical", details=details)

    def _warning(self, details: str) -> QualityGateResult:
        return self._emit(passed=False, severity="warning", details=details)

    def _info(self, details: str = "") -> QualityGateResult:
        return self._emit(passed=True, severity="info", details=details)

    def _emit(
        self, *, passed: bool, severity: Literal["info", "warning", "critical"], details: str
    ) -> QualityGateResult:
        # Pre: ``_using_phase`` is active.
        assert self._phase is not None, (
            f"{type(self).__name__} must be inside `with self._using_phase(...)` "
            "to emit a QualityGateResult"
        )
        return QualityGateResult(
            gate_name=self.GATE,
            phase=self._phase,
            passed=passed,
            severity=severity,
            details=details,
        )
