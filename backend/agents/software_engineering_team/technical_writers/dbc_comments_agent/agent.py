"""Design by Contract Comments agent: reviews code and adds DbC-compliant comments."""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from llm_service import LLMClient, complete_validated, get_client
from software_engineering_team.code_review_agent.chunking import build_review_chunks
from software_engineering_team.code_review_agent.models import ReviewChunk
from software_engineering_team.shared.chunking import parse_code_into_file_blocks
from software_engineering_team.shared.context_sizing import compute_code_review_map_chunk_chars

from .merge import apply_dbc_insertions
from .models import (
    DbcCommentInsertion,
    DbcCommentsInput,
    DbcCommentsLLMResponse,
    DbcCommentsOutput,
    DbcCommentsStatus,
)
from .prompts import DBC_COMMENTS_PROMPT

logger = logging.getLogger(__name__)

# Per-chunk LLM-call budget: 1 initial attempt + 1 automatic retry.
# complete_validated's own correction_attempts only retries a JSON-parse/
# schema-validation failure -- everything else that can escape it untouched
# (LLMNotConfiguredError, rate limits, semantic exhaustion, truncation, a
# bare network error) gets zero retries from complete_validated itself, so
# this outer loop is what actually guarantees at least one automatic retry
# across the full class of failures the old fail-open handler covered. A
# large ``code`` input is bounded into one or more chunks (see
# _build_prompt_for_chunk/run below); each chunk gets its own independent
# _MAX_LLM_ATTEMPTS budget, and any one chunk exhausting its budget fails
# the WHOLE run (see run()'s docstring) rather than silently dropping that
# chunk's review.
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

    @staticmethod
    def _build_prompt_for_chunk(
        input_data: DbcCommentsInput, chunk: ReviewChunk, chunk_index: int, chunk_count: int
    ) -> str:
        """Build one chunk's review prompt.

        Preconditions:
            - chunk_index is 1-based and <= chunk_count.

        Postconditions:
            - Returns a prompt whose code section is chunk.content (a bounded
              slice of the full input), never the full, unbounded code.
            - When chunk_count > 1, the prompt notes this is a partial view
              (chunk_index/chunk_count) so the model does not treat missing
              files/symbols as omissions to flag, and separately notes any
              segment that is itself a partial (line-range) slice of its file
              so the model knows to report the embedded original line-number
              prefixes rather than snippet-relative ones.
        """
        context_parts = [f"**Language:** {input_data.language}"]

        if input_data.task_description:
            context_parts.append(f"**Task description:** {input_data.task_description}")

        if input_data.architecture:
            context_parts.extend(
                ["", "**Architecture overview:**", input_data.architecture.overview]
            )

        if chunk_count > 1:
            context_parts.extend(
                [
                    "",
                    f"**Note:** This is chunk {chunk_index} of {chunk_count} of a larger "
                    "codebase being reviewed in multiple passes -- only add insertions for "
                    "symbols actually shown below; other files are reviewed separately.",
                ]
            )
            partial_notes = [
                f"{seg.path or 'this code'} is shown only from original line {seg.start_line} "
                f"to {seg.end_line} (of {seg.total_lines} total); every line carries its "
                "original line-number prefix (e.g. `123: code`) -- set `line` to those exact "
                "prefixed numbers."
                for seg in chunk.segments
                if seg.is_partial and not seg.pre_numbered
            ]
            context_parts.extend(partial_notes)

        context_parts.extend(
            [
                "",
                "**Code to review and annotate with DbC comments:**",
                "```",
                chunk.content,
                "```",
            ]
        )

        return "\n".join(context_parts)

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
            - code is split into one or more bounded chunks (see
              code_review_agent.chunking.build_review_chunks, sized via
              compute_code_review_map_chunk_chars) so no unbounded prompt is
              ever sent regardless of input size; a small input yields exactly
              one chunk covering the whole input, and this method's observable
              behavior is then identical to a single, unchunked call
            - Each chunk's LLM call is retried up to _MAX_LLM_ATTEMPTS times,
              validated against DbcCommentsLLMResponse on each attempt via
              complete_validated; a malformed reply (including one malformed
              insertion entry, since insertions is a required, non-permissive
              schema field) fails the whole attempt and drives a retry, not a
              silent partial acceptance
            - If any chunk exhausts its attempts: already_compliant=False and
              summary describes the failure, and the whole run returns
              immediately -- a persistent LLM failure on any chunk can never
              silently and permanently mark the code compliant, even when
              other chunks already succeeded
            - Otherwise, insertions is the concatenation of every chunk's
              validated DbcCommentInsertion objects, kept unmerged (for
              observability) regardless of already_compliant
            - already_compliant is True only when every chunk's response
              reported True (a logical AND across chunks), overridden to True
              only when the combined result reported False but returned no
              insertions at all
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
            attempt (on any chunk) is surfaced via on_status(NEEDS_RETRY,
            ...); a chunk's final exhaustion via on_status(FAILED, ...) and
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

        # Bound the LLM prompt regardless of input size: split the (possibly
        # very large) concatenated code into one or more chunks whose rendered
        # size fits the model's context budget. A small input yields exactly
        # one chunk covering the whole input verbatim (see
        # code_review_agent.chunking.build_review_chunks's contract), so this
        # is a no-op for the common case.
        blocks = parse_code_into_file_blocks(code)
        max_chunk_chars = compute_code_review_map_chunk_chars(self.llm)
        chunks = build_review_chunks(blocks, max_chunk_chars)
        chunk_count = len(chunks)

        all_insertions: List[DbcCommentInsertion] = []
        all_compliant = True
        summaries: List[str] = []
        suggested_commit_message: Optional[str] = None

        for chunk_index, chunk in enumerate(chunks, start=1):
            prompt = self._build_prompt_for_chunk(input_data, chunk, chunk_index, chunk_count)

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
                            "DbcComments: chunk %d/%d LLM call attempt %d/%d failed (%s), retrying",
                            chunk_index,
                            chunk_count,
                            attempt,
                            _MAX_LLM_ATTEMPTS,
                            e,
                        )
                        _update(
                            DbcCommentsStatus.NEEDS_RETRY,
                            f"chunk {chunk_index}/{chunk_count}: {e}",
                        )
                    else:
                        logger.warning(
                            "DbcComments: chunk %d/%d LLM call failed after %d attempt(s) (%s), "
                            "surfacing failure -- never marking compliant",
                            chunk_index,
                            chunk_count,
                            _MAX_LLM_ATTEMPTS,
                            e,
                        )

            if validated is None:
                _update(
                    DbcCommentsStatus.FAILED, f"chunk {chunk_index}/{chunk_count}: {last_error}"
                )
                return DbcCommentsOutput(
                    already_compliant=False,
                    summary=f"DbC review failed on chunk {chunk_index}/{chunk_count} after "
                    f"{_MAX_LLM_ATTEMPTS} attempt(s) and could not determine compliance: "
                    f"{last_error}",
                )

            all_insertions.extend(validated.insertions)
            all_compliant = all_compliant and validated.already_compliant
            if validated.summary:
                summaries.append(validated.summary)
            if suggested_commit_message is None and validated.suggested_commit_message:
                suggested_commit_message = validated.suggested_commit_message

        _update(DbcCommentsStatus.ADDING_COMMENTS)

        insertions = all_insertions
        already_compliant = all_compliant
        summary = " ".join(summaries)
        suggested_commit_message = (
            suggested_commit_message
            or DbcCommentsLLMResponse.model_fields["suggested_commit_message"].default
        )

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
