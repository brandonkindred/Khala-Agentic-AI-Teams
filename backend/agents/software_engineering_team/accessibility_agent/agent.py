"""Accessibility Expert agent: WCAG 2.2 compliance review.

Built on the AWS Strands Agents SDK via ``llm_service.get_strands_model``. The
model returned by ``get_strands_model`` is passed to a Strands ``Agent`` so the
agent inherits retries, per-agent model routing, telemetry, and the
dummy-client path for tests.
"""

from __future__ import annotations

import logging

from strands import Agent

from llm_service import get_strands_model
from llm_service.strands_model import model_fingerprint, resolve_strands_model
from software_engineering_team.shared.persona_agent_base import run_structured_persona
from software_engineering_team.shared.review_result_cache import ReviewResultCache
from software_engineering_team.shared.security_service import derive_approved

from .models import AccessibilityInput, AccessibilityOutput
from .prompts import ACCESSIBILITY_PROMPT

logger = logging.getLogger(__name__)

_CACHE_LABEL = "Accessibility"

# Shared review-result cache: keyed on the whole AccessibilityInput content
# plus the resolved review model, so a byte-identical resubmission skips the
# LLM call entirely. Same shared ``ReviewResultCache`` used by qa_agent's and
# security_agent's analogous caches; this module supplies only its own
# namespace stem, env var, capacity default, and output model.
DEFAULT_REVIEW_CACHE_SIZE = 256  # ACCESSIBILITY_REVIEW_CACHE_SIZE, floor 0
_REVIEW_CACHE: ReviewResultCache[AccessibilityOutput] = ReviewResultCache(
    namespace_stem="accessibility:review:v1",
    env_var="ACCESSIBILITY_REVIEW_CACHE_SIZE",
    default_capacity=DEFAULT_REVIEW_CACHE_SIZE,
    label=_CACHE_LABEL,
    output_model=AccessibilityOutput,
)


def clear_review_cache() -> None:
    """Drop every cached Accessibility review result.

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
    _REVIEW_CACHE.clear()


class AccessibilityExpertAgent:
    """
    Accessibility expert that reviews frontend code for WCAG 2.2 compliance
    and produces a list of issues for the coding agent to fix.
    """

    def __init__(self, llm_client=None) -> None:
        self._model = resolve_strands_model(
            llm_client, agent_key="accessibility", get_strands_model_fn=get_strands_model
        )

    def run(self, input_data: AccessibilityInput) -> AccessibilityOutput:
        """Review code for WCAG 2.2 compliance and produce issue list.

        Preconditions:
            - ``input_data`` is a valid :class:`AccessibilityInput`.
        Postconditions:
            - A cache hit (byte-identical ``AccessibilityInput`` and resolved
              model) returns the prior result without invoking the LLM. A
              cache miss, a disabled cache
              (``ACCESSIBILITY_REVIEW_CACHE_SIZE=0``), or any cache backend
              error falls open to a genuine review — never raises for a
              cache failure. Only a genuine (non-fallback) result is written
              back to the cache, regardless of ``approved``.
        """
        logger.info("Accessibility: reviewing %s chars of code", len(input_data.code or ""))

        model_fp = model_fingerprint(self._model)
        if _REVIEW_CACHE.capacity() > 0:
            cached_result = _REVIEW_CACHE.get(input_data, model_fp)
            if cached_result is not None:
                logger.info(
                    "Accessibility: review cache hit; skipping LLM call (approved=%s)",
                    cached_result.approved,
                )
                return cached_result

        user_prompt = self._build_user_prompt(input_data)

        is_fallback = False

        def _fallback(exc: Exception) -> AccessibilityOutput:
            nonlocal is_fallback
            is_fallback = True
            logger.warning("Accessibility: structured_output failed (%s); returning fallback", exc)
            return AccessibilityOutput(
                issues=[],
                approved=False,
                summary=f"Accessibility analysis failed: {exc}",
            )

        def _finalize(result: AccessibilityOutput) -> AccessibilityOutput:
            # Re-derive ``approved`` from severities so a disagreement between the
            # LLM's ``approved`` flag and the reported issue list is resolved in
            # favor of the issue list. Only applied to a genuine model result —
            # the fallback above is already a final, safe ``approved=False``.
            result.approved = derive_approved(result.issues, llm_approved=None)
            return result

        # A fresh Strands Agent per call — reusing the same instance across
        # calls breaks structured_output forced-tool-choice on the second
        # call (Strands accumulates message history).
        result = run_structured_persona(
            model=self._model,
            system_prompt=ACCESSIBILITY_PROMPT,
            user_prompt=user_prompt,
            output_model=AccessibilityOutput,
            fallback_factory=_fallback,
            agent_factory=Agent,
            on_success=_finalize,
        )

        logger.info(
            "Accessibility: done, %s issues found, approved=%s",
            len(result.issues),
            result.approved,
        )

        if not is_fallback:
            _REVIEW_CACHE.put(input_data, model_fp, result)

        return result

    @staticmethod
    def _build_user_prompt(input_data: AccessibilityInput) -> str:
        """Assemble the user-facing prompt.

        The persona (``ACCESSIBILITY_PROMPT``) lives on the Strands
        ``Agent``'s system prompt. The user prompt carries the code under
        review plus an explicit schema hint. The words "accessibility",
        "wcag", and "issues" MUST appear here because
        ``DummyLLMClient.complete_json`` pattern-matches on them to return
        a deterministic stub in tests — see llm_service/README.md
        "Migration rule: keep pattern anchors in the user prompt".
        """
        parts = [
            "Review the following code for WCAG 2.2 accessibility issues. "
            "Produce structured JSON with fields: issues, summary, approved. "
            "Each issue must include severity, wcag_criterion, description, "
            "location, and recommendation.",
            "",
            f"**Language:** {input_data.language}",
        ]
        if input_data.task_description:
            parts.append(f"**Task:** {input_data.task_description}")
        parts.extend(
            [
                "**Code to review:**",
                "```",
                input_data.code,
                "```",
            ]
        )
        if input_data.architecture:
            parts.append(f"**Architecture:** {input_data.architecture.overview}")

        return "\n".join(parts)
