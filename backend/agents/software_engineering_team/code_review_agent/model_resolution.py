"""Shared strands-model resolution for the code-review agents.

The synthesis pass, the false-positive verifier, ``architecture_consistency_pass``,
and ``side_effect_impact_pass`` all need the same thing: run a strands ``Agent`` on
the injected ``llm`` when it already implements the strands ``Model`` interface
(the test path injects such a client), or on the shared, cached production model
otherwise. Keeping that one rule here stops those call sites from drifting apart.

``resolve_code_review_model`` covers the primary ``code_review`` key (still used by
synthesis, architecture-consistency, side-effect impact, and ``mapping.py`` cache
fingerprinting — the chunk reviewer itself now calls ``LLMClient.complete_json``
directly rather than dispatching through this module).
``resolve_code_review_verify_model`` routes the false-positive verifier onto the
lighter ``code_review_verify`` key and pins Ollama failover candidates to that
key's per-agent / default model so a non-empty provider-list ``entry.model``
cannot shadow the lighter selection; wiring synthesis onto that key is separate
follow-up work.

This is intentionally distinct from the generic
``llm_service.strands_model.resolve_strands_model``: the
code-review subsystem resolves its production model by agent key via the cached
``get_strands_model(...)`` helpers (so ``LLM_MODEL_code_review`` /
``LLM_MODEL_code_review_verify`` routing applies and one cached, concurrency-safe
model is reused across parallel calls), rather than wrapping the injected client
with a cache-bypassing ``get_strands_model(client=llm)``. The two are not
interchangeable; these helpers preserve the existing code-review behavior.
"""

from __future__ import annotations

import os
from typing import Optional, Union

from strands.models.model import Model as _StrandsModel

from llm_service import LLMClient, LLMClientModel, get_strands_model, with_model_override
from llm_service.config import AGENT_DEFAULT_MODELS, ENV_LLM_MODEL

_VERIFY_AGENT_KEY = "code_review_verify"


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


def _code_review_verify_model_pin() -> str:
    """Return the model id the verify path should pin Ollama candidates to.

    Prefer ``LLM_MODEL_code_review_verify``, else ``AGENT_DEFAULT_MODELS`` for
    the verify key. Intentionally skips the global ``LLM_MODEL`` / UI runtime
    model so the lighter verify default is not shadowed by the primary review's
    configured model (those globals still apply to blank ``entry.model`` fallback
    via ``resolve_model``; the pin is specifically for overriding a filled
    provider-list entry).

    Preconditions: ``AGENT_DEFAULT_MODELS`` contains ``code_review_verify``.
    Postconditions: returns a non-empty model id string when the precondition
        holds. Raises ``KeyError`` if ``code_review_verify`` is absent from
        ``AGENT_DEFAULT_MODELS`` (precondition violation).
    """
    per_agent = (os.environ.get(f"{ENV_LLM_MODEL}_{_VERIFY_AGENT_KEY}") or "").strip()
    if per_agent:
        return per_agent
    return AGENT_DEFAULT_MODELS[_VERIFY_AGENT_KEY]


def _apply_code_review_verify_model_pin(
    model: "Union[LLMClient, LLMClientModel]",
) -> "Union[LLMClient, LLMClientModel]":
    """Pin Ollama failover candidates on ``model`` to the verify-key model id.

    Mirrors the blog pipeline's stage-model override: when the backing client is
    a :class:`~llm_service.factory.FailoverLLMClient`, ``with_model_override``
    forces Ollama entries to use the verify pin even if ``entry.model`` is set.
    Non-Ollama (e.g. Claude) candidates keep their configured model — same
    contract as the factory helper. A Dummy / non-failover backing is unchanged.

    The adapter ``model_id`` is updated to the pin only when the pinned
    backing's active ``.model`` matches the pin (the Ollama path). When Claude
    (or another non-Ollama candidate) is active, the original ``model_id`` is
    preserved so observability does not report the Ollama pin for a Claude call.

    Preconditions: ``model`` is an ``LLMClientModel`` (the normal production
        path) or an ``LLMClient`` (for callers that bypass resolution).
    Postconditions: returns a ready-to-use model; ``model`` is never mutated.
    """
    pin = _code_review_verify_model_pin()
    if isinstance(model, LLMClientModel):
        pinned_backing = with_model_override(model.client, pin)
        if pinned_backing is model.client:
            return model
        # Defensive copy: ``get_config`` already returns a fresh dict today, but
        # the postcondition forbids mutating ``model`` regardless of that.
        cfg = dict(model.get_config())
        active_model = getattr(pinned_backing, "model", None)
        if isinstance(active_model, str) and active_model.strip() == pin:
            cfg["model_id"] = pin
        return LLMClientModel(pinned_backing, **cfg)
    return with_model_override(model, pin)


def resolve_code_review_verify_model(
    llm: "Union[LLMClient, _StrandsModel]", think: Optional[Union[bool, str]] = None
) -> "Union[LLMClient, _StrandsModel]":
    """Resolve the strands model for code-review's narrower verify/synthesis sub-passes.

    Same shape as :func:`resolve_code_review_model`, but keyed on the
    ``code_review_verify`` agent key (its own, genuinely lighter
    ``AGENT_DEFAULT_MODELS`` entry) instead of ``code_review`` — intended for
    bounded tasks like false-positive verification (and potentially narrative
    synthesis) as opposed to open-ended chunk review. ``false_positive_filter.py``
    already calls this resolver; ``synthesis.py`` still uses
    :func:`resolve_code_review_model` until its own follow-up wires it over.

    After resolving via ``get_strands_model("code_review_verify")``, the
    production path also pins Ollama failover candidates to the verify key's
    per-agent env / ``AGENT_DEFAULT_MODELS`` entry (see
    :func:`_apply_code_review_verify_model_pin`). Without that pin, a
    non-empty provider-list ``entry.model`` would keep the primary review
    model and the agent-key swap would change attribution only.

    Preconditions:
        - ``llm`` is an ``LLMClient`` or an object implementing the strands
          ``Model`` interface.
        - ``think`` is ``None`` (use the model's default thinking level), or an
          explicit override applied only on the production path.

    Postconditions:
        - Returns ``llm`` itself when it already implements the strands ``Model``
          interface (the test path injects such a client); ``think`` is ignored
          for it (see ``thinking_override_supported``). Otherwise returns a
          ``code_review_verify``-keyed model whose Ollama failover candidates
          use the verify pin (Claude candidates keep their configured model),
          safe to share across concurrent ``Agent`` calls.
    """
    if isinstance(llm, _StrandsModel):
        return llm
    if think is None:
        base = get_strands_model(_VERIFY_AGENT_KEY)
    else:
        base = get_strands_model(_VERIFY_AGENT_KEY, think=think)
    return _apply_code_review_verify_model_pin(base)


def thinking_override_supported(llm: "Union[LLMClient, _StrandsModel]") -> bool:
    """Whether the chunk reviewer can override the thinking level for ``llm``.

    Postconditions:
        - Returns ``False`` when ``llm`` is an injected strands ``Model`` (its
          thinking level is baked in and cannot be re-resolved — the test path),
          ``True`` on the production path where ``resolve_code_review_model``
          builds the model via ``get_strands_model`` and can pass ``think=``.
    """
    return not isinstance(llm, _StrandsModel)
