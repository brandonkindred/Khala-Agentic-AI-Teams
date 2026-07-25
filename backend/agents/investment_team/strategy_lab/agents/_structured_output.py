"""Shared Strategy Lab provider-enforced structured-output plumbing.

Owns both the availability seam and the structured-invoke helper used by
``design.py``, ``refinement.py``, and ``design_review.py``.

Call sites import this module and look up attributes on it::

    from . import _structured_output as so
    if so.structured_output_available():
        so.invoke_structured_with_schema(...)

so tests can monkeypatch ``_structured_output.structured_output_available``
(and ``_structured_output.get_strands_model``) once and affect every caller.

Preconditions: none at module import.
Postconditions: ``structured_output_available`` is synchronous, no network,
never raises. ``invoke_structured_with_schema`` does not run a parse/validation
retry loop of its own and never falls back to legacy Agent decoding — callers
own degrade decisions. Transport retries still come from the shared envelope.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Mapping

from llm_service import provider_supports_structured_output
from llm_service.config import resolve_provider, resolve_timeout
from llm_service.interface import LLMSemanticExhaustionError
from shared.env_config import env_float

from ._llm_budget import charge_active_budget
from ._llm_envelope import run_structured_agent
from ._parse_helpers import extract_json_object
from .model_factory import get_strands_model

REASONING_MODE_SUFFIX = (
    "\n\n---\n"
    "For this pass, do NOT emit JSON. Think the problem through against every "
    "rule and constraint above, then answer in clearly labeled structured "
    "prose covering every field your JSON response would otherwise need — "
    "including your reasoning for each choice. A later pass transcribes your "
    "analysis into the required JSON shape; nothing you write here is "
    "discarded.\n"
)
"""Append to an existing system prompt to build its reasoning-pass variant
for :func:`invoke_structured_with_schema`'s ``reasoning_system_prompt``
argument, e.g. ``_get_design_system_prompt() + REASONING_MODE_SUFFIX``. Kept
here (rather than duplicated per caller) since all four call sites
(``design.py`` x2, ``design_review.py``, ``refinement.py``) need the same
override."""


def structured_output_available() -> bool:
    """Whether the active LLM provider supports provider-enforced schema-conformant decoding.

    Preconditions: none.
    Postconditions: synchronous, no network call, never raises.
    """
    return provider_supports_structured_output(resolve_provider())


def invoke_structured_with_schema(
    agent_key: str,
    system_prompt: str,
    user_prompt: str,
    *,
    phase: str,
    schema: Mapping[str, Any],
    charge: bool,
    objective: str,
    logger: logging.Logger,
    reasoning_system_prompt: str,
) -> Dict[str, Any]:
    """Reason (think=True, prose), then request schema-conformant JSON (think=False).

    Routes through :func:`run_structured_agent` for the shared invoke / timeout /
    parse envelope, which wraps ONE blocking call — here, ``_call`` internally
    sequences two LLM calls (a think=True prose reasoning pass via
    ``client.complete``, then the think=False schema-conformant formatting
    pass via ``client.complete_json``) under that single envelope. A transport
    retry re-runs both sub-calls; this is a deliberate, documented tradeoff to
    keep the existing timeout/retry plumbing untouched. Since the closure now
    performs two sequential provider calls instead of one, the per-attempt
    ``timeout_s`` passed to the envelope is DOUBLED from the resolved
    single-call default — otherwise two individually healthy calls could
    exceed a budget sized for one, aborting the attempt (and abandoning a
    still-running daemon thread, see ``_call_with_timeout``) even though
    neither provider request was actually slow. ``total_budget_s`` is left
    unset so it scales off the doubled ``timeout_s`` automatically (see
    ``_resolve_config``).

    Charging is handled entirely by this function rather than forwarded to
    ``run_structured_agent``'s own ``charge=`` (which charges exactly once):
    when ``charge`` is True, the design-phase budget is charged TWICE, both
    up front before ``run_structured_agent`` is invoked — matching
    ``STRATEGY_LAB_DESIGN_MAX_LLM_CALLS``' per-*provider-call* accounting now
    that ``_call`` makes two — and both charges happen before either
    provider call runs, so a trip on the second charge stops the attempt
    before wasting the first (reasoning) call. Callers that charge
    explicitly and pass ``charge=False`` (see ``design_review.py``) must
    likewise charge twice at their own call site.

    This helper does **not** run its own parse/validation retry loop and never
    falls back to legacy Agent decoding for the *formatting* call — callers
    inspect ``schema_forced`` starvation (raised only by the formatting call's
    ``complete_json(schema=...)``, exactly as before this split) and decide
    whether to degrade. Transient *transport* retries still happen inside the
    envelope (``invoke_agent``); only parse/validation re-prompts are out of
    scope here.

    Preconditions: :func:`structured_output_available` is True; ``agent_key`` /
    ``system_prompt`` / ``user_prompt`` / ``phase`` / ``objective`` /
    ``reasoning_system_prompt`` are non-empty strings; ``schema`` is a
    non-empty mapping.
    Postconditions: returns the parsed JSON dict on success. Raises
    :class:`~..exceptions.StrategyLabLLMError` when the envelope exhausts
    transport retries or hits a fatal LLM error, and ``ValueError`` when
    :func:`extract_json_object` cannot recover a balanced JSON object from the
    response.
    """
    assert structured_output_available(), (
        "precondition: caller must verify structured_output_available() before "
        "invoking (only that path exposes an adapter with a .client)"
    )
    assert agent_key and system_prompt and user_prompt and phase and objective, (
        "precondition: agent_key, system_prompt, user_prompt, phase, and objective "
        "must be non-empty"
    )
    assert reasoning_system_prompt, "precondition: reasoning_system_prompt must be non-empty"
    assert isinstance(schema, Mapping) and schema, (
        "precondition: schema must be a non-empty mapping"
    )
    client = get_strands_model(agent_key).client

    def _call(prompt: str) -> str:
        # ``prompt`` (the caller's user_prompt) already carries the target
        # JSON-schema instructions for this module (embedded per-template,
        # not in system_prompt) — reuse it unchanged for both calls rather
        # than replacing it, so neither pass loses the task-specific content
        # guidance (DSL shape reminders, worked examples, etc.) baked into it.
        try:
            prose = client.complete(
                prompt,
                objective=f"{objective} (reasoning)",
                system_prompt=reasoning_system_prompt,
                temperature=0.3,
                think=True,
            )
        except LLMSemanticExhaustionError as exc:
            # The reasoning call is unconstrained (no schema=), so the client
            # always raises this with schema_forced=False — but the caller's
            # degrade check gates on schema_forced to decide whether to fall
            # back to the legacy unconstrained parse-retry loop. From the
            # pipeline's perspective, starving before the schema-constrained
            # formatting call even runs is equally "structured decoding could
            # not produce output", so mark it schema_forced=True here too —
            # otherwise this now-reachable failure mode (impossible pre-split,
            # when there was only one, schema-constrained call) would
            # propagate as fatal instead of degrading like its formatting-call
            # counterpart.
            exc.schema_forced = True
            raise
        format_prompt = (
            f"{prompt}\n\n"
            "--- YOUR PRIOR ANALYSIS (produced under a separate reasoning pass) ---\n"
            f"{prose}\n"
            "--- END ANALYSIS ---\n\n"
            "Use the analysis above as the basis for your answer — do not contradict it. "
            "Now emit the JSON object exactly as instructed above."
        )
        result = client.complete_json(
            format_prompt,
            objective=f"{objective} (format)",
            system_prompt=system_prompt,
            schema=dict(schema),
            think=False,
        )
        # invoke_agent unconditionally does str(result) on whatever this
        # callable returns before handing it to `parse` — a raw dict would
        # come back as Python repr (single-quoted, True/False/None), which
        # extract_json_object cannot parse. json.dumps re-renders it as valid
        # JSON so the round trip is exact.
        return json.dumps(result)

    # _call now makes two sequential provider calls (reasoning + formatting)
    # under the envelope's single per-attempt timeout — double the resolved
    # single-call default so two individually healthy calls can't trip a
    # budget sized for one. Mirrors _resolve_config's own resolution order
    # (explicit -> STRATEGY_LAB_LLM_TIMEOUT -> per-model default) so this
    # stays in sync with an operator override of either env var.
    single_call_timeout_s = max(0.001, env_float("STRATEGY_LAB_LLM_TIMEOUT", resolve_timeout(agent_key)))
    timeout_s = single_call_timeout_s * 2

    # total_budget_s is left unset (None) so _resolve_config's own default
    # formula (attempts * timeout_s * 1.5) applies, which already scales
    # correctly since it derives from the doubled timeout_s above. An
    # *explicit* STRATEGY_LAB_LLM_TOTAL_BUDGET override is intentionally left
    # unscaled: _resolve_config documents it as the hard wall-clock deadline
    # for the whole call ("the real terminator" of the retry loop, not a
    # per-attempt figure), so doubling it would let a call run for twice the
    # operator-approved latency/cost window — an explicit cap must be
    # honored as configured, even if that means fewer retry attempts fit
    # inside it now that each attempt takes longer.
    if charge:
        # Two provider calls happen per attempt now (reasoning + formatting)
        # — charge both units up front, before run_structured_agent (and
        # therefore before either provider call) runs, so DesignBudgetExhausted
        # on the second charge stops the attempt before the first call is made.
        charge_active_budget()
        charge_active_budget()

    return run_structured_agent(
        _call,
        user_prompt,
        agent_key=agent_key,
        phase=phase,
        parse=extract_json_object,
        charge=False,
        logger=logger,
        timeout_s=timeout_s,
    )
