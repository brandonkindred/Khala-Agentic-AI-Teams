"""Cybersecurity Expert agent: security review and vulnerability reporting.

Calls the LLM via ``shared.single_shot_review.run_single_shot_review`` in
schema-validated mode, which resolves the client, validates the reply
against ``SecurityLLMResponse``, and drives one bounded corrective retry
(re-prompting with the schema/validation error) before falling back — in
place of the single-shot, no-retry Strands ``structured_output_model`` path
this agent used previously.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import model_fingerprint, resolve_strands_model
from shared.cache import get_shared_cache
from shared.env_config import env_int
from software_engineering_team.shared.security_service import derive_approved
from software_engineering_team.shared.single_shot_review import run_single_shot_review

from .models import SecurityInput, SecurityLLMResponse, SecurityOutput
from .prompts import SECURITY_PROMPT

logger = logging.getLogger(__name__)

# Shared review-result cache: keyed on the whole SecurityInput content plus
# the resolved review model, so a byte-identical resubmission (e.g. across
# the review->fix->re-review retry loop, or an unchanged sibling task) skips
# the LLM call entirely. Mirrors qa_agent's review cache exactly (same
# whole-input key *shape*, same "cache every genuine outcome regardless of
# approved" *policy*, since this is a single atomic call with no reduce
# phase to short-circuit) — see qa_agent/agent.py's module-level comment for
# the fuller rationale shared by both. Backed by shared.cache (Redis, falls
# open to an in-process store). Base stem; ``_review_cache_namespace()``
# appends build id.
DEFAULT_REVIEW_CACHE_SIZE = 256  # SECURITY_REVIEW_CACHE_SIZE, floor 0
_REVIEW_CACHE_NAMESPACE = "security:review:v1"


def _review_cache_namespace() -> str:
    """Shared-cache namespace for security review results (includes build id)."""
    from shared.cache import with_cache_build_id  # noqa: PLC0415

    return with_cache_build_id(_REVIEW_CACHE_NAMESPACE)


def _review_cache_size() -> int:
    """Resolve the review cache capacity from the environment.

    Postconditions:
        - Returns ``SECURITY_REVIEW_CACHE_SIZE`` parsed as an int, clamped to
          a floor of 0: an unset or unparseable value falls back to
          ``DEFAULT_REVIEW_CACHE_SIZE``, a negative value clamps to 0. An
          explicit or clamped-to 0 disables the cache — every ``run()`` call
          re-invokes the model, matching pre-cache behavior.
    """
    return env_int("SECURITY_REVIEW_CACHE_SIZE", DEFAULT_REVIEW_CACHE_SIZE, 0)


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
    try:
        get_shared_cache(_review_cache_namespace()).clear()
    except Exception:
        logger.warning("Security: review cache clear failed", exc_info=True)


def _security_model_fingerprint(llm: Optional[LLMClient]) -> str:
    """Best-effort stable identifier for the model a security review will run on.

    Unlike ``qa_agent``, this agent never resolves/holds a Strands model —
    it calls the LLM via ``run_single_shot_review`` on the raw ``self.llm``.
    Mirrors ``code_review_agent.mapping._review_model_fingerprint``: resolve
    a Strands model purely for identity purposes via the generic
    ``resolve_strands_model`` (not the code-review-specific resolver), then
    delegate the attribute probing to
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
    payload = input_data.model_dump(mode="json")
    payload["__model__"] = model_fp
    body = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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

        capacity = _review_cache_size()
        cache_key: Optional[str] = None
        if capacity > 0:
            cache_key = _review_cache_key(input_data, _security_model_fingerprint(self.llm))
            cache = get_shared_cache(_review_cache_namespace())
            # shared.cache is fail-open, but keep an explicit local guard so a
            # misbehaving backend / unexpected raise never aborts the review
            # (mirrors qa_agent's review cache).
            try:
                raw = cache.get(cache_key)
            except Exception:
                logger.warning("Security: review cache get failed; treating as miss", exc_info=True)
                raw = None
            if raw is not None:
                try:
                    cached_result = SecurityOutput.model_validate_json(raw)
                except Exception:
                    logger.warning(
                        "Security: corrupt review cache entry for %s; treating as miss",
                        cache_key,
                        exc_info=True,
                    )
                    try:
                        cache.delete(cache_key)
                    except Exception:
                        logger.warning(
                            "Security: review cache delete failed after corrupt entry",
                            exc_info=True,
                        )
                else:
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
            payload = result.model_dump_json().encode("utf-8")
            try:
                get_shared_cache(_review_cache_namespace()).set(
                    cache_key, payload, max_entries=capacity
                )
            except Exception:
                logger.warning(
                    "Security: review cache set failed; continuing without cache write",
                    exc_info=True,
                )
            else:
                logger.info(
                    "Security: cached review result under key=%s (bytes=%d)",
                    cache_key,
                    len(payload),
                )

        return result

    @staticmethod
    def _build_user_prompt(input_data: SecurityInput) -> str:
        """Assemble the user-facing prompt.

        The persona (``SECURITY_PROMPT``) is passed as
        ``run_single_shot_review``'s ``system_prompt``. The user prompt
        carries the code under review plus an explicit schema hint. The
        words "security" and "vulnerabilities" MUST appear here because
        ``DummyLLMClient.complete_json`` pattern-matches on them to return a
        deterministic stub in tests — see llm_service/README.md "Migration
        rule: keep pattern anchors in the user prompt".
        """
        parts = [
            "Review the following code for security vulnerabilities. Produce "
            "structured JSON with fields: vulnerabilities, summary, "
            "remediations. Each vulnerability must include severity, "
            "category, description, location, and recommendation.",
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
        if input_data.context:
            parts.append(f"**Context:** {input_data.context}")
        if input_data.architecture:
            parts.append(f"**Architecture:** {input_data.architecture.overview}")

        return "\n".join(parts)
