"""
Revision planning for blog drafts: builds the structured revision-plan prompt,
generates a ``RevisionPlan`` from the LLM (with a plain-text fallback), and
builds the batch "apply all feedback" revision prompt.

Free functions here take explicit ``call_text``/``call_json`` callbacks
instead of an agent's bound ``_call_text``/``_call_agent_json`` methods, so
this module has no dependency on ``BlogWriterAgent`` (or on ``agent.py`` at
all) and can be adopted by any caller that can supply a
``(prompt, system_prompt) -> str`` text completion and a
``(prompt, system_prompt) -> dict`` JSON completion.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from pydantic import ValidationError
from strands.types.exceptions import EventLoopException

from llm_service import (
    LLMError,
    LLMJsonParseError,
    LLMRateLimitError,
    LLMTemporaryError,
    compact_text,
)

from .feedback_tracker import MAX_PREVIOUS_FEEDBACK_ITEMS
from .models import ReviseWriterInput, RevisionPlan, RevisionPlanChange
from .prompts import REVISION_TASK_INSTRUCTIONS, WRITING_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# A text-completion callback: ``call_text(prompt, system_prompt) -> response``.
# Mirrors ``BlogWriterAgent._call_text``'s signature.
CallText = Callable[[str, str], str]

# A JSON-completion callback: ``call_json(prompt, system_prompt) -> dict``.
# Mirrors ``BlogWriterAgent._call_agent_json``'s signature.
CallJson = Callable[[str, str], dict]

# Context budget for compaction — content exceeding these thresholds is compacted
# (LLM-summarised) rather than naively truncated, preserving technical detail.
# The model context (e.g. 262K tokens ≈ 917K chars) is large enough that
# compaction should rarely be needed.
COMPACT_OUTLINE_CHARS = 200_000


def _unwrap_llm_cause(exc: BaseException) -> BaseException:
    """Return the underlying model error when strands wraps it in EventLoopException.

    Preconditions:
        - ``exc`` is the exception caught at an LLM call boundary.
    Postconditions:
        - If ``exc`` is an ``EventLoopException`` with a non-None ``original_exception``,
          returns that original exception.
        - Otherwise returns ``exc`` unchanged.
    """
    if isinstance(exc, EventLoopException):
        original = getattr(exc, "original_exception", None)
        if isinstance(original, BaseException):
            return original
    return exc


def _format_feedback_item_line(item: Any, index: int) -> str:
    """One numbered feedback line (+ optional suggestion) for batch revise prompts.

    Preconditions:
        ``index`` is a positive int. ``item`` exposes ``severity``, ``category``,
        and ``issue`` (via attribute or duck typing); empty/missing values are
        rejected. ``location`` and ``suggestion`` are optional.
    Postconditions:
        Returns a numbered feedback line; includes a location bracket and a
        suggestion sub-line when those optional fields are present.
    Raises:
        ValueError: if ``index`` is not a positive int, or required item
            fields are missing.
    """
    if not isinstance(index, int) or index <= 0:
        raise ValueError(f"index must be a positive int, got {index!r}")
    severity = getattr(item, "severity", None)
    category = getattr(item, "category", None)
    issue = getattr(item, "issue", None)
    if not all([severity, category, issue]):
        raise ValueError(f"Feedback item missing required fields: {item!r}")
    location = getattr(item, "location", None)
    loc = f" [{location}]" if location else ""
    line = f"{index}. [{severity}] {category}{loc}: {issue}"
    suggestion = getattr(item, "suggestion", None)
    if suggestion:
        line += f"\n   Suggestion: {suggestion}"
    return line


def build_revision_plan_prompt(
    draft: str, feedback_items: list[Any], revise_input: ReviseWriterInput, *, llm: Any
) -> str:
    """Build a prompt that asks the LLM for a structured revision plan.

    Preconditions:
        - ``draft`` is the current Markdown draft text.
        - ``feedback_items`` is a sequence of items that each expose
          ``severity``, ``category``, and ``issue`` (and optionally
          ``location`` / ``suggestion``) for ``_format_feedback_item_line``.
        - ``revise_input`` provides the content plan via
          ``outline_for_prompt()``.
        - ``llm`` is the ``LLMClient`` passed to ``compact_text`` when the
          content plan exceeds ``COMPACT_OUTLINE_CHARS`` (e.g. an agent's
          ``self._model``).
    Postconditions:
        - Returns a prompt string that instructs the model to return JSON
          matching the ``RevisionPlan`` schema (``summary``, ordered
          ``changes`` with ``section`` / ``feedback_ids`` / ``action`` /
          ``rationale``, and ``risks``), with feedback referenced by
          1-based index and ``must_fix`` severity prioritized.
    """
    feedback_lines = [
        _format_feedback_item_line(item, i) for i, item in enumerate(feedback_items, start=1)
    ]
    cp = compact_text(revise_input.outline_for_prompt(), COMPACT_OUTLINE_CHARS, llm, "content plan")
    parts = [
        "Analyse ALL feedback items and create a structured revision plan for this draft.",
        "Return valid JSON matching this schema exactly:",
        "{",
        '  "summary": "One-paragraph overview of the revision strategy",',
        '  "changes": [',
        "    {",
        '      "section": "Which section or location this change targets",',
        '      "feedback_ids": [1, 2],',
        '      "action": "rewrite | delete | merge | add | rephrase | restructure",',
        '      "rationale": "Why this change is needed"',
        "    }",
        "  ],",
        '  "risks": ["Potential regressions or trade-offs"]',
        "}",
        "",
        "List changes in priority order (must_fix severity first).",
        "Reference feedback items by their 1-based index number.",
        "",
        "---",
        "CONTENT PLAN:",
        "---",
        cp,
        "",
        "---",
        "FEEDBACK ITEMS:",
        "---",
        "\n\n".join(feedback_lines),
        "",
        "---",
        "CURRENT DRAFT:",
        "---",
        draft,
    ]
    return "\n".join(parts)


def build_revise_all_items_prompt(
    draft: str,
    feedback_items: list[Any],
    revision_plan: str,
    style_guide_text: str,
    revise_input: ReviseWriterInput,
    *,
    brand_section: str,
    llm: Any,
) -> str:
    """Build one revision prompt that applies every copy-editor feedback item.

    Preconditions:
        - ``brand_section`` is the caller's rendered brand/style section
          (e.g. an agent's ``_brand_section_for_prompt()`` output).
        - ``llm`` is the ``LLMClient`` passed to ``compact_text`` when the
          content plan exceeds ``COMPACT_OUTLINE_CHARS`` (e.g. an agent's
          ``self._model``).
    Postconditions:
        - Returns a prompt string embedding the brand/style sections, the
          content plan, every feedback item formatted via
          ``_format_feedback_item_line``, ``revision_plan`` as planning
          context, and the current draft.
        - When present on ``revise_input``: ``revise_input.persistent_issues``
          is inserted before the feedback block; ``previous_feedback_items``
          (capped at ``MAX_PREVIOUS_FEEDBACK_ITEMS``) is inserted after it;
          ``selected_title`` and ``elicited_stories`` are each appended as
          their own labeled section near the end (title before stories);
          and ``tone_or_purpose`` / ``audience`` are each prepended as a
          single labeled line at the very front (tone_or_purpose before
          audience). Absent fields are omitted rather than left blank.
    """
    feedback_lines = [
        _format_feedback_item_line(item, i) for i, item in enumerate(feedback_items, start=1)
    ]
    feedback_block = "\n\n".join(feedback_lines)

    cp = compact_text(revise_input.outline_for_prompt(), COMPACT_OUTLINE_CHARS, llm, "content plan")
    prompt_parts = [
        REVISION_TASK_INSTRUCTIONS,
        "",
        "---",
        "BRAND AND STYLE (mandatory for every sentence):",
        "---",
        brand_section,
        "",
        "---",
        "STYLE GUIDE (follow in the revised draft):",
        "---",
        style_guide_text,
        "",
        "---",
        "CONTENT PLAN (preserve section intent and narrative flow):",
        "---",
        cp,
        "",
    ]
    # Persistent issues — placed BEFORE current feedback for higher LLM attention.
    if revise_input.persistent_issues:
        pi_lines = []
        for i, pi in enumerate(revise_input.persistent_issues, 1):
            location = getattr(pi, "location", None)
            loc = f" [{location}]" if location else ""
            occurrence_count = getattr(pi, "occurrence_count", 0)
            severity = getattr(pi, "severity", "unknown")
            category = getattr(pi, "category", "")
            line = (
                f"{i}. [{severity}] {category}{loc} "
                f"(flagged {occurrence_count} times): {getattr(pi, 'issue', '')}"
            )
            suggestion = getattr(pi, "suggestion", None)
            if suggestion:
                line += f'\n   REQUIRED FIX: "{suggestion}"'
            pi_lines.append(line)
        prompt_parts.extend(
            [
                "---",
                "PERSISTENT ISSUES — THESE HAVE FAILED TO BE FIXED AND MUST BE RESOLVED THIS ITERATION:",
                "---",
                "\n\n".join(pi_lines),
                "",
            ]
        )
    prompt_parts.extend(
        [
            "---",
            "REVISION PLAN (execute this plan before writing):",
            "---",
            revision_plan.strip() or "No explicit plan generated; apply all feedback directly.",
            "",
            "---",
            "COPY EDITOR FEEDBACK (apply every numbered item below):",
            "---",
            feedback_block,
            "",
        ]
    )
    if revise_input.previous_feedback_items:
        prev_lines = []
        for i, item in enumerate(
            revise_input.previous_feedback_items[:MAX_PREVIOUS_FEEDBACK_ITEMS], 1
        ):
            location = getattr(item, "location", None)
            loc = f" [{location}]" if location else ""
            severity = getattr(item, "severity", "unknown")
            category = getattr(item, "category", "")
            issue = getattr(item, "issue", "")
            prev_lines.append(f"{i}. [{severity}] {category}{loc}: {issue}")
        prompt_parts.extend(
            [
                "---",
                "RECENTLY RESOLVED FEEDBACK (do NOT regress on these):",
                "---",
                "\n\n".join(prev_lines),
                "",
            ]
        )
    prompt_parts.extend(
        [
            "---",
            "CURRENT DRAFT:",
            "---",
            draft,
        ]
    )
    prefixes: list[str] = []
    if revise_input.tone_or_purpose:
        prefixes.append(f"Tone/Purpose: {revise_input.tone_or_purpose}")
    if revise_input.audience:
        prefixes.append(f"Audience: {revise_input.audience}")
    if prefixes:
        prompt_parts = prefixes + prompt_parts
    if revise_input.selected_title:
        prompt_parts.extend(
            [
                "",
                "---",
                f"AUTHOR-CHOSEN TITLE (preserve this exact H1): {revise_input.selected_title}",
            ]
        )
    if revise_input.elicited_stories:
        prompt_parts.extend(
            [
                "",
                "---",
                "AUTHOR'S PERSONAL STORIES (preserve these in the revision):\n"
                + revise_input.elicited_stories,
            ]
        )
    length_block = (
        revise_input.length_guidance.strip()
        if (revise_input.length_guidance or "").strip()
        else (
            f"TARGET LENGTH: Aim for roughly {revise_input.target_word_count} words "
            f"(acceptable range: {int(revise_input.target_word_count * 0.75)}–{int(revise_input.target_word_count * 1.3)} words). "
            "Apply all feedback above without significantly expanding the post beyond this target."
        )
    )
    prompt_parts.extend(
        [
            "",
            "---",
            length_block,
            "",
            "---",
            'Use this format: first line {"draft": 0}, then ---DRAFT---, then the full revised blog post in Markdown.',
        ]
    )
    return "\n".join(prompt_parts)


def generate_revision_plan(
    draft: str,
    feedback_items: list[Any],
    revise_input: ReviseWriterInput,
    *,
    call_json: CallJson,
    call_text: CallText,
    llm: Any = None,
) -> RevisionPlan:
    """Build a structured revision plan, with a plain-text fallback.

    Calls the JSON-oriented LLM path first (via ``call_json``) and converts
    its response to a ``RevisionPlan``. Non-transient LLM/parse failures fall
    back to a plain-text plan (via ``call_text``); transient LLM errors are
    unwrapped and re-raised. Unexpected programming errors (non-``LLMError``)
    propagate rather than being swallowed into the unstructured fallback.

    Preconditions:
        - ``call_json(prompt, system_prompt)`` returns a parsed JSON object
          (``dict``), or raises an ``LLMError`` subclass on failure.
        - ``call_text(prompt, system_prompt)`` returns the model's text
          output, or raises an ``LLMError`` subclass on failure.
        - ``llm`` is the ``LLMClient`` forwarded to ``compact_text`` inside
          ``build_revision_plan_prompt`` (e.g. an agent's ``self._model``).
    Postconditions:
        - Returns a ``RevisionPlan``; never returns ``None``.
    """
    prompt = build_revision_plan_prompt(draft, feedback_items, revise_input, llm=llm)
    try:
        data = call_json(prompt, WRITING_SYSTEM_PROMPT)
        if data is None or not isinstance(data, dict):
            return RevisionPlan(summary="Planning produced no output.", changes=[], risks=[])
        changes: list[RevisionPlanChange] = []
        changes_raw = data.get("changes") or []
        if not isinstance(changes_raw, list):
            logger.warning("Revision plan 'changes' is not a list: %r", changes_raw)
            changes_raw = []
        for c in changes_raw:
            if not isinstance(c, dict):
                continue
            try:
                changes.append(RevisionPlanChange(**c))
            except (TypeError, ValueError, ValidationError) as change_exc:
                logger.debug("Skipping malformed revision plan change: %s", change_exc)
                continue
        risks_raw = data.get("risks") or []
        if not isinstance(risks_raw, list):
            logger.warning("Revision plan 'risks' is not a list: %r", risks_raw)
            risks_raw = []
        # Normalize model fields before RevisionPlan construction so a
        # non-string summary / non-string risk entry is treated as a
        # structured-response failure (plain-text fallback) rather than a
        # programming error that aborts the draft pipeline.
        summary_raw = data.get("summary", "")
        if summary_raw is None:
            summary = ""
        elif isinstance(summary_raw, str):
            summary = summary_raw
        else:
            raise LLMJsonParseError(
                f"Revision plan 'summary' must be a string, got {type(summary_raw).__name__}",
                response_preview=repr(summary_raw)[:200],
            )
        risks: list[str] = []
        for risk in risks_raw:
            if isinstance(risk, str):
                risks.append(risk)
            else:
                raise LLMJsonParseError(
                    f"Revision plan 'risks' entries must be strings, got {type(risk).__name__}",
                    response_preview=repr(risk)[:200],
                )
        try:
            return RevisionPlan(
                summary=summary,
                changes=changes,
                risks=risks,
            )
        except ValidationError as plan_exc:
            raise LLMJsonParseError(
                f"Revision plan failed schema validation: {plan_exc}",
                response_preview=repr(data)[:200],
            ) from plan_exc
    except Exception as e:
        cause = _unwrap_llm_cause(e)
        if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
            raise cause
        if not isinstance(cause, LLMError):
            raise
        logger.warning(
            "Structured revision planning failed: %s — falling back to unstructured", cause
        )
        # Graceful degradation: try plain-text plan
        try:
            plain = call_text(prompt, WRITING_SYSTEM_PROMPT)
            return RevisionPlan(summary=(plain or "").strip(), changes=[], risks=[])
        except Exception as fallback_exc:
            fallback_cause = _unwrap_llm_cause(fallback_exc)
            if isinstance(fallback_cause, (LLMRateLimitError, LLMTemporaryError)):
                raise fallback_cause
            if not isinstance(fallback_cause, LLMError):
                raise
            return RevisionPlan(summary="Revision planning failed.", changes=[], risks=[])
