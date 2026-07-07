"""Shared strands-model resolution for the code-review agents.

The chunk reviewer, the synthesis pass, and the false-positive verifier all need
the same thing: run a strands ``Agent`` on the injected ``llm`` when it already
implements the strands ``Model`` interface (the test path injects such a
client), or on the shared, cached production model otherwise. Keeping that one
rule here stops the three call sites from drifting apart.

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


def resolve_code_review_model(llm: LLMClient, think: Optional[Union[bool, str]] = None):
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


def thinking_override_supported(llm: LLMClient) -> bool:
    """Whether the chunk reviewer can override the thinking level for ``llm``.

    Postconditions:
        - Returns ``False`` when ``llm`` is an injected strands ``Model`` (its
          thinking level is baked in and cannot be re-resolved — the test path),
          ``True`` on the production path where ``resolve_code_review_model``
          builds the model via ``get_strands_model`` and can pass ``think=``.
    """
    return not isinstance(llm, _StrandsModel)
