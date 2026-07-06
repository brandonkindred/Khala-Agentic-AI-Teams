"""Code Review agent: reviews code against spec, standards, and conventions.

``CodeReviewAgent.run`` always delegates to the map-reduce coordinator
(`coordinator.run_coordinator`), which bounds every LLM call independently of
input size, re-anchors line numbers from split segments, re-checks each genuine
finding against the whole submission to drop false positives the bounded chunk
review could not have caught (`false_positive_filter`), and applies the
deterministic approval gate with its anti-loop safety nets. A chunk that
cannot be reviewed after recovery degrades to a blocking ``high`` "not
reviewed" finding (rejecting the review so unreviewed code never passes the
gate) rather than aborting the run; an infrastructure failure or a run in which
no chunk could be reviewed raises ``CodeReviewUnavailableError``, and an
unexpected reviewer defect propagates unchanged (fails closed).
"""

from __future__ import annotations

import logging

from llm_service import get_client

from .coordinator import run_coordinator
from .models import CodeReviewInput, CodeReviewOutput, ReviewProgressCallback
from .repo_reader import RepoReader

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
        repo_reader: RepoReader | None = None,
    ) -> CodeReviewOutput:
        """Review code and return approval or issues.

        Preconditions:
            - ``input_data`` carries the code under review via ``files`` or ``code``.
            - ``progress_callback`` is None or satisfies the
              ``ReviewProgressCallback`` contract (non-raising, accepts
              ``(step, detail, fraction)``).
            - ``repo_reader`` is None or a ``repo_reader.RepoReader`` giving the
              false-positive verifier whole-repo read access (so it can confirm a
              file/module a finding calls missing already exists outside the diff).

        Postconditions:
            - Returns the coordinator's merged verdict covering every submitted
              line; ``approved is False`` implies at least one critical/high issue.
              A chunk unreviewable after recovery is named by a blocking ``high``
              "not reviewed" finding, so the merged review is rejected and
              unreviewed code never passes the gate as approved.
            - Findings that the verifier confirms are false positives (judged
              against the full codebase) are absent from the result; the
              not-reviewed coverage findings are never removed this way.
            - When ``progress_callback`` is provided, it is invoked with
              non-decreasing fractions ending at 1.0 (step ``done``) on every
              successful return; the review result is identical whether or not
              a callback is provided.

        Raises:
            CodeReviewUnavailableError: when the review could not be completed —
                the model was unavailable (an infrastructure failure), or no
                chunk could be reviewed at all. Callers must treat this as a
                failed review run — never as review feedback for the coding agent.
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
        return run_coordinator(
            self.llm, input_data, progress_callback=progress_callback, repo_reader=repo_reader
        )
