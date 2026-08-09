"""Unified fault-tolerance envelope for every Strategy Lab LLM call.

The Strategy Lab agents invoke ``strands.Agent`` directly and synchronously
(``result = agent(prompt)``). Those calls had no consistent timeout, no
backoff, and no retriable-vs-fatal distinction — a transient transport fault
looked identical to a clean response, which in a trading-strategy pipeline is
the failure mode that ships a broken strategy.

:func:`invoke_agent` is the single chokepoint every strategy-lab LLM call now
routes through. It provides:

  * a per-call wall-clock timeout (a daemon-thread guard; see below),
  * bounded jittered exponential backoff between attempts,
  * a distinct retriable-vs-fatal classification of the raised exception,
  * a bounded total wall-time budget across all attempts,
  * a structured ``logger.exception`` on every failed attempt carrying
    ``agent / phase / attempt / latency_ms / error_class``.

It reuses the platform's exception hierarchy (``llm_service.interface``) and
mirrors the backoff formula used by ``llm_service.util.call_llm_with_retries``.
It deliberately does NOT delegate to ``call_llm_with_retries`` because that
helper has neither a per-call timeout nor a total-budget, and its bare
``except Exception`` clause retries fatal 4xx/auth errors — both of which this
envelope must not do.

Timeout semantics (layered, by design):
  The blocking ``agent(prompt)`` call cannot be cancelled from Python without
  killing its thread. The *primary* timeout that actually frees the socket is
  configured on the underlying model client in ``model_factory.get_strands_model``.
  This envelope adds a *secondary* wall-clock guard by running the call on a
  daemon thread and joining with a timeout. On timeout the worker thread keeps
  running (a leak) until the primary transport timeout fires — the daemon flag
  keeps it from blocking interpreter shutdown, and the transport timeout bounds
  the leak. Relying on the guard alone would leak unboundedly under a hung
  endpoint, so both layers are required.

This module imports only stdlib + ``llm_service`` (no ``strategy_lab`` imports),
with two narrow carve-outs: ``._llm_budget`` for ``charge_active_budget``, and
``..budget_config`` for ``StrategyLabBudgetConfig`` (the retry/timeout/backoff
default resolution below). Both sibling modules are themselves leaves — neither
imports anything else from ``strategy_lab`` — so the carve-outs introduce no
dependency on the rest of the package and cannot create an import cycle with
the agents that consume this module.
"""

from __future__ import annotations

import contextvars
import logging
import random
import threading
import time
from typing import Any, Callable, Optional

import httpx

from llm_service.backoff import rate_limit_retry_delay
from llm_service.interface import (
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMSchemaValidationError,
    LLMSemanticExhaustionError,
    LLMTemporaryError,
)
from shared.env_config import env_float

from ..budget_config import StrategyLabBudgetConfig
from ..exceptions import StrategyLabLLMError
from ._llm_budget import DesignBudgetExhausted, charge_active_budget

_module_logger = logging.getLogger(__name__)

# Canonical structured-failure message. Every fail-closed site (envelope and
# the near-miss adjudicator guard) emits these same five fields so operators
# can grep one schema across the whole lab.
_FAILURE_FMT = (
    "strategy_lab LLM call failed: agent=%s phase=%s attempt=%s/%s latency_ms=%d error_class=%s"
)


class _EnvelopeTimeout(Exception):
    """Raised internally when the per-call wall-clock guard trips.

    Classified RETRIABLE — a stuck call is treated like any other transient
    transport fault. Never escapes the envelope (it is wrapped in
    :class:`StrategyLabLLMError` on exhaustion).
    """

    def __init__(self, timeout_s: float) -> None:
        super().__init__(f"strategy-lab LLM call exceeded {timeout_s:.1f}s wall-clock guard")
        self.timeout_s = timeout_s


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class _EnvelopeConfig:
    """Resolved per-call envelope tunables. Pure value object."""

    __slots__ = (
        "max_attempts",
        "timeout_s",
        "total_budget_s",
        "backoff_base",
        "backoff_max",
        "rl_initial",
        "rl_cap",
    )

    def __init__(
        self,
        *,
        max_attempts: int,
        timeout_s: float,
        total_budget_s: float,
        backoff_base: float,
        backoff_max: float,
        rl_initial: float,
        rl_cap: float,
    ) -> None:
        self.max_attempts = max_attempts
        self.timeout_s = timeout_s
        self.total_budget_s = total_budget_s
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        # Rate-limit (429) backoff schedule — the slow, distinct schedule applied
        # when a failure is a rate limit. The number of rate-limit retries is
        # governed by ``max_attempts`` and the total-budget deadline (the real
        # terminator), not a separate counter; only the per-attempt *delay* comes
        # from this schedule.
        self.rl_initial = rl_initial
        self.rl_cap = rl_cap


def _resolve_config(
    agent_key: str,
    max_attempts: Optional[int],
    timeout_s: Optional[float],
    total_budget_s: Optional[float],
    backoff_base: Optional[float],
    backoff_max: Optional[float],
) -> _EnvelopeConfig:
    """Resolve envelope tunables from explicit args → ``STRATEGY_LAB_LLM_*`` →
    generic ``LLM_*`` → defaults.

    Preconditions: ``agent_key`` is a non-empty model key.
    Postconditions: every field is finite and floored to a safe minimum
    (``max_attempts >= 1``, timeouts/budget ``> 0``). Never raises.
    """
    budget_config = StrategyLabBudgetConfig.from_env()

    if max_attempts is None:
        attempts = budget_config.llm_max_retries + 1
    else:
        attempts = max_attempts
    attempts = max(1, attempts)

    if timeout_s is None:
        timeout_s = budget_config.llm_timeout_s
    timeout_s = max(0.001, float(timeout_s))

    if backoff_base is None:
        backoff_base = budget_config.llm_backoff_base_s
    backoff_base = max(1.0, float(backoff_base))

    if backoff_max is None:
        backoff_max = budget_config.llm_backoff_max_s
    backoff_max = max(0.0, float(backoff_max))

    # total_budget_s is deliberately NOT sourced from
    # ``budget_config.llm_total_budget_s``: that field is derived from the
    # env-resolved ``llm_max_retries``/``llm_timeout_s``, but this default must
    # instead reflect *this call's* post-override ``attempts``/``timeout_s`` —
    # callers routinely pass an explicit ``max_attempts`` (e.g.
    # ``alignment.py``'s ``_alignment_max_attempts()``) or a scaled ``timeout_s``
    # (``_structured_output.py`` doubles it for its two-call envelope) while
    # leaving ``total_budget_s=None``, expecting the budget to scale with that
    # override. Sourcing it from the config's own env-derived retries/timeout
    # would silently ignore the override.
    if total_budget_s is None:
        total_budget_s = env_float("STRATEGY_LAB_LLM_TOTAL_BUDGET", attempts * timeout_s * 1.5)
    total_budget_s = max(0.001, float(total_budget_s))

    # Rate-limit (429) backoff schedule. Neither field has an explicit-arg
    # override, so these are always the config's env-resolved values.
    rl_initial = max(1.0, budget_config.llm_rate_limit_backoff_initial_s)
    # Floor the cap at the initial so rate_limit_retry_delay's precondition
    # (cap >= initial) always holds even under a misconfigured override.
    rl_cap = max(rl_initial, budget_config.llm_rate_limit_backoff_max_s)

    return _EnvelopeConfig(
        max_attempts=attempts,
        timeout_s=timeout_s,
        total_budget_s=total_budget_s,
        backoff_base=backoff_base,
        backoff_max=backoff_max,
        rl_initial=rl_initial,
        rl_cap=rl_cap,
    )


# ---------------------------------------------------------------------------
# Classification + backoff
# ---------------------------------------------------------------------------


_FATAL_MARKERS = (
    "unauthorized",
    "forbidden",
    "notfound",
    "not found",
    "badrequest",
    "bad request",
    "invalid",
    "validation",
    "401",
    "403",
    "404",
    "422",
)
_RETRIABLE_MARKERS = (
    "timeout",
    "timedout",
    "connection",
    "serviceunavailable",
    "service unavailable",
    "throttl",
    "ratelimit",
    "rate limit",
    "overloaded",
    "unavailable",
    "502",
    "503",
    "504",
)


def classify_strands_exception(exc: BaseException) -> bool:
    """Classify ``exc`` as retriable (``True``) or fatal (``False``).

    Conservative policy: retry transient faults (5xx / connection / timeout /
    throttle); do NOT retry obvious client/auth/malformed errors. Unknown
    exceptions default to retriable (bounded by ``max_attempts`` + budget).

    Preconditions: ``exc`` is any raised exception.
    Postconditions: returns a bool — pure, no I/O, never raises.
    """
    # 1. Known permanent / parse / schema failures — re-calling with the same
    #    prompt cannot help.
    if isinstance(exc, (LLMPermanentError, LLMJsonParseError, LLMSchemaValidationError)):
        return False
    # 1b. Semantic exhaustion — the client already proved that this payload
    #     yields no content even after a reduced-thinking retry. It subclasses
    #     LLMTemporaryError for caller compatibility, but macro-retrying the
    #     identical prompt would re-burn the full thinking budget per attempt
    #     with no proof of change, so the envelope treats it as fatal.
    if isinstance(exc, LLMSemanticExhaustionError):
        return False
    # 2. Rate limit — retry with backoff, except a hard weekly cap.
    if isinstance(exc, LLMRateLimitError):
        return OLLAMA_WEEKLY_LIMIT_MESSAGE not in str(exc)
    # 3. Known transient transport types.
    if isinstance(
        exc,
        (
            LLMTemporaryError,
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.ReadTimeout,
            ConnectionError,
            TimeoutError,
            _EnvelopeTimeout,
        ),
    ):
        return True
    # 4. HTTP status code carried on the exception, if any.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in (408, 429) or 500 <= status <= 599:
            return True
        if 400 <= status <= 499:
            return False
    # 5. Heuristics on type name / message for raw provider exceptions we
    #    cannot import. Retriable markers win over fatal markers so a
    #    "ConnectionTimeout"-style name is never misread as fatal.
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if any(m in name or m in msg for m in _RETRIABLE_MARKERS):
        return True
    if any(m in name or m in msg for m in _FATAL_MARKERS):
        return False
    # 6. Unknown — assume transient, bounded by attempts + budget.
    return True


def _is_rate_limit_kind(exc: BaseException) -> bool:
    """Whether ``exc`` is a 429 rate limit that should use the SLOW backoff schedule.

    A weekly-usage cap is deliberately NOT a rate-limit kind: it is fatal (see
    :func:`classify_strands_exception`), so it must never enter the rate-limit
    retry branch.

    Preconditions: ``exc`` is any raised exception.
    Postconditions: returns a bool — pure, no I/O, never raises.
    """
    if OLLAMA_WEEKLY_LIMIT_MESSAGE in str(exc):
        return False
    if isinstance(exc, LLMRateLimitError):
        return True
    if getattr(exc, "status_code", None) == 429:
        return True
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return any(m in name or m in msg for m in ("throttl", "ratelimit", "rate limit"))


def _backoff_delay(attempt: int, base: float, max_: float) -> float:
    """Jittered exponential backoff seconds for a 0-based ``attempt`` index.

    Mirrors ``llm_service.util.call_llm_with_retries``:
    ``min(base**attempt + uniform(0, 1), max_)``.

    Preconditions: ``attempt >= 0``, ``base >= 1``, ``max_ >= 0``.
    Postconditions: returns a value in ``[0, max_]``.
    """
    return min(base**attempt + random.uniform(0, 1), max_)


# ---------------------------------------------------------------------------
# Timeout guard
# ---------------------------------------------------------------------------


def _call_with_timeout(fn: Callable[[], Any], timeout_s: float) -> Any:
    """Run ``fn`` on a daemon thread, joining with ``timeout_s``.

    Preconditions: ``fn`` is a zero-arg callable; ``timeout_s > 0``.
    Postconditions: returns ``fn()`` on success; re-raises any exception ``fn``
    raised; raises :class:`_EnvelopeTimeout` if ``fn`` did not finish within
    ``timeout_s`` (the worker thread is left running as a daemon — bounded by
    the transport-level timeout configured in the model factory).

    The worker runs under a copy of the caller's ``contextvars`` context so
    bindings such as the design-phase LLM budget (``charge_active_budget``)
    remain visible inside ``fn`` — without this, charges made from a
    two-pass ``_call`` closure would silently no-op on the worker thread.
    """
    box: dict[str, Any] = {}
    ctx = contextvars.copy_context()

    def _runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — propagate to caller thread
            box["error"] = exc

    thread = threading.Thread(
        target=ctx.run, args=(_runner,), daemon=True, name="strategy-lab-llm"
    )
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise _EnvelopeTimeout(timeout_s)
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def invoke_agent(
    agent_callable: Callable[[str], Any],
    prompt: str,
    *,
    agent_key: str,
    phase: str,
    max_attempts: Optional[int] = None,
    timeout_s: Optional[float] = None,
    total_budget_s: Optional[float] = None,
    backoff_base: Optional[float] = None,
    backoff_max: Optional[float] = None,
    retriable_classifier: Optional[Callable[[BaseException], bool]] = None,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Invoke a synchronous strands ``Agent`` under the fault-tolerance envelope.

    Preconditions:
      * ``agent_callable`` is the constructed ``strands.Agent`` (called as
        ``agent_callable(prompt)``); the caller is responsible for budget
        charging (``charge_active_budget``) either BEFORE calling this or
        inside ``agent_callable`` itself when that callable owns multiple
        provider calls per attempt (see
        ``_structured_output.invoke_structured_with_schema``, which charges
        inside its retried closure so transport retries re-charge).
        This envelope does not charge on its own.
      * ``agent_key`` / ``phase`` are non-empty diagnostic labels.

    Postconditions:
      * Returns ``str(result)`` on the first successful attempt.
      * Re-raises :class:`~._llm_budget.DesignBudgetExhausted` immediately,
        unmodified — a per-cycle design-budget trip is a cycle-level stop, not
        a transport fault. Callables that charge inside the attempt (e.g.
        ``invoke_structured_with_schema``'s two-pass ``_call``) must still
        surface this to callers that catch it distinctly from
        :class:`StrategyLabLLMError` / fail-closed paths.
      * Raises :class:`StrategyLabLLMError` (an ``LLMTemporaryError`` subclass,
        so existing broad ``except`` clauses keep their fail-closed contract)
        when attempts are exhausted, the total budget is spent, or the
        exception classifies as fatal.
      * Emits one structured ``logger.exception`` per failed attempt and one
        summary ``logger.error`` on terminal failure. Budget trips skip that
        logging — they are not LLM-call failures.
      * A retriable 429 rate limit backs off on the dedicated rate-limit schedule
        (first retry ~30s, ``STRATEGY_LAB_LLM_RATE_LIMIT_*`` /
        ``LLM_RATE_LIMIT_*``); transient faults keep the fast backoff. A
        weekly-usage cap stays fatal (never retried). Each rate-limit delay is
        clamped to the remaining total budget.
    """
    log = logger or _module_logger
    classify = retriable_classifier or classify_strands_exception
    cfg = _resolve_config(
        agent_key, max_attempts, timeout_s, total_budget_s, backoff_base, backoff_max
    )

    deadline = time.monotonic() + cfg.total_budget_s
    last_exc: Optional[BaseException] = None

    for attempt in range(cfg.max_attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        effective_ts = min(cfg.timeout_s, remaining)
        t0 = time.monotonic()
        try:
            result = _call_with_timeout(lambda: agent_callable(prompt), effective_ts)
            return str(result)
        except DesignBudgetExhausted:
            # Cycle-level stop from a charge inside agent_callable — do not
            # classify, retry, log as an LLM failure, or wrap.
            raise
        except Exception as exc:  # noqa: BLE001 — classify + log every failure
            latency_ms = int((time.monotonic() - t0) * 1000)
            last_exc = exc
            log.exception(
                _FAILURE_FMT,
                agent_key,
                phase,
                attempt + 1,
                cfg.max_attempts,
                latency_ms,
                type(exc).__name__,
            )
            if not classify(exc):
                raise StrategyLabLLMError(
                    f"strategy-lab LLM call failed (fatal): {type(exc).__name__}: {exc}",
                    agent_key=agent_key,
                    phase=phase,
                    attempts=attempt + 1,
                    last_error_class=type(exc).__name__,
                    outcome="fatal",
                    cause=exc,
                ) from exc
            # Retriable: back off before the next attempt, clamped to budget. A
            # 429 rate limit uses the dedicated schedule (first retry ~30s); transient
            # faults keep the fast schedule. The budget deadline is the terminator,
            # so a clamped rate-limit delay that overruns the budget ends the loop
            # with outcome="budget_exhausted" on the next iteration.
            if attempt < cfg.max_attempts - 1:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if _is_rate_limit_kind(exc):
                    computed = rate_limit_retry_delay(attempt, cfg.rl_initial, cfg.rl_cap)
                else:
                    computed = _backoff_delay(attempt, cfg.backoff_base, cfg.backoff_max)
                delay = min(computed, remaining)
                if delay > 0:
                    time.sleep(delay)

    outcome = "budget_exhausted" if (deadline - time.monotonic()) <= 0 else "exhausted"
    last_class = type(last_exc).__name__ if last_exc else None
    log.error(
        "strategy_lab LLM call terminal: agent=%s phase=%s attempts=%s outcome=%s error_class=%s",
        agent_key,
        phase,
        cfg.max_attempts,
        outcome,
        last_class or "None",
    )
    raise StrategyLabLLMError(
        f"strategy-lab LLM call unreachable after {cfg.max_attempts} attempt(s) "
        f"({outcome}; last_error={last_class}): {last_exc}",
        agent_key=agent_key,
        phase=phase,
        attempts=cfg.max_attempts,
        last_error_class=last_class,
        outcome=outcome,
        cause=last_exc if isinstance(last_exc, Exception) else None,
    )


def run_structured_agent(
    agent_callable: Callable[[str], Any],
    prompt: str,
    *,
    agent_key: str,
    phase: str,
    parse: Callable[[str], Any],
    coerce: Optional[Callable[[Any], Any]] = None,
    charge: bool = True,
    logger: Optional[logging.Logger] = None,
    **invoke_kwargs: Any,
) -> Any:
    """Collapse the charge → invoke → parse → (coerce) sequence shared by every
    Strategy Lab structured-output LLM call into one call.

    This is a thin, non-swallowing pipeline: it adds no exception handling of
    its own. Every exception raised while charging, invoking, parsing, or
    coercing propagates to the caller completely unmodified, so callers keep
    wrapping this single call in whatever try/except shape (fail-closed,
    fail-open, narrow parse-retry) they used to wrap the multi-line
    invoke+parse sequence, with identical resulting behavior.

    Preconditions:
      * ``agent_callable`` is callable as ``agent_callable(prompt) -> Any``
        (normally a constructed ``strands.Agent``) built with the SAME
        ``agent_key`` passed here — a mismatched key silently mis-routes
        per-agent telemetry, timeouts, and model selection (see
        ``model_factory._resolve_strands_timeout``).
      * ``prompt`` / ``agent_key`` / ``phase`` are non-empty strings.
      * ``parse`` accepts the raw ``str`` result of the LLM call and returns
        the parsed value, raising on malformed input.
      * ``coerce``, if given, accepts ``parse``'s return value and returns the
        final result, raising on failure. Leave it ``None`` when a call
        site's own coercion step has different-shaped extra arguments or
        asymmetric fail-open/fail-closed semantics that don't fit a single
        pass-through callable — call it explicitly after this function
        returns instead (e.g. ``alignment.py``'s fail-open
        ``_coerce_report`` step, or ``design_review.py``'s
        ``_coerce_critique``, both of which stay outside this helper).
      * ``charge=True`` is only safe when the caller does not wrap this call
        in a handler that would catch ``DesignBudgetExhausted`` (e.g. a bare
        ``except Exception``) — such a handler would otherwise swallow a
        budget trip that must propagate to the design loop. A call site with
        such a broad handler must charge explicitly, before entering its
        try block, and pass ``charge=False`` here (see ``design_review.py``).
      * ``**invoke_kwargs`` are forwarded verbatim to :func:`invoke_agent`
        (e.g. ``max_attempts``).

    Postconditions:
      * When ``charge`` is True, charges the active design-phase budget
        exactly once, as the very first action, before any transport call —
        with no enclosing try/except in this function, so
        ``DesignBudgetExhausted`` is raised and propagates immediately,
        never caught here.
      * Otherwise invokes ``agent_callable`` via :func:`invoke_agent`,
        forwarding ``agent_key``, ``phase``, ``logger``, and
        ``**invoke_kwargs``; raises :class:`StrategyLabLLMError` on
        transport exhaustion (see :func:`invoke_agent`'s contract —
        unchanged).
      * Calls ``parse(raw)``; propagates any exception it raises.
      * Returns ``coerce(parsed)`` when ``coerce`` is given, else ``parsed``;
        propagates any exception ``coerce`` raises.
      * Raises no exception type of its own.

    Invariant: stateless and side-effect-free beyond the budget charge and
    the transport call — safe to call concurrently (inherits thread-safety
    from :func:`invoke_agent` and the ``contextvars``-backed
    ``charge_active_budget``).
    """
    if charge:
        charge_active_budget()
    raw = invoke_agent(
        agent_callable,
        prompt,
        agent_key=agent_key,
        phase=phase,
        logger=logger,
        **invoke_kwargs,
    )
    parsed = parse(raw)
    return coerce(parsed) if coerce is not None else parsed


__all__ = [
    "invoke_agent",
    "run_structured_agent",
    "classify_strands_exception",
    "_is_rate_limit_kind",
]
