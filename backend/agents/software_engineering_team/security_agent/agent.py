"""Cybersecurity Expert agent: security review and vulnerability reporting.

Calls the LLM via a Strands ``Agent`` in ``structured_output_model`` mode
(through :func:`run_structured_persona`), validates the reply against
``SecurityLLMResponse``, and re-derives the approval flag from reported
vulnerabilities. The file-context prefix (language + code under review) is
emitted as a ``CacheBreakpoint`` in system content so the provider caches it
across Code Review / QA / Security gates and retry cycles (Story 2c, Step 2).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from strands import Agent

from llm_service import CacheBreakpoint, LLMClient, get_strands_model
from llm_service.strands_model import model_fingerprint, resolve_strands_model
from shared.cache import get_shared_cache
from shared.cache.pydantic_cache import (
    build_model_cache_key,
    cache_capacity_for,
    cache_namespace_for,
    clear_cache_namespace,
    get_cached_model,
    set_cached_model,
)
from software_engineering_team.shared.persona_agent_base import run_structured_persona
from software_engineering_team.shared.security_service import derive_approved

from .models import SecurityInput, SecurityLLMResponse, SecurityOutput
from .prompts import SECURITY_PROMPT

logger = logging.getLogger(__name__)

_CACHE_LABEL = "Security"

# Shared review-result cache: keyed on the whole SecurityInput content plus
# the resolved review model, so a byte-identical resubmission (e.g. across
# the review->fix->re-review retry loop, or an unchanged sibling task) skips
# the LLM call entirely. Mirrors qa_agent's review cache exactly (same
# whole-input key *shape*, same "cache every genuine outcome regardless of
# approved" *policy*, since this is a single atomic call with no reduce
# phase to short-circuit) — the shared policy itself lives in
# ``shared.cache.pydantic_cache``, imported above; this module supplies only
# its own namespace stem, env var, capacity default, and output model.
# Backed by shared.cache (Redis, falls open to an in-process store). Base
# stem; ``_review_cache_namespace()`` appends build id.
DEFAULT_REVIEW_CACHE_SIZE = 256  # SECURITY_REVIEW_CACHE_SIZE, floor 0
_REVIEW_CACHE_NAMESPACE = "security:review:v1"


def _review_cache_namespace() -> str:
    """Shared-cache namespace for security review results (includes build id)."""
    return cache_namespace_for(_REVIEW_CACHE_NAMESPACE)


def _review_cache_size() -> int:
    """Resolve the review cache capacity from the environment.

    Postconditions:
        - Returns ``SECURITY_REVIEW_CACHE_SIZE`` parsed as an int, clamped to
          a floor of 0: an unset or unparseable value falls back to
          ``DEFAULT_REVIEW_CACHE_SIZE``, a negative value clamps to 0. An
          explicit or clamped-to 0 disables the cache — every ``run()`` call
          re-invokes the model, matching pre-cache behavior.
    """
    return cache_capacity_for("SECURITY_REVIEW_CACHE_SIZE", DEFAULT_REVIEW_CACHE_SIZE)


def clear_review_cache() -> None:
    """Drop every cached security review result.

    Preconditions:
        - None.
    Postconditions:
        - This process's view of the shared review-cache namespace is empty
          when the call returns (best-effort across Redis). A cache backend
          error is caught and logged rather than propagated — fails open,
          same as every other cache operation in this module — so a broken
          backend never breaks a caller (e.g. a test-teardown fixture)
          forcing a cold review. Intended for tests and for callers that
          must force a cold review.
    """
    clear_cache_namespace(_CACHE_LABEL, lambda: get_shared_cache(_review_cache_namespace()))


def _security_model_fingerprint(llm: Optional[LLMClient]) -> str:
    """Best-effort stable identifier for the model a security review will run on.

    Resolves a Strands model for identity purposes via ``resolve_strands_model``,
    then delegates the attribute probing to
    ``llm_service.strands_model.model_fingerprint``.

    Preconditions:
        - ``llm`` is ``None`` or an ``LLMClient`` (the value this
          ``CybersecurityExpertAgent`` instance was constructed with).

    Postconditions:
        - Returns a string that changes when the resolved review model
          changes, so it can invalidate the review cache. Never raises: any
          failure to resolve the model falls back to ``type(llm).__name__``.
          The value is identity-only — safe to hash into a cache key, never
          a secret.
    """
    try:
        model = resolve_strands_model(
            llm, agent_key="security", get_strands_model_fn=get_strands_model
        )
    except Exception:
        logger.warning(
            "Security: model fingerprint resolution failed; falling back to client type name",
            exc_info=True,
        )
        return type(llm).__name__
    return model_fingerprint(model)


def _review_cache_key(input_data: SecurityInput, model_fp: str) -> str:
    """Hash of the whole security input plus the resolved review model.

    Keys the entire ``SecurityInput`` — code, language, task description,
    architecture, context — so any reviewed-file byte change naturally
    busts the key with no explicit invalidation logic. ``SecurityInput``
    carries no per-invocation id field, so nothing needs to be excluded
    before hashing.

    Preconditions:
        - ``input_data`` is a valid ``SecurityInput``.
        - ``model_fp`` is the value returned by
          ``_security_model_fingerprint(self.llm)`` for this
          ``CybersecurityExpertAgent`` instance.

    Postconditions:
        - Returns a hex digest that changes whenever any input field or the
          resolved model changes, and is stable (``sort_keys``) across calls
          in a process, so a byte-identical resubmission is recognized.
    """
    return build_model_cache_key(input_data, model_fp)


def _build_security_file_context_prefix(input_data: SecurityInput) -> list[str]:
    """Render the microtask file context (language + code) as a stable prefix.

    Positioned ahead of ``_build_security_role_instructions``'s output in the
    assembled user prompt (reorder/isolation only; no cache marking here).

    Preconditions:
        - ``input_data`` is a valid ``SecurityInput`` with ``code`` set.

    Postconditions:
        - Returns non-empty prompt lines: the language line, then the code
          fence around ``input_data.code``. Never raises or transforms the code.
    """
    return [
        f"**Language:** {input_data.language}",
        "**Code to review:**",
        "```",
        input_data.code,
        "```",
    ]


def _build_security_role_instructions(input_data: SecurityInput) -> list[str]:
    """Render the security-specific review instructions that follow the file-context prefix.

    The words "security" and "vulnerabilities" MUST appear here because
    ``DummyLLMClient.complete_json`` pattern-matches on them (substring check
    over the whole prompt, order-independent) to return a deterministic stub
    in tests — see llm_service/README.md "Migration rule: keep pattern
    anchors in the user prompt".

    Preconditions:
        - ``input_data`` is a valid ``SecurityInput``.

    Postconditions:
        - Returns non-empty prompt lines: the schema-hint sentence, then the
          task description, context, and architecture when each is present —
          in that fixed order. Never raises.
    """
    parts = [
        "",
        "Review the code for security vulnerabilities. Produce "
        "structured JSON with fields: vulnerabilities, summary, "
        "remediations. Each vulnerability must include severity, "
        "category, description, location, and recommendation.",
    ]
    if input_data.task_description:
        parts.append(f"**Task:** {input_data.task_description}")
    if input_data.context:
        parts.append(f"**Context:** {input_data.context}")
    if input_data.architecture:
        parts.append(f"**Architecture:** {input_data.architecture.overview}")
    return parts


class CybersecurityExpertAgent:
    """
    Cybersecurity expert that reviews code for security flaws. Reports
    vulnerabilities for the coding agent to remediate — never patches them
    itself (see ``SecurityOutput.vulnerabilities``).
    """

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        """Resolve the review model and store the system prompt.

        Preconditions: ``llm_client`` is ``None``, an ``LLMClient``, or a
        Strands ``Model``.
        Postconditions: ``self._model`` is a usable Strands model, and
        ``self._llm_client`` preserves the original client for fingerprinting.
        """
        self._llm_client = llm_client
        self._model = resolve_strands_model(
            llm_client, agent_key="security", get_strands_model_fn=get_strands_model
        )

    def run(self, input_data: SecurityInput) -> SecurityOutput:
        """Review code for security vulnerabilities.

        Preconditions:
            ``input_data`` is a ``SecurityInput`` (``code`` may be empty).
        Postconditions:
            Returns a ``SecurityOutput`` whose ``approved`` is re-derived by the
            unified rule (:func:`derive_approved`): ``approved`` is ``False`` iff
            any vulnerability is critical/high severity (severity comparison is
            case-insensitive), regardless of any ``approved`` flag the model
            returned. On any model/validation failure surviving
            ``run_structured_persona``'s try/except, returns a safe
            fallback with ``approved=False`` and no vulnerabilities. Never
            raises. A cache hit (byte-identical ``SecurityInput`` and resolved
            model) returns the prior result without invoking the LLM. A cache
            miss, a disabled cache (``SECURITY_REVIEW_CACHE_SIZE=0``), or any
            cache backend error falls open to a genuine review. Only a
            genuine (non-fallback) result is written back to the cache,
            regardless of ``approved``.
        """
        logger.info("Security: reviewing %s chars of code", len(input_data.code or ""))

        capacity = _review_cache_size()
        cache_key: Optional[str] = None
        if capacity > 0:
            cache_key = _review_cache_key(
                input_data, _security_model_fingerprint(self._llm_client)
            )
            cache = get_shared_cache(_review_cache_namespace())
            cached_result = get_cached_model(_CACHE_LABEL, cache, cache_key, SecurityOutput)
            if cached_result is not None:
                logger.info(
                    "Security: review cache hit; skipping LLM call (approved=%s)",
                    cached_result.approved,
                )
                return cached_result

        user_prompt = self._build_user_prompt(input_data)

        is_fallback = False
        _fallback_summary = ""

        def _fallback(exc: Exception) -> SecurityLLMResponse:
            nonlocal is_fallback, _fallback_summary
            is_fallback = True
            _fallback_summary = f"Security analysis failed: {exc}"
            logger.warning("Security: structured_output failed (%s); returning fallback", exc)
            return SecurityLLMResponse(
                vulnerabilities=[],
                summary=_fallback_summary,
                remediations=[],
            )

        def _finalize(response: SecurityLLMResponse) -> SecurityLLMResponse:
            return response

        # The file-context prefix (language + code) is marked as a
        # CacheBreakpoint in system content so the provider caches it across
        # Code Review / QA / Security gates and retry cycles (Story 2c, Step 2).
        file_context_content: List[CacheBreakpoint] = []
        prefix_text = "\n".join(_build_security_file_context_prefix(input_data))
        if prefix_text:
            file_context_content = [CacheBreakpoint(prefix_text)]

        response = run_structured_persona(
            model=self._model,
            system_prompt=SECURITY_PROMPT,
            user_prompt=user_prompt,
            output_model=SecurityLLMResponse,
            fallback_factory=_fallback,
            agent_factory=Agent,
            on_success=_finalize,
            system_prompt_content=file_context_content or None,
        )

        # On fallback, return a safe SecurityOutput with approved=False — do not
        # re-derive approval from the empty vulnerability list (which would
        # incorrectly yield approved=True).
        if is_fallback:
            return SecurityOutput(
                vulnerabilities=[],
                approved=False,
                summary=_fallback_summary,
                remediations=[],
                suggested_commit_message="",
            )

        # Re-derive ``approved`` via the unified rule so a disagreement between
        # the LLM's ``approved`` flag (not part of ``SecurityLLMResponse`` — the
        # prompt never asks for one) and the reported vulnerability list is
        # resolved in favor of the vulnerability list. ``SecurityVulnerability``
        # has no ``blocking`` attribute, so this reduces to "no critical/high".
        result = SecurityOutput(
            vulnerabilities=response.vulnerabilities,
            summary=response.summary,
            remediations=response.remediations,
        )
        result.approved = derive_approved(result.vulnerabilities, llm_approved=None)

        logger.info(
            "Security: done, %s issues found, approved=%s",
            len(result.vulnerabilities),
            result.approved,
        )

        if cache_key is not None:
            cache = get_shared_cache(_review_cache_namespace())
            set_cached_model(_CACHE_LABEL, cache, cache_key, result, capacity=capacity)

        return result

    @staticmethod
    def _build_user_prompt(input_data: SecurityInput) -> str:
        """Assemble the user-facing prompt.

        The persona (``SECURITY_PROMPT``) lives on the Strands ``Agent``'s
        system prompt, alongside the ``CacheBreakpoint``-marked file-context
        prefix (see ``run()``). The user prompt carries only the per-gate
        role instructions — the schema hint, task description, context, and
        architecture. The words "security" and "vulnerabilities" MUST appear
        somewhere in the prompt because ``DummyLLMClient.complete_json``
        pattern-matches on them (order-independent substring check) to return
        a deterministic stub in tests — see llm_service/README.md "Migration
        rule: keep pattern anchors in the user prompt".
        """
        # Only role instructions in the user prompt — the file-context prefix
        # (language + code) is emitted as a CacheBreakpoint in system content.
        parts = _build_security_role_instructions(input_data)
        return "\n".join(parts)
