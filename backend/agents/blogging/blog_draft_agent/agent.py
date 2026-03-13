"""
Blog draft agent: takes a research document and an outline and generates
a blog post draft that complies with a brand and writing style guide.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

from llm_service import LLMClient

from blog_research_agent.models import ResearchReference

from .models import DraftInput, DraftOutput, ReviseDraftInput
from .prompts import (
    ALLOWED_CLAIMS_INSTRUCTION,
    DRAFT_SYSTEM_REMINDER,
    EXTRACT_NOTES_PROMPT,
    MINIMAL_STYLE_REMINDER,
    REVISE_DRAFT_PROMPT,
)

try:
    from shared.brand_spec import BrandSpec, load_brand_spec
except ImportError:
    BrandSpec = None
    load_brand_spec = None

logger = logging.getLogger(__name__)

# Caps for prompt inputs so the combined prompt fits within model context (e.g. 262K tokens for qwen3.5:397b-cloud).
# Exceeding these can cause "prompt too long; exceeded max context length" (400).
MAX_RESEARCH_CHARS_FOR_DRAFT = 100_000
MAX_OUTLINE_CHARS_FOR_DRAFT = 20_000
MAX_CLAIMS_CHARS_FOR_DRAFT = 15_000
# Per-source cap for extraction calls (each document sent to one LLM call).
MAX_CHARS_PER_SOURCE = 12_000

# Default style guide path (Brandon Kindred brand and writing guide) relative to project root
_DEFAULT_STYLE_GUIDE_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "brandon_kindred_brand_and_writing_style_guide.md"
)


def _load_style_guide(path: str | Path) -> str:
    """Load style guide text from a file. Raises OSError if file cannot be read."""
    return Path(path).read_text(encoding="utf-8").strip()


class BlogDraftAgent:
    """
    Expert agent that generates a blog post draft from a research document and outline,
    following a provided brand and writing style guide.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        default_style_guide_path: Optional[str | Path] = None,
        brand_spec_path: Optional[str | Path] = None,
    ) -> None:
        """
        Preconditions:
            - llm_client is not None.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        if default_style_guide_path is not None:
            self.default_style_guide_path = Path(default_style_guide_path)
        else:
            self.default_style_guide_path = _DEFAULT_STYLE_GUIDE_PATH if _DEFAULT_STYLE_GUIDE_PATH.exists() else None
        self.brand_spec_path = Path(brand_spec_path) if brand_spec_path else None

    def _resolve_style_guide(
        self,
        style_guide: Optional[str],
        brand_spec_path: Optional[str],
        brand_spec: Optional[dict],
    ) -> str:
        """Resolve style guide text: prefer brand_spec when provided, else style_guide or default."""
        if brand_spec and load_brand_spec:
            try:
                spec = BrandSpec.model_validate(brand_spec) if hasattr(BrandSpec, "model_validate") else BrandSpec.parse_obj(brand_spec)
                return spec.to_prompt_summary()
            except Exception:
                pass
        path = brand_spec_path or (self.brand_spec_path if self.brand_spec_path and self.brand_spec_path.exists() else None)
        if path and load_brand_spec:
            try:
                spec = load_brand_spec(path)
                return spec.to_prompt_summary()
            except Exception as e:
                logger.warning("Could not load brand spec from %s: %s", path, e)
        if style_guide:
            return style_guide.strip()
        if self.default_style_guide_path and self.default_style_guide_path.exists():
            try:
                return _load_style_guide(self.default_style_guide_path)
            except OSError as e:
                logger.warning("Could not load default style guide: %s", e)
        return MINIMAL_STYLE_REMINDER

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
        if len(doc_text) > MAX_CHARS_PER_SOURCE:
            doc_text = doc_text[:MAX_CHARS_PER_SOURCE] + "\n\n[... truncated for context ...]"
        source_ref_str = f"{ref.title} ({ref.url})"
        prompt = (
            EXTRACT_NOTES_PROMPT
            + "\n\n---\nOUTLINE:\n"
            + outline[:4000]
            + "\n\n---\n"
        )
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
    ) -> DraftOutput:
        """
        Generate a blog post draft from the research document and/or references and outline.

        When research_references is non-empty, extracts notes/citations from each source in parallel,
        combines them, then drafts from the combined notes. Otherwise uses research_document (with truncation).
        """
        outline = draft_input.outline.strip()
        if len(outline) > MAX_OUTLINE_CHARS_FOR_DRAFT:
            logger.warning(
                "Truncating outline from %s to %s chars to fit context",
                len(outline),
                MAX_OUTLINE_CHARS_FOR_DRAFT,
            )
            outline = outline[:MAX_OUTLINE_CHARS_FOR_DRAFT] + "\n\n[... outline truncated for context ...]"
        if not outline:
            logger.warning("Empty outline; returning minimal draft.")
            return DraftOutput(draft="# Draft\n\nAdd outline to generate a draft.")

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
            if len(research) > MAX_RESEARCH_CHARS_FOR_DRAFT:
                logger.warning(
                    "Truncating research_document from %s to %s chars to fit context",
                    len(research),
                    MAX_RESEARCH_CHARS_FOR_DRAFT,
                )
                research = research[:MAX_RESEARCH_CHARS_FOR_DRAFT] + "\n\n[... research truncated for context ...]"
            if not research:
                logger.warning("Empty research_document; returning minimal draft.")
                return DraftOutput(draft="# Draft\n\nAdd research document and outline to generate a draft.")

        # Resolve style guide text (brand_spec takes precedence when provided)
        style_guide_text = self._resolve_style_guide(
            draft_input.style_guide,
            draft_input.brand_spec_path,
            draft_input.brand_spec,
        )

        logger.info(
            "Generating draft: research len=%s, outline len=%s, style_guide len=%s",
            len(research),
            len(outline),
            len(style_guide_text),
        )

        prompt_parts = [
            DRAFT_SYSTEM_REMINDER,
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
                f"- [CLAIM:{c.get('id','')}] {c.get('text','')} (sources: {', '.join(c.get('citations',[]))})"
                for c in claims_list
            )
            if len(claims_text) > MAX_CLAIMS_CHARS_FOR_DRAFT:
                logger.warning(
                    "Truncating allowed_claims from %s to %s chars to fit context",
                    len(claims_text),
                    MAX_CLAIMS_CHARS_FOR_DRAFT,
                )
                claims_text = claims_text[:MAX_CLAIMS_CHARS_FOR_DRAFT] + "\n... [claims truncated for context]"
            prompt_parts.append(ALLOWED_CLAIMS_INSTRUCTION.format(claims_text=claims_text))
            prompt_parts.append("")
        prompt_parts.extend([
            "---",
            "RESEARCH DOCUMENT (use this for facts, examples, and substance):",
            "---",
            research,
            "",
            "---",
            "OUTLINE (follow this structure):",
            "---",
            outline,
        ])
        if draft_input.audience:
            prompt_parts.append("")
            prompt_parts.append(f"Audience: {draft_input.audience}")
        if draft_input.tone_or_purpose:
            prompt_parts.append(f"Tone/Purpose: {draft_input.tone_or_purpose}")
        prompt_parts.append("")
        prompt_parts.append("---")
        prompt_parts.append('Use this format: first line {"draft": 0}, then ---DRAFT---, then the full blog post in Markdown.')
        prompt = "\n".join(prompt_parts)

        if on_llm_request:
            on_llm_request("Generating draft...")
        data = self.llm.complete_json(prompt, temperature=0.3)
        raw_draft = data.get("draft")
        if isinstance(raw_draft, str):
            draft = raw_draft.strip()
        else:
            draft = ""

        if not draft:
            logger.warning("LLM returned no draft content; returning placeholder.")
            draft = "# Draft\n\nNo draft was generated. Check the model response or try again."

        logger.info("Draft generated: length=%s", len(draft))
        return DraftOutput(draft=draft)

    def revise(
        self,
        revise_input: ReviseDraftInput,
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
    ) -> DraftOutput:
        """
        Revise a draft based on copy editor feedback.

        Preconditions:
            - revise_input has non-empty draft and feedback_items.
        Postconditions:
            - Returns DraftOutput with the revised draft.
        """
        draft = revise_input.draft.strip()
        if not draft:
            logger.warning("Empty draft in revise; returning as-is.")
            return DraftOutput(draft=revise_input.draft)
        if not revise_input.feedback_items:
            logger.info("No feedback items; returning draft unchanged.")
            return DraftOutput(draft=draft)

        # Resolve style guide (brand_spec takes precedence when provided)
        style_guide_text = self._resolve_style_guide(
            revise_input.style_guide,
            revise_input.brand_spec_path,
            revise_input.brand_spec,
        )

        # Format feedback for prompt
        feedback_lines = []
        if revise_input.feedback_summary:
            feedback_lines.append(f"Note from editor: {revise_input.feedback_summary}\n")
        for i, item in enumerate(revise_input.feedback_items, 1):
            loc = f" [{item.location}]" if item.location else ""
            feedback_lines.append(f"{i}. [{item.severity}] {item.category}{loc}: {item.issue}")
            if item.suggestion:
                feedback_lines.append(f"   Suggestion: {item.suggestion}")
            feedback_lines.append("")

        prompt_parts = [
            REVISE_DRAFT_PROMPT,
            "",
            "---",
            "STYLE GUIDE:",
            "---",
            style_guide_text,
            "",
            "---",
            "COPY EDITOR FEEDBACK:",
            "---",
            "\n".join(feedback_lines).strip(),
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
                f"- [CLAIM:{c.get('id','')}] {c.get('text','')}"
                for c in claims_list
            )
            block = "\n".join([
                "",
                "---",
                "ALLOWED CLAIMS (preserve [CLAIM:id] tags; do not add new factual claims):",
                "---",
                claims_text,
            ])
            prompt_parts.insert(len(prompt_parts) - 5, block)
        if revise_input.research_document:
            research = revise_input.research_document.strip()
            research_snippet = research[:3000] + ("..." if len(research) > 3000 else "")
            prompt_parts.extend([
                "",
                "---",
                "RESEARCH (for context; preserve facts):",
                "---",
                research_snippet,
            ])
        prompt_parts.extend([
            "",
            "---",
            'Use this format: first line {"draft": 0}, then ---DRAFT---, then the full revised blog post in Markdown.',
        ])
        prompt = "\n".join(prompt_parts)

        if on_llm_request:
            on_llm_request("Revising draft...")
        data = self.llm.complete_json(prompt, temperature=0.2)
        raw_draft = data.get("draft")
        if isinstance(raw_draft, str):
            revised = raw_draft.strip()
        else:
            revised = draft

        if not revised:
            logger.warning("LLM returned no revised content; keeping original draft.")
            revised = draft

        logger.info("Draft revised: length=%s", len(revised))
        return DraftOutput(draft=revised)
