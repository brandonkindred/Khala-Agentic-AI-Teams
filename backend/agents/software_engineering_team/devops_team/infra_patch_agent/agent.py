"""Infrastructure Patch agent -- produces minimal IaC artifact patches."""

from __future__ import annotations

import logging

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

from .models import IaCPatchInput, IaCPatchOutput
from .prompts import INFRA_PATCH_PROMPT

logger = logging.getLogger(__name__)

_CACHE_LABEL = "InfraPatch"

# Shared review-result cache: keyed on the whole IaCPatchInput content plus
# the resolved model. Only covers the LLM-backed tail of run() — the
# "not fixable" early return above it always runs and never touches the
# cache. The shared policy lives in
# ``software_engineering_team.shared.review_result_cache``; this module
# supplies only its own namespace stem, env var, capacity default, and
# output model.
_REVIEW_CACHE_NAMESPACE = "devops:infra_patch:v1"
DEFAULT_REVIEW_CACHE_SIZE = 128  # DEVOPS_INFRA_PATCH_CACHE_SIZE, floor 0


def _review_cache_namespace() -> str:
    """Shared-cache namespace for infra patch results (includes build id)."""
    return cache_namespace_for(_REVIEW_CACHE_NAMESPACE)


def _review_cache_size() -> int:
    """Resolve the review cache capacity from the environment."""
    return cache_capacity_for("DEVOPS_INFRA_PATCH_CACHE_SIZE", DEFAULT_REVIEW_CACHE_SIZE)


def clear_review_cache() -> None:
    """Drop every cached infra patch result. Intended for test teardown."""
    clear_review_cache_namespace(_CACHE_LABEL, lambda: get_shared_cache(_review_cache_namespace()))


class InfraPatchAgent:
    """Produces minimal IaC artifact patches for fixable debug errors.

    Invariants: instance state is limited to ``llm`` and ``_model``; ``run``
    is stateless across calls.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        """Store the review client and resolve its Strands model.

        Preconditions: ``llm_client`` is not ``None`` (an ``LLMClient``).
        Postconditions: ``self.llm`` is the stored client; ``self._model`` is
        the resolved Strands model under ``agent_key="devops"``.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = resolve_strands_model(
            llm_client, agent_key="devops", get_strands_model_fn=get_strands_model
        )

    def run(self, input_data: IaCPatchInput) -> IaCPatchOutput:
        """Patch the artifacts named by a fixable debug result.

        Preconditions:
            ``input_data`` is a valid ``IaCPatchInput``.
        Postconditions:
            When ``input_data.debug_output.fixable`` is ``False``, returns an
            empty ``IaCPatchOutput`` immediately — no LLM call, no cache
            lookup. Otherwise, a cache hit (byte-identical ``input_data`` and
            resolved model) returns the prior result without invoking the
            LLM. A cache miss, a disabled cache
            (``DEVOPS_INFRA_PATCH_CACHE_SIZE=0``), or any cache-backend error
            falls open to a genuine call, and every result reaching the
            cache-write step is written back (this method has no fallback
            branch of its own past the ``fixable`` check).
        """
        if not input_data.debug_output.fixable:
            return IaCPatchOutput(
                summary="Errors are not fixable via code changes",
            )

        capacity = _review_cache_size()
        cache_key = None
        if capacity > 0:
            cache_key = build_review_cache_key(input_data, model_fingerprint(self._model))
            cache = get_shared_cache(_review_cache_namespace())
            cached = get_cached_review_result(_CACHE_LABEL, cache, cache_key, IaCPatchOutput)
            if cached is not None:
                return cached

        errors_text = "\n".join(
            f"- [{e.error_type}] {e.file_path or '?'}:{e.line_number or '?'} — {e.error_message}"
            for e in input_data.debug_output.errors
        )

        artifacts_text = ""
        for fname, content in input_data.original_artifacts.items():
            artifacts_text += f"\n### {fname} ###\n{content}\n"

        context = f"--- Errors ---\n{errors_text}\n\n--- Current Artifacts ---\n{artifacts_text}\n"

        data = complete_json_with_continuation(
            self._model,
            INFRA_PATCH_PROMPT + "\n\n---\n\n" + context,
            temperature=0.1,
            think=True,
        )

        patched = data.get("patched_artifacts") or {}
        patched = {k: v for k, v in patched.items() if v and v.strip()}

        result = IaCPatchOutput(
            patched_artifacts=patched,
            summary=data.get("summary", ""),
            edits_applied=data.get("edits_applied", len(patched)),
        )

        if cache_key is not None:
            cache = get_shared_cache(_review_cache_namespace())
            set_cached_review_result(_CACHE_LABEL, cache, cache_key, result, capacity=capacity)

        return result
