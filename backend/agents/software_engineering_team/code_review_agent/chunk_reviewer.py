"""Chunk Reviewer: reviews one chunk of code (used by CodeReviewCoordinator)."""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from strands import Agent

from llm_service import LLMClient, compact_text, get_strands_model
from software_engineering_team.shared.context_sizing import (
    compute_code_review_arch_overview_chars,
    compute_code_review_chunk_chars,
    compute_code_review_existing_codebase_chars,
    compute_code_review_spec_excerpt_chars,
)

from .models import ChunkReviewInput, ChunkReviewOutput
from .prompts import CODE_REVIEW_PROMPT

logger = logging.getLogger(__name__)

CHUNK_REVIEW_NOTE = "\n**Note:** This is one chunk of the full codebase. Review only the code below. Report issues with file_path set to the path provided for this chunk.\n"


class ChunkReviewAgent:
    """Reviews one chunk of code. Used by CodeReviewCoordinator for large codebases."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(self, input_data: ChunkReviewInput) -> ChunkReviewOutput:
        """Review one chunk and return approved, issues, summary."""
        result = _run_chunk_review(self.llm, input_data)
        return ChunkReviewOutput(
            approved=result["approved"],
            issues=result["issues"],
            summary=result["summary"],
        )


def _run_chunk_review(llm: LLMClient, input_data: ChunkReviewInput) -> dict:
    """
    Review one chunk of code. Returns dict with approved, issues, summary.
    """
    max_chunk_chars = compute_code_review_chunk_chars(llm)
    max_spec = compute_code_review_spec_excerpt_chars(llm)
    max_arch = compute_code_review_arch_overview_chars(llm)
    max_existing = compute_code_review_existing_codebase_chars(llm)
    code_chunk = compact_text(input_data.code_chunk, max_chunk_chars, llm, "code chunk")
    spec_excerpt = compact_text(input_data.spec_excerpt, max_spec, llm, "specification excerpt")
    architecture_overview = compact_text(
        input_data.architecture_overview, max_arch, llm, "architecture overview"
    )
    existing_codebase_excerpt = compact_text(
        input_data.existing_codebase_excerpt or "", max_existing, llm, "existing codebase excerpt"
    )

    context_parts = [
        CHUNK_REVIEW_NOTE,
        f"**Files in this chunk:** {input_data.file_path_or_label}",
        "**Language:** python" if "def " in code_chunk[:500] else "**Language:** typescript",
        f"**Task description:** {input_data.task_description}",
    ]
    if input_data.task_requirements:
        context_parts.extend(["", "**Task requirements:**", input_data.task_requirements])
    if input_data.acceptance_criteria:
        context_parts.extend(
            [
                "",
                "**Acceptance criteria (code MUST meet all of these):**",
                *[f"- {c}" for c in input_data.acceptance_criteria],
            ]
        )
    if spec_excerpt:
        context_parts.extend(
            [
                "",
                "**Project specification (excerpt):**",
                "---",
                spec_excerpt,
                "---",
            ]
        )
    if architecture_overview:
        context_parts.extend(["", "**Architecture:**", architecture_overview])
    if existing_codebase_excerpt:
        context_parts.extend(
            [
                "",
                "**Existing codebase (excerpt):**",
                "---",
                existing_codebase_excerpt,
                "---",
            ]
        )
    context_parts.extend(
        [
            "",
            "**Code to review:**",
            "```",
            code_chunk,
            "```",
        ]
    )

    prompt = "\n".join(context_parts)
    # Use the injected LLM client as model when available (it implements Model);
    # fall back to get_strands_model for production.
    from strands.models.model import Model as _StrandsModel

    _model = llm if isinstance(llm, _StrandsModel) else get_strands_model("code_review")
    agent = Agent(model=_model, system_prompt=CODE_REVIEW_PROMPT)
    result = agent(prompt)
    raw = str(result).strip()
    data = json.loads(raw)

    issues = []
    for issue_data in data.get("issues") or []:
        if isinstance(issue_data, dict) and issue_data.get("description"):
            fp = issue_data.get("file_path") or input_data.file_path_or_label
            issues.append(
                {
                    "severity": issue_data.get("severity", "high"),
                    "category": issue_data.get("category", "general"),
                    "file_path": fp,
                    "description": issue_data.get("description", ""),
                    "suggestion": issue_data.get("suggestion", ""),
                }
            )

    return {
        "approved": bool(data.get("approved", False)),
        "issues": issues,
        "summary": str(data.get("summary", "")),
    }


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
    """Legacy function: review one chunk. Prefer ChunkReviewAgent.run(ChunkReviewInput(...))."""
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
    result = _run_chunk_review(llm, inp)
    return result
