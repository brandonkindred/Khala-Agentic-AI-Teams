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

from strands.models.model import Model as _StrandsModel

from llm_service import LLMClient, get_strands_model


def resolve_code_review_model(llm: LLMClient):
    """Resolve the strands model a code-review agent should run on.

    Preconditions:
        - ``llm`` is an ``LLMClient`` or an object implementing the strands
          ``Model`` interface.

    Postconditions:
        - Returns ``llm`` itself when it already implements the strands ``Model``
          interface (the test path injects such a client); otherwise the shared,
          cached ``get_strands_model("code_review")`` (production). The result is
          safe to share across concurrent ``Agent`` calls — the central
          ``llm_service`` client guards its shared state internally.
    """
    return llm if isinstance(llm, _StrandsModel) else get_strands_model("code_review")
