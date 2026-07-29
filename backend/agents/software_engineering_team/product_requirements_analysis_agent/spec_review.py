"""
Spec Review phase for the Product Requirements Analysis Agent.

Reviews the current specification and produces issues, gaps, and open questions
for the user, reconciling them against previously answered questions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from software_engineering_team.shared.json_utils import parse_json_with_recovery

from .constraint_analysis import analyze_constraint_status, generate_constraint_hints
from .models import AnsweredQuestion, SpecReviewResult
from .prompts import SPEC_REVIEW_PROMPT
from .qa_history import format_answered_questions_for_prompt, read_qa_history
from .question_processing import (
    MAX_OPEN_QUESTIONS,
    filter_duplicate_questions,
    filter_organizational_questions,
    parse_spec_review_response,
)
from .spec_writing import update_spec_from_duplicates

logger = logging.getLogger(__name__)


def format_context_for_review(context_files: Dict[str, str]) -> str:
    """Format context files for inclusion in the spec review prompt.

    Preconditions: ``context_files`` maps file path -> content (possibly empty).
    Postconditions: returns an empty string when there are no context files or they
        format to nothing; otherwise a Markdown "Additional Context Files" section.
    """
    if not context_files:
        return ""

    from software_engineering_team.spec_parser import format_context_for_prompt

    formatted = format_context_for_prompt(context_files)

    if not formatted:
        return ""

    return f"""

## Additional Context Files

The following additional files were provided in the project folder. Review these alongside the main specification to understand the full context:

{formatted}

---
"""


def run_spec_review(
    model: Any,
    llm: Any,
    context_files: Dict[str, str],
    spec_content: str,
    repo_path: Path,
    iteration: int = 1,
    spec_version: Optional[int] = None,
    answered_questions: Optional[List[AnsweredQuestion]] = None,
    on_chunk_progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[SpecReviewResult, str]:
    """Run the Spec Review phase to identify gaps and questions.

    Args:
        model: Strands model for the clarification call when duplicates are found.
        llm: LLMClient used for context sizing and JSON recovery.
        context_files: Additional project files (path -> content) for context.
        spec_content: Current specification content.
        repo_path: Path to the repository.
        iteration: Current iteration number (for logging/qa_history).
        spec_version: Version number for updated_spec_vN.md when writing (e.g. from
            duplicates). If None, iteration is used.
        answered_questions: List of previously answered questions for constraint analysis.
        on_chunk_progress: Optional callback (chunk_index, total_chunks) for progress
            updates during chunked LLM calls.

    Returns:
        Tuple of (SpecReviewResult, updated_spec_content). The spec may be updated if
        duplicate questions were found and clarified.

    Preconditions: ``model`` is a Strands ``Model``; ``llm`` is the ``LLMClient``.
    Postconditions: returns a valid :class:`SpecReviewResult` and the (possibly
        clarified) spec; on JSON-recovery failure returns a retry-flagged result and
        the unmodified spec. Never raises on LLM/parse failure.
    """
    if spec_version is None:
        spec_version = iteration
    # Full Q&A for prompt: file history + in-memory answered_questions (current session)
    qa_from_file = read_qa_history(repo_path)
    qa_for_prompt = qa_from_file
    if answered_questions:
        session_block = format_answered_questions_for_prompt(answered_questions)
        if session_block:
            if qa_for_prompt:
                qa_for_prompt += "\n\n## Current session answers\n\n" + session_block
            else:
                qa_for_prompt = "## Current session answers\n\n" + session_block
    # Optional cap to leave room for spec + instructions (e.g. last 12k chars)
    if len(qa_for_prompt) > 12000:
        qa_for_prompt = qa_for_prompt[-12000:]
        logger.debug("Capped qa_for_prompt to last 12k chars")

    # Analyze constraint status and generate hints for the LLM
    constraint_status = analyze_constraint_status(spec_content, answered_questions or [])
    constraint_hints = generate_constraint_hints(constraint_status)

    logger.info("Constraint status: %s", {d: f"L{lvl}" for d, lvl in constraint_status.items()})

    # Build the full content including context files
    context_section = format_context_for_review(context_files)
    full_spec_content = spec_content
    if context_section:
        full_spec_content = spec_content + context_section
        logger.info(
            "Spec review: Including %d context files in review",
            len(context_files),
        )

    # Single whole-spec prompt; include full Q&A only when non-empty (edge-empty-qa)
    if qa_for_prompt:
        prompt = SPEC_REVIEW_PROMPT.format(
            spec_content=full_spec_content,
            constraint_hints=constraint_hints,
        )
        prompt += (
            """

IMPORTANT: The following questions have ALREADY been answered. Do NOT ask these questions again or any variations of them. Only ask NEW questions about topics NOT covered below. The spec and this Q&A are the source of truth.

Previously Answered Questions:
---
"""
            + qa_for_prompt
            + """
---
"""
        )
    else:
        prompt = SPEC_REVIEW_PROMPT.format(
            spec_content=full_spec_content,
            constraint_hints=constraint_hints,
        )

    if on_chunk_progress is not None:
        on_chunk_progress(0, 1)

    # Single LLM call for whole-spec review (no decomposition or merge)
    raw = parse_json_with_recovery(
        llm,
        prompt,
        agent_name="PRA_spec_review",
    )

    if not raw:
        logger.warning("PRA spec_review: No JSON recovered, will retry in next iteration")
        return (
            SpecReviewResult(
                summary="Spec review JSON parsing failed - will retry",
                issues=["JSON parsing failed - response may have been truncated"],
                gaps=[],
                open_questions=[],
            ),
            spec_content,
        )

    result = parse_spec_review_response(raw)
    updated_spec = spec_content

    # Filter duplicates and clarify spec using full qa_for_prompt (file + session)
    if qa_for_prompt and result.open_questions:
        filtered, duplicates = filter_duplicate_questions(result.open_questions, qa_for_prompt)
        result.open_questions = filtered

        if duplicates:
            logger.info(
                "Found %d duplicate questions - clarifying spec with existing answers",
                len(duplicates),
            )
            updated_spec = update_spec_from_duplicates(
                model, duplicates, qa_for_prompt, spec_content, repo_path, spec_version
            )

    result.open_questions = filter_organizational_questions(result.open_questions)

    # Cap after filters so organizational / already-answered entries do not
    # crowd out retained material questions.
    if len(result.open_questions) > MAX_OPEN_QUESTIONS:
        logger.info(
            "Truncated open questions after filters: %d->%d",
            len(result.open_questions),
            MAX_OPEN_QUESTIONS,
        )
        result.open_questions = result.open_questions[:MAX_OPEN_QUESTIONS]

    return result, updated_spec
