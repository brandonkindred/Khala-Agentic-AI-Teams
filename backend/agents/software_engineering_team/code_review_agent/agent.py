"""Code Review agent: reviews code against spec, standards, and conventions.

``CodeReviewAgent.run`` always delegates to the map-reduce coordinator
(`coordinator.run_coordinator`), which bounds every LLM call independently of
input size, re-anchors line numbers from split segments, and applies the
deterministic approval gate with its anti-loop safety nets.
"""

from __future__ import annotations

import logging

from llm_service import get_client

from .coordinator import run_coordinator
from .models import CodeReviewInput, CodeReviewOutput, ReviewProgressCallback

logger = logging.getLogger(__name__)


class CodeReviewAgent:
    """
    Code review agent that reviews code produced by coding agents
    against the project specification, coding standards, and conventions.

    Returns approval or a list of issues that must be resolved.

    Invariants:
        - Every ``run`` call goes through the map-reduce coordinator, so review
          prompts stay bounded regardless of how much code is submitted.
    """

    def __init__(self, llm_client=None) -> None:
        # The chunk reviewer resolves its own strands model per call; this
        # client is used for context sizing and shared-context compaction.
        self.llm = llm_client if llm_client is not None else get_client("code_review")

    def run(
        self,
        input_data: CodeReviewInput,
        progress_callback: ReviewProgressCallback | None = None,
    ) -> CodeReviewOutput:
        """Review code and return approval or issues.

        Preconditions:
            - ``input_data`` carries the code under review via ``files`` or ``code``.
            - ``progress_callback`` is None or satisfies the
              ``ReviewProgressCallback`` contract (non-raising, accepts
              ``(step, detail, fraction)``).

        Postconditions:
            - Returns the coordinator's merged verdict covering every submitted
              line; ``approved is False`` implies at least one critical/high issue.
            - When ``progress_callback`` is provided, it is invoked with
              non-decreasing fractions ending at 1.0 (step ``done``) on every
              successful return; the review result is identical whether or not
              a callback is provided.

        Raises:
            CodeReviewUnavailableError: when the review could not be completed
                (model unavailable, or a chunk stayed unreviewable after retry
                and bisection). Callers must treat this as a failed review run
                — never as review feedback for the coding agent.
        """
        code_size = (
            sum(len(c) for c in input_data.files.values())
            if input_data.files is not None
            else len(input_data.code or "")
        )
        logger.info(
            "CodeReview: reviewing %s chars of %s code | task=%s | has_spec=%s | has_architecture=%s | acceptance_criteria=%s",
            code_size,
            input_data.language,
            input_data.task_description[:80] if input_data.task_description else "",
            bool(input_data.spec_content),
            input_data.architecture is not None,
            len(input_data.acceptance_criteria),
        )
        return run_coordinator(self.llm, input_data, progress_callback=progress_callback)
