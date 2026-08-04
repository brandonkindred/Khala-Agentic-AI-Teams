"""Design by Contract Comments agent: reviews code and adds DbC-compliant comments."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from llm_service import get_strands_model
from llm_service.strands_model import resolve_strands_model
from software_engineering_team.shared.llm import complete_json_with_continuation

from .merge import apply_dbc_insertions
from .models import DbcCommentInsertion, DbcCommentsInput, DbcCommentsOutput, DbcCommentsStatus
from .prompts import DBC_COMMENTS_PROMPT

logger = logging.getLogger(__name__)


class DbcCommentsAgent:
    """
    Design by Contract Comments agent that reviews code produced by coding agents
    and ensures all methods, functions, classes, and interfaces have comments
    complying with Design by Contract principles.

    Preconditions:
        - llm_client must be a valid, non-None LLMClient instance

    Postconditions:
        - Agent is ready to review code via the run() method

    Invariants:
        - The agent never modifies code logic, only comments
    """

    def __init__(self, llm_client=None) -> None:
        """
        Initialize the DbC Comments agent.

        Postconditions:
            - self._model is set to a resolved Strands Model
        """
        self._model = resolve_strands_model(
            llm_client, agent_key="dbc_comments", get_strands_model_fn=get_strands_model
        )

    def run(
        self,
        input_data: DbcCommentsInput,
        on_status: Optional[Callable[[DbcCommentsStatus, str], None]] = None,
    ) -> DbcCommentsOutput:
        """
        Review code for Design by Contract compliance and return anchored comment insertions.

        Preconditions:
            - input_data.code is a non-empty string containing code to review
            - input_data.language is one of: python, typescript, java

        Postconditions:
            - Returns DbcCommentsOutput with either:
              (a) a non-empty insertions list (anchored file/symbol/comment
                  entries) and already_compliant=False, or
              (b) an empty insertions list and already_compliant=True
            - summary field always contains a message for the coding agent
            - insertions is always the raw, unmerged list the model returned
              (kept for observability); files holds the deterministic,
              LLM-free merge of the subset of insertions that could be
              safely anchored (see merge.apply_dbc_insertions); a file is
              only present in files when at least one insertion applied
              cleanly and, for '.py' files, the merge still parses
            - rejected_insertions holds one reason per insertion that could
              not be safely anchored/merged; comments_added/comments_updated
              count only insertions the merge actually applied, never the
              model's self-reported counts

        Raises:
            Exception: If LLM call fails (caught internally, returns fail-open response)
        """

        def _update(status: DbcCommentsStatus, detail: str = "") -> None:
            if on_status:
                on_status(status, detail)
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

        try:
            data = complete_json_with_continuation(
                self._model, prompt, system_prompt=DBC_COMMENTS_PROMPT
            )
            if not isinstance(data, dict):
                raise ValueError(f"expected a JSON object, got {type(data).__name__}")
        except Exception as e:
            # Fail-open: if LLM call fails, don't block the pipeline
            logger.warning(
                "DbcComments: LLM call failed (%s), returning compliant (fail-open)",
                e,
            )
            _update(DbcCommentsStatus.FAILED, str(e))
            return DbcCommentsOutput(
                already_compliant=True,
                summary=f"DbC review skipped due to error: {e}",
            )

        _update(DbcCommentsStatus.ADDING_COMMENTS)

        # Parse response
        raw_insertions = data.get("insertions") or []
        if not isinstance(raw_insertions, list):
            logger.warning(
                "DbcComments: LLM returned non-list insertions field (%s), treating as compliant",
                type(raw_insertions).__name__,
            )
            raw_insertions = []

        # Build typed insertions, skipping any malformed entry rather than
        # failing the whole review over one bad item.
        insertions: list[DbcCommentInsertion] = []
        for entry in raw_insertions:
            if not isinstance(entry, dict):
                continue
            try:
                insertions.append(DbcCommentInsertion(**entry))
            except Exception as e:
                logger.warning("DbcComments: skipping malformed insertion (%s): %s", entry, e)

        already_compliant = bool(data.get("already_compliant", False))
        summary = data.get("summary", "")
        suggested_commit_message = data.get(
            "suggested_commit_message",
            "docs(dbc): add Design by Contract comments",
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
            files, comments_added, comments_updated, rejected_insertions = apply_dbc_insertions(
                code, insertions
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
