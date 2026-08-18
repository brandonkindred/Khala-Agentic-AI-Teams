"""Config-driven base for devops_team single-shot JSON agents.

Canonical helper decision
-------------------------
``complete_json_with_continuation`` is the canonical helper for
``devops_team``'s single-shot JSON agents.

``run_structured_persona`` (``shared/persona_agent_base.py``) remains the
pattern for the four agents already using it (``security_agent``,
``qa_agent``, ``accessibility_agent``, ``integration_team``). Switching
devops onto ``run_structured_persona`` was considered and deferred for
these reasons:

1. That helper centralizes dataclass construction via Strands
   ``structured_output_model`` and requires a ``fallback_factory`` per
   agent.
2. Several devops outputs carry nested models
   (``DevOpsCompletionPackage``, ``IaCExecutionError``, ``ReviewFinding``)
   that would need verification against Strands'
   ``structured_output_model`` before a switch.
3. The devops standardization effort only asks to standardize on one
   helper, not to migrate away from ``complete_json_with_continuation``.

Resolution status (post pipeline-framework migration)
-----------------------------------------------------
The devops per-task pipeline later moved onto ``BaseTeamLead`` / shared
phase functions. That control-flow migration did not change how
single-shot JSON agents call the LLM, so each original reason above
still applies as an intentional limitation:

1. Still applies — ``run_structured_persona`` still requires a
   ``fallback_factory`` and ``structured_output_model``; devops agents
   still build outputs in ``build_output`` from a parsed dict instead.
2. Still applies — ``DevOpsCompletionPackage``, ``IaCExecutionError``,
   and ``ReviewFinding`` remain nested models in devops outputs; they
   have not been verified against Strands' ``structured_output_model``.
3. Still applies — devops single-shot agents (including subclasses of
   ``DevOpsSingleShotAgent``) continue to call
   ``complete_json_with_continuation``; no helper migration was in
   scope for the pipeline work.

Monkeypatchability
------------------
This module imports and calls ``complete_json_with_continuation`` from
``software_engineering_team.shared.llm`` directly (no per-subclass-module
lookup). Because the import binds the name on this module, the effective
patch target is
``software_engineering_team.devops_team._agent_template.complete_json_with_continuation``,
not the attribute on ``shared.llm``. When a consumer agent is migrated onto
this base, any test that monkeypatches
``…devops_team.<agent>.agent.complete_json_with_continuation`` must retarget
the patch to
``software_engineering_team.devops_team._agent_template.complete_json_with_continuation``
(or continue patching ``shared.llm.Agent``, which fence-recovery helpers
already do via ``_patch_fenced_response``).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Type

from pydantic import BaseModel

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import model_fingerprint, resolve_strands_model
from shared.cache import get_shared_cache
from software_engineering_team.shared.llm import complete_json_with_continuation
from software_engineering_team.shared.review_result_cache import (
    build_review_cache_key,
    cache_capacity_for,
    cache_namespace_for,
    clear_review_cache_namespace,
    get_cached_review_result,
    set_cached_review_result,
)


class DevOpsSingleShotAgent:
    """Shared scaffolding for devops single-shot JSON agents.

    Invariants:
        - Instance state is limited to ``llm`` and the resolved Strands
          ``_model``.
        - ``run`` is stateless across calls aside from that resolved model.
        - Subclasses set a non-empty ``PROMPT`` and override
          ``build_context`` / ``build_output``; ``pre_call`` may short-circuit.
        - A subclass that sets ``CACHE_NAMESPACE``/``CACHE_ENV_VAR``/
          ``OUTPUT_MODEL`` gets a cache-checked/populated ``run`` for free; an
          unset ``CACHE_NAMESPACE`` (the default) disables caching for that
          subclass, matching the ``capacity <= 0`` passthrough convention.
    """

    PROMPT: str = ""
    PROMPT_SEPARATOR: str = "\n\n---\n\n"
    temperature: Optional[float] = 0.1
    think: Optional[bool] = True
    CACHE_NAMESPACE: str = ""
    CACHE_ENV_VAR: str = ""
    CACHE_DEFAULT_SIZE: int = 128
    OUTPUT_MODEL: Optional[Type[BaseModel]] = None

    def __init__(self, llm_client: LLMClient) -> None:
        """Resolve the devops-routed Strands model.

        Preconditions: ``llm_client`` is not ``None`` (an ``LLMClient`` or a
        Strands ``Model``).
        Postconditions: ``self.llm`` is the passed client; ``self._model`` is
        the resolved Strands model under ``agent_key="devops"``.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = resolve_strands_model(
            llm_client, agent_key="devops", get_strands_model_fn=get_strands_model
        )

    def pre_call(self, input_data: Any) -> Any | None:
        """Optional early-return hook before any LLM call.

        Preconditions: ``input_data`` is whatever the subclass ``run`` accepts.
        Postconditions: returns ``None`` to continue, or a finished output to
        return from ``run`` without calling the LLM. Default: ``None``.
        """
        return None

    def build_context(self, input_data: Any) -> str:
        """Build the context string appended after the prompt separator.

        Preconditions: called only when ``pre_call`` returned ``None``.
        Postconditions: returns a string (possibly empty) concatenated into
        the LLM prompt. Subclasses must override.
        """
        raise NotImplementedError(f"{type(self).__name__}.build_context must be overridden")

    def build_output(self, input_data: Any, data: Dict[str, Any]) -> Any:
        """Construct the agent output from the parsed JSON dict.

        Preconditions: ``data`` is the dict returned by
        ``complete_json_with_continuation``.
        Postconditions: returns the subclass output object. Owns all post-call
        special cases (derived fields, secondary non-LLM objects). Subclasses
        must override.
        """
        raise NotImplementedError(f"{type(self).__name__}.build_output must be overridden")

    def run(self, input_data: Any) -> Any:
        """Run the single-shot LLM call and build the output.

        Preconditions:
            ``self.PROMPT`` is a non-empty string; ``build_context`` and
            ``build_output`` are overridden on the concrete subclass.
        Postconditions:
            If ``pre_call`` returns non-``None``, that value is returned and
            the LLM is not called (no cache lookup either — nothing was
            hashed yet). Otherwise, when ``CACHE_NAMESPACE`` and
            ``CACHE_ENV_VAR`` are both set (non-empty) AND the resolved
            capacity is ``> 0`` — all three conditions gate caching; a
            subclass with ``CACHE_NAMESPACE`` set but ``CACHE_ENV_VAR`` left
            empty (the default) never caches, regardless of
            ``CACHE_DEFAULT_SIZE`` — a cache hit (byte-identical
            ``input_data`` and resolved model) returns the prior
            ``OUTPUT_MODEL`` instance without invoking the LLM. On a cache
            miss, a disabled cache, or any cache-backend error, returns
            ``build_output(input_data, data)`` where ``data`` comes from
            ``complete_json_with_continuation`` with prompt
            ``PROMPT + PROMPT_SEPARATOR + context``, and (when caching is
            enabled) writes the result back to the cache. ``temperature`` /
            ``think`` class attrs are passed as kwargs only when not ``None``.
            LLM/parse errors propagate unchanged.
        """
        early = self.pre_call(input_data)
        if early is not None:
            return early

        assert self.PROMPT, f"{type(self).__name__}.PROMPT must be a non-empty string"

        cache_key: Optional[str] = None
        capacity = 0
        if self.CACHE_NAMESPACE and self.CACHE_ENV_VAR:
            capacity = cache_capacity_for(self.CACHE_ENV_VAR, self.CACHE_DEFAULT_SIZE)
            if capacity > 0:
                assert self.OUTPUT_MODEL is not None, (
                    f"{type(self).__name__}.OUTPUT_MODEL must be set when CACHE_NAMESPACE and "
                    "CACHE_ENV_VAR are set and capacity > 0"
                )
                cache_key = build_review_cache_key(input_data, model_fingerprint(self._model))
                cache = get_shared_cache(cache_namespace_for(self.CACHE_NAMESPACE))
                cached = get_cached_review_result(
                    type(self).__name__, cache, cache_key, self.OUTPUT_MODEL
                )
                if cached is not None:
                    return cached

        context = self.build_context(input_data)
        prompt = self.PROMPT + self.PROMPT_SEPARATOR + context

        kwargs: Dict[str, Any] = {}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.think is not None:
            kwargs["think"] = self.think

        data = complete_json_with_continuation(self._model, prompt, **kwargs)
        result = self.build_output(input_data, data)

        if cache_key is not None:
            cache = get_shared_cache(cache_namespace_for(self.CACHE_NAMESPACE))
            set_cached_review_result(
                type(self).__name__, cache, cache_key, result, capacity=capacity
            )

        return result

    @classmethod
    def clear_cache(cls) -> None:
        """Drop every cached result for this subclass. Intended for test teardown.

        Preconditions:
            - None.
        Postconditions:
            - A no-op when ``cls.CACHE_NAMESPACE`` is unset (caching disabled
              for this subclass). Otherwise this process's view of the
              namespace is empty when the call returns (best-effort across
              Redis), fail-open on any backend error.
        """
        if not cls.CACHE_NAMESPACE:
            return
        clear_review_cache_namespace(
            cls.__name__, lambda: get_shared_cache(cache_namespace_for(cls.CACHE_NAMESPACE))
        )
