"""
Spec rewriting, cleanup, and PRD generation for the Product Requirements Analysis Agent.

Folds answered questions into the specification, rewrites it for consistency and
clarity, validates and cleans it, and generates the Product Requirements Document
from the result.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from software_engineering_team.shared.context_sizing import compute_prd_snippet_chars
from software_engineering_team.shared.json_utils import (
    default_decompose_by_sections,
    parse_json_with_recovery,
)

if TYPE_CHECKING:
    from llm_service import LLMClient

from .llm_io import call_llm_text
from .models import AnsweredQuestion, OpenQuestion, SpecCleanupResult
from .prompts import (
    PRD_PROMPT,
    SPEC_CLEANUP_CHUNK_PROMPT,
    SPEC_CLEANUP_PROMPT,
    SPEC_CONSISTENCY_CLARIFICATION_PROMPT,
    SPEC_UPDATE_PROMPT,
)
from .qa_history import extract_answer_from_qa_history

logger = logging.getLogger(__name__)


def format_answered_questions(answered_questions: List[AnsweredQuestion]) -> str:
    """Format answered questions for the LLM prompt.

    Preconditions: ``answered_questions`` is a list of :class:`AnsweredQuestion`.
    Postconditions: returns a plain-text ``Q:``/``A:`` block; empty string for an
        empty list; never raises.
    """
    lines = []
    for aq in answered_questions:
        lines.append(f"Q: {aq.question_text}")
        lines.append(f"A: {aq.selected_answer}")
        if aq.rationale:
            lines.append(f"Rationale: {aq.rationale}")
        if aq.was_auto_answered:
            lines.append(f"(Auto-answered with {aq.confidence:.0%} confidence)")
        elif aq.was_default:
            lines.append("(Default applied)")
        lines.append("")
    return "\n".join(lines)


def _merge_spec_cleanup_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Combine cleanup results from multiple chunks.

    Args:
        results: List of parsed JSON dicts from chunk cleanup

    Returns:
        Merged dict with combined validation issues and cleaned spec

    Preconditions: ``results`` is a list of dicts.
    Postconditions: ``is_valid`` is ``False`` if any chunk was invalid; validation
        issues concatenated; cleaned specs joined with blank lines.
    """
    merged: Dict[str, Any] = {
        "is_valid": True,
        "validation_issues": [],
        "cleaned_spec": "",
        "summary": "",
    }

    cleaned_parts = []
    for r in results:
        if r.get("is_valid") is False:
            merged["is_valid"] = False
        if isinstance(r.get("validation_issues"), list):
            merged["validation_issues"].extend(r["validation_issues"])
        if r.get("cleaned_spec"):
            cleaned_parts.append(str(r["cleaned_spec"]))

    merged["cleaned_spec"] = "\n\n".join(cleaned_parts)
    merged["summary"] = f"Cleanup completed for {len(results)} sections"
    return merged


def _write_spec_artifact(repo_path: Path, filename: str, spec_text: str) -> Path:
    """Persist ``spec_text`` under plan/product_analysis as a versioned artifact
    plus the "latest" updated_spec.md alias.

    Shared by :func:`update_spec`, :func:`update_spec_from_duplicates`, and
    :func:`update_spec_for_consistency_and_clarity`, which otherwise duplicated
    this exact write sequence.

    Preconditions: ``filename`` is a bare filename (no path separators).
    Postconditions: creates ``plan/product_analysis`` if missing; writes both
        ``filename`` and ``updated_spec.md`` with ``spec_text``; returns the
        versioned file's path for the caller to log/reference.
    """
    plan_dir = repo_path / "plan" / "product_analysis"
    plan_dir.mkdir(parents=True, exist_ok=True)
    spec_file = plan_dir / filename
    spec_file.write_text(spec_text, encoding="utf-8")
    (plan_dir / "updated_spec.md").write_text(spec_text, encoding="utf-8")
    return spec_file


def update_spec(
    model: Any,
    current_spec: str,
    answered_questions: List[AnsweredQuestion],
    repo_path: Path,
    version: int,
) -> str:
    """Update the spec with answered questions. version is used for updated_spec_v{version}.md filename.

    Preconditions: ``model`` is a Strands ``Model``; ``version`` is an int.
    Postconditions: on success writes ``updated_spec_v{version}.md`` and
        ``updated_spec.md`` and returns the new text; on LLM failure returns
        ``current_spec`` unchanged.
    """
    answered_text = format_answered_questions(answered_questions)

    prompt = SPEC_UPDATE_PROMPT.format(
        spec_content=current_spec,
        answered_questions=answered_text,
    )

    try:
        updated_spec = call_llm_text(model, prompt)
    except Exception as e:
        logger.error("Failed to update spec with LLM: %s", e)
        return current_spec

    spec_file = _write_spec_artifact(repo_path, f"updated_spec_v{version}.md", updated_spec)
    logger.info("Saved updated spec to %s", spec_file)

    return updated_spec


def build_specialist_collaboration_plan(
    cleaned_spec: str,
    answered_questions: List[AnsweredQuestion],
) -> str:
    """Build deterministic recommendations for specialist agents/tooling.

    This gives the PRD writer concrete handoff guidance for areas that often require
    cross-team collaboration (UX, architecture, risk, data, security).

    Preconditions: ``cleaned_spec`` is a string; ``answered_questions`` a list.
    Postconditions: returns a newline-joined, de-duplicated, deterministically
        ordered recommendation list keyed off keywords present in the spec + answers.
    """
    spec_text = (cleaned_spec + "\n" + format_answered_questions(answered_questions)).lower()

    recommendations: List[str] = []

    def include(label: str, reason: str) -> None:
        recommendations.append(f"- {label}: {reason}")

    # Always include these core spokes for higher-quality PRDs.
    include(
        "Requirements Analyst Agent",
        "Own FR/NFR decomposition, prioritization, and traceability mapping.",
    )
    include(
        "QA and Acceptance Criteria Agent",
        "Ensure every Must requirement has verifiable acceptance criteria.",
    )
    include(
        "PRD Critic (Gatekeeper) Agent",
        "Run completeness/consistency/testability/traceability/pragmatism gates before Final.",
    )

    if any(
        k in spec_text
        for k in [
            "ui",
            "ux",
            "screen",
            "design",
            "workflow",
            "journey",
            "persona",
            "onboarding",
        ]
    ):
        include(
            "UX and Flows Agent",
            "Define textual workflows, edge cases, accessibility baseline, and screen/IA notes.",
        )
        include(
            "Design System Tool Agent",
            "Capture reusable component patterns, interaction states, and consistency rules.",
        )
        include(
            "Branding Guidance Agent",
            "Document tone, visual direction, and brand constraints for product surfaces.",
        )

    if any(
        k in spec_text
        for k in [
            "architecture",
            "api",
            "integration",
            "service",
            "event",
            "database",
            "deployment",
        ]
    ):
        include(
            "Architecture Agent",
            "Define high-level components, interfaces, and data flow boundaries.",
        )
        include(
            "API and Integration Agent",
            "Specify integration contracts, failure modes, and auth patterns.",
        )

    if any(
        k in spec_text
        for k in ["risk", "assumption", "dependency", "migration", "rollout", "timeline"]
    ):
        include(
            "Risk Analysis Agent",
            "Maintain risk register with owners, probabilities, impacts, and mitigations.",
        )
        include(
            "Scope and Milestones Planner Agent",
            "Align MVP/V1/VNext scope to dependencies and timeline options.",
        )

    if any(
        k in spec_text
        for k in ["security", "privacy", "compliance", "pii", "retention", "audit", "auth"]
    ):
        include(
            "Security, Privacy, and Compliance Agent",
            "Define data handling, retention, authz, and compliance questions.",
        )

    if any(k in spec_text for k in ["analytics", "kpi", "metric", "dashboard", "event tracking"]):
        include(
            "Data and Analytics Agent",
            "Define events, KPI ownership, and dashboards tied to goals.",
        )

    include(
        "Question Concierge (Human Interface) Agent",
        "Bundle unresolved questions by owner/impact with due dates and escalation policy.",
    )

    # Keep deterministic output order and avoid duplicates.
    seen = set()
    deduped: List[str] = []
    for item in recommendations:
        if item not in seen:
            seen.add(item)
            deduped.append(item)

    return "\n".join(deduped)


def generate_prd_document(
    model: Any,
    llm: "LLMClient",
    cleaned_spec: str,
    answered_questions: List[AnsweredQuestion],
) -> str:
    """Generate a Product Requirements Document (PRD) from the spec and answers.

    Uses the cleaned, validated spec as the base and integrates resolved answers
    (including constraint decisions) into a structured PRD suitable for Planning.

    Preconditions: ``model`` is a Strands ``Model``; ``llm`` is the ``LLMClient``
        used for context sizing.
    Postconditions: returns the generated PRD text, or ``cleaned_spec`` when the LLM
        fails or returns empty output.
    """
    # Summarize answered questions for the prompt; this may be empty on the first run
    answered_summary = format_answered_questions(answered_questions)

    # Keep prompt size reasonable while fitting within model context (e.g. 256K)
    max_chars = compute_prd_snippet_chars(llm)
    cleaned_spec_snippet = cleaned_spec[:max_chars]
    answered_summary_snippet = answered_summary[:max_chars]
    specialist_plan = build_specialist_collaboration_plan(
        cleaned_spec=cleaned_spec_snippet,
        answered_questions=answered_questions,
    )
    specialist_plan_snippet = specialist_plan[:max_chars]

    prompt = PRD_PROMPT.format(
        cleaned_spec=cleaned_spec_snippet,
        answered_questions_summary=answered_summary_snippet,
        specialist_collaboration_plan=specialist_plan_snippet,
    )

    try:
        prd_content = call_llm_text(model, prompt)
    except Exception as e:
        logger.error("Failed to generate PRD with LLM: %s", e)
        return cleaned_spec

    if not isinstance(prd_content, str) or not prd_content.strip():
        logger.warning(
            "Product Requirements Analysis: PRD generation returned empty output, "
            "falling back to cleaned specification"
        )
        return cleaned_spec

    return prd_content


def update_spec_from_duplicates(
    model: Any,
    duplicate_questions: List[OpenQuestion],
    qa_history: str,
    current_spec: str,
    repo_path: Path,
    version: int,
) -> str:
    """Update spec using answers from qa_history for duplicate questions.

    When a question is re-asked but was previously answered, this indicates the spec
    wasn't updated clearly enough. This extracts the existing answers and re-applies
    them with emphasis on clarity.

    Args:
        model: The Strands model for the clarification call.
        duplicate_questions: Questions that were filtered as duplicates.
        qa_history: Raw content of qa_history.md file.
        current_spec: Current specification content.
        repo_path: Path to the repository.
        version: Version number for updated_spec_v{version}.md filename.

    Returns:
        Updated specification content.

    Preconditions: ``version`` is an int.
    Postconditions: returns ``current_spec`` unchanged when no answers can be
        extracted or the LLM fails; otherwise writes the versioned + latest spec
        files and returns the clarified text.
    """
    from .prompts import SPEC_CLARIFICATION_PROMPT

    # Extract answers from qa_history for each duplicate
    extracted_answers: List[AnsweredQuestion] = []
    for q in duplicate_questions:
        answer = extract_answer_from_qa_history(q, qa_history)
        if answer:
            extracted_answers.append(answer)

    if not extracted_answers:
        logger.debug("No answers extracted from qa_history for duplicates")
        return current_spec

    logger.info(
        "Clarifying spec with %d previously answered questions that were re-asked",
        len(extracted_answers),
    )

    # Format the Q&A pairs for the clarification prompt
    qa_pairs = format_answered_questions(extracted_answers)

    prompt = SPEC_CLARIFICATION_PROMPT.format(
        spec_content=current_spec,
        duplicate_qa_pairs=qa_pairs,
    )

    try:
        clarified_spec = call_llm_text(model, prompt)
    except Exception as e:
        logger.error("Failed to clarify spec with LLM: %s", e)
        return current_spec

    # Save the clarified spec using the same versioned pattern as update_spec
    spec_file = _write_spec_artifact(repo_path, f"updated_spec_v{version}.md", clarified_spec)
    logger.info("Saved updated spec (clarification) to %s", spec_file)

    return clarified_spec


def update_spec_for_consistency_and_clarity(
    model: Any,
    current_spec: str,
    repo_path: Path,
    qa_history: str,
    all_answered_questions: List[AnsweredQuestion],
    version: int,
    consistency_loop: int,
) -> str:
    """Update spec for clarity and consistency; use QA as source of truth for conflicts.

    Called when deduplication reduces questions by 50%+ so the spec is edited to
    clarify answers and resolve conflicting information, then re-reviewed.

    Preconditions: ``version`` and ``consistency_loop`` are ints.
    Postconditions: writes ``updated_spec_consistency_v{version}_loop{consistency_loop}.md``
        on success; returns ``current_spec`` on LLM failure.
    """
    qa_source = qa_history.strip() if qa_history else ""
    if all_answered_questions:
        formatted = format_answered_questions(all_answered_questions)
        qa_source = (qa_source + "\n\n" + formatted).strip() if qa_source else formatted
    if not qa_source:
        qa_source = "(No prior Q&A yet; focus on removing internal conflicts and clarifying ambiguous wording.)"

    prompt = SPEC_CONSISTENCY_CLARIFICATION_PROMPT.format(
        spec_content=current_spec,
        qa_source=qa_source,
    )
    try:
        updated_spec = call_llm_text(model, prompt)
    except Exception as e:
        logger.error("Failed to update spec for consistency with LLM: %s", e)
        return current_spec

    spec_file = _write_spec_artifact(
        repo_path,
        f"updated_spec_consistency_v{version}_loop{consistency_loop}.md",
        updated_spec,
    )
    logger.info("Saved consistency-updated spec to %s", spec_file.name)
    return updated_spec


def run_spec_cleanup(
    llm: "LLMClient",
    spec_content: str,
    repo_path: Path,
    on_chunk_progress: Optional[Callable[[int, int], None]] = None,
) -> SpecCleanupResult:
    """Run the Spec Cleanup phase to validate and clean the spec.

    Preconditions: ``llm`` is the ``LLMClient``; ``spec_content`` is a string.
    Postconditions: returns a :class:`SpecCleanupResult`; when JSON recovery fails,
        returns the original spec marked valid.
    """
    prompt = SPEC_CLEANUP_PROMPT.format(spec_content=spec_content)

    raw = parse_json_with_recovery(
        llm=llm,
        prompt=prompt,
        agent_name="PRA_spec_cleanup",
        decompose_fn=default_decompose_by_sections,
        merge_fn=_merge_spec_cleanup_results,
        original_content=spec_content,
        chunk_prompt_template=SPEC_CLEANUP_CHUNK_PROMPT,
        on_chunk_progress=on_chunk_progress,
    )

    if not raw:
        # All recovery failed - return the original spec as valid
        logger.warning("PRA spec_cleanup: No JSON recovered, returning original spec")
        return SpecCleanupResult(
            is_valid=True,
            cleaned_spec=spec_content,
            summary="Spec cleanup skipped - JSON parsing failed",
        )

    return parse_spec_cleanup_response(raw, spec_content)


def parse_spec_cleanup_response(
    raw: Any,
    fallback_spec: str,
) -> SpecCleanupResult:
    """Parse LLM response into SpecCleanupResult.

    Preconditions: ``fallback_spec`` is a string.
    Postconditions: returns a valid :class:`SpecCleanupResult`; a non-dict ``raw``
        yields the fallback spec marked valid; never raises.
    """
    if not isinstance(raw, dict):
        return SpecCleanupResult(
            is_valid=True,
            cleaned_spec=fallback_spec,
            summary="Spec cleanup completed (no structured output)",
        )

    return SpecCleanupResult(
        is_valid=bool(raw.get("is_valid", True)),
        validation_issues=list(raw.get("validation_issues", []))
        if isinstance(raw.get("validation_issues"), list)
        else [],
        cleaned_spec=str(raw.get("cleaned_spec", fallback_spec)),
        summary=str(raw.get("summary", "Spec cleanup complete")),
    )
