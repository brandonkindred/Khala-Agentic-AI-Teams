"""Shared helper for resolving the Strands model an SE-team agent should use.

Both the v2 ``phases/*.py`` modules and the ``tool_agents/*/agent.py`` modules
share the same pattern: take an injectable ``llm`` (either a pre-built Strands
``Model``, a raw ``LLMClient`` from the orchestrator, or ``None`` for default
construction) and produce a Strands ``Model`` ready to pass to ``Agent(model=...)``.

Before this helper, that logic was duplicated across 22 sites with subtle
inconsistencies — most notably, the tool agents only checked for
``_StrandsModel`` and silently discarded raw ``LLMClient`` injections by
falling through to the default ``get_strands_model()`` path, while the phase
``_resolve_model`` helpers correctly handled the ``LLMClient`` branch via
``get_strands_model(client=llm, ...)``. Tests didn't catch the asymmetry
because ``DummyLLMClient`` doubles as a Strands ``Model``.

This module collapses the pattern to one definition.
"""

from __future__ import annotations

from typing import Any

from llm_service import get_strands_model


def resolve_strands_model(llm: Any, *, response_format: str = "json") -> Any:
    """Resolve an injectable LLM handle to a Strands ``Model``.

    Resolution rules:

    1. If ``llm`` is already a Strands ``Model``, return it as-is. This lets
       callers pin a specific mode by passing a pre-built model.
    2. If ``llm`` is an ``LLMClient`` (e.g. ``OllamaLLMClient`` from the
       orchestrator), wrap it in an ``LLMClientModel`` with the requested
       ``response_format``. Callers' retries / telemetry / rate-limit guard
       continue to flow through the injected client.
    3. Otherwise (``None`` or anything unrecognized), construct a default
       Strands model via ``get_strands_model(response_format=...)``.

    Parameters
    ----------
    llm:
        Either a Strands ``Model``, an ``LLMClient``, or ``None``.
    response_format:
        ``"json"`` (default) or ``"text"``. Selects the JSON / text branch of
        ``LLMClient.chat`` on the wire. See ``llm_service/strands_adapter.py``
        for details.
    """
    # Local imports keep the optional ``strands`` and ``LLMClient`` imports off
    # the module-level path — this helper is imported at import time by the
    # tool agents and the v2 phases, and we don't want to force every consumer
    # to install the Strands SDK just to import the module.
    from strands.models.model import Model as _StrandsModel  # noqa: PLC0415

    from llm_service import LLMClient as _LLMClient  # noqa: PLC0415

    if llm is not None and isinstance(llm, _StrandsModel):
        return llm
    if llm is not None and isinstance(llm, _LLMClient):
        return get_strands_model(client=llm, response_format=response_format)
    return get_strands_model(response_format=response_format)


def resolve_text_mode_strands_model(llm: Any) -> Any:
    """Convenience wrapper: ``resolve_strands_model(llm, response_format="text")``.

    The v2 phase modules and template-based tool agents all need text-mode
    output (their downstream consumers are template parsers like
    ``parse_planning_template`` / ``parse_review_template`` /
    ``parse_files_and_summary_template``, not ``json.loads``). Calling this
    named helper at the use site removes the need for an 8-copy
    ``_resolve_model`` trampoline per file *and* makes the text-mode intent
    legible to a grep.
    """
    return resolve_strands_model(llm, response_format="text")
