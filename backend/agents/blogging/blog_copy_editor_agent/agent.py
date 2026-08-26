"""
Blog copy editor agent: expert that provides feedback on a draft blog post
based on a brand and writing style guide.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from agents.blogging.shared.agent_base import _BlogAgentBase
from agents.blogging.shared.json_retry import run_json_gate
from agents.blogging.shared.word_count import count_words

from .models import CopyEditorInput, CopyEditorOutput, FeedbackItem
from .prompts import COPY_EDITOR_PROMPT

logger = logging.getLogger(__name__)

# For technical deep dives, a draft below this fraction of soft_min_words is flagged as thin.
_THIN_DRAFT_RATIO = 0.88

# Soft JSON instruction baked into the base prompt for call_json_with_retry (attempt 0).
COPY_EDITOR_SOFT_JSON_INSTRUCTION = "\n\nRespond with valid JSON only, no markdown fences."


def _fallback_editor_data(summary: str) -> Dict[str, Any]:
    """Editor output for a copy-edit tooling failure (unparseable JSON or unexpected error).

    Preconditions:
        - ``summary`` explains the failure so a human reviewer sees why the automated
          pass did not run.
    Postconditions:
        - Returns ``{"approved": True, "summary": summary, "feedback_items": []}``.
          ``approved=True`` is deliberate: the copy editor is an *advisory* style/clarity
          pass, so a tooling failure must not drive a pointless no-op rewrite — an
          unapproved draft carrying zero actionable feedback would loop the editor for
          no reason. The deterministic length gate downstream still sets ``has_blocking``
          and can withhold approval for an over-length draft, and the hard quality gates
          (fact-check, compliance, validators) run separately and are unaffected.
    """
    return {"approved": True, "summary": summary, "feedback_items": []}


class BlogCopyEditorAgent(_BlogAgentBase):
    """
    Expert agent that provides copy editing feedback on a blog draft,
    evaluating it against a brand and writing style guide.
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        writing_style_guide_content: str = "",
        brand_spec_content: str = "",
    ) -> None:
        """
        Preconditions:
            - llm_client is not None.
        Callers load writing style and brand spec files before instantiation and pass full contents here.
        """
        super().__init__(llm_client)
        writing = (writing_style_guide_content or "").strip()
        brand = (brand_spec_content or "").strip()
        parts: list[str] = []
        if brand:
            parts.append("--- BRAND SPEC ---\n" + brand)
        if writing:
            parts.append("--- WRITING STYLE GUIDE ---\n" + writing)
        self._style_prompt = "\n\n".join(parts)

    def _write_feedback_to_path(self, output: CopyEditorOutput, path: Union[str, Path]) -> bool:
        """
        Serialize CopyEditorOutput to JSON and write it to ``path``.

        This is a best-effort side write: the returned CopyEditorOutput is always
        authoritative, and a failed diagnostic write must never abort the copy-edit
        run. Filesystem/serialization errors (permission denied, disk-full, invalid
        path) are therefore caught, logged at WARNING, and reported via the return
        value rather than raised.

        Preconditions:
            - output is a CopyEditorOutput.
            - path is a non-empty filesystem path.
        Postconditions:
            - Returns True iff the JSON was successfully written to the resolved
              form of ``path`` (``Path(path).resolve()``).
            - Returns False on any filesystem/serialization error; in that case a
              WARNING is logged and no exception propagates.
        """
        try:
            p = Path(path).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            data = output.to_dict()
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            logger.warning("Failed to write editor feedback to %s: %s", path, e)
            return False

    def _build_editor_prompt(
        self,
        copy_editor_input: CopyEditorInput,
        draft: str,
        style_guide_text: str,
    ) -> str:
        """
        Assemble the per-request editor context from length intent, author/context
        signals, the style guide, the content plan, and the draft itself.

        The base instructions (``COPY_EDITOR_PROMPT``) are delivered once via the
        Agent's ``system_prompt`` in :meth:`_invoke_editor_llm`, so they are
        intentionally not repeated here.

        Preconditions:
            - draft is the stripped, non-empty draft text to review.
        Postconditions:
            - Returns the assembled context (draft last), as one string.
            - Has no side effects on self or the inputs.
            - Word count is computed via :func:`count_words`, a naive
              whitespace-token heuristic (not a linguistic word count).
        """
        actual_word_count = count_words(draft)
        target_word_count = copy_editor_input.target_word_count
        soft_min = copy_editor_input.soft_min_words
        soft_max = copy_editor_input.soft_max_words

        context_parts: list[str] = []
        band = f"{soft_min}–{soft_max}" if soft_min is not None and soft_max is not None else None
        if band:
            context_parts.append(
                f"Length intent: target ~{target_word_count} words, soft band ~{band} words "
                f"(draft is currently {actual_word_count} words)."
            )
        else:
            context_parts.append(
                f"Target word count: {target_word_count} words (draft is currently {actual_word_count} words)."
            )
        if (copy_editor_input.length_guidance or "").strip():
            context_parts.append("")
            context_parts.append(
                "CONTENT PROFILE / LENGTH GUIDANCE (use when judging depth vs. length):"
            )
            context_parts.append(copy_editor_input.length_guidance.strip())
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
            context_parts.append(
                "PREVIOUS PASS FEEDBACK (already sent to writer — do not re-raise resolved issues):"
            )
            context_parts.append("---")
            for i, item in enumerate(copy_editor_input.previous_feedback_items, 1):
                loc = f" [{item.location}]" if item.location else ""
                context_parts.append(f"{i}. [{item.severity}] {item.category}{loc}: {item.issue}")
        if context_parts:
            context_parts.append("")

        if style_guide_text:
            context_parts.extend(
                [
                    "---",
                    "EVALUATION INSTRUCTION:",
                    "---",
                    "Evaluate the draft against the style guide below. Apply every rule in that guide.",
                    "",
                    "---",
                    "STYLE GUIDE (evaluate the draft against these rules):",
                    "---",
                    style_guide_text,
                    "",
                ]
            )
        else:
            context_parts.extend(
                [
                    "---",
                    "EVALUATION INSTRUCTION:",
                    "---",
                    "No style guidelines were provided. There is nothing to evaluate against; approve the draft or provide only optional high-level feedback if you wish.",
                    "",
                ]
            )

        if (
            copy_editor_input.content_plan_context
            and copy_editor_input.content_plan_context.strip()
        ):
            context_parts.extend(
                [
                    "---",
                    "CONTENT PLAN (align feedback with this structure and section intent):",
                    "---",
                    copy_editor_input.content_plan_context.strip(),
                    "",
                ]
            )

        context_parts.extend(
            [
                "---",
                "DRAFT TO REVIEW:",
                "---",
                draft,
            ]
        )

        return "\n".join(context_parts)

    def _invoke_editor_llm(
        self,
        prompt: str,
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """
        Invoke the editor LLM via the shared JSON-gate helper (up to two attempts).

        This method wires the model, system prompt, soft/strict JSON prompt suffixes,
        and fallback factories into ``run_json_gate``. Agent construction, the
        ``EventLoopException`` unwrap, attempt-count, soft-then-strict re-prompt, and
        transient-vs-unexpected classification are owned by that helper's contract;
        this method does not re-implement them.

        Args:
            prompt: Fully assembled per-request editor context (style/brand/draft).
            on_llm_request: Optional progress callback invoked once before the helper
                runs (status text: "Reviewing draft for style and clarity...").

        Preconditions:
            - ``prompt`` is the fully assembled per-request editor context.
        Postconditions:
            - Returns a non-None ``dict`` on successful parse or on a fallback path
              (JSON-parse exhaustion / unexpected error via ``on_exhausted`` /
              ``on_unexpected_error``, or a parseable but empty ``{}`` normalized to
              ``_fallback_editor_data``). A non-empty successful parse may omit keys;
              callers in :meth:`run` supply defaults for missing ``summary`` /
              ``feedback_items``. Fallback dicts include ``approved``, ``summary``,
              and ``feedback_items``.
            - Does **not** always return: transient LLM errors
              (``LLMRateLimitError`` / ``LLMTemporaryError``, including when strands
              wraps them in ``EventLoopException``) are re-raised by ``run_json_gate``
              after its unwrap hook classifies the cause — they are never swallowed
              into a return value.
        """
        if on_llm_request:
            on_llm_request("Reviewing draft for style and clarity...")
        strict_json_suffix = (
            "\n\nRespond with a single JSON object only (no markdown, no code fence). "
            "Keys: approved (boolean), summary (string), feedback_items (array of objects with "
            "category, severity, location?, issue, suggestion?)."
        )

        data = run_json_gate(
            self._model,
            COPY_EDITOR_PROMPT,
            prompt + COPY_EDITOR_SOFT_JSON_INSTRUCTION,
            max_attempts=2,
            strict_json_suffix=strict_json_suffix,
            on_exhausted=lambda e: _fallback_editor_data(
                "Copy editor could not parse the model response. Please review the draft manually."
            ),
            on_unexpected_error=lambda e: _fallback_editor_data(
                "Copy editor could not complete review. Please review the draft manually."
            ),
            logger=logger,
        )
        # A parseable but empty dict ({}) is treated like tooling failure: the advisory
        # fallback approves with no feedback so callers do not loop on approved=False
        # with zero actionable items.
        if not data:
            return _fallback_editor_data(
                "Copy editor could not parse the model response. Please review the draft manually."
            )
        return data

    def _parse_feedback_items(self, feedback_data: Any) -> list[FeedbackItem]:
        """
        Convert raw model feedback entries into validated FeedbackItem objects.

        Preconditions:
            - feedback_data is iterable; non-dict entries and entries with an empty
              issue are skipped.
        Postconditions:
            - Returns a list with one FeedbackItem per entry that has a non-empty issue,
              in input order.
        """
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
        return feedback_items

    def _inject_length_feedback(
        self,
        feedback_items: list[FeedbackItem],
        copy_editor_input: CopyEditorInput,
        actual_word_count: int,
    ) -> list[FeedbackItem]:
        """
        Add programmatic length feedback based on the draft's word-count bands.

        When soft_max is set, anything at or below that ceiling is acceptable — do not
        flag for being merely above the nominal target (e.g. 1134 words vs ~1000 target
        is fine when soft_max is 1300). Above soft_max, use profile-tunable ratios vs
        target for must_fix / should_fix. Thin technical deep dives get a 'consider' hint.

        Preconditions:
            - ``actual_word_count`` equals the word count of the reviewed draft
              (computed by the caller via :func:`count_words`, a naive
              whitespace-token heuristic).
            - ``target_word_count > 0`` to inject any length items; non-positive
              targets are treated as "no length target" and leave
              ``feedback_items`` unchanged.
        Postconditions:
            - If target_word_count <= 0, returns the same list object unchanged.
            - Otherwise returns the same list object passed in, mutated with 0..2
              length items: an over-length item (must_fix inserted at the front,
              else should_fix appended) when the draft is past the soft ceiling,
              and/or a 'consider' under-length hint appended for thin technical
              deep dives.
        Invariants:
            - Item ordering matches the pre-refactor behavior: must_fix is prepended;
              should_fix and the deep-dive hint are appended.
        """
        target_word_count = copy_editor_input.target_word_count
        if target_word_count <= 0:
            return feedback_items

        soft_min = copy_editor_input.soft_min_words
        soft_max = copy_editor_input.soft_max_words
        must_ratio = copy_editor_input.editor_must_fix_over_ratio
        should_ratio = copy_editor_input.editor_should_fix_over_ratio

        over_ratio = actual_word_count / target_word_count
        cap_label = soft_max if soft_max is not None else target_word_count
        past_soft_ceiling = soft_max is None or actual_word_count > soft_max

        if past_soft_ceiling and over_ratio > must_ratio:
            severity = "must_fix"
            issue = (
                f"Draft is {actual_word_count} words — well over the intended length (~{target_word_count} words"
                + (f", soft ceiling ~{soft_max}" if soft_max is not None else "")
                + f") at {over_ratio:.0%} of target. Trim to fit the content profile."
            )
            suggestion = (
                f"Cut or condense the least essential sections to land near ~{target_word_count} words"
                + (f" (stay under ~{soft_max} if possible)" if soft_max is not None else "")
                + ". Remove redundant examples, repeated points, and padded transitions."
            )
            feedback_items.insert(
                0,
                FeedbackItem(
                    category="structure",
                    severity=severity,
                    location="entire draft",
                    issue=issue,
                    suggestion=suggestion,
                ),
            )
            logger.info(
                "Length check: draft=%d words, target=%d words, over_ratio=%.2f — injecting %s feedback",
                actual_word_count,
                target_word_count,
                over_ratio,
                severity,
            )
        elif past_soft_ceiling and over_ratio > should_ratio:
            feedback_items.append(
                FeedbackItem(
                    category="structure",
                    severity="should_fix",
                    location="entire draft",
                    issue=(
                        f"Draft is {actual_word_count} words, somewhat over the ~{target_word_count}-word target "
                        f"({over_ratio:.0%} of target). Consider tightening for readability."
                    ),
                    suggestion=(
                        f"Look for redundant examples or long transitions; aim for approximately {target_word_count} words"
                        + (f" (soft ceiling ~{cap_label})" if soft_max is not None else "")
                        + "."
                    ),
                )
            )
            logger.info(
                "Length check: draft=%d words, target=%d words, over_ratio=%.2f — injecting should_fix feedback",
                actual_word_count,
                target_word_count,
                over_ratio,
            )

        if (
            copy_editor_input.content_profile == "technical_deep_dive"
            and soft_min is not None
            and actual_word_count < int(soft_min * _THIN_DRAFT_RATIO)
        ):
            feedback_items.append(
                FeedbackItem(
                    category="structure",
                    severity="consider",
                    location="entire draft",
                    issue=(
                        f"Draft is {actual_word_count} words — for a technical deep dive, it may be thin relative "
                        f"to the ~{soft_min}–{target_word_count}+ word intent. Check whether key mechanisms, "
                        "trade-offs, or examples are under-explained."
                    ),
                    suggestion=(
                        "Add substantive detail where it helps the reader (steps, edge cases, rationale) without padding."
                    ),
                )
            )

        return feedback_items

    def run(
        self,
        copy_editor_input: CopyEditorInput,
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
        feedback_output_path: Optional[Union[str, Path]] = None,
    ) -> CopyEditorOutput:
        """
        Provide copy editing feedback on the draft based on the style guide.

        Orchestrates the pass: build the prompt, invoke the LLM, parse the response,
        inject programmatic length feedback, derive approval, and return the output.

        Preconditions:
            - copy_editor_input is a CopyEditorInput instance.
            - The draft may be empty; if so, a minimal output is returned without calling the LLM.
        Postconditions:
            - Returns CopyEditorOutput with summary and feedback_items.
            - If the draft is empty (or whitespace-only), returns a minimal output with an
              explanatory summary and no feedback items, without invoking the LLM.
            - If feedback_output_path is set, best-effort writes the same output to
              that path before returning. This write may silently fail (e.g. permission
              denied or disk-full); such failures are logged at WARNING and never raised,
              so the returned output is authoritative and callers must not assume the
              file exists. The outcome is reported to the caller via
              `output.feedback_file_written` (True/False), which stays None when
              feedback_output_path is not given.
        """
        draft = copy_editor_input.draft.strip()
        if not draft:
            logger.warning("Empty draft; returning minimal feedback.")
            output = CopyEditorOutput(
                summary="No draft provided. Please supply a blog post draft to review.",
                feedback_items=[],
            )
            if feedback_output_path:
                output.feedback_file_written = self._write_feedback_to_path(
                    output, feedback_output_path
                )
            return output

        style_guide_text = self._style_prompt

        logger.info(
            "Copy editing: draft len=%s, style_guide len=%s",
            len(draft),
            len(style_guide_text),
        )

        actual_word_count = count_words(draft)

        prompt = self._build_editor_prompt(copy_editor_input, draft, style_guide_text)
        data = self._invoke_editor_llm(prompt, on_llm_request=on_llm_request)

        raw_summary = data.get("summary")
        summary = (
            raw_summary.strip() if isinstance(raw_summary, str) else ""
        ) or "No summary generated."
        raw_feedback_items = data.get("feedback_items")
        feedback_items = self._parse_feedback_items(
            raw_feedback_items if isinstance(raw_feedback_items, list) else []
        )
        feedback_items = self._inject_length_feedback(
            feedback_items, copy_editor_input, actual_word_count
        )

        # Derive approved: true when the LLM says so and there are no blocking items.
        # Fall back to checking severity counts when the model omits the field.
        has_blocking = any(f.severity in ("must_fix", "should_fix") for f in feedback_items)
        if "approved" in data:
            raw_approved = data["approved"]
            llm_approved = raw_approved if isinstance(raw_approved, bool) else False
        else:
            llm_approved = not has_blocking
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
            output.feedback_file_written = self._write_feedback_to_path(
                output, feedback_output_path
            )
        return output
