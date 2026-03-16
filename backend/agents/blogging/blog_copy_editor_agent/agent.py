"""
Blog copy editor agent: expert that provides feedback on a draft blog post
based on a brand and writing style guide.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Optional, Union

from llm_service import LLMClient

from shared.errors import LLMJsonParseError

from .models import CopyEditorInput, CopyEditorOutput, FeedbackItem
from .prompts import COPY_EDITOR_PROMPT, MINIMAL_STYLE_CHECKLIST

try:
    from shared.brand_spec import BrandSpec, load_brand_spec
except ImportError:
    BrandSpec = None
    load_brand_spec = None

logger = logging.getLogger(__name__)

# Default style guide path (Brandon Kindred brand and writing guide) relative to project root
_DEFAULT_STYLE_GUIDE_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "brandon_kindred_brand_and_writing_style_guide.md"
)


def _load_style_guide(path: str | Path) -> str:
    """Load style guide text from a file. Raises OSError if file cannot be read."""
    return Path(path).read_text(encoding="utf-8").strip()


class BlogCopyEditorAgent:
    """
    Expert agent that provides copy editing feedback on a blog draft,
    evaluating it against a brand and writing style guide.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        default_style_guide_path: Optional[str | Path] = None,
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

    def _resolve_style_guide(
        self,
        style_guide: Optional[str],
        brand_spec: Optional[dict],
    ) -> str:
        """Resolve style guide text from brand_spec dict or style_guide string, else default."""
        if brand_spec and load_brand_spec:
            try:
                spec = BrandSpec.model_validate(brand_spec) if hasattr(BrandSpec, "model_validate") else BrandSpec.parse_obj(brand_spec)
                return spec.to_prompt_summary()
            except Exception:
                pass
        if style_guide:
            return style_guide.strip()
        if self.default_style_guide_path and self.default_style_guide_path.exists():
            try:
                return _load_style_guide(self.default_style_guide_path)
            except OSError as e:
                logger.warning("Could not load default style guide: %s", e)
        return MINIMAL_STYLE_CHECKLIST

    def _write_feedback_to_path(self, output: CopyEditorOutput, path: Union[str, Path]) -> None:
        """Serialize CopyEditorOutput to JSON and write to path. On failure log warning and do not raise."""
        try:
            p = Path(path).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            data = output.model_dump() if hasattr(output, "model_dump") else output.dict()
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to write editor feedback to %s: %s", path, e)

    def run(
        self,
        copy_editor_input: CopyEditorInput,
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
        feedback_output_path: Optional[Union[str, Path]] = None,
    ) -> CopyEditorOutput:
        """
        Provide copy editing feedback on the draft based on the style guide.

        Preconditions:
            - copy_editor_input is a valid CopyEditorInput (draft non-empty).
        Postconditions:
            - Returns CopyEditorOutput with summary and feedback_items.
            - If feedback_output_path is set, writes the same output to that path before returning.
        """
        draft = copy_editor_input.draft.strip()
        if not draft:
            logger.warning("Empty draft; returning minimal feedback.")
            output = CopyEditorOutput(
                summary="No draft provided. Please supply a blog post draft to review.",
                feedback_items=[],
            )
            if feedback_output_path:
                self._write_feedback_to_path(output, feedback_output_path)
            return output

        # Resolve style guide text
        style_guide_text = self._resolve_style_guide(
            copy_editor_input.style_guide,
            copy_editor_input.brand_spec,
        )

        logger.info(
            "Copy editing: draft len=%s, style_guide len=%s",
            len(draft),
            len(style_guide_text),
        )

        context_parts = []
        if copy_editor_input.audience:
            context_parts.append(f"Audience: {copy_editor_input.audience}")
        if copy_editor_input.tone_or_purpose:
            context_parts.append(f"Tone/Purpose: {copy_editor_input.tone_or_purpose}")
        if copy_editor_input.human_feedback:
            context_parts.append("")
            context_parts.append("**AUTHOR'S REQUESTED CHANGES (must address these):**")
            context_parts.append(copy_editor_input.human_feedback.strip())
        if copy_editor_input.previous_feedback_items:
            context_parts.append("")
            context_parts.append("---")
            context_parts.append("PREVIOUS PASS FEEDBACK (already sent to writer — do not re-raise resolved issues):")
            context_parts.append("---")
            for i, item in enumerate(copy_editor_input.previous_feedback_items, 1):
                loc = f" [{item.location}]" if item.location else ""
                context_parts.append(f"{i}. [{item.severity}] {item.category}{loc}: {item.issue}")
        if context_parts:
            context_parts.append("")

        context_parts.extend([
            "---",
            "STYLE GUIDE (evaluate the draft against these rules):",
            "---",
            style_guide_text,
            "",
            "---",
            "DRAFT TO REVIEW:",
            "---",
            draft,
        ])

        prompt = COPY_EDITOR_PROMPT + "\n\n" + "\n".join(context_parts)

        if on_llm_request:
            on_llm_request("Reviewing draft for style and clarity...")
        data = None
        for attempt in range(2):
            try:
                data = self.llm.complete_json(prompt, temperature=0.2)
                break
            except LLMJsonParseError as e:
                if attempt == 0:
                    logger.warning(
                        "Copy editor JSON parse failed (attempt 1), retrying with strict instruction: %s",
                        e,
                    )
                    prompt = prompt + "\n\nRespond with a single JSON object only (no markdown, no code fence). Keys: approved (boolean), summary (string), feedback_items (array of objects with category, severity, location?, issue, suggestion?)."
                else:
                    logger.warning(
                        "Copy editor JSON parse failed after retry; using fallback output: %s",
                        e,
                    )
                    data = {
                        "summary": "Copy editor could not parse the model response. Please review the draft manually.",
                        "feedback_items": [],
                    }
                    break

        if not data:
            data = {
                "summary": "Copy editor could not parse the model response. Please review the draft manually.",
                "feedback_items": [],
            }

        summary = (data.get("summary") or "").strip() or "No summary generated."
        feedback_data = data.get("feedback_items") or []

        feedback_items: list[FeedbackItem] = []
        for item in feedback_data:
            if not isinstance(item, dict):
                continue
            category = (item.get("category") or "style").strip()
            severity = (item.get("severity") or "consider").strip()
            location = (item.get("location") or "").strip() or None
            issue = (item.get("issue") or "").strip()
            suggestion = (item.get("suggestion") or "").strip() or None
            if issue:
                feedback_items.append(
                    FeedbackItem(
                        category=category,
                        severity=severity,
                        location=location,
                        issue=issue,
                        suggestion=suggestion,
                    )
                )

        # Derive approved: true when the LLM says so and there are no blocking items.
        # Fall back to checking severity counts when the model omits the field.
        has_blocking = any(f.severity in ("must_fix", "should_fix") for f in feedback_items)
        llm_approved = bool(data.get("approved", False))
        approved = llm_approved and not has_blocking

        logger.info(
            "Copy edit complete: approved=%s, summary len=%s, %s feedback items",
            approved,
            len(summary),
            len(feedback_items),
        )
        for i, item in enumerate(feedback_items, 1):
            loc = f" [{item.location}]" if item.location else ""
            sugg = f" Suggestion: {item.suggestion}" if item.suggestion else ""
            logger.info(
                "Feedback item %s: [%s] %s%s — %s%s",
                i,
                item.severity,
                item.category,
                loc,
                item.issue,
                sugg,
            )
        output = CopyEditorOutput(approved=approved, summary=summary, feedback_items=feedback_items)
        if feedback_output_path:
            self._write_feedback_to_path(output, feedback_output_path)
        return output
