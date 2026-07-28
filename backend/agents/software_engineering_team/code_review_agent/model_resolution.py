"""Shared strands-model resolution for the code-review agents.

The chunk reviewer, the synthesis pass, and the false-positive verifier all need
the same thing: run a strands ``Agent`` on the injected ``llm`` when it already
implements the strands ``Model`` interface (the test path injects such a
client), or on the shared, cached production model otherwise. Keeping that one
rule here stops the three call sites from drifting apart. Today all three call
sites (and ``architecture_consistency_pass``/``side_effect_impact_pass``) use
``resolve_code_review_model``; ``resolve_code_review_verify_model`` below adds
the lighter ``code_review_verify`` routing path but is not yet wired into any
of them — that adoption is tracked as separate follow-up work.

This is intentionally distinct from the generic
``software_engineering_team.shared.strands_model.resolve_strands_model``: the
code-review subsystem resolves its production model by agent key via the cached
``get_strands_model("code_review")`` (so ``LLM_MODEL_code_review`` routing
applies and one cached, concurrency-safe model is reused across the parallel
chunk and verification calls), rather than wrapping the injected client with a
cache-bypassing ``get_strands_model(client=llm)``. The two are not
interchangeable; this helper preserves the existing code-review behavior.
"""

from __future__ import annotations

from typing import Optional, Union

from strands.models.model import Model as _StrandsModel

from llm_service import LLMClient, get_strands_model


def resolve_code_review_model(
    llm: "Union[LLMClient, _StrandsModel]", think: Optional[Union[bool, str]] = None
) -> "Union[LLMClient, _StrandsModel]":
    """Resolve the strands model a code-review agent should run on.

    Preconditions:
        - ``llm`` is an ``LLMClient`` or an object implementing the strands
          ``Model`` interface.
        - ``think`` is ``None`` (use the model's default thinking level), or an
          explicit override (``False`` to force reasoning off, a level string,
          etc.) applied only on the production path.

    Postconditions:
        - Returns ``llm`` itself when it already implements the strands ``Model``
          interface (the test path injects such a client) — an injected model
          cannot have its thinking level overridden, so ``think`` is ignored for
          it (see ``thinking_override_supported``). Otherwise returns
          ``get_strands_model("code_review", think=think)``: with ``think=None``
          this is the default production model (unchanged behavior); with an
          explicit override it is a fresh wrapper over the same cached client
          with the requested thinking level. The result is safe to share across
          concurrent ``Agent`` calls — the central ``llm_service`` client guards
          its shared state internally.
    """
    if isinstance(llm, _StrandsModel):
        return llm
    if think is None:
        return get_strands_model("code_review")
    return get_strands_model("code_review", think=think)


def resolve_code_review_verify_model(
    llm: "Union[LLMClient, _StrandsModel]", think: Optional[Union[bool, str]] = None
) -> "Union[LLMClient, _StrandsModel]":
    """Resolve the strands model for code-review's narrower verify/synthesis sub-passes.

    Same shape as :func:`resolve_code_review_model`, but keyed on the
    ``code_review_verify`` agent key (its own, genuinely lighter
    ``AGENT_DEFAULT_MODELS`` entry) instead of ``code_review`` — intended for
    bounded tasks like false-positive verification and narrative synthesis, as
    opposed to open-ended chunk review. Not yet called by any production call
    site (``false_positive_filter.py`` and ``synthesis.py`` still call
    :func:`resolve_code_review_model`); wiring them over to this resolver is
    tracked as separate follow-up work, not part of adding the routing path
    itself.

    Preconditions:
        - ``llm`` is an ``LLMClient`` or an object implementing the strands
          ``Model`` interface.
        - ``think`` is ``None`` (use the model's default thinking level), or an
          explicit override applied only on the production path.

    Postconditions:
        - Returns ``llm`` itself when it already implements the strands ``Model``
          interface (the test path injects such a client); ``think`` is ignored
          for it (see ``thinking_override_supported``). Otherwise returns
          ``get_strands_model("code_review_verify", think=think)`` (or without
          ``think`` when it is ``None``), safe to share across concurrent
          ``Agent`` calls.
    """
    if isinstance(llm, _StrandsModel):
        return llm
    if think is None:
        return get_strands_model("code_review_verify")
    return get_strands_model("code_review_verify", think=think)


def thinking_override_supported(llm: "Union[LLMClient, _StrandsModel]") -> bool:
    """Whether the chunk reviewer can override the thinking level for ``llm``.

    Postconditions:
        - Returns ``False`` when ``llm`` is an injected strands ``Model`` (its
          thinking level is baked in and cannot be re-resolved — the test path),
          ``True`` on the production path where ``resolve_code_review_model``
          builds the model via ``get_strands_model`` and can pass ``think=``.
    """
    return not isinstance(llm, _StrandsModel)
