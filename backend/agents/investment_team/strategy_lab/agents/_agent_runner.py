"""Generic JSON-parse-retry driver shared by every Strategy Lab structured-output agent.

Every Strategy Lab agent that authors JSON (design, refinement, and future
agents) needs the same shape: build a fresh, history-free ``Agent``, invoke it
under the fault-tolerance envelope, extract a JSON object from the raw text,
optionally validate the parsed shape, and re-prompt with a caller-specific
correction message on either kind of failure — bounded by a retry budget.

Both ``design.py`` and ``refinement.py`` currently consume 
:func:`run_json_with_parse_retry` for their execution paths.

Preconditions:
  * ``agent_key`` / ``phase`` are non-empty diagnostic + model-routing
    labels; ``agent_key`` is passed directly to
    ``model_factory.get_strands_model`` to resolve the model for every
    attempt's fresh ``Agent``.
  * ``system_prompt`` / ``base_user_prompt`` are non-empty strings;
    ``base_user_prompt`` is the prompt re-sent (via the correction
    callbacks) on every retry — it is never itself mutated.
  * ``retry_budget >= 0``; ``retry_budget + 1`` is the total number of
    attempts made.
  * ``on_parse_error`` is required and is called as
    ``on_parse_error(base_user_prompt, exc)`` where ``exc`` is the
    ``ValueError`` raised by JSON extraction; it must return the prompt
    string to send on the next attempt.
  * ``validate``, if given, is called as ``validate(parsed)`` after a
    successful parse and must return the finalized result or raise on an
    invalid shape; ``on_validation_error`` is REQUIRED when ``validate`` is
    given (checked at call time; raises ``ValueError`` immediately if
    violated) and is called as ``on_validation_error(base_user_prompt, exc)``.
  * ``before_attempt``, if given, is a zero-arg callable invoked once per
    attempt, before that attempt's LLM call — the intended use is per-cycle
    LLM-call budget charging (e.g. ``_llm_budget.charge_active_budget``),
    pulled out of the transport layer so callers control exactly when and
    whether charging happens.

Postconditions:
  * Returns the parsed (and, if ``validate`` is given, validated) ``dict`` on
    the first attempt that both parses and validates successfully.
  * Raises the terminal ``ValueError`` (unparseable JSON) or the terminal
    validation exception UNMODIFIED — no wrapping, no swallowing — once
    ``retry_budget + 1`` attempts are exhausted.
  * Any exception NOT caught here (e.g.
    ``strategy_lab.exceptions.StrategyLabLLMError`` from transport
    exhaustion, or a budget error raised by a caller's ``before_attempt``)
    propagates immediately and is never retried by this driver — only a
    JSON-parse ``ValueError`` or a ``validate``-raised exception trigger a
    retry.

Invariants:
  * Every attempt builds a brand-new ``Agent`` instance (no reused instance,
    no ``self.messages`` carryover) — a correction re-prompt is always
    "reissue the whole object correctly," never "continue from what you
    just emitted."
  * ``before_attempt`` (when given) is called exactly once per attempt,
    including the attempt that ultimately succeeds.
  * Stateless / side-effect-free beyond what the caller's hooks and
    ``run_structured_agent`` do — safe to call concurrently.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

from strands import Agent

from ._llm_budget import DesignBudgetExhausted
from ._llm_envelope import run_structured_agent
from ._parse_helpers import extract_json_object
from .model_factory import get_strands_model


def run_json_with_parse_retry(
    *,
    agent_key: str,
    phase: str,
    system_prompt: str,
    base_user_prompt: str,
    retry_budget: int,
    logger: logging.Logger,
    before_attempt: Optional[Callable[[], None]] = None,
    on_parse_error: Callable[[str, ValueError], str],
    validate: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    on_validation_error: Optional[Callable[[str, Exception], str]] = None,
) -> Dict[str, Any]:
    """Run the charge → build-Agent → invoke → parse → (validate) → retry loop.

    See the module docstring for the full contract.
    """
    if validate is not None and on_validation_error is None:
        raise ValueError(
            "run_json_with_parse_retry: on_validation_error is required when validate is given"
        )

    prompt = base_user_prompt

    for attempt in range(retry_budget + 1):
        # A fresh, history-free agent per attempt (see module docstring):
        # ``strands.Agent`` accumulates conversation history in
        # ``self.messages``, so reusing one instance across a correction
        # re-prompt would feed the model its own rejected output back as
        # context and bias it toward defending the malformed shape.
        agent = Agent(model=get_strands_model(agent_key), system_prompt=system_prompt, tools=[])

        if before_attempt is not None:
            before_attempt()

        try:
            parsed = run_structured_agent(
                agent,
                prompt,
                agent_key=agent_key,
                phase=phase,
                parse=extract_json_object,
                charge=False,
                logger=logger,
            )
        except ValueError as exc:
            logger.warning(
                "agent=%s phase=%s emitted unparseable JSON (attempt %d/%d): %s",
                agent_key,
                phase,
                attempt + 1,
                retry_budget + 1,
                exc,
            )
            if attempt >= retry_budget:
                raise
            prompt = on_parse_error(base_user_prompt, exc)
            continue

        if validate is None:
            return parsed

        try:
            return validate(parsed)
        except Exception as exc:  # noqa: BLE001 — caller-defined validation exception type
            logger.warning(
                "agent=%s phase=%s failed validation (attempt %d/%d): %s",
                agent_key,
                phase,
                attempt + 1,
                retry_budget + 1,
                exc,
            )
            if attempt >= retry_budget:
                raise
            prompt = on_validation_error(base_user_prompt, exc)

    raise AssertionError(  # pragma: no cover - unreachable: loop always returns or re-raises
        "unreachable: run_json_with_parse_retry exited without return"
    )


def run_single_shot_agent(
    *,
    agent_key: str,
    phase: str,
    system_prompt: str,
    user_prompt: str,
    on_failure: Callable[[Exception], Any],
    parse: Callable[[str], Any] = extract_json_object,
    charge: bool = True,
    max_attempts: Optional[int] = None,
    guard_design_budget: bool = True,
    model_kwargs: Optional[Dict[str, Any]] = None,
    logger: logging.Logger,
) -> Tuple[bool, Any]:
    """Run the build-Agent → invoke → catch-and-wrap sequence shared by every
    Strategy Lab non-retrying structured-output call.

    Preconditions:
      * ``agent_key`` / ``phase`` are non-empty diagnostic + model-routing
        labels, forwarded to ``model_factory.get_strands_model`` and
        ``run_structured_agent`` respectively.
      * ``system_prompt`` / ``user_prompt`` are the (already fully rendered)
        strings to build the ``Agent`` and invoke it with; neither is mutated
        or retried by this driver — this is a single-shot call, not a retry
        loop (see :func:`run_json_with_parse_retry` for that variant).
      * ``on_failure`` is required and is called as ``on_failure(exc)``
        exactly once, and only, when the invocation raises an exception this
        driver does not itself propagate bare (see Postconditions). It may
        raise (typically a caller-specific domain exception, chained via
        ``from exc``) or return a fallback value.
      * ``guard_design_budget`` controls whether ``DesignBudgetExhausted`` is
        treated specially (re-raised bare, the default) or handed to
        ``on_failure`` like any other exception — callers whose current
        behavior has no ``DesignBudgetExhausted``-specific handling must pass
        ``guard_design_budget=False`` to preserve that behavior unchanged.
      * ``model_kwargs``, if given, is forwarded as ``**model_kwargs`` to
        ``get_strands_model`` (e.g. ``{"response_format": "text"}``).

    Postconditions:
      * Returns ``(True, result)`` where ``result`` is whatever
        ``run_structured_agent`` returned, on success.
      * Returns ``(False, on_failure(exc))`` when the invocation raises and
        ``on_failure`` returns a value instead of raising.
      * If ``on_failure`` itself raises, that exception propagates
        unmodified — this driver never returns in that case.
      * When ``guard_design_budget`` is True (the default), a
        ``DesignBudgetExhausted`` raised by the invocation propagates bare,
        unmodified, without ever reaching ``on_failure``.

    Invariants:
      * ``on_failure(exc)`` is invoked synchronously, from within this
        function's own ``except`` block — never deferred or stored for later
        — so that an ``on_failure`` implementation calling
        ``logger.exception(...)`` (relying on ambient ``sys.exc_info()``
        rather than an explicit ``exc_info=`` argument) captures the correct
        traceback.
      * Exactly one ``Agent`` is built and invoked per call — no retry, no
        history carryover (matches the "fresh Agent per attempt" invariant of
        :func:`run_json_with_parse_retry`).
    """
    agent = Agent(
        model=get_strands_model(agent_key, **(model_kwargs or {})),
        system_prompt=system_prompt,
        tools=[],
    )
    try:
        result = run_structured_agent(
            agent,
            user_prompt,
            agent_key=agent_key,
            phase=phase,
            parse=parse,
            charge=charge,
            max_attempts=max_attempts,
            logger=logger,
        )
    except DesignBudgetExhausted as exc:
        if guard_design_budget:
            raise
        return False, on_failure(exc)
    except Exception as exc:  # noqa: BLE001 — caller-defined wrapping exception type
        return False, on_failure(exc)
    return True, result


__all__ = ["run_json_with_parse_retry", "run_single_shot_agent"]
