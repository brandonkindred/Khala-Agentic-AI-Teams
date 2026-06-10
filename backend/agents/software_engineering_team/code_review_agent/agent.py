"""Code Review agent: reviews code against spec, standards, and conventions.

``CodeReviewAgent.run`` always delegates to the map-reduce coordinator
(`coordinator.run_coordinator`), which bounds every LLM call independently of
input size, re-anchors line numbers from split segments, and applies the
deterministic approval gate with its anti-loop safety nets.
"""

from __future__ import annotations

import logging

from llm_service import get_client, get_strands_model

from .coordinator import run_coordinator
from .models import CodeReviewInput, CodeReviewOutput

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
        from strands.models.model import Model as _StrandsModel

        if llm_client is not None and isinstance(llm_client, _StrandsModel):
            self._model = llm_client
        else:
            self._model = get_strands_model("code_review")
        # Keep LLMClient for context_sizing utilities
        self.llm = llm_client if llm_client is not None else get_client("code_review")

    def run(self, input_data: CodeReviewInput) -> CodeReviewOutput:
        """Review code and return approval or issues.

        Preconditions:
            - ``input_data`` carries the code under review via ``files`` or ``code``.

        Postconditions:
            - Returns the coordinator's merged verdict; ``approved is False``
              implies at least one critical/high issue.
            - Never raises on per-chunk LLM failures (the coordinator degrades
              or fails closed instead).
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
        return run_coordinator(self.llm, input_data)
