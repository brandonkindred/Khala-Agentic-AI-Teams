"""Design by Contract Comments agent: reviews code and adds DbC-compliant comments."""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from strands import Agent

from llm_service import get_strands_model

from .models import DbcCommentsInput, DbcCommentsOutput, DbcCommentsStatus
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
            - self._agent is set to a Strands Agent
        """
        from strands.models.model import Model as _StrandsModel

        if llm_client is not None and isinstance(llm_client, _StrandsModel):
            _model = llm_client
        else:
            _model = get_strands_model("dbc_comments")
        self._agent = Agent(model=_model, system_prompt=DBC_COMMENTS_PROMPT)

    def run(
        self,
        input_data: DbcCommentsInput,
        on_status: Optional[Callable[[DbcCommentsStatus, str], None]] = None,
    ) -> DbcCommentsOutput:
        """
        Review code for Design by Contract compliance and return annotated files.

        Preconditions:
            - input_data.code is a non-empty string containing code to review
            - input_data.language is one of: python, typescript, java

        Postconditions:
            - Returns DbcCommentsOutput with either:
              (a) files dict containing updated code and already_compliant=False, or
              (b) empty files dict and already_compliant=True
            - summary field always contains a message for the coding agent

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
            result = self._agent(prompt)
            raw = str(result).strip()
            data = json.loads(raw)
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
        files = data.get("files") or {}
        if not isinstance(files, dict):
            logger.warning(
                "DbcComments: LLM returned non-dict files field (%s), treating as compliant",
                type(files).__name__,
            )
            files = {}

        # Filter out empty file entries
        files = {
            path: content
            for path, content in files.items()
            if isinstance(path, str) and isinstance(content, str) and content.strip()
        }

        comments_added = int(data.get("comments_added", 0))
        comments_updated = int(data.get("comments_updated", 0))
        already_compliant = bool(data.get("already_compliant", False))
        summary = data.get("summary", "")
        suggested_commit_message = data.get(
            "suggested_commit_message",
            "docs(dbc): add Design by Contract comments",
        )

        # Safety: if LLM says not compliant but returned no files, treat as compliant
        if not already_compliant and not files:
            logger.warning(
                "DbcComments: LLM returned already_compliant=False but no files -- "
                "overriding to compliant (no actionable changes)"
            )
            already_compliant = True
            if not summary:
                summary = "Code reviewed for DbC compliance. No changes needed."

        # If compliant and no summary, provide a default praise message
        if already_compliant and not summary:
            summary = (
                "All code fully complies with Design by Contract principles. "
                "Excellent documentation!"
            )

        logger.info(
            "DbcComments: done, compliant=%s, files_changed=%s, added=%s, updated=%s",
            already_compliant,
            len(files),
            comments_added,
            comments_updated,
        )

        _update(DbcCommentsStatus.COMPLETE)

        return DbcCommentsOutput(
            files=files,
            comments_added=comments_added,
            comments_updated=comments_updated,
            already_compliant=already_compliant,
            summary=summary,
            suggested_commit_message=suggested_commit_message,
        )
