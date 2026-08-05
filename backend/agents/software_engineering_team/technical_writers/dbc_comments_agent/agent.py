"""Design by Contract Comments agent: reviews code and adds DbC-compliant comments."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from llm_service import LLMClient, complete_validated, get_client

from .merge import apply_dbc_insertions
from .models import (
    DbcCommentsInput,
    DbcCommentsLLMResponse,
    DbcCommentsOutput,
    DbcCommentsStatus,
)
from .prompts import DBC_COMMENTS_PROMPT

logger = logging.getLogger(__name__)

# Total LLM-call budget for one run(): 1 initial attempt + 1 automatic retry.
# complete_validated's own correction_attempts only retries a JSON-parse/
# schema-validation failure -- everything else that can escape it untouched
# (LLMNotConfiguredError, rate limits, semantic exhaustion, truncation, a
# bare network error) gets zero retries from complete_validated itself, so
# this outer loop is what actually guarantees at least one automatic retry
# across the full class of failures the old fail-open handler covered.
_MAX_LLM_ATTEMPTS = 2


class DbcCommentsAgent:
    """
    Design by Contract Comments agent that reviews code produced by coding agents
    and ensures all methods, functions, classes, and interfaces have comments
    complying with Design by Contract principles.

    Preconditions:
        - llm_client may be None or an LLMClient; when None,
          get_client("dbc_comments") resolves the default client

    Postconditions:
        - Agent is ready to review code via the run() method

    Invariants:
        - The agent never modifies code logic, only comments
    """

    def __init__(self, llm_client: Optional[LLMClient] = None) -> None:
        """
        Initialize the DbC Comments agent.

        Postconditions:
            - self.llm is set to a resolved LLMClient
        """
        self.llm = llm_client if llm_client is not None else get_client("dbc_comments")

    def run(
        self,
        input_data: DbcCommentsInput,
        on_status: Optional[Callable[[DbcCommentsStatus, str], None]] = None,
    ) -> DbcCommentsOutput:
        """
        Review code for Design by Contract compliance and return anchored comment insertions.

        Preconditions:
            - input_data.code is a string; if empty or whitespace-only, the
              method returns an already_compliant response without calling
              the LLM
            - input_data.language is one of: python, typescript, java

        Postconditions:
            - The LLM call is retried up to _MAX_LLM_ATTEMPTS times, validated
              against DbcCommentsLLMResponse on each attempt via
              complete_validated; a malformed reply (including one malformed
              insertion entry, since insertions is a required, non-permissive
              schema field) fails the whole attempt and drives a retry, not a
              silent partial acceptance
            - If every attempt fails: already_compliant=False and summary
              describes the failure -- a persistent LLM failure can never
              silently and permanently mark the code compliant
            - Otherwise, insertions is the validated list of
              DbcCommentInsertion objects from the successful reply, kept
              unmerged (for observability) regardless of already_compliant
            - already_compliant reflects the model's assessment, overridden
              to True only when the model reported False but returned no
              insertions
            - files holds the deterministic, LLM-free merge of the subset of
              insertions that could be safely anchored (see
              merge.apply_dbc_insertions); a file is only present when at
              least one insertion applied cleanly and, for '.py' files, the
              merge still parses -- files can be empty even when insertions
              is non-empty, if every insertion was rejected
            - rejected_insertions holds one reason per insertion that could
              not be safely anchored/merged; comments_added/comments_updated
              count only insertions the merge actually applied, never the
              model's self-reported counts
            - summary contains a message when already_compliant=True
              (defaulted when the model didn't provide one); otherwise it is
              the model-provided summary as-is, which may be empty

        Raises:
            Nothing -- run() never raises. Each failed-but-retryable LLM
            attempt is surfaced via on_status(NEEDS_RETRY, ...); final
            exhaustion via on_status(FAILED, ...) and
            already_compliant=False, never a silent fail-open. A merge-step
            exception (apply_dbc_insertions) is a separate, still fail-open
            path -- see that block's own comment.
        """

        def _update(status: DbcCommentsStatus, detail: str = "") -> None:
            if on_status:
                try:
                    on_status(status, detail)
                except Exception as e:  # noqa: BLE001 -- a status hook is observability
                    # and must never abort the review it's reporting on.
                    logger.warning("DbcComments: on_status callback failed (ignored): %s", e)
            logger.info(
                "DbcComments: %s %s",
                status.value,
                detail,
            )

        _update(DbcCommentsStatus.STARTING)

        code = input_data.code or ""
        if not code.strip():
            logger.warning("DbcComments: no code provided, returning compliant")
            return DbcCommentsOutput(
                already_compliant=True,
                summary="No code to review.",
            )

        logger.info(
            "DbcComments: reviewing %s chars of %s code | task=%s",
            len(code),
            input_data.language,
            input_data.task_description[:80] if input_data.task_description else "",
        )

        _update(DbcCommentsStatus.ANALYZING_CODE)

        # Build context for the LLM
        context_parts = [
            f"**Language:** {input_data.language}",
        ]

        if input_data.task_description:
            context_parts.append(f"**Task description:** {input_data.task_description}")

        if input_data.architecture:
            context_parts.extend(
                [
                    "",
                    "**Architecture overview:**",
                    input_data.architecture.overview,
                ]
            )

        context_parts.extend(
            [
                "",
                "**Code to review and annotate with DbC comments:**",
                "```",
                code,
                "```",
            ]
        )

        prompt = "\n".join(context_parts)

        validated: Optional[DbcCommentsLLMResponse] = None
        last_error: Optional[Exception] = None
        for attempt in range(1, _MAX_LLM_ATTEMPTS + 1):
            try:
                validated = complete_validated(
                    self.llm,
                    prompt,
                    schema=DbcCommentsLLMResponse,
                    objective="review code for Design by Contract compliance",
                    system_prompt=DBC_COMMENTS_PROMPT,
                    temperature=0.0,
                )
                break
            except Exception as e:
                last_error = e
                if attempt < _MAX_LLM_ATTEMPTS:
                    logger.warning(
                        "DbcComments: LLM call attempt %d/%d failed (%s), retrying",
                        attempt,
                        _MAX_LLM_ATTEMPTS,
                        e,
                    )
                    _update(DbcCommentsStatus.NEEDS_RETRY, str(e))
                else:
                    logger.warning(
                        "DbcComments: LLM call failed after %d attempt(s) (%s), "
                        "surfacing failure -- never marking compliant",
                        _MAX_LLM_ATTEMPTS,
                        e,
                    )

        if validated is None:
            _update(DbcCommentsStatus.FAILED, str(last_error))
            return DbcCommentsOutput(
                already_compliant=False,
                summary=f"DbC review failed after {_MAX_LLM_ATTEMPTS} attempt(s) and could "
                f"not determine compliance: {last_error}",
            )

        _update(DbcCommentsStatus.ADDING_COMMENTS)

        insertions = validated.insertions
        already_compliant = validated.already_compliant
        summary = validated.summary
        suggested_commit_message = validated.suggested_commit_message

        # Safety: if LLM says not compliant but returned no insertions, treat as compliant
        if not already_compliant and not insertions:
            logger.warning(
                "DbcComments: LLM returned already_compliant=False but no insertions -- "
                "overriding to compliant (no actionable changes)"
            )
            already_compliant = True
            if not summary:
                summary = "Code reviewed for DbC compliance. No changes needed."

        # Deterministically merge the insertions that can be safely anchored onto the
        # original source; comments_added/comments_updated reflect what was actually
        # applied, never the model's self-reported counts.
        files: dict[str, str] = {}
        comments_added = 0
        comments_updated = 0
        rejected_insertions: list[str] = []
        if insertions:
            try:
                files, comments_added, comments_updated, rejected_insertions = apply_dbc_insertions(
                    code, insertions
                )
            except Exception as e:
                # Fail-open: merge.py is pure and LLM-free, so there is nothing
                # a retry could fix here (unlike the LLM-call path above, which
                # is retried and surfaces failure instead of failing open). An
                # unexpected merge error must not crash the calling pipeline or
                # violate run()'s documented "never raises" contract.
                logger.warning("DbcComments: merge failed (%s), returning compliant (fail-open)", e)
                _update(DbcCommentsStatus.FAILED, str(e))
                return DbcCommentsOutput(
                    already_compliant=True,
                    summary=f"DbC review skipped due to a merge error: {e}",
                )
            if rejected_insertions:
                logger.warning(
                    "DbcComments: %d insertion(s) could not be safely merged: %s",
                    len(rejected_insertions),
                    rejected_insertions,
                )

        # If compliant and no summary, provide a default praise message
        if already_compliant and not summary:
            summary = (
                "All code fully complies with Design by Contract principles. "
                "Excellent documentation!"
            )

        logger.info(
            "DbcComments: done, compliant=%s, insertions=%s, files=%s, added=%s, updated=%s, "
            "rejected=%s",
            already_compliant,
            len(insertions),
            len(files),
            comments_added,
            comments_updated,
            len(rejected_insertions),
        )

        _update(DbcCommentsStatus.COMPLETE)

        return DbcCommentsOutput(
            insertions=insertions,
            files=files,
            rejected_insertions=rejected_insertions,
            comments_added=comments_added,
            comments_updated=comments_updated,
            already_compliant=already_compliant,
            summary=summary,
            suggested_commit_message=suggested_commit_message,
        )
