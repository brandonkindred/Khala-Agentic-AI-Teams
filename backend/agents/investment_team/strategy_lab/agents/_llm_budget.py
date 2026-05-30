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
"""

from __future__ import annotations


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
