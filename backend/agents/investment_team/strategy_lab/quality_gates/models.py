"""Shared models for quality gate results."""

from __future__ import annotations

from typing import ClassVar, Literal, Optional

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
    call ``self._set_phase(phase)`` once at the entry of their public
    ``check()`` / ``validate()`` method. Helpers (``_critical`` / ``_warning``
    / ``_info``) then read the gate name and phase from instance state, so
    individual result construction sites collapse from seven lines to one.

    Contract:
      Pre: ``GATE`` is defined; ``_set_phase`` has been called with a valid
      phase before any helper is invoked.
      Post: every emitted ``QualityGateResult`` carries the gate's ``GATE``
      string and the phase the caller declared.

    The pattern is not re-entrant: helpers read a single ``_phase`` slot, so
    a gate instance must not be shared across concurrent ``check()`` calls.
    Strategy Lab orchestration is sequential, so this is acceptable.
    """

    GATE: ClassVar[str] = ""
    _phase: Optional[StrategyLabPhase] = None

    def _set_phase(self, phase: StrategyLabPhase) -> None:
        # Pre: phase is one of the four valid labels.
        assert phase in _VALID_PHASES, f"invalid phase: {phase!r}"
        # Pre: subclass must declare GATE.
        assert self.GATE, f"{type(self).__name__} must declare a non-empty GATE constant"
        self._phase = phase

    def _critical(self, details: str) -> QualityGateResult:
        return self._emit(passed=False, severity="critical", details=details)

    def _warning(self, details: str) -> QualityGateResult:
        return self._emit(passed=False, severity="warning", details=details)

    def _info(self, details: str = "") -> QualityGateResult:
        return self._emit(passed=True, severity="info", details=details)

    def _emit(
        self, *, passed: bool, severity: Literal["info", "warning", "critical"], details: str
    ) -> QualityGateResult:
        # Pre: _set_phase has been called.
        assert self._phase is not None, (
            f"{type(self).__name__}._set_phase must be called before emitting results"
        )
        return QualityGateResult(
            gate_name=self.GATE,
            phase=self._phase,
            passed=passed,
            severity=severity,
            details=details,
        )
