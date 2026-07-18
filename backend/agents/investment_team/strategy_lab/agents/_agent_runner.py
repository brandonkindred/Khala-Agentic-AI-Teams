"""Fresh-``Agent``-per-call construction/invoke/parse scaffold shared across
the Strategy Lab agent modules.

Eight one-shot call sites across six modules (``design_review.py``,
``alignment.py`` x2, ``zero_trade_repair.py``, ``analysis.py`` x2,
``code_synthesis.py``, ``design.py``'s ``_self_review``) each repeated the
same scaffold inline: construct a fresh
``strands.Agent(model=get_strands_model(agent_key), system_prompt=...,
tools=[])``, invoke it through the fault-tolerance envelope
(:func:`_llm_envelope.invoke_agent`), and — every site except
``code_synthesis.py``'s text-mode call — parse the result with
:func:`_parse_helpers.extract_json_object`. :func:`invoke_json_agent` (and
its text-mode sibling :func:`invoke_text_agent`) are the single chokepoint
that scaffold now routes through. ``design.py``'s and ``refinement.py``'s
own ``_invoke_and_parse`` parse-*retry-loop* methods are NOT among these —
their retry-loop unification is tracked as separate follow-up work, so they
still build their own ``Agent`` inline for now.

Each call site keeps its own fail-closed ``except`` policy — what to log, at
what level, and what to return or raise on failure. That policy exists to
handle LLM-transport and JSON-parse failures; it was never meant to also
catch a pre-flight *construction* failure (a bad ``LLM_PROVIDER``, a missing
API key, an empty ``agent_key``) that would previously have raised before a
call site's ``try`` block was even entered. To preserve that distinction now
that construction lives in this shared module rather than inline at each
site, a construction-time exception is wrapped in
:class:`AgentConstructionError` — a type distinct from anything
``invoke_agent``/``extract_json_object`` can raise — so call sites can (and
should) let it propagate past their LLM/parse-specific ``except`` instead of
silently folding a deployment bug into a "the model returned garbage"
diagnosis. Every other failure (transport, timeout, rate-limit, JSON parse)
propagates unchanged, exactly as before.

Import-cycle rationale (mirrors ``_llm_envelope.py``'s own note): this
module imports ``strands.Agent`` plus three sibling/leaf modules —
:func:`_llm_envelope.invoke_agent`, :func:`model_factory.get_strands_model`,
and :func:`_parse_helpers.extract_json_object` — none of which import from
any of the five call-site agent modules above. That keeps this module
strictly one layer *below* the five call sites in the import graph: every
one of them may safely import from this module, and this module must never
import from any of them — doing so would reintroduce the cycle
``_llm_envelope.py`` was written to avoid, just one hop later.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from strands import Agent

from ._llm_envelope import invoke_agent
from ._parse_helpers import extract_json_object
from .model_factory import get_strands_model


class AgentConstructionError(Exception):
    """Raised when ``Agent``/``get_strands_model`` construction fails,
    before any LLM call is attempted.

    Distinct from any exception ``invoke_agent`` or ``extract_json_object``
    can raise, so a call site's LLM/parse-specific fail-closed ``except``
    can let this propagate instead of misdiagnosing a pre-flight
    configuration failure as a transport or parse failure.
    """


def invoke_text_agent(
    user_prompt: str,
    *,
    agent_key: str,
    phase: str,
    system_prompt: str,
    response_format: str = "text",
    max_attempts: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Construct a fresh ``strands.Agent`` and invoke it under the
    fault-tolerance envelope, returning the raw response text unparsed.

    The text-mode sibling of :func:`invoke_json_agent` — used by the one
    call site (``code_synthesis.py``) whose response is a raw source file,
    not a JSON object, and which must therefore never be routed through
    :func:`_parse_helpers.extract_json_object`.

    Preconditions:
      * ``agent_key`` / ``phase`` are non-empty diagnostic labels, forwarded
        verbatim to :func:`model_factory.get_strands_model` and
        :func:`_llm_envelope.invoke_agent`; a blank value raises
        ``ValueError``.
      * ``system_prompt`` / ``user_prompt`` are the fully-rendered prompt
        strings for this call — this helper does no templating.
      * The caller has already charged any active budget
        (``_llm_budget.charge_active_budget``) BEFORE calling this, exactly
        as documented on :func:`_llm_envelope.invoke_agent` — this helper
        never charges any budget itself.
      * The caller owns the fail-closed ``except`` policy for LLM-transport
        and parse failures; that policy should let :class:`AgentConstructionError`
        propagate rather than catch it (see module docstring).

    Postconditions:
      * Returns ``invoke_agent``'s raw ``str`` result verbatim — no
        parsing, no stripping, no post-processing.
      * Raises ``ValueError`` when ``agent_key``/``phase`` is blank.
      * Raises :class:`AgentConstructionError` when ``Agent``/
        ``get_strands_model`` construction fails, wrapping the original
        exception via ``__cause__``.
      * Propagates :class:`StrategyLabLLMError` unchanged when the envelope
        exhausts retries/budget or classifies the failure as fatal (see
        :func:`_llm_envelope.invoke_agent`).

    Invariants:
      * A brand-new ``Agent`` is constructed on **every call** — never
        cached or reused across invocations, even for repeated calls with
        the same ``agent_key``. This mirrors every pre-extraction call site
        (each built a fresh ``Agent(...)`` inline per invocation) and is
        the one behavioral property this extraction must never change.
    """
    if not agent_key or not agent_key.strip():
        raise ValueError("agent_key must be a non-empty string")
    if not phase or not phase.strip():
        raise ValueError("phase must be a non-empty string")

    try:
        agent = Agent(
            model=get_strands_model(agent_key, response_format=response_format),
            system_prompt=system_prompt,
            tools=[],
        )
    except Exception as exc:
        raise AgentConstructionError(f"{type(exc).__name__}: {exc}") from exc

    return invoke_agent(
        agent,
        user_prompt,
        agent_key=agent_key,
        phase=phase,
        max_attempts=max_attempts,
        logger=logger,
    )


def invoke_json_agent(
    user_prompt: str,
    *,
    agent_key: str,
    phase: str,
    system_prompt: str,
    max_attempts: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Construct a fresh ``strands.Agent``, invoke it under the
    fault-tolerance envelope, and parse its response as a JSON object.

    The single chokepoint for the "build an Agent -> invoke_agent ->
    extract_json_object" scaffold repeated across every JSON-mode Strategy
    Lab agent call site (design review, alignment near-miss / propose-fix,
    zero-trade repair, analysis draft / self-review). Delegates
    construction and invocation to :func:`invoke_text_agent` (always with
    ``response_format="json"`` — every JSON-mode call site wants the wire
    forced to JSON, matching the strict parse below) so the two helpers
    never duplicate the ``Agent``-construction/invoke lines between them;
    only the parse step is added here.

    Preconditions:
      * Same as :func:`invoke_text_agent`: ``agent_key`` / ``phase`` are
        non-empty diagnostic labels; ``system_prompt`` / ``user_prompt`` are
        fully-rendered; the caller charges any active budget BEFORE calling
        this; the caller's fail-closed ``except`` should let
        :class:`AgentConstructionError` propagate rather than catch it.

    Postconditions:
      * Returns the parsed ``dict`` from
        :func:`_parse_helpers.extract_json_object` on success.
      * Raises ``ValueError`` when ``agent_key``/``phase`` is blank, or when
        the raw response is not a strictly-parseable JSON object (see
        :func:`_parse_helpers.extract_json_object`) — this helper never
        retries or repairs a parse failure itself; that policy belongs to
        the caller (parse-retry-loop unification is tracked separately).
      * Raises :class:`AgentConstructionError` when construction fails (see
        :func:`invoke_text_agent`).
      * Propagates :class:`StrategyLabLLMError` unchanged when the envelope
        exhausts retries/budget or classifies the failure as fatal.

    Invariants:
      * Inherits :func:`invoke_text_agent`'s fresh-Agent-per-call guarantee:
        a brand-new ``Agent`` is constructed on every call, never cached or
        reused.
    """
    raw = invoke_text_agent(
        user_prompt,
        agent_key=agent_key,
        phase=phase,
        system_prompt=system_prompt,
        response_format="json",
        max_attempts=max_attempts,
        logger=logger,
    )
    return extract_json_object(raw)


__all__ = ["AgentConstructionError", "invoke_json_agent", "invoke_text_agent"]
