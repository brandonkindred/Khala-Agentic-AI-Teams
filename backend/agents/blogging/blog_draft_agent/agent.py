"""
Blog draft agent: takes a research document and an outline and generates
a blog post draft that complies with a brand and writing style guide.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional, Union

from blog_research_agent.models import ResearchReference

from llm_service import (
    LLMClient,
    LLMJsonParseError,
    LLMTemporaryError,
    LLMTruncatedError,
    compact_text,
)

from .models import DraftInput, DraftOutput, ReviseDraftInput
from .prompts import (
    ALLOWED_CLAIMS_INSTRUCTION,
    DRAFT_SYSTEM_REMINDER,
    EXTRACT_NOTES_PROMPT,
    REVISE_DRAFT_PROMPT,
    REVISE_SINGLE_ITEM_PROMPT,
)

logger = logging.getLogger(__name__)

# Context budget for compaction — content exceeding these thresholds is compacted
# (LLM-summarised) rather than naively truncated, preserving technical detail.
# The model context (e.g. 262K tokens ≈ 917K chars) is large enough that
# compaction should rarely be needed.
COMPACT_RESEARCH_CHARS = 800_000
COMPACT_OUTLINE_CHARS = 200_000
COMPACT_CLAIMS_CHARS = 150_000
COMPACT_PER_SOURCE_CHARS = 100_000


def _extract_draft_after_marker(raw_response: str) -> str:
    """
    Extract draft content from model output that uses the hybrid format:
    first line {\"draft\": 0}, then ---DRAFT---, then the full blog post in Markdown.
    Falls back to parsing the whole response as JSON with a \"draft\" key.
    """
    if not raw_response or not isinstance(raw_response, str):
        return ""
    text = raw_response.strip()
    for marker in ("\n---DRAFT---\n", "\n---DRAFT---", "---DRAFT---\n", "---DRAFT---"):
        if marker in text:
            after = text.split(marker, 1)[1].strip()
            if after:
                return after
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            d = data.get("draft")
            if isinstance(d, str) and d.strip():
                return d.strip()
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


def _write_draft_to_path(draft: str, path: Union[str, Path]) -> None:
    """Write draft content to path; create parent dirs if needed. Log the saved path."""
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(draft, encoding="utf-8")
    logger.info("Draft written to %s", p)


class BlogDraftAgent:
    """
    Expert agent that generates a blog post draft from a research document and outline,
    following a provided brand and writing style guide.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        writing_style_guide_content: str = "",
        brand_spec_content: str = "",
    ) -> None:
        """
        Preconditions:
            - llm_client is not None.
        Callers load writing style and brand spec files before instantiation and pass full contents here.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._writing_style_prompt = (writing_style_guide_content or "").strip()
        self._brand_spec_prompt = (brand_spec_content or "").strip()
        parts: list[str] = []
        if self._brand_spec_prompt:
            parts.append("--- BRAND SPEC ---\n" + self._brand_spec_prompt)
        if self._writing_style_prompt:
            parts.append("--- WRITING STYLE GUIDE ---\n" + self._writing_style_prompt)
        self._style_prompt = "\n\n".join(parts)

    def _extract_notes_from_source(
        self,
        ref: ResearchReference,
        outline: str,
        audience: Optional[str],
        tone: Optional[str],
    ) -> dict[str, Any]:
        """
        Extract notes and citations from a single source for use when drafting.
        Returns dict with "notes" (str) and "citations" (list). On failure, returns safe default.
        """
        doc_text = (ref.content or ref.summary or "").strip()
        if ref.key_points:
            doc_text = doc_text + "\n\nKey points:\n" + "\n".join(f"- {p}" for p in ref.key_points)
        doc_text = compact_text(doc_text, COMPACT_PER_SOURCE_CHARS, self.llm, "source document")
        source_ref_str = f"{ref.title} ({ref.url})"
        prompt = EXTRACT_NOTES_PROMPT + "\n\n---\nOUTLINE:\n" + outline + "\n\n---\n"
        if audience:
            prompt += f"Audience: {audience}\n"
        if tone:
            prompt += f"Tone/Purpose: {tone}\n"
        prompt += f"\n---\nSOURCE: {ref.title}\nURL: {ref.url}\n---\nDocument text:\n{doc_text}"
        try:
            data = self.llm.complete_json(prompt, temperature=0.2)
            notes = data.get("notes") or ""
            citations = data.get("citations")
            if not isinstance(citations, list):
                citations = []
            return {"notes": notes, "citations": citations, "source_ref": source_ref_str}
        except Exception as e:
            logger.warning("Extraction failed for source %s: %s", ref.title, e)
            return {
                "notes": ref.summary or "(No summary)",
                "citations": [],
                "source_ref": source_ref_str,
            }

    def run(
        self,
        draft_input: DraftInput,
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
        draft_output_path: Optional[Union[str, Path]] = None,
    ) -> DraftOutput:
        """
        Generate a blog post draft from the research document and/or references and outline.

        When research_references is non-empty, extracts notes/citations from each source in parallel,
        combines them, then drafts from the combined notes. Otherwise uses research_document (with truncation).

        When draft_output_path is set, writes the draft to that path and logs the path.
        """
        outline = draft_input.outline_for_prompt().strip()
        outline = compact_text(outline, COMPACT_OUTLINE_CHARS, self.llm, "content plan")
        if not outline:
            logger.warning("Empty content plan; returning minimal draft.")
            return DraftOutput(draft="# Draft\n\nAdd a content plan to generate a draft.")

        refs = draft_input.research_references if draft_input.research_references else []
        if refs:
            if on_llm_request:
                on_llm_request("Extracting notes from sources...")
            max_workers = min(len(refs), 8)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(
                        self._extract_notes_from_source,
                        ref,
                        outline,
                        draft_input.audience,
                        draft_input.tone_or_purpose,
                    )
                    for ref in refs
                ]
                extractions = [fut.result() for fut in futures]
            combined_parts = []
            for ext in extractions:
                section = f"## Source: {ext['source_ref']}\n\n{ext['notes']}"
                if ext.get("citations"):
                    cit_lines = [
                        f"- {c.get('fact_or_quote', '')} (source: {c.get('source_ref', ext['source_ref'])})"
                        for c in ext["citations"]
                    ]
                    section += "\n\nCitations:\n" + "\n".join(cit_lines)
                combined_parts.append(section)
            research = "\n\n".join(combined_parts)
            logger.info(
                "Combined notes from %s sources, total len=%s",
                len(refs),
                len(research),
            )
        else:
            research = (draft_input.research_document or "").strip()
            research = compact_text(research, COMPACT_RESEARCH_CHARS, self.llm, "research document")
            if not research:
                logger.warning("Empty research_document; returning minimal draft.")
                return DraftOutput(
                    draft="# Draft\n\nAdd research document and outline to generate a draft."
                )

        style_guide_text = self._style_prompt

        logger.info(
            "Generating draft: research len=%s, outline len=%s, style_guide len=%s",
            len(research),
            len(outline),
            len(style_guide_text),
        )

        brand_section = (
            self._brand_spec_prompt
            if self._brand_spec_prompt
            else "No brand specification was provided. Follow the style guide below."
        )
        prompt_parts = [
            DRAFT_SYSTEM_REMINDER,
            "",
            "---",
            "BRAND AND STYLE (mandatory for every sentence):",
            "---",
            brand_section,
            "",
            "---",
            "STYLE GUIDE (you must follow every applicable rule):",
            "---",
            style_guide_text,
            "",
        ]
        if draft_input.allowed_claims and draft_input.allowed_claims.get("claims"):
            claims_list = draft_input.allowed_claims["claims"]
            claims_text = "\n".join(
                f"- [CLAIM:{c.get('id', '')}] {c.get('text', '')} (sources: {', '.join(c.get('citations', []))})"
                for c in claims_list
            )
            claims_text = compact_text(
                claims_text, COMPACT_CLAIMS_CHARS, self.llm, "allowed claims"
            )
            prompt_parts.append(ALLOWED_CLAIMS_INSTRUCTION.format(claims_text=claims_text))
            prompt_parts.append("")
        prompt_parts.extend(
            [
                "---",
                "RESEARCH DOCUMENT (use this for facts, examples, and substance):",
                "---",
                research,
                "",
                "---",
                "CONTENT PLAN (follow narrative flow and section coverage):",
                "---",
                outline,
            ]
        )
        if draft_input.selected_title:
            prompt_parts.append("")
            prompt_parts.append("---")
            prompt_parts.append(
                f"AUTHOR-CHOSEN TITLE (NON-NEGOTIABLE): Use this exact string as the H1 heading at the top of the post — do not rephrase, shorten, or change it:\n{draft_input.selected_title}"
            )
        if draft_input.elicited_stories:
            prompt_parts.append("")
            prompt_parts.append("---")
            prompt_parts.append(
                "AUTHOR'S PERSONAL STORIES (use these in the relevant sections — do not invent new details beyond what is provided):\n"
                + draft_input.elicited_stories
            )
        if draft_input.audience:
            prompt_parts.append("")
            prompt_parts.append(f"Audience: {draft_input.audience}")
        if draft_input.tone_or_purpose:
            prompt_parts.append(f"Tone/Purpose: {draft_input.tone_or_purpose}")
        prompt_parts.append("")
        prompt_parts.append("---")
        prompt_parts.append(
            "Before outputting, ensure: no banned phrases; 8th grade reading level; descriptive headings; first-person opening hook from author-provided stories (or placeholder if none provided — NEVER fabricate); at least one transparent-failure moment from author stories (or placeholder if none — NEVER fabricate); at least one specific number (dollar figure, percentage, or duration) if the topic supports it; trade-offs acknowledged; technical concepts introduced through the pain they solve (not as definitions); one practical next step in the conclusion. FINAL CHECK: scan every 'I' or 'my' sentence — if it describes a specific event not from the AUTHOR'S PERSONAL STORIES section, replace it with a placeholder."
        )
        if (draft_input.length_guidance or "").strip():
            prompt_parts.append("")
            prompt_parts.append("---")
            prompt_parts.append(draft_input.length_guidance.strip())
        else:
            prompt_parts.append(
                f"TARGET LENGTH: Write approximately {draft_input.target_word_count} words. "
                "Be complete and thorough within this limit — do not pad or repeat yourself to reach it, "
                "and do not cut substance to stay under it. Aim for the target, not perfection."
            )
        prompt_parts.append("")
        prompt_parts.append(
            'Use this format: first line {"draft": 0}, then ---DRAFT---, then the full blog post in Markdown.'
        )
        prompt = "\n".join(prompt_parts)

        if on_llm_request:
            on_llm_request("Generating draft...")

        # Use raw-text completion so the model can output the hybrid format (---DRAFT--- then markdown).
        # complete_json() forces a single JSON object, so the model would output only {"draft": 0} and we'd get no content.
        draft = ""
        draft_max_tokens = 32768
        try:
            raw_response = self.llm.complete(
                prompt,
                temperature=0.3,
                max_tokens=draft_max_tokens,
                system_prompt=DRAFT_SYSTEM_REMINDER,
            )
            draft = _extract_draft_after_marker(raw_response)
        except (LLMJsonParseError, LLMTruncatedError) as e:
            logger.warning("Draft complete() failed: %s; trying complete_json fallback.", e)
            try:
                data = self.llm.complete_json(prompt, temperature=0.3, max_tokens=draft_max_tokens)
                raw_draft = data.get("draft")
                if isinstance(raw_draft, str) and raw_draft.strip():
                    draft = raw_draft.strip()
            except (LLMJsonParseError, LLMTruncatedError):
                pass

        if not draft:
            logger.warning("LLM returned no draft content; returning placeholder.")
            draft = "# Draft\n\nNo draft was generated. Check the model response or try again."

        logger.info("Draft generated: length=%s", len(draft))
        if draft_output_path:
            _write_draft_to_path(draft, draft_output_path)
        return DraftOutput(draft=draft)

    def _build_revise_single_item_prompt(
        self,
        draft: str,
        item: Any,
        item_index: int,
        style_guide_text: str,
        revise_input: ReviseDraftInput,
    ) -> str:
        """Build the revision prompt for a single feedback item."""
        loc = f" [{item.location}]" if getattr(item, "location", None) else ""
        line = f"[{item.severity}] {item.category}{loc}: {item.issue}"
        if getattr(item, "suggestion", None):
            line += f"\n   Suggestion: {item.suggestion}"

        brand_section = (
            self._brand_spec_prompt
            if self._brand_spec_prompt
            else "No brand specification was provided. Follow the style guide below."
        )
        cp = compact_text(
            revise_input.outline_for_prompt(), COMPACT_OUTLINE_CHARS, self.llm, "content plan"
        )
        prompt_parts = [
            REVISE_SINGLE_ITEM_PROMPT,
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
            "CONTENT PLAN (preserve section intent):",
            "---",
            cp,
            "",
            "---",
            f"SINGLE FEEDBACK ITEM TO APPLY (item {item_index}):",
            "---",
            line,
            "",
            "---",
            "CURRENT DRAFT:",
            "---",
            draft,
        ]
        if revise_input.audience:
            prompt_parts.insert(0, f"Audience: {revise_input.audience}\n")
        if revise_input.tone_or_purpose:
            prompt_parts.insert(0, f"Tone/Purpose: {revise_input.tone_or_purpose}\n")
        if revise_input.allowed_claims and revise_input.allowed_claims.get("claims"):
            claims_list = revise_input.allowed_claims["claims"]
            claims_text = "\n".join(
                f"- [CLAIM:{c.get('id', '')}] {c.get('text', '')}" for c in claims_list
            )
            block = "\n".join(
                [
                    "",
                    "---",
                    "ALLOWED CLAIMS (preserve [CLAIM:id] tags; do not add new factual claims):",
                    "---",
                    claims_text,
                ]
            )
            prompt_parts.insert(len(prompt_parts) - 5, block)
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
        if revise_input.research_document:
            research = revise_input.research_document.strip()
            prompt_parts.extend(
                [
                    "",
                    "---",
                    "RESEARCH (for context; preserve facts):",
                    "---",
                    research,
                ]
            )
        length_block = (
            revise_input.length_guidance.strip()
            if (revise_input.length_guidance or "").strip()
            else (
                f"TARGET LENGTH: The revised draft should be approximately {revise_input.target_word_count} words. "
                "Apply the feedback item above without significantly expanding the post beyond this target."
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

    def _format_feedback_item_line(self, item: Any, index: int) -> str:
        """One numbered feedback line (+ optional suggestion) for batch revise prompts."""
        loc = f" [{item.location}]" if getattr(item, "location", None) else ""
        line = f"{index}. [{item.severity}] {item.category}{loc}: {item.issue}"
        if getattr(item, "suggestion", None):
            line += f"\n   Suggestion: {item.suggestion}"
        return line

    def _build_revise_all_items_prompt(
        self,
        draft: str,
        feedback_items: list[Any],
        style_guide_text: str,
        revise_input: ReviseDraftInput,
    ) -> str:
        """Build one revision prompt that applies every copy-editor feedback item."""
        brand_section = (
            self._brand_spec_prompt
            if self._brand_spec_prompt
            else "No brand specification was provided. Follow the style guide below."
        )
        feedback_lines = [
            self._format_feedback_item_line(item, i)
            for i, item in enumerate(feedback_items, start=1)
        ]
        feedback_block = "\n\n".join(feedback_lines)

        cp = compact_text(
            revise_input.outline_for_prompt(), COMPACT_OUTLINE_CHARS, self.llm, "content plan"
        )
        prompt_parts = [
            REVISE_DRAFT_PROMPT,
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
            "---",
            "COPY EDITOR FEEDBACK (apply every numbered item below):",
            "---",
            feedback_block,
            "",
        ]
        if revise_input.previous_feedback_items:
            prev_lines = []
            for i, item in enumerate(revise_input.previous_feedback_items, 1):
                loc = f" [{item.location}]" if item.location else ""
                prev_lines.append(f"{i}. [{item.severity}] {item.category}{loc}: {item.issue}")
            prompt_parts.extend(
                [
                    "---",
                    "PREVIOUSLY ADDRESSED FEEDBACK (do NOT regress on these fixes — the editor already flagged them):",
                    "---",
                    "\n".join(prev_lines),
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
        if revise_input.audience:
            prompt_parts.insert(0, f"Audience: {revise_input.audience}\n")
        if revise_input.tone_or_purpose:
            prompt_parts.insert(0, f"Tone/Purpose: {revise_input.tone_or_purpose}\n")
        if revise_input.allowed_claims and revise_input.allowed_claims.get("claims"):
            claims_list = revise_input.allowed_claims["claims"]
            claims_text = "\n".join(
                f"- [CLAIM:{c.get('id', '')}] {c.get('text', '')}" for c in claims_list
            )
            block = "\n".join(
                [
                    "",
                    "---",
                    "ALLOWED CLAIMS (preserve [CLAIM:id] tags; do not add new factual claims):",
                    "---",
                    claims_text,
                ]
            )
            prompt_parts.insert(len(prompt_parts) - 5, block)
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
        if revise_input.research_document:
            research = revise_input.research_document.strip()
            prompt_parts.extend(
                [
                    "",
                    "---",
                    "RESEARCH (for context; preserve facts):",
                    "---",
                    research,
                ]
            )
        length_block = (
            revise_input.length_guidance.strip()
            if (revise_input.length_guidance or "").strip()
            else (
                f"TARGET LENGTH: The revised draft should be approximately {revise_input.target_word_count} words. "
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

    def revise(
        self,
        revise_input: ReviseDraftInput,
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
        draft_output_path: Optional[Union[str, Path]] = None,
    ) -> DraftOutput:
        """
        Revise a draft by addressing all copy editor feedback in a single LLM pass.

        One prompt lists every feedback item; the model must apply all must_fix /
        should_fix items (and consider items where appropriate). No self-review or
        compliance check is performed here; the pipeline sends the result back to the editor.

        When draft_output_path is set, writes the final revised draft to that path and logs the path.

        Preconditions:
            - revise_input has non-empty draft and feedback_items.
        Postconditions:
            - Returns DraftOutput with the complete revised draft (never partial).
        """
        draft = revise_input.draft.strip()
        if not draft:
            logger.warning("Empty draft in revise; returning as-is.")
            return DraftOutput(draft=revise_input.draft)
        if not revise_input.feedback_items:
            logger.info("No feedback items; returning draft unchanged.")
            return DraftOutput(draft=draft)

        style_guide_text = self._style_prompt
        revise_max_tokens = 32768
        num_items = len(revise_input.feedback_items)

        logger.info("Revising draft for %s feedback items in a single pass", num_items)
        if on_llm_request:
            on_llm_request(f"Addressing all copy editor feedback ({num_items} items)...")

        prompt = self._build_revise_all_items_prompt(
            draft, list(revise_input.feedback_items), style_guide_text, revise_input
        )

        revised: Optional[str] = None
        for attempt in range(3):
            try:
                raw_response = self.llm.complete(
                    prompt,
                    temperature=0.2,
                    max_tokens=revise_max_tokens,
                    system_prompt=REVISE_DRAFT_PROMPT,
                )
                revised = _extract_draft_after_marker(raw_response)
                if revised and revised.strip():
                    break
            except LLMTemporaryError:
                if attempt < 2:
                    logger.warning(
                        "Revise (batch): empty/transient LLM response (attempt %s/3); retrying.",
                        attempt + 1,
                    )
                    time.sleep(2.0 + attempt)
                else:
                    logger.warning(
                        "Revise (batch): empty/transient after 3 attempts; keeping original draft.",
                    )
                    break
            except (LLMJsonParseError, LLMTruncatedError) as e:
                if attempt == 0:
                    logger.warning("Revise (batch) complete() failed: %s; retrying.", e)
                else:
                    logger.warning(
                        "Revise (batch) failed after retry; trying complete_json fallback."
                    )
                    break

        if not revised or not revised.strip():
            try:
                data = self.llm.complete_json(prompt, temperature=0.2, max_tokens=revise_max_tokens)
                raw_draft = data.get("draft") if data else None
                if isinstance(raw_draft, str) and raw_draft.strip():
                    revised = raw_draft.strip()
            except (LLMJsonParseError, LLMTruncatedError):
                pass

        current_draft = draft
        if revised and revised.strip():
            current_draft = revised.strip()
            logger.info("Draft revised in one pass: length=%s", len(current_draft))
        else:
            logger.warning("Revise (batch) produced no content; keeping original draft.")

        logger.info(
            "Final revised draft: length=%s (%s feedback items in prompt)",
            len(current_draft),
            num_items,
        )
        if draft_output_path:
            _write_draft_to_path(current_draft, draft_output_path)
        return DraftOutput(draft=current_draft)
