"""Named four-phase contract for the Strategy Lab orchestrator.

The orchestrator pipeline is exposed as exactly four phases:

    DESIGN → DESIGN_REVIEW → CODE_SYNTHESIS → BACKTEST_AND_VERIFICATION → ∅

Each phase exit fires a single ``PhaseTransition`` event carrying SHA-256
artefact hashes so any consumer can detect upstream-artefact drift across
phase boundaries. The inner sub-loops (design ↔ review, refinement,
alignment, walk-forward, analysis) are implementation detail bucketed
under these four parent phases and continue to emit their own free-form
sub-phase events through the same callback.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # avoid circular import: models.py → spec_dsl → (none) but
    # phases.py is imported by orchestrator.py which already imports models.
    from ..models import StrategySpec


class Phase(str, Enum):
    """The four named phases of a Strategy Lab cycle.

    Invariants:
      - Membership is exactly four values, in the listed order.
      - Each phase has at most one named successor; ``BACKTEST_AND_VERIFICATION``
        exits to ``None`` (terminal).
    """

    DESIGN = "design"
    DESIGN_REVIEW = "design_review"
    CODE_SYNTHESIS = "code_synthesis"
    BACKTEST_AND_VERIFICATION = "backtest_and_verification"


PHASES: tuple[Phase, ...] = (
    Phase.DESIGN,
    Phase.DESIGN_REVIEW,
    Phase.CODE_SYNTHESIS,
    Phase.BACKTEST_AND_VERIFICATION,
)


PHASE_TRANSITION_EVENT_NAME: str = "phase_transition"
"""Wire format: the orchestrator emits ``PhaseTransition`` events through
the existing ``PhaseCallback`` as ``emit(PHASE_TRANSITION_EVENT_NAME, data)``
where ``data`` is the ``model_dump(mode="json")`` of a ``PhaseTransition``.
This preserves backward compatibility with every existing sub-phase emit
site and dashboard consumer.
"""


_EMPTY_SHA256: str = hashlib.sha256(b"").hexdigest()


def hash_spec(spec: "StrategySpec") -> str:
    """SHA-256 of the canonical-JSON serialisation of ``spec``, excluding
    the ``strategy_code`` field.

    The ``strategy_code`` field is excluded because it is tracked
    independently by :func:`hash_code` in the four-phase contract: the
    spec is the design-time artefact, the code is the synthesis-time
    artefact, and treating them separately makes drift in each cleanly
    attributable to the phase that owns it.

    Preconditions:
      - ``spec`` is a constructed ``StrategySpec`` (Pydantic model).
    Postconditions:
      - Returns a 64-character lowercase hex digest.
      - Output is stable across runs for any ``spec`` with the same
        non-``strategy_code`` payload: keys sorted, no whitespace, JSON
        ``mode="json"`` so datetimes/enums serialise canonically.
      - ``hash_spec(s)`` is unaffected by mutations to ``s.strategy_code``.
    Invariants:
      - Function is pure: no side effects, no I/O.
    """
    payload_dict = spec.model_dump(mode="json")
    payload_dict.pop("strategy_code", None)
    payload = json.dumps(
        payload_dict,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_code(code: Optional[str]) -> str:
    """SHA-256 of the UTF-8 encoding of ``code`` (or empty-string SHA-256).

    Preconditions:
      - ``code`` is a ``str`` or ``None`` (treated as the empty string).
    Postconditions:
      - Returns a 64-character lowercase hex digest.
      - ``hash_code(None) == hash_code("") == _EMPTY_SHA256``.
    Invariants:
      - Function is pure: no side effects, no I/O.
    """
    if not code:
        return _EMPTY_SHA256
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class PhaseTransition(BaseModel):
    """Boundary event fired exactly once per phase exit per design attempt.

    Preconditions:
      - ``from_phase`` is a member of :data:`PHASES`.
      - ``to_phase`` is either the next member of :data:`PHASES` after
        ``from_phase`` OR ``None`` (terminal exit out of the last phase).
      - ``spec_hash`` is the output of :func:`hash_spec` on the spec as it
        existed at phase exit.
      - ``code_hash`` is the output of :func:`hash_code` on the strategy
        code as it existed at phase exit (empty-string SHA-256 before
        ``CODE_SYNTHESIS`` has produced code).
      - ``attempt`` is the zero-indexed design re-entry counter from
        ``run_cycle``; ``0`` on the first attempt, incremented each time
        ``SpecImplementabilityError`` routes the cycle back to the
        design phase.

    Postconditions:
      - The event is immutable (Pydantic ``frozen=True``).

    Invariants:
      - ``spec_hash`` is stable from the ``DESIGN_REVIEW → CODE_SYNTHESIS``
        transition onward for any given design attempt: the spec is frozen
        post-design (with a tighten-only ``risk_limits`` carve-out documented
        in ``_orchestrator_helpers._merge_risk_limits_tighten_only``).
      - ``code_hash`` is stable from the ``CODE_SYNTHESIS →
        BACKTEST_AND_VERIFICATION`` transition onward for any given design
        attempt: code is not regenerated past the synthesis loop.
    """

    model_config = ConfigDict(frozen=True)

    from_phase: Phase
    to_phase: Optional[Phase] = None
    spec_hash: str = Field(..., min_length=64, max_length=64)
    code_hash: str = Field(..., min_length=64, max_length=64)
    attempt: int = Field(0, ge=0)
