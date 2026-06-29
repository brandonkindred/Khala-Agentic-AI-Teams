"""Chunk Reviewer: the map step of the map-reduce code review.

``ChunkReviewAgent`` reviews exactly one ``ReviewChunk`` in a single LLM call
and returns that chunk's findings. The coordinator (``coordinator.py``) owns
splitting the submission into bounded chunks, recovering failed chunks, and
reducing the per-chunk findings into one verdict; this module owns only the
single bounded review pass.

Preconditions:
    - The caller (the coordinator) has already bounded the chunk: the
      ``code_chunk`` carries at most ``compute_code_review_map_chunk_chars`` of
      code, and any spec/architecture/existing-codebase context has been
      compacted to its absolute cap. This module re-applies those caps to the
      context excerpts defensively but never to the code, which is reviewed
      verbatim.

Postconditions:
    - Returns the LLM's findings for this chunk only (``approved``, ``issues``,
      ``summary``, and the ``spec_compliance_notes``/``suggested_commit_message``
      passthroughs); it never re-anchors line numbers, dedupes, or applies the
      approval gate — those are the reduce phase's job.

Invariants:
    - Stateless apart from the injected ``llm`` handle: every call builds a
      fresh strands ``Agent``, so concurrent reviews share no mutable state.
      The code under review is sent verbatim and is never compacted.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from strands import Agent

from llm_service import LLMClient
from software_engineering_team.shared.context_sizing import (
    compute_code_review_arch_overview_chars,
    compute_code_review_existing_codebase_chars,
    compute_code_review_map_chunk_chars,
    compute_code_review_spec_excerpt_chars,
)

from .model_resolution import resolve_code_review_model
from .models import ChunkReviewInput, ChunkReviewOutput
from .profiles import build_review_system_prompt

logger = logging.getLogger(__name__)

CHUNK_REVIEW_NOTE = "\n**Note:** This is one chunk of the full codebase. Review only the code below. Report issues with file_path set to the path provided for this chunk.\n"


class ChunkReviewAgent:
    """Reviews one chunk of code. Used by CodeReviewCoordinator for large codebases.

    Invariants:
        - Stateless apart from the injected ``llm`` handle: every ``run`` call
          builds a fresh strands ``Agent`` (and, in production, a fresh model
          via ``get_strands_model``), so concurrent ``run`` calls share no
          mutable agent state. The injected ``llm`` must itself support
          concurrent calls — the central ``llm_service`` clients do (they
          guard shared state internally); test doubles used with the parallel
          coordinator must do the same.
    """

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(self, input_data: ChunkReviewInput) -> ChunkReviewOutput:
        """Review one chunk and return approved, issues, summary.

        Postconditions:
            - ``spec_compliance_notes``/``suggested_commit_message`` from the
              LLM are passed through so single-chunk reviews keep full output
              fidelity.
        """
        result = _run_chunk_review(self.llm, input_data)
        return ChunkReviewOutput(
            approved=result["approved"],
            issues=result["issues"],
            summary=result["summary"],
            spec_compliance_notes=result["spec_compliance_notes"],
            suggested_commit_message=result["suggested_commit_message"],
        )


def _run_chunk_review(llm: LLMClient, input_data: ChunkReviewInput) -> dict:
    """
    Review one chunk of code. Returns dict with approved, issues, summary,
    spec_compliance_notes, and suggested_commit_message.

    Preconditions:
        - ``input_data.code_chunk`` is already bounded by the coordinator
          (≤ ``compute_code_review_map_chunk_chars``); it is reviewed verbatim,
          never compacted or truncated here.

    Postconditions:
        - Shared context (spec/architecture/existing code) is hard-capped to
          its budget deterministically — no LLM compaction happens here (the
          coordinator already compacted once), so a chunk call never grows the
          prompt or fires extra LLM calls even when upstream compaction failed.
    """
    max_chunk_chars = compute_code_review_map_chunk_chars(llm)
    max_spec = compute_code_review_spec_excerpt_chars(llm)
    max_arch = compute_code_review_arch_overview_chars(llm)
    max_existing = compute_code_review_existing_codebase_chars(llm)
    code_chunk = input_data.code_chunk
    if len(code_chunk) > max_chunk_chars:
        # Coordinator invariant violation (e.g. a single line longer than the
        # cap): log it but never mutate the code under review.
        logger.warning(
            "ChunkReview: code chunk is %s chars, above the %s-char map budget — reviewing as-is",
            len(code_chunk),
            max_chunk_chars,
        )
    spec_excerpt = input_data.spec_excerpt[:max_spec]
    architecture_overview = input_data.architecture_overview[:max_arch]
    existing_codebase_excerpt = (input_data.existing_codebase_excerpt or "")[:max_existing]

    language = input_data.language.strip().lower() if input_data.language else ""
    if not language:
        # Fallback guess for legacy callers that did not declare a language.
        language = "python" if "def " in code_chunk else "typescript"

    context_parts = [CHUNK_REVIEW_NOTE]
    if input_data.segment_note:
        context_parts.extend(["**Segment notes:**", input_data.segment_note, ""])
    context_parts += [
        f"**Files in this chunk:** {input_data.file_path_or_label}",
        f"**Language:** {language}",
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
    if input_data.user_decisions:
        context_parts.extend(
            [
                "",
                "**User decisions already made (settled — do NOT flag these as open/unanswered "
                "questions or suggest reconsidering them):**",
                *[f"- {d}" for d in input_data.user_decisions],
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
    _model = resolve_code_review_model(llm)
    agent = Agent(model=_model, system_prompt=build_review_system_prompt(input_data.profile))
    result = agent(prompt)
    raw = str(result).strip()
    data = json.loads(raw)

    # Issue dicts are passed through raw: normalization (defaults, line
    # coercion, path resolution) happens exactly once, in the coordinator's
    # ``_issues_from_chunk_output``. A blank file_path is deliberately kept
    # blank — fabricating the multi-file chunk label here would break the
    # coordinator's per-path offset lookup.
    issues = [item for item in (data.get("issues") or []) if isinstance(item, dict)]

    return {
        "approved": bool(data.get("approved", False)),
        "issues": issues,
        "summary": str(data.get("summary", "")),
        "spec_compliance_notes": str(data.get("spec_compliance_notes", "") or ""),
        "suggested_commit_message": str(data.get("suggested_commit_message", "") or ""),
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
