"""``StrategyLabBudgetConfig`` — one validated object for every Strategy Lab
retry/round-cap/budget knob.

Today these knobs are each resolved by an ad-hoc ``env_int``/``env_float``
call scattered across ``orchestrator.py``, ``orchestrator_alignment.py``,
``orchestrator_design.py``, ``agents/design.py``, ``agents/refinement.py``,
``agents/alignment.py``, ``agents/_llm_envelope.py``, and
``quality_gates/predicate_conformance.py`` — each site re-implements its own
default/floor/fallback logic, so nothing can look at "the current retry/round
budget" as a whole. :class:`StrategyLabBudgetConfig` gathers those knobs into
a single validated value object; :meth:`StrategyLabBudgetConfig.from_env`
resolves every field with the exact env var names, default values, and
floors the existing call sites use today, so building this object changes no
observable behavior on its own.

Every ``STRATEGY_LAB_*`` env var name and default is preserved for backward
compatibility. Wiring individual call sites onto this object, and computing
the worst-case per-cycle LLM-call count from it, are follow-on changes — this
module only introduces the validated config object.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from llm_service.backoff import parse_rate_limit_retry_config
from llm_service.config import resolve_timeout
from shared.env_config import env_float, env_int

# Last-resort finite fallbacks for platform helpers (`resolve_timeout`,
# `parse_rate_limit_retry_config`) that can themselves return a non-finite
# value when the *generic* env var they read (``LLM_TIMEOUT``,
# ``LLM_RATE_LIMIT_BACKOFF_INITIAL``/``_MAX``) is set to ``"inf"``/``"nan"``
# — neither helper's own env parsing guards against a non-finite *parsed*
# value the way ``shared.env_config.env_float`` does. Mirror each field's
# own dataclass default so a malformed generic var degrades to the same
# value an unset one would.
_DEFAULT_LLM_TIMEOUT_S = 3600.0
_DEFAULT_RATE_LIMIT_BACKOFF_INITIAL_S = 30.0
_DEFAULT_RATE_LIMIT_BACKOFF_MAX_S = 120.0
# The all-defaults derivation ((2 retries + 1) * 3600s timeout * 1.5) — the
# last-resort fallback when deriving the total budget from a resolved
# retries/timeout pair overflows (see ``from_env()``).
_DEFAULT_TOTAL_BUDGET_S = (2 + 1) * _DEFAULT_LLM_TIMEOUT_S * 1.5


def _finite_or(value: float, fallback: float) -> float:
    """Return ``value`` if finite, else ``fallback``."""
    return value if math.isfinite(value) else fallback


def _derive_total_budget_s(max_retries: int, timeout_s: float, fallback: float) -> float:
    """Compute ``(max_retries + 1) * timeout_s * 1.5``, sanitized to a finite
    result.

    An astronomically large ``max_retries`` (unbounded — ``env_int`` applies
    no ceiling) raises ``OverflowError`` converting it to ``float`` for the
    multiplication; a very large but individually-finite ``timeout_s`` can
    instead overflow the *product* to ``inf`` without raising. Both collapse
    to ``fallback`` so callers never have to handle two different failure
    shapes for "the derivation didn't produce a usable number".

    Preconditions: ``timeout_s`` is finite; ``fallback`` is finite and > 0.
    Postconditions: returns a finite float.
    """
    try:
        budget = (max_retries + 1) * timeout_s * 1.5
    except OverflowError:
        return fallback
    return _finite_or(budget, fallback)


def _require_at_least(field_name: str, value: float, floor: float) -> None:
    """Raise ``ValueError`` naming ``field_name`` when ``value`` is a
    ``bool``, non-finite, or ``< floor``.

    A ``nan`` fails every ordering comparison (``nan < floor`` is ``False``),
    so a bare ``<`` check alone would silently accept it; ``from_env()``
    already can't produce one (``env_float`` rejects non-finite values), but
    a direct caller passing ``float("nan")``/``float("inf")`` must not slip
    past a "validated" config. A ``bool`` is-a ``int`` is-a valid operand for
    both ``math.isfinite`` and ``<`` (``True`` reads as a "finite" ``1.0``),
    so it must be rejected explicitly before either check — otherwise
    ``llm_timeout_s=True`` would silently become a one-second timeout.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a float, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite, got {value!r}")
    if value < floor:
        raise ValueError(f"{field_name} must be >= {floor}, got {value!r}")


def _require_int_at_least(field_name: str, value: int, floor: int) -> None:
    """Raise ``ValueError`` naming ``field_name`` when ``value`` is not a
    plain, non-boolean ``int`` ``>= floor``.

    ``from_env()`` only ever produces plain ``int``s here (``env_int``
    returns ``int(raw.strip())``), but a direct caller could pass a float
    (``design_review_rounds=1.5``) that would silently misbehave wherever
    this later feeds an integer-only operation (e.g. ``range()``), or a
    ``bool`` (a ``bool`` is-a ``int`` in Python, but ``True``/``False`` are
    never a meaningful round/retry count).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an int, got {value!r}")
    if value < floor:
        raise ValueError(f"{field_name} must be >= {floor}, got {value!r}")


@dataclass(frozen=True)
class StrategyLabBudgetConfig:
    """Validated snapshot of every Strategy Lab retry/round-cap/budget knob.

    Each field mirrors one ``STRATEGY_LAB_*`` env var (name unchanged, default
    unchanged) currently read ad hoc at its own call site:

    - ``alignment_retries`` — ``STRATEGY_LAB_ALIGNMENT_RETRIES`` (default ``2``).
      Envelope retries for the alignment fix-proposer.
    - ``llm_max_retries`` — ``STRATEGY_LAB_LLM_MAX_RETRIES`` (falls back to
      ``LLM_MAX_RETRIES``, else ``2``). Envelope retries for a retriable
      transport failure on any Strategy Lab LLM call.
    - ``llm_timeout_s`` — ``STRATEGY_LAB_LLM_TIMEOUT`` (falls back to the
      platform ``resolve_timeout()``). Per-call wall-clock timeout.
    - ``llm_backoff_base_s`` / ``llm_backoff_max_s`` —
      ``STRATEGY_LAB_LLM_BACKOFF_BASE`` / ``STRATEGY_LAB_LLM_BACKOFF_MAX``
      (fall back to ``LLM_BACKOFF_BASE``/``LLM_BACKOFF_MAX``, else
      ``2.0``/``60.0``; ``from_env()`` floors the max at the base, same as the
      rate-limit pair below). Jittered exponential backoff between envelope
      retries.
    - ``llm_rate_limit_backoff_initial_s`` / ``llm_rate_limit_backoff_max_s`` —
      ``STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL`` /
      ``STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_MAX`` (fall back to the platform
      429 schedule). Slow backoff schedule applied on a rate-limit response.
    - ``llm_total_budget_s`` — ``STRATEGY_LAB_LLM_TOTAL_BUDGET`` (``None``
      derives ``(llm_max_retries + 1) * llm_timeout_s * 1.5`` in
      ``__post_init__``, from *this instance's* — not the field defaults' —
      retries/timeout, so a direct-construction override of either still
      gets a sized budget instead of silently keeping the all-defaults
      value). Hard wall-time cap across all attempts of a single envelope call.
    - ``design_review_rounds`` — ``STRATEGY_LAB_DESIGN_REVIEW_ROUNDS``
      (default ``20``). Cap on the design <-> design-review loop.
    - ``design_review_stall_rounds`` — ``STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS``
      (default ``3``). Consecutive-unchanged-round stall threshold for that loop.
    - ``design_parse_retries`` — ``STRATEGY_LAB_DESIGN_PARSE_RETRIES``
      (default ``2``). Re-prompt budget when a design response fails DSL validation.
    - ``design_self_revision_rounds`` — ``STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS``
      (default ``1``). Cap on internal design self-revision rounds.
    - ``design_max_llm_calls`` — ``STRATEGY_LAB_DESIGN_MAX_LLM_CALLS``
      (default ``120``). Per-cycle hard cap on total design-phase LLM calls.
    - ``refinement_parse_retries`` — ``STRATEGY_LAB_REFINEMENT_PARSE_RETRIES``
      (default ``2``). Re-prompt budget when a refinement response carries no
      recoverable JSON object.
    - ``refinement_stall_rounds`` — ``STRATEGY_LAB_REFINEMENT_STALL_ROUNDS``
      (default ``3``). Stall threshold for the code-refinement loop.
    - ``max_code_refinement_rounds`` — ``STRATEGY_LAB_MAX_CODE_REFINEMENT_ROUNDS``
      (default ``50``). Cap on the code-refinement loop.
    - ``code_conformance_retries`` — ``STRATEGY_LAB_CODE_CONFORMANCE_RETRIES``
      (default ``2``). Predicate-conformance gate retries before demoting
      criticals to warnings.
    - ``max_alignment_rounds`` — ``STRATEGY_LAB_MAX_ALIGNMENT_ROUNDS``
      (default ``10``). Cap on the trade-alignment audit/fix loop.

    Invariants:
        Every "retries"/"rounds"/"calls" cap field is a plain, non-boolean
        ``int`` (retries ``>= 0``, rounds/calls caps ``>= 1``); every
        timeout/backoff/budget field is a non-boolean, finite float ``> 0``,
        and each *max*/*cap* field is ``>=`` its paired *base*/*initial*
        (backoff max vs. base, rate-limit max vs. initial) — ``bool``
        (``True``/``False`` read as a "finite" ``1.0``/``0.0``) and
        ``nan``/``inf`` (which a bare ``<`` comparison would let slip
        through) are both rejected explicitly. Enforced in
        ``__post_init__`` regardless of construction path (``from_env`` or
        direct instantiation), so a caller building this object with
        explicit overrides — tests included — gets the same
        guarantees as the env-driven default.
    """

    alignment_retries: int = 2
    llm_max_retries: int = 2
    llm_timeout_s: float = _DEFAULT_LLM_TIMEOUT_S
    llm_backoff_base_s: float = 2.0
    llm_backoff_max_s: float = 60.0
    llm_rate_limit_backoff_initial_s: float = _DEFAULT_RATE_LIMIT_BACKOFF_INITIAL_S
    llm_rate_limit_backoff_max_s: float = _DEFAULT_RATE_LIMIT_BACKOFF_MAX_S
    llm_total_budget_s: Optional[float] = None
    design_review_rounds: int = 20
    design_review_stall_rounds: int = 3
    design_parse_retries: int = 2
    design_self_revision_rounds: int = 1
    design_max_llm_calls: int = 120
    refinement_parse_retries: int = 2
    refinement_stall_rounds: int = 3
    max_code_refinement_rounds: int = 50
    code_conformance_retries: int = 2
    max_alignment_rounds: int = 10

    def __post_init__(self) -> None:
        """Validate every field's invariant.

        Preconditions: none — runs on every construction path.
        Postconditions: returns normally iff every field satisfies its
        documented invariant; otherwise raises ``ValueError`` naming the
        offending field. When constructed with ``llm_total_budget_s=None``
        (the field default), it is replaced with
        ``(llm_max_retries + 1) * llm_timeout_s * 1.5`` computed from *this
        instance's* (already-validated) retries/timeout before validation —
        so a direct-construction override of either still yields a budget
        sized to it, matching ``from_env()``'s derivation. A retries/timeout
        combination whose product overflows raises ``ValueError`` naming
        ``llm_total_budget_s`` rather than leaking a raw ``OverflowError``.
        """
        _require_int_at_least("alignment_retries", self.alignment_retries, 0)
        _require_int_at_least("llm_max_retries", self.llm_max_retries, 0)
        _require_at_least("llm_timeout_s", self.llm_timeout_s, 0.001)
        _require_at_least("llm_backoff_base_s", self.llm_backoff_base_s, 1.0)
        _require_at_least("llm_backoff_max_s", self.llm_backoff_max_s, self.llm_backoff_base_s)
        _require_at_least(
            "llm_rate_limit_backoff_initial_s", self.llm_rate_limit_backoff_initial_s, 1.0
        )
        _require_at_least(
            "llm_rate_limit_backoff_max_s",
            self.llm_rate_limit_backoff_max_s,
            self.llm_rate_limit_backoff_initial_s,
        )
        if self.llm_total_budget_s is None:
            try:
                derived_budget = (self.llm_max_retries + 1) * self.llm_timeout_s * 1.5
            except OverflowError as exc:
                raise ValueError(
                    f"llm_total_budget_s could not be derived from "
                    f"llm_max_retries={self.llm_max_retries!r} and "
                    f"llm_timeout_s={self.llm_timeout_s!r}: {exc}"
                ) from exc
            object.__setattr__(self, "llm_total_budget_s", derived_budget)
        _require_at_least("llm_total_budget_s", self.llm_total_budget_s, 0.001)
        _require_int_at_least("design_review_rounds", self.design_review_rounds, 1)
        _require_int_at_least("design_review_stall_rounds", self.design_review_stall_rounds, 1)
        _require_int_at_least("design_parse_retries", self.design_parse_retries, 0)
        _require_int_at_least("design_self_revision_rounds", self.design_self_revision_rounds, 0)
        _require_int_at_least("design_max_llm_calls", self.design_max_llm_calls, 1)
        _require_int_at_least("refinement_parse_retries", self.refinement_parse_retries, 0)
        _require_int_at_least("refinement_stall_rounds", self.refinement_stall_rounds, 1)
        _require_int_at_least("max_code_refinement_rounds", self.max_code_refinement_rounds, 1)
        _require_int_at_least("code_conformance_retries", self.code_conformance_retries, 0)
        _require_int_at_least("max_alignment_rounds", self.max_alignment_rounds, 1)

    @classmethod
    def from_env(cls) -> "StrategyLabBudgetConfig":
        """Resolve every field from its ``STRATEGY_LAB_*`` env var.

        Mirrors, field for field, the resolution already performed ad hoc at
        each knob's existing call site (same env var name, same fallback
        cascade, same default, same floor) — this constructor changes no
        default. Garbage/unset env values fall back exactly as they do today
        (``shared.env_config.env_int``/``env_float`` never raise for a bad
        environment *value*); only a floor violation on the *resolved* value
        would raise, and every floor here matches what the resolved value
        already satisfies today, so ``from_env()`` cannot raise in practice.

        Postconditions: returns a fully validated ``StrategyLabBudgetConfig``.
        """
        alignment_retries = env_int("STRATEGY_LAB_ALIGNMENT_RETRIES", 2, floor=0)

        # Each generic ``LLM_*``/platform fallback is clamped to the floor
        # *before* being handed to the outer ``env_*`` call as its ``default``:
        # ``env_int``/``env_float`` reject a default that already violates the
        # supplied floor (a caller-contract check, raised even when the
        # STRATEGY_LAB_* override is absent or valid, since the default
        # argument is evaluated eagerly). A sub-floor generic fallback (e.g.
        # ``LLM_MAX_RETRIES=-1``) would otherwise turn a merely-unset
        # STRATEGY_LAB_* var into a ``ValueError`` instead of the documented
        # never-raises resolution.
        generic_max_retries = max(0, env_int("LLM_MAX_RETRIES", 2))
        llm_max_retries = env_int("STRATEGY_LAB_LLM_MAX_RETRIES", generic_max_retries, floor=0)
        # ``resolve_timeout()`` returns its parsed ``LLM_TIMEOUT`` value
        # unchanged when positive — including ``inf`` — so a non-finite
        # result is sanitized before use, the same as the rate-limit
        # fallbacks below.
        platform_timeout = max(0.001, _finite_or(resolve_timeout(), _DEFAULT_LLM_TIMEOUT_S))
        llm_timeout_s = env_float("STRATEGY_LAB_LLM_TIMEOUT", platform_timeout, floor=0.001)
        generic_backoff_base = max(1.0, env_float("LLM_BACKOFF_BASE", 2.0))
        llm_backoff_base_s = env_float(
            "STRATEGY_LAB_LLM_BACKOFF_BASE", generic_backoff_base, floor=1.0
        )
        # Floor the max at the resolved base — same shape as the rate-limit
        # initial/max pair below — so a misconfigured max below the base can
        # never leave the schedule capped under its own starting delay.
        generic_backoff_max = max(llm_backoff_base_s, env_float("LLM_BACKOFF_MAX", 60.0))
        llm_backoff_max_s = env_float(
            "STRATEGY_LAB_LLM_BACKOFF_MAX", generic_backoff_max, floor=llm_backoff_base_s
        )

        # ``parse_rate_limit_retry_config`` parses its env vars with plain
        # ``float()`` + a ``<= 0`` guard, which "inf"/"nan" both pass (neither
        # is ``<= 0``) — so a malformed ``LLM_RATE_LIMIT_BACKOFF_INITIAL``/
        # ``_MAX`` can hand back a non-finite value here. Sanitize before use.
        _, global_rl_initial, global_rl_cap = parse_rate_limit_retry_config()
        global_rl_initial = max(
            1.0, _finite_or(global_rl_initial, _DEFAULT_RATE_LIMIT_BACKOFF_INITIAL_S)
        )
        llm_rate_limit_backoff_initial_s = env_float(
            "STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL", global_rl_initial, floor=1.0
        )
        global_rl_cap = max(
            llm_rate_limit_backoff_initial_s,
            _finite_or(global_rl_cap, _DEFAULT_RATE_LIMIT_BACKOFF_MAX_S),
        )
        llm_rate_limit_backoff_max_s = env_float(
            "STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_MAX",
            global_rl_cap,
            floor=llm_rate_limit_backoff_initial_s,
        )

        # An unbounded llm_max_retries (env_int applies no ceiling) can raise
        # OverflowError converting it to float here, and a merely-huge-but-
        # finite llm_timeout_s can instead overflow the product to `inf`
        # without raising — both are sanitized to a finite fallback.
        default_total_budget_s = _derive_total_budget_s(
            llm_max_retries, llm_timeout_s, _DEFAULT_TOTAL_BUDGET_S
        )
        llm_total_budget_s = env_float(
            "STRATEGY_LAB_LLM_TOTAL_BUDGET", default_total_budget_s, floor=0.001
        )

        design_review_rounds = env_int("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", 20, floor=1)
        design_review_stall_rounds = env_int("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", 3, floor=1)
        design_parse_retries = env_int("STRATEGY_LAB_DESIGN_PARSE_RETRIES", 2, floor=0)
        design_self_revision_rounds = env_int(
            "STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS", 1, floor=0
        )
        design_max_llm_calls = env_int("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", 120, floor=1)

        refinement_parse_retries = env_int("STRATEGY_LAB_REFINEMENT_PARSE_RETRIES", 2, floor=0)
        refinement_stall_rounds = env_int("STRATEGY_LAB_REFINEMENT_STALL_ROUNDS", 3, floor=1)
        max_code_refinement_rounds = env_int("STRATEGY_LAB_MAX_CODE_REFINEMENT_ROUNDS", 50, floor=1)
        code_conformance_retries = env_int("STRATEGY_LAB_CODE_CONFORMANCE_RETRIES", 2, floor=0)
        max_alignment_rounds = env_int("STRATEGY_LAB_MAX_ALIGNMENT_ROUNDS", 10, floor=1)

        return cls(
            alignment_retries=alignment_retries,
            llm_max_retries=llm_max_retries,
            llm_timeout_s=llm_timeout_s,
            llm_backoff_base_s=llm_backoff_base_s,
            llm_backoff_max_s=llm_backoff_max_s,
            llm_rate_limit_backoff_initial_s=llm_rate_limit_backoff_initial_s,
            llm_rate_limit_backoff_max_s=llm_rate_limit_backoff_max_s,
            llm_total_budget_s=llm_total_budget_s,
            design_review_rounds=design_review_rounds,
            design_review_stall_rounds=design_review_stall_rounds,
            design_parse_retries=design_parse_retries,
            design_self_revision_rounds=design_self_revision_rounds,
            design_max_llm_calls=design_max_llm_calls,
            refinement_parse_retries=refinement_parse_retries,
            refinement_stall_rounds=refinement_stall_rounds,
            max_code_refinement_rounds=max_code_refinement_rounds,
            code_conformance_retries=code_conformance_retries,
            max_alignment_rounds=max_alignment_rounds,
        )


__all__ = ["StrategyLabBudgetConfig"]
