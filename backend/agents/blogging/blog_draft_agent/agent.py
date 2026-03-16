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
from typing import Any, Callable, Dict, Optional, Union

from llm_service import LLMClient, LLMJsonParseError, LLMTemporaryError, LLMTruncatedError

from blog_research_agent.models import ResearchReference

from .models import DraftInput, DraftOutput, ReviseDraftInput
from .prompts import (
    ALLOWED_CLAIMS_INSTRUCTION,
    BRAND_AND_STYLE_PRIMER,
    DRAFT_SYSTEM_REMINDER,
    EXTRACT_NOTES_PROMPT,
    MANDATORY_STYLE_CHECKLIST,
    MINIMAL_STYLE_REMINDER,
    REVISE_SINGLE_ITEM_PROMPT,
    SELF_REVIEW_PROMPT,
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

# Default paths for brand and writing guidelines (blogging/docs/)
_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
_DEFAULT_WRITING_STYLE_GUIDE_PATH = _DOCS_DIR / "brandon_kindred_brand_and_writing_style_guide.md"
_DEFAULT_BRAND_SPEC_PATH = _DOCS_DIR / "brand_spec.yaml"


def _load_style_guide(path: str | Path) -> str:
    """Load style guide text from a file. Raises OSError if file cannot be read."""
    return Path(path).read_text(encoding="utf-8").strip()


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
            self.default_style_guide_path = _DEFAULT_WRITING_STYLE_GUIDE_PATH if _DEFAULT_WRITING_STYLE_GUIDE_PATH.exists() else None
        self.brand_spec_path = Path(brand_spec_path) if brand_spec_path else None
        if self.brand_spec_path is None and _DEFAULT_BRAND_SPEC_PATH.exists():
            self.brand_spec_path = _DEFAULT_BRAND_SPEC_PATH

    def _resolve_style_guide(
        self,
        style_guide: Optional[str],
        brand_spec_path: Optional[str],
        brand_spec: Optional[dict],
    ) -> str:
        """
        Resolve combined brand and writing guidelines: use both brand_spec.yaml and
        brandon_kindred_brand_and_writing_style_guide.md when available.
        """
        parts: list[str] = []

        # 1. Brand spec (from dict, or from path)
        brand_text: Optional[str] = None
        if brand_spec and load_brand_spec:
            try:
                spec = BrandSpec.model_validate(brand_spec) if hasattr(BrandSpec, "model_validate") else BrandSpec.parse_obj(brand_spec)
                brand_text = spec.to_prompt_summary()
            except Exception:
                pass
        if not brand_text:
            path = brand_spec_path or (self.brand_spec_path if self.brand_spec_path and self.brand_spec_path.exists() else None)
            if path and load_brand_spec:
                try:
                    spec = load_brand_spec(path)
                    brand_text = spec.to_prompt_summary()
                except Exception as e:
                    logger.warning("Could not load brand spec from %s: %s", path, e)
        if brand_text:
            parts.append("--- BRAND SPEC (voice, formatting, content rules) ---\n" + brand_text.strip())

        # 2. Writing style guide (explicit string, or from default .md file)
        writing_text: Optional[str] = None
        if style_guide and style_guide.strip():
            writing_text = style_guide.strip()
        elif self.default_style_guide_path and self.default_style_guide_path.exists():
            try:
                writing_text = _load_style_guide(self.default_style_guide_path)
            except OSError as e:
                logger.warning("Could not load default writing style guide from %s: %s", self.default_style_guide_path, e)
        if writing_text:
            parts.append("--- WRITING STYLE GUIDE ---\n" + writing_text)

        if not parts:
            return MINIMAL_STYLE_REMINDER
        return "\n\n".join(parts)

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
        draft_output_path: Optional[Union[str, Path]] = None,
    ) -> DraftOutput:
        """
        Generate a blog post draft from the research document and/or references and outline.

        When research_references is non-empty, extracts notes/citations from each source in parallel,
        combines them, then drafts from the combined notes. Otherwise uses research_document (with truncation).

        When draft_output_path is set, writes the draft to that path and logs the path.
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
            "BRAND AND STYLE (mandatory for every sentence):",
            "---",
            BRAND_AND_STYLE_PRIMER.strip(),
            "",
            "---",
            "STYLE GUIDE (you must follow every applicable rule):",
            "---",
            style_guide_text,
            "",
            "---",
            MANDATORY_STYLE_CHECKLIST.strip(),
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
        prompt_parts.append(
            "Before outputting, ensure: no banned phrases; 8th grade reading level; descriptive headings; concrete opening hook; one practical next step in the conclusion."
        )
        prompt_parts.append("")
        prompt_parts.append('Use this format: first line {"draft": 0}, then ---DRAFT---, then the full blog post in Markdown.')
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

        prompt_parts = [
            REVISE_SINGLE_ITEM_PROMPT,
            "",
            "---",
            "BRAND AND STYLE (mandatory for every sentence):",
            "---",
            BRAND_AND_STYLE_PRIMER.strip(),
            "",
            "---",
            "STYLE GUIDE (follow in the revised draft):",
            "---",
            style_guide_text,
            "",
            "---",
            MANDATORY_STYLE_CHECKLIST.strip(),
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
        return "\n".join(prompt_parts)

    def _self_review(
        self,
        revised_draft: str,
        feedback_items: list,
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """
        Self-review a revised draft against the editor's feedback.

        Returns a dict with "all_addressed" (bool) and "unresolved_items" (list).
        """
        feedback_lines = []
        for i, item in enumerate(feedback_items, 1):
            loc = f" [{item.location}]" if item.location else ""
            feedback_lines.append(f"{i}. [{item.severity}] {item.category}{loc}: {item.issue}")
            if item.suggestion:
                feedback_lines.append(f"   Suggestion: {item.suggestion}")
            feedback_lines.append("")

        prompt_parts = [
            SELF_REVIEW_PROMPT,
            "",
            "---",
            "EDITOR FEEDBACK TO VERIFY:",
            "---",
            "\n".join(feedback_lines).strip(),
            "",
            "---",
            "REVISED DRAFT:",
            "---",
            revised_draft,
        ]
        prompt = "\n".join(prompt_parts)

        if on_llm_request:
            on_llm_request("Self-reviewing revised draft...")
        try:
            data = self.llm.complete_json(prompt, temperature=0.1)
            all_addressed = data.get("all_addressed", True)
            unresolved = data.get("unresolved_items", [])
            if not isinstance(unresolved, list):
                unresolved = []
            return {"all_addressed": bool(all_addressed), "unresolved_items": unresolved}
        except Exception as e:
            logger.warning("Self-review failed: %s; treating feedback as addressed.", e)
            return {"all_addressed": True, "unresolved_items": []}

    def revise(
        self,
        revise_input: ReviseDraftInput,
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
        draft_output_path: Optional[Union[str, Path]] = None,
    ) -> DraftOutput:
        """
        Revise a draft by addressing each copy editor feedback item individually.

        For each of the N feedback items, the agent revises the draft once (addressing
        only that item), then passes the updated draft to the next item. No self-review
        or compliance check is performed; the full revised draft is returned after all
        items have been addressed. The pipeline sends the result back to the editor.

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

        style_guide_text = self._resolve_style_guide(
            revise_input.style_guide,
            revise_input.brand_spec_path,
            revise_input.brand_spec,
        )

        current_draft = draft
        revise_max_tokens = 32768
        num_items = len(revise_input.feedback_items)

        for item_index, item in enumerate(revise_input.feedback_items, 1):
            logger.info("Revising for feedback item %s/%s", item_index, num_items)
            if on_llm_request:
                on_llm_request(f"Addressing feedback item {item_index}/{num_items}...")

            prompt = self._build_revise_single_item_prompt(
                current_draft, item, item_index, style_guide_text, revise_input
            )

            revised: Optional[str] = None
            for attempt in range(3):
                try:
                    raw_response = self.llm.complete(
                        prompt,
                        temperature=0.2,
                        max_tokens=revise_max_tokens,
                        system_prompt=REVISE_SINGLE_ITEM_PROMPT,
                    )
                    revised = _extract_draft_after_marker(raw_response)
                    if revised and revised.strip():
                        break
                except LLMTemporaryError as e:
                    if attempt < 2:
                        logger.warning(
                            "Revise for item %s: empty/transient LLM response (attempt %s/3); retrying.",
                            item_index,
                            attempt + 1,
                        )
                        time.sleep(2.0 + attempt)
                    else:
                        logger.warning(
                            "Revise for item %s: empty/transient after 3 attempts; keeping previous draft.",
                            item_index,
                        )
                        break
                except (LLMJsonParseError, LLMTruncatedError) as e:
                    if attempt == 0:
                        logger.warning("Revise for item %s failed: %s; retrying with complete_json.", item_index, e)
                    else:
                        logger.warning("Revise for item %s failed after retry; keeping previous draft.", item_index)
                        break
            if not revised or not revised.strip():
                try:
                    data = self.llm.complete_json(prompt, temperature=0.2, max_tokens=revise_max_tokens)
                    raw_draft = data.get("draft") if data else None
                    if isinstance(raw_draft, str) and raw_draft.strip():
                        revised = raw_draft.strip()
                except (LLMJsonParseError, LLMTruncatedError):
                    pass

            if revised and revised.strip():
                current_draft = revised.strip()
                logger.info("Draft revised for item %s/%s: length=%s", item_index, num_items, len(current_draft))
            else:
                logger.warning("Revise for item %s/%s produced no content; keeping previous draft.", item_index, num_items)

        logger.info("Final revised draft: length=%s (%s items addressed)", len(current_draft), num_items)
        if draft_output_path:
            _write_draft_to_path(current_draft, draft_output_path)
        return DraftOutput(draft=current_draft)
