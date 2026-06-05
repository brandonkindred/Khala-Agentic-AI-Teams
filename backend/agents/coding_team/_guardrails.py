"""Cost guardrails for the coding-team swarm.

The swarm loop fans a single job into many LLM round-trips: a coordinator
(Tech Lead) assignment + review call every round, plus one multi-turn
implementation session per worker, plus per-task quality gates. Left
unbounded, a non-converging job (e.g. a review that never approves, or a
worker re-implementing the same task every round) burns cloud spend until a
flat round ceiling is hit, and historically still reported success. This
module supplies three deterministic guardrails:

1. **Per-job LLM-call budget** — :class:`CodingTeamLLMBudget`, charged once
   before every logical model call through the :class:`_BudgetedClient`
   wrapper. When the cap trips the next charge raises
   :class:`CodingTeamBudgetExhausted` and the orchestrator short-circuits
   with ``status="failed: budget_exhausted"``. No further model calls run.

2. **Task-count-scaled round ceiling** — :func:`max_rounds_for` replaces the
   old flat 500 with ``max(min_rounds, multiplier * num_tasks)`` so the cap
   tracks the actual amount of work, and exhausting it is an explicit
   ``failed: max_rounds_exhausted`` (never ``completed`` with zero merges).

3. **Per-role thinking levels** — :func:`role_think` / :func:`mechanical_think_value`
   drop the mechanical agents (assignment, lint, code-review gates) to no
   reasoning while implementation keeps the configured high level.

The module is stdlib-only and imports nothing from ``coding_team`` so it can
be imported by the orchestrator without an import cycle.
"""

from __future__ import annotations

import contextvars
import logging
import os
from contextlib import contextmanager
from typing import Iterator, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# Agent keys whose calls are mechanical (structured JSON, no genuine
# reasoning needed): the Tech Lead's per-round assignment and the post-
# implementation lint / code-review gates. Implementation ("coding_team")
# and planning ("tech_lead" plan) are intentionally absent — they keep the
# configured high thinking level.
MECHANICAL_AGENT_KEYS = frozenset({"linting_tool_agent", "code_review"})

ThinkValue = Union[bool, str, None]


# ---------------------------------------------------------------------------
# Environment parsing
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int, *, floor: int = 1) -> int:
    """Read env var ``name`` as an int, flooring and falling back on garbage.

    Preconditions:
      ``default >= floor`` (the documented defaults all satisfy this).
    Postconditions:
      Returns an int ``>= floor``. An unset/blank/non-integer value yields
      ``default``; a parseable value below ``floor`` is raised to ``floor``.
      Never raises.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; falling back to default %d", name, raw, default)
        return default
    return max(value, floor)


def max_llm_calls_from_env() -> int:
    """Per-job LLM-call budget ceiling.

    Reads ``CODING_TEAM_MAX_LLM_CALLS`` (default 300, floor 1, garbage →
    default). Postcondition: returns an int ``>= 1``.
    """
    return _env_int("CODING_TEAM_MAX_LLM_CALLS", 300, floor=1)


def round_multiplier_from_env() -> int:
    """Rounds-per-task multiplier for the swarm ceiling.

    Reads ``CODING_TEAM_ROUND_MULTIPLIER`` (default 3, floor 1, garbage →
    default). Postcondition: returns an int ``>= 1``.
    """
    return _env_int("CODING_TEAM_ROUND_MULTIPLIER", 3, floor=1)


def min_rounds_from_env() -> int:
    """Floor on the swarm round ceiling regardless of task count.

    Reads ``CODING_TEAM_MIN_ROUNDS`` (default 10, floor 1, garbage →
    default). Postcondition: returns an int ``>= 1``.
    """
    return _env_int("CODING_TEAM_MIN_ROUNDS", 10, floor=1)


def max_rounds_for(num_tasks: int) -> int:
    """Derive the swarm round ceiling from the task count.

    Preconditions:
      ``num_tasks >= 0``.
    Postconditions:
      Returns ``max(min_rounds, multiplier * num_tasks)`` — always ``>= 1``
      because ``min_rounds >= 1``. Scales the ceiling with the real amount of
      work instead of a flat constant, while never dropping below the floor.
    """
    assert num_tasks >= 0, "num_tasks must be non-negative"
    return max(min_rounds_from_env(), round_multiplier_from_env() * num_tasks)


def mechanical_think_value() -> ThinkValue:
    """Thinking value applied to mechanical agents.

    Postconditions:
      Returns ``False`` (no reasoning) by default. When
      ``CODING_TEAM_THINKING_MECHANICAL`` is truthy (``1``/``true``/``yes``,
      case-insensitive) returns ``None`` instead, which resolves to the
      platform default thinking level — i.e. the operator override re-enables
      reasoning on the mechanical agents. Never raises.
    """
    raw = (os.environ.get("CODING_TEAM_THINKING_MECHANICAL") or "").strip().lower()
    if raw in ("1", "true", "yes"):
        return None
    return False


def role_think(agent_key: Optional[str]) -> ThinkValue:
    """Resolve the per-call thinking value for ``agent_key``.

    Preconditions:
      ``agent_key`` is the model-routing key the agent is built with.
    Postconditions:
      Returns :func:`mechanical_think_value` for keys in
      :data:`MECHANICAL_AGENT_KEYS`, else ``None`` (platform default — the
      generative implementation / planning agents keep their high level).
    """
    if agent_key in MECHANICAL_AGENT_KEYS:
        return mechanical_think_value()
    return None


# ---------------------------------------------------------------------------
# Per-job LLM-call budget
# ---------------------------------------------------------------------------


class CodingTeamBudgetExhausted(BaseException):
    """Raised by :meth:`CodingTeamLLMBudget.charge` when the budget is spent.

    Deliberately a :class:`BaseException`, not :class:`Exception`: the budget
    is charged deep inside agent methods (Senior SWE ``run_implement``, Tech
    Lead calls, quality gates) that wrap their model calls in broad
    ``except Exception`` handlers. A plain ``Exception`` would be swallowed
    there and the swarm would keep spinning; subclassing ``BaseException``
    lets the trip propagate straight through those handlers to the
    orchestrator's explicit ``except CodingTeamBudgetExhausted``, which maps
    it to ``status="failed: budget_exhausted"``.

    Preconditions:
      ``limit >= 1`` and ``calls_made >= 0`` (maintained by
      :class:`CodingTeamLLMBudget`).
    Postconditions:
      ``self.limit`` / ``self.calls_made`` expose the trip context; the
      message names the controlling env var.
    """

    def __init__(self, limit: int, calls_made: int) -> None:
        self.limit = limit
        self.calls_made = calls_made
        super().__init__(
            f"coding-team LLM-call budget exhausted: {calls_made} call(s) made, "
            f"limit {limit} (raise CODING_TEAM_MAX_LLM_CALLS to allow more)"
        )


class CodingTeamLLMBudget:
    """Counter for the logical LLM calls a single coding-team job may make.

    Created once per ``run_coding_team_orchestrator`` invocation and bound for
    the whole job via :func:`use_budget`; every model round-trip charges it
    through :func:`charge_active_budget` (driven by :class:`_BudgetedClient`).
    A transport-level retry inside one ``chat`` call is a single logical call
    and charges once — the cap counts work requested, not wire attempts.

    Invariants:
      * ``0 <= calls_made <= limit`` at all times.
      * Exactly ``limit`` charges succeed; the ``(limit + 1)``-th raises.
    """

    def __init__(self, limit: int) -> None:
        """Construct a budget admitting exactly ``limit`` charges.

        Preconditions:
          ``limit >= 1`` — callers resolve this from
          :func:`max_llm_calls_from_env`, which floors sub-1 values to 1.
        Postconditions:
          ``self.calls_made == 0`` and ``self.limit == limit``.
        """
        assert limit >= 1, "CodingTeamLLMBudget limit must be >= 1"
        self.limit = limit
        self.calls_made = 0

    def charge(self) -> None:
        """Account for one imminent LLM call, or refuse when the budget is spent.

        Pre-charge / check-then-increment: called immediately *before* each
        model call so the job stops before exceeding the documented budget.

        Preconditions:
          Called once per imminent LLM call.
        Postconditions:
          On success ``calls_made`` is incremented by exactly 1. When
          ``calls_made >= limit`` raises :class:`CodingTeamBudgetExhausted`
          without incrementing and without the call running — so exactly
          ``limit`` charges ever succeed.
        """
        if self.calls_made >= self.limit:
            raise CodingTeamBudgetExhausted(self.limit, self.calls_made)
        self.calls_made += 1


_active_budget: contextvars.ContextVar[Optional[CodingTeamLLMBudget]] = contextvars.ContextVar(
    "coding_team_llm_budget", default=None
)


@contextmanager
def use_budget(budget: Optional[CodingTeamLLMBudget]) -> Iterator[None]:
    """Bind ``budget`` as the active per-job budget for the duration.

    Preconditions:
      Called once per job around the whole LLM-using region.
    Postconditions:
      Within the ``with`` block :func:`charge_active_budget` charges
      ``budget``; the prior binding is restored on exit even if the block
      raises. ``None`` disables charging (the default for code paths — e.g.
      unit tests — that invoke an agent outside a job).
    """
    token = _active_budget.set(budget)
    try:
        yield
    finally:
        _active_budget.reset(token)


def active_budget() -> Optional[CodingTeamLLMBudget]:
    """Return the budget bound by the enclosing :func:`use_budget`, or ``None``.

    Postconditions: pure read — never raises, never mutates.
    """
    return _active_budget.get()


def charge_active_budget() -> None:
    """Charge the active budget for one imminent LLM call, if one is bound.

    Single chokepoint for charging. A no-op when no budget is bound.

    Postconditions:
      When a budget is bound, behaves exactly like
      :meth:`CodingTeamLLMBudget.charge`. When none is bound, returns without
      effect.
    """
    budget = _active_budget.get()
    if budget is not None:
        budget.charge()


class _BudgetedClient:
    """Duck-typed ``LLMClient`` wrapper that charges the active budget per call.

    Wraps the backing ``LLMClient`` so every logical model entry point
    (``chat`` for the Strands streaming path, ``complete_json`` /
    ``complete`` / ``complete_text`` for direct callers) charges the budget
    *before* delegating. Non-call attributes (``model``,
    ``get_max_context_tokens``, …) pass straight through, so the wrapper is a
    transparent stand-in everywhere ``LLMClientModel`` touches its client.

    Invariants:
      * Each wrapped method charges exactly once per invocation, before the
        inner call runs — so a charge that raises
        :class:`CodingTeamBudgetExhausted` guarantees the inner call did not
        execute (no spend past the cap).
    """

    def __init__(self, inner: object) -> None:
        """Preconditions: ``inner`` exposes the LLMClient call methods used
        by the Strands adapter. Postconditions: attribute access not
        overridden here delegates to ``inner``."""
        assert inner is not None, "inner client is required"
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        # Only reached for attributes not defined on the wrapper itself
        # (``_inner`` is a real instance attribute, so this never recurses).
        return getattr(self._inner, name)

    def chat(self, *args: object, **kwargs: object) -> object:
        charge_active_budget()
        return self._inner.chat(*args, **kwargs)

    def complete_json(self, *args: object, **kwargs: object) -> object:
        charge_active_budget()
        return self._inner.complete_json(*args, **kwargs)

    def complete(self, *args: object, **kwargs: object) -> object:
        charge_active_budget()
        return self._inner.complete(*args, **kwargs)

    def complete_text(self, *args: object, **kwargs: object) -> object:
        charge_active_budget()
        return self._inner.complete_text(*args, **kwargs)


# ---------------------------------------------------------------------------
# Terminal-status decision
# ---------------------------------------------------------------------------


def terminal_status(
    run_reason: str, merged_count: int, max_rounds: int
) -> Optional[Tuple[str, str]]:
    """Map a swarm outcome to the job's terminal ``(status, status_text)``.

    Replaces the old unconditional ``status="completed"`` so a swarm that
    merged nothing can never be reported as a success.

    Preconditions:
      ``run_reason`` is one of ``"complete"``, ``"max_rounds_exhausted"``,
      ``"cancelled"``; ``merged_count >= 0``; ``max_rounds >= 1``.
    Postconditions:
      * ``"cancelled"`` → ``None`` (the swarm already set the cancelled
        status; the caller must not override it).
      * ``"max_rounds_exhausted"`` → ``("failed: max_rounds_exhausted", …)``.
      * ``"complete"`` with ``merged_count == 0`` →
        ``("failed: no_tasks_merged", …)``.
      * ``"complete"`` with ``merged_count > 0`` → ``("completed", …)``.
    """
    assert merged_count >= 0, "merged_count must be non-negative"
    if run_reason == "cancelled":
        return None
    if run_reason == "max_rounds_exhausted":
        return (
            "failed: max_rounds_exhausted",
            f"Swarm exhausted its {max_rounds}-round ceiling with {merged_count} task(s) merged",
        )
    if merged_count == 0:
        return (
            "failed: no_tasks_merged",
            "Swarm finished but produced no merged tasks",
        )
    return ("completed", f"Completed: {merged_count} tasks merged")
