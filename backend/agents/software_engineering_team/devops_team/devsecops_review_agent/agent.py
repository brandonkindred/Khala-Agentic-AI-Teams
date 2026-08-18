"""DevSecOps review agent."""

from __future__ import annotations

import logging

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import model_fingerprint, resolve_strands_model
from shared.cache import get_shared_cache
from software_engineering_team.shared.llm import DEFAULT_JSON_SYSTEM_PROMPT
from software_engineering_team.shared.review_result_cache import (
    build_review_cache_key,
    cache_capacity_for,
    cache_namespace_for,
    clear_review_cache_namespace,
    get_cached_review_result,
    set_cached_review_result,
)
from software_engineering_team.shared.security_service import derive_approved
from software_engineering_team.shared.single_shot_review import run_single_shot_review

from .models import DevSecOpsReviewInput, DevSecOpsReviewLLMResponse, DevSecOpsReviewOutput
from .prompts import DEVSECOPS_REVIEW_PROMPT

logger = logging.getLogger(__name__)

_CACHE_LABEL = "DevSecOps"


def clear_review_cache() -> None:
    """Drop every cached DevSecOps review result. Intended for test teardown."""
    clear_review_cache_namespace(
        _CACHE_LABEL,
        lambda: get_shared_cache(cache_namespace_for(DevSecOpsReviewAgent.CACHE_NAMESPACE)),
    )


class DevSecOpsReviewAgent:
    """Infra security reviewer for DevOps artifacts (IAM/secrets/network).

    Invariants: instance state is limited to the injectable ``llm`` client
    and the resolved Strands ``_model``. ``run`` is deterministic for
    identical inputs and the resolved model: repeated identical calls may
    return a cached result and skip the LLM. Cache reads/writes are
    fail-open and gated by ``DEVOPS_DEVSECOPS_CACHE_SIZE``.
    """

    # Shared review-result cache: keyed on the whole DevSecOpsReviewInput
    # content plus the resolved review model, so a byte-identical
    # resubmission (e.g. a retry cycle) skips the LLM call entirely. Same
    # shape as qa_agent's / security_agent's review cache — the shared
    # policy lives in
    # ``software_engineering_team.shared.review_result_cache``; this class
    # supplies only its own namespace stem, env var, capacity default, and
    # output model. Public (mirrors ``DevOpsSingleShotAgent`` subclasses'
    # ``CACHE_NAMESPACE``) so tests reference this constant rather than a
    # private module attribute.
    CACHE_NAMESPACE = "devops:devsecops:v1"
    CACHE_ENV_VAR = "DEVOPS_DEVSECOPS_CACHE_SIZE"
    CACHE_DEFAULT_SIZE = 128

    def __init__(self, llm_client: LLMClient) -> None:
        """Store the review client and resolve its Strands model.

        Preconditions: ``llm_client`` is not None (an ``LLMClient``).
        Postconditions: ``self.llm`` is the stored client, passed to
        ``run_single_shot_review`` verbatim on every ``run`` call.
        ``self._model`` is the resolved Strands model under
        ``agent_key="devops"``, used only to fingerprint the cache key (the
        actual call still routes through ``run_single_shot_review(self.llm,
        ...)``, not ``self._model``).
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = resolve_strands_model(
            llm_client, agent_key="devops", get_strands_model_fn=get_strands_model
        )

    def run(self, input_data: DevSecOpsReviewInput) -> DevSecOpsReviewOutput:
        """Review DevOps artifacts and derive a blocking decision.

        Preconditions:
            ``input_data`` is a ``DevSecOpsReviewInput``.
        Postconditions:
            Returns a ``DevSecOpsReviewOutput`` whose ``approved`` follows the
            unified rule (:func:`derive_approved`): any blocking finding
            (critical/high severity or an explicit ``blocking`` flag) forces
            ``approved=False``; otherwise the model's ``approved`` is honored. An
            ``approved`` value that is present but null is treated as an explicit
            non-approval (fail closed), matching the legacy contract; an absent
            key defers entirely to the finding-derived default. On any
            model/validation failure surviving ``run_single_shot_review``'s
            corrective retry, returns a safe fallback with ``approved=False``, no
            findings, and a diagnostic summary. Never raises.

            A cache hit (byte-identical ``input_data`` and resolved model)
            returns the prior result without invoking the LLM. A cache miss,
            a disabled cache (``DEVOPS_DEVSECOPS_CACHE_SIZE=0``), or any cache
            backend error falls open to a genuine review. Only a genuine
            (non-fallback) result is written back to the cache.
        """
        context = (
            f"task={input_data.task_description}\n"
            f"requirements={input_data.requirements}\n"
            f"artifacts={list(input_data.artifacts.keys())}\n"
        )

        capacity = cache_capacity_for(self.CACHE_ENV_VAR, self.CACHE_DEFAULT_SIZE)
        cache_key = None
        if capacity > 0:
            cache_key = build_review_cache_key(input_data, model_fingerprint(self._model))
            cache = get_shared_cache(cache_namespace_for(self.CACHE_NAMESPACE))
            cached = get_cached_review_result(_CACHE_LABEL, cache, cache_key, DevSecOpsReviewOutput)
            if cached is not None:
                return cached

        try:
            response = run_single_shot_review(
                self.llm,
                agent_key="devops",
                prompt=DEVSECOPS_REVIEW_PROMPT + "\n\n---\n\n" + context,
                system_prompt=DEFAULT_JSON_SYSTEM_PROMPT,
                schema=DevSecOpsReviewLLMResponse,
                temperature=0.0,
                think=True,
            )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning("DevSecOps: review failed (%s); returning fallback", exc)
            return DevSecOpsReviewOutput(
                approved=False,
                findings=[],
                summary=f"DevSecOps review failed: {exc}",
            )

        # Distinguish an absent ``approved`` key (no opinion -> defer to findings)
        # from a present-but-null value (an explicit non-approval -> fail closed).
        llm_approved = bool(response.approved) if "approved" in response.model_fields_set else None
        approved = derive_approved(response.findings, llm_approved=llm_approved)
        result = DevSecOpsReviewOutput(
            approved=approved,
            findings=response.findings,
            summary=response.summary,
        )

        if cache_key is not None:
            cache = get_shared_cache(cache_namespace_for(self.CACHE_NAMESPACE))
            set_cached_review_result(_CACHE_LABEL, cache, cache_key, result, capacity=capacity)

        return result
