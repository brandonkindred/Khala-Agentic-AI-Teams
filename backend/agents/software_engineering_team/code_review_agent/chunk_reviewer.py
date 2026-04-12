"""Chunk Reviewer: reviews one chunk of code (used by CodeReviewCoordinator).

Built on the AWS Strands Agents SDK via ``llm_service.LLMClientModel``.
Internally uses ``CodeReviewOutput`` as the ``structured_output_model`` —
it has the right shape (typed ``List[CodeReviewIssue]``). The result is
converted to ``ChunkReviewOutput`` (which keeps ``List[Dict[str, Any]]``
for ``issues``) at the boundary so existing consumers of
``ChunkReviewOutput.issues`` that index by dict keys continue to work.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from strands import Agent

from llm_service import LLMClient, LLMClientModel, compact_text
from software_engineering_team.shared.context_sizing import (
    compute_code_review_arch_overview_chars,
    compute_code_review_chunk_chars,
    compute_code_review_existing_codebase_chars,
    compute_code_review_spec_excerpt_chars,
)

from .models import ChunkReviewInput, ChunkReviewOutput, CodeReviewOutput
from .prompts import CODE_REVIEW_PROMPT

logger = logging.getLogger(__name__)

CHUNK_REVIEW_NOTE = (
    "\n**Note:** This is one chunk of the full codebase. Review only the "
    "code below. Report issues with file_path set to the path provided for "
    "this chunk.\n"
)


class ChunkReviewAgent:
    """Reviews one chunk of code. Used by CodeReviewCoordinator for large codebases."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self._model = LLMClientModel(
            llm,
            agent_key="code_review",
            temperature=0.1,
            think=True,
        )

    def run(self, input_data: ChunkReviewInput) -> ChunkReviewOutput:
        """Review one chunk and return approved, issues, summary."""
        user_prompt = self._build_user_prompt(input_data)

        # A fresh Strands Agent per call — reusing the same instance across
        # calls breaks structured_output forced-tool-choice on the second
        # call. This matters especially here because the coordinator
        # dispatches one chunk at a time sequentially to the same
        # ``ChunkReviewAgent`` instance.
        agent = Agent(model=self._model, system_prompt=CODE_REVIEW_PROMPT)

        try:
            agent_result = agent(user_prompt, structured_output_model=CodeReviewOutput)
            review = agent_result.structured_output
            if not isinstance(review, CodeReviewOutput):
                raise TypeError(
                    f"Expected CodeReviewOutput, got {type(review).__name__ if review else 'None'}"
                )
        except Exception as exc:  # noqa: BLE001 — LLM/validation errors must not crash the run
            logger.warning("ChunkReview: structured_output failed (%s); returning fallback", exc)
            return ChunkReviewOutput(
                approved=False, issues=[], summary=f"Chunk review failed: {exc}"
            )

        # Convert typed CodeReviewIssue → plain dict and fall back to the
        # chunk's file_path label when the LLM omits it on an issue.
        issue_dicts: List[dict] = []
        for i in review.issues:
            if not i.description:
                continue
            issue_dicts.append(
                {
                    "severity": i.severity,
                    "category": i.category,
                    "file_path": i.file_path or input_data.file_path_or_label,
                    "description": i.description,
                    "suggestion": i.suggestion,
                }
            )

        return ChunkReviewOutput(
            approved=bool(review.approved),
            issues=issue_dicts,
            summary=str(review.summary or ""),
        )

    def _build_user_prompt(self, input_data: ChunkReviewInput) -> str:
        """Assemble the user-facing prompt for one chunk.

        Compacts long inputs (chunk content, spec excerpt, architecture,
        existing codebase) to fit the model's context, then assembles a
        schema-hinted prompt. The schema hint includes "senior code
        reviewer" and "approved"/"issues" so ``DummyLLMClient`` routes to
        the code-review stub in tests.
        """
        max_chunk_chars = compute_code_review_chunk_chars(self.llm)
        max_spec = compute_code_review_spec_excerpt_chars(self.llm)
        max_arch = compute_code_review_arch_overview_chars(self.llm)
        max_existing = compute_code_review_existing_codebase_chars(self.llm)

        code_chunk = compact_text(input_data.code_chunk, max_chunk_chars, self.llm, "code chunk")
        spec_excerpt = compact_text(
            input_data.spec_excerpt, max_spec, self.llm, "specification excerpt"
        )
        architecture_overview = compact_text(
            input_data.architecture_overview, max_arch, self.llm, "architecture overview"
        )
        existing_codebase_excerpt = compact_text(
            input_data.existing_codebase_excerpt or "",
            max_existing,
            self.llm,
            "existing codebase excerpt",
        )

        parts = [
            "Acting as a senior code reviewer, review this chunk of code and "
            "produce structured JSON with fields: approved, issues, summary. "
            "Each issue must include severity, category, file_path, "
            "description, and suggestion.",
            CHUNK_REVIEW_NOTE,
            f"**Files in this chunk:** {input_data.file_path_or_label}",
            "**Language:** python" if "def " in code_chunk[:500] else "**Language:** typescript",
            f"**Task description:** {input_data.task_description}",
        ]
        if input_data.task_requirements:
            parts.extend(["", "**Task requirements:**", input_data.task_requirements])
        if input_data.acceptance_criteria:
            parts.extend(
                [
                    "",
                    "**Acceptance criteria (code MUST meet all of these):**",
                    *[f"- {c}" for c in input_data.acceptance_criteria],
                ]
            )
        if spec_excerpt:
            parts.extend(
                [
                    "",
                    "**Project specification (excerpt):**",
                    "---",
                    spec_excerpt,
                    "---",
                ]
            )
        if architecture_overview:
            parts.extend(["", "**Architecture:**", architecture_overview])
        if existing_codebase_excerpt:
            parts.extend(
                [
                    "",
                    "**Existing codebase (excerpt):**",
                    "---",
                    existing_codebase_excerpt,
                    "---",
                ]
            )
        parts.extend(
            [
                "",
                "**Code to review:**",
                "```",
                code_chunk,
                "```",
            ]
        )
        return "\n".join(parts)


def review_chunk(
    llm: LLMClient,
    code_chunk: str,
    file_paths_label: str,
    task_description: str,
    task_requirements: str,
    acceptance_criteria: List[str],
    spec_excerpt: str,
    architecture_overview: str,
    existing_codebase_excerpt: Optional[str],
) -> dict:
    """Legacy helper: review one chunk and return a plain dict.

    Prefer ``ChunkReviewAgent(llm).run(ChunkReviewInput(...))``. This
    wrapper now delegates to ``ChunkReviewAgent`` so callers get the
    Strands-backed behavior for free.
    """
    inp = ChunkReviewInput(
        code_chunk=code_chunk,
        file_path_or_label=file_paths_label,
        task_description=task_description,
        task_requirements=task_requirements,
        acceptance_criteria=acceptance_criteria,
        spec_excerpt=spec_excerpt,
        architecture_overview=architecture_overview,
        existing_codebase_excerpt=existing_codebase_excerpt,
    )
    out = ChunkReviewAgent(llm).run(inp)
    return {
        "approved": out.approved,
        "issues": out.issues,
        "summary": out.summary,
    }
