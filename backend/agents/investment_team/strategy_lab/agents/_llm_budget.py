"""Per-cycle LLM-call budget for the Strategy Lab design phase.

The design phase can fan a single ``run_cycle`` into a large number of
LLM round-trips: each design re-entry runs a bounded design ↔ review loop,
and within each round the designer may parse-retry, self-review, and
self-revise. Left uncapped, a non-converging spec drifting on a borderline
design burns a multiplicative number of calls before the deterministic
``design_not_ready`` short-circuit ever fires, which both wastes cloud
spend and can exhaust rate-limited quotas mid-cycle.

:class:`LLMCallBudget` is a plain counter threaded from ``run_cycle`` down
to every design-phase ``agent(prompt)`` call site. Each site charges the
budget *before* invoking the model; when the cap is reached the next charge
raises :class:`DesignBudgetExhausted`, which the design loop translates into
a structured ``status="failed: budget_exhausted"`` short-circuit.

This module imports nothing from ``strategy_lab`` (stdlib only) so it can be
imported by the orchestrator and both design agents without creating an
import cycle.

Charging is accessed through a single chokepoint — :func:`charge_active_budget`,
backed by a context variable the orchestrator binds via :func:`use_budget`
for the duration of the design phase. Agents call ``charge_active_budget()``
right before every model invocation; they no longer thread a ``budget``
argument through their signatures, so a new design-phase LLM call site only
has to make that one call to be covered by the cap.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator, Optional


class DesignBudgetExhausted(Exception):
    """Raised by :meth:`LLMCallBudget.charge` when the per-cycle budget is hit.

    Caught in ``_run_design_loop`` and translated to
    ``status="failed: budget_exhausted"``. Carries the configured ``limit``
    and the ``calls_made`` so far for diagnostics and the abort reason.

    Preconditions:
      ``limit >= 1`` and ``calls_made >= 0`` (the raiser is
      :class:`LLMCallBudget`, which maintains both invariants).
    Postconditions:
      ``self.limit`` / ``self.calls_made`` expose the trip context; the
      message names the controlling env var so operators can size quota.
    """

    def __init__(self, limit: int, calls_made: int) -> None:
        self.limit = limit
        self.calls_made = calls_made
        super().__init__(
            f"design-phase LLM-call budget exhausted: {calls_made} call(s) made, "
            f"limit {limit} (raise STRATEGY_LAB_DESIGN_MAX_LLM_CALLS to allow more)"
        )


def _annotate_budget_exhaustion(
    exc: DesignBudgetExhausted,
    spec: object,
    *,
    code: Optional[object] = None,
    rationale: Optional[object] = None,
    mechanical_repair_count: Optional[int] = None,
) -> DesignBudgetExhausted:
    """Stamp the latest in-loop state onto a budget trip before re-raising.

    The design / refinement / alignment / synthesis loops each catch a
    :class:`DesignBudgetExhausted` at the point the per-cycle LLM-call budget
    trips and attach the freshest spec — and, depending on the call site, the
    code, rationale, and mechanical-repair count they were working on — before
    re-raising, so the outer ``_run_design_loop`` budget handler can build the
    ``failed: budget_exhausted`` short-circuit record from the state actually
    reached rather than a stale pre-loop draft. This centralises that
    annotate-and-reraise idiom.

    Callers re-raise with a bare ``raise`` immediately after calling this, so
    the original traceback and propagation semantics are preserved exactly;
    this function only mutates ``exc`` in place and never raises or swallows.

    Preconditions:
      ``exc`` is the :class:`DesignBudgetExhausted` currently being handled.
      ``spec`` is the latest realised spec for the failing attempt.
    Postconditions:
      ``exc.latest_spec is spec``. ``exc.latest_code`` /
      ``exc.latest_rationale`` / ``exc.mechanical_repair_count`` are set only
      for the keyword arguments that were supplied (non-``None``), leaving any
      other annotations untouched. Returns the same ``exc`` object.
    """
    assert isinstance(exc, DesignBudgetExhausted), "exc must be a DesignBudgetExhausted"
    exc.latest_spec = spec
    if code is not None:
        exc.latest_code = code
    if rationale is not None:
        exc.latest_rationale = rationale
    if mechanical_repair_count is not None:
        exc.mechanical_repair_count = mechanical_repair_count
    return exc


class LLMCallBudget:
    """Counter for design-phase LLM calls within a single ``run_cycle``.

    Created once in ``run_cycle`` and threaded through ``_run_design_attempt``
    → ``_run_design_loop`` → the design/review agents, so the cap is a true
    ceiling on the whole cycle (spanning every ``MAX_DESIGN_REENTRIES``
    re-entry), not a per-attempt allowance.

    Invariants:
      * ``0 <= calls_made <= limit`` at all times.
      * Exactly ``limit`` calls succeed; the ``(limit + 1)``-th raises.
    """

    def __init__(self, limit: int) -> None:
        """Construct a budget admitting exactly ``limit`` charges.

        Preconditions:
          ``limit >= 1`` — callers resolve this from
          ``_design_max_llm_calls`` which floors sub-1 values to 1.
        Postconditions:
          ``self.calls_made == 0`` and ``self.limit == limit``.
        """
        assert limit >= 1, "LLMCallBudget limit must be >= 1"
        self.limit = limit
        self.calls_made = 0

    def charge(self) -> None:
        """Account for one LLM call, or refuse when the budget is spent.

        Pre-charge / check-then-increment: callers invoke ``charge()``
        immediately before each ``agent(prompt)`` call, so the cycle stops
        *before* exceeding the documented budget rather than after.

        Preconditions:
          Called once per imminent LLM call.
        Postconditions:
          On success ``calls_made`` is incremented by exactly 1 and the
          caller may make its LLM call. When ``calls_made >= limit`` raises
          :class:`DesignBudgetExhausted` without incrementing — so exactly
          ``limit`` charges ever succeed.
        """
        if self.calls_made >= self.limit:
            raise DesignBudgetExhausted(self.limit, self.calls_made)
        self.calls_made += 1


# The budget in force for the current design phase. Bound by ``use_budget``
# in ``run_cycle`` and read by ``charge_active_budget`` at each LLM call site.
# Default ``None`` means "no cap" — agents invoked outside a design cycle
# (e.g. unit tests calling an agent directly) are unaffected.
_active_budget: contextvars.ContextVar[Optional[LLMCallBudget]] = contextvars.ContextVar(
    "strategy_lab_design_budget", default=None
)


@contextmanager
def use_budget(budget: Optional[LLMCallBudget]) -> Iterator[None]:
    """Bind ``budget`` as the active design-phase budget for the duration.

    Preconditions:
      Called once per cycle around the whole design phase (all re-entries).
    Postconditions:
      Within the ``with`` block ``charge_active_budget`` charges ``budget``;
      the prior binding is restored on exit even if the block raises.
    """
    token = _active_budget.set(budget)
    try:
        yield
    finally:
        _active_budget.reset(token)


def active_budget() -> Optional[LLMCallBudget]:
    """Return the budget bound by the enclosing :func:`use_budget`, or ``None``.

    Postconditions: pure read — never raises, never mutates.
    """
    return _active_budget.get()


def charge_active_budget() -> None:
    """Charge the active budget for one imminent LLM call, if one is bound.

    Single chokepoint for design-phase charging: every model invocation
    calls this immediately before the call. A no-op when no budget is bound.

    Postconditions:
      When a budget is bound, behaves exactly like :meth:`LLMCallBudget.charge`
      (increments, or raises :class:`DesignBudgetExhausted` when spent). When
      none is bound, returns without effect.
    """
    budget = _active_budget.get()
    if budget is not None:
        budget.charge()
