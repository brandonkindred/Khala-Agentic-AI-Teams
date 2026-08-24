"""Cybersecurity Expert agent: security review and vulnerability reporting.

Calls the LLM via ``shared.single_shot_review.run_single_shot_review`` in
schema-validated mode, which resolves the client, validates the reply
against ``SecurityLLMResponse``, and drives one bounded corrective retry
(re-prompting with the schema/validation error) before falling back. The
file-context prefix (language + code under review) is kept in the user
message — it is untrusted repository content and must not be elevated to
system-level instructions.
"""

from __future__ import annotations

import logging
from typing import Optional

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import model_fingerprint, resolve_strands_model
from software_engineering_team.shared.review_prompt_utils import build_file_context_prefix
from software_engineering_team.shared.review_result_cache import ReviewResultCache
from software_engineering_team.shared.security_service import derive_approved
from software_engineering_team.shared.single_shot_review import run_single_shot_review

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
# phase to short-circuit) — the shared ``ReviewResultCache`` supplies the
# get/put/clear policy; this module supplies only its own namespace stem,
# env var, capacity default, and output model.
DEFAULT_REVIEW_CACHE_SIZE = 256  # SECURITY_REVIEW_CACHE_SIZE, floor 0
_REVIEW_CACHE: ReviewResultCache[SecurityOutput] = ReviewResultCache(
    namespace_stem="security:review:v1",
    env_var="SECURITY_REVIEW_CACHE_SIZE",
    default_capacity=DEFAULT_REVIEW_CACHE_SIZE,
    label=_CACHE_LABEL,
    output_model=SecurityOutput,
)


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
    _REVIEW_CACHE.clear()


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
    return build_file_context_prefix(input_data.language, input_data.code)


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
        """Store the injectable review client.

        Preconditions: ``llm_client`` is ``None`` or an ``LLMClient``.
        Postconditions: ``self.llm`` is ``llm_client`` unchanged —
        ``run_single_shot_review`` resolves the default ``security`` client
        per call when it is ``None``.
        """
        self.llm = llm_client

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
            ``run_single_shot_review``'s corrective retry, returns a safe
            fallback with ``approved=False`` and no vulnerabilities. Never
            raises. A cache hit (byte-identical ``SecurityInput`` and resolved
            model) returns the prior result without invoking the LLM. A cache
            miss, a disabled cache (``SECURITY_REVIEW_CACHE_SIZE=0``), or any
            cache backend error falls open to a genuine review. Only a
            genuine (non-fallback) result is written back to the cache,
            regardless of ``approved``.
        """
        logger.info("Security: reviewing %s chars of code", len(input_data.code or ""))

        model_fp = _security_model_fingerprint(self.llm)
        cached_result = _REVIEW_CACHE.get(input_data, model_fp)
        if cached_result is not None:
            logger.info(
                "Security: review cache hit; skipping LLM call (approved=%s)",
                cached_result.approved,
            )
            return cached_result

        user_prompt = self._build_user_prompt(input_data)

        try:
            response = run_single_shot_review(
                self.llm,
                agent_key="security",
                prompt=user_prompt,
                system_prompt=SECURITY_PROMPT,
                schema=SecurityLLMResponse,
                objective="security review",
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 — LLM/validation failures must not crash the run
            logger.warning("Security: structured_output failed (%s); returning fallback", exc)
            return SecurityOutput(
                vulnerabilities=[],
                approved=False,
                summary=f"Security analysis failed: {exc}",
                remediations=[],
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

        _REVIEW_CACHE.put(input_data, model_fp, result)

        return result

    @staticmethod
    def _build_user_prompt(input_data: SecurityInput) -> str:
        """Assemble the user-facing prompt.

        The persona (``SECURITY_PROMPT``) is passed as
        ``run_single_shot_review``'s ``system_prompt``. The user prompt
        carries the code under review (see ``_build_security_file_context_prefix``)
        followed by the per-gate role instructions — the schema hint, task
        description, context, and architecture. The code under review is
        untrusted repository content and must stay in the user message, not
        be elevated to system-level instructions. The words "security" and
        "vulnerabilities" MUST appear somewhere in the prompt because
        ``DummyLLMClient.complete_json`` pattern-matches on them
        (order-independent substring check) to return a deterministic stub
        in tests — see llm_service/README.md "Migration rule: keep pattern
        anchors in the user prompt".
        """
        parts = _build_security_file_context_prefix(input_data) + _build_security_role_instructions(
            input_data
        )
        return "\n".join(parts)
