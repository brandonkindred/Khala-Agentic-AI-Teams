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
never raises. ``invoke_structured_with_schema`` never retries and never falls
back — callers own degrade decisions.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Mapping

from llm_service import provider_supports_structured_output
from llm_service.config import resolve_provider

from ._llm_envelope import run_structured_agent
from ._parse_helpers import extract_json_object
from .model_factory import get_strands_model


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
) -> Dict[str, Any]:
    """Bypass ``strands.Agent`` and request schema-conformant JSON via ``complete_json``.

    Routes through :func:`run_structured_agent` for the shared charge / invoke /
    timeout / parse envelope. Never retries and never falls back; callers inspect
    ``schema_forced`` starvation and decide whether to degrade.

    Preconditions: :func:`structured_output_available` is True; ``agent_key`` /
    ``system_prompt`` / ``user_prompt`` / ``phase`` / ``objective`` are non-empty
    strings; ``schema`` is a non-empty mapping.
    Postconditions: returns the parsed JSON dict on success. Raises
    :class:`~..exceptions.StrategyLabLLMError` on transport/parse failure.
    """
    assert structured_output_available(), (
        "precondition: caller must verify structured_output_available() before "
        "invoking (only that path exposes an adapter with a .client)"
    )
    assert agent_key and system_prompt and user_prompt and phase and objective, (
        "precondition: agent_key, system_prompt, user_prompt, phase, and objective "
        "must be non-empty"
    )
    assert isinstance(schema, Mapping) and schema, (
        "precondition: schema must be a non-empty mapping"
    )
    client = get_strands_model(agent_key).client

    def _call(prompt: str) -> str:
        result = client.complete_json(
            prompt,
            objective=objective,
            system_prompt=system_prompt,
            schema=dict(schema),
        )
        # invoke_agent unconditionally does str(result) on whatever this
        # callable returns before handing it to `parse` — a raw dict would
        # come back as Python repr (single-quoted, True/False/None), which
        # extract_json_object cannot parse. json.dumps re-renders it as valid
        # JSON so the round trip is exact.
        return json.dumps(result)

    return run_structured_agent(
        _call,
        user_prompt,
        agent_key=agent_key,
        phase=phase,
        parse=extract_json_object,
        charge=charge,
        logger=logger,
    )
