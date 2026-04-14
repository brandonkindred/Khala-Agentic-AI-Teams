"""
Blog writer agent: takes a research document and an outline and generates
a blog post draft that complies with a brand and writing style guide.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

from blog_planning_agent.json_utils import parse_json_object
from blog_planning_agent.prompts import GENERATE_PLAN_SYSTEM, REFINE_PLAN_SYSTEM
from shared.content_plan import (
    ContentPlan,
    PlanningFailureReason,
    PlanningInput,
    PlanningPhaseResult,
    TitleCandidate,
    section_count_bounds_for_profile,
)
from shared.content_profile import LengthPolicy
from shared.errors import PlanningError
from strands import Agent

from llm_service import (
    compact_text,
)

from .models import (
    ReviseWriterInput,
    RevisionPlan,
    RevisionPlanChange,
    UncertaintyQuestion,
    WriterInput,
    WriterOutput,
    WritingGuidelineUpdate,
)
from .prompts import (
    ANALYZE_USER_FEEDBACK_FOR_GUIDELINES_PROMPT,
    DRAFT_TASK_INSTRUCTIONS,
    ESCALATION_SUMMARY_PROMPT,
    REVISION_TASK_INSTRUCTIONS,
    SELF_REVIEW_PROMPT,
    UNCERTAINTY_DETECTION_PROMPT,
    USER_FEEDBACK_REVISION_INSTRUCTIONS,
    WRITING_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deterministic compliance constants
# ---------------------------------------------------------------------------

BANNED_PHRASES = [
    "In today's fast-paced world",
    "In the ever-evolving landscape of",
    "In an era where",
    "Now more than ever",
    "As we navigate",
    "With the rise of",
    "As technology continues to evolve",
    "It's worth noting that",
    "It's important to understand that",
    "It bears mentioning",
    "It's no secret that",
    "Needless to say",
    "Of course,",
    "As mentioned above",
    "This is a game-changer",
    "This is incredibly important",
    "This is essential for success",
    "Harnessing the power of",
    "Furthermore,",
    "Moreover,",
    "Additionally,",
    "In conclusion,",
    "To summarize,",
]

VAGUE_CITATION_PATTERNS = [
    r"[Ss]tudies show",
    r"[Rr]esearch indicates",
    r"[Ee]xperts agree",
    r"[Ii]t'?s well[- ]known that",
    r"[Dd]ata suggests",
    r"[Mm]any organizations have found",
    r"[Tt]eams often discover",
    r"[Aa]ccording to industry best practices",
    r"[Ss]tatistics show",
    r"[Ii]t'?s widely recognized",
]

# Context budget for compaction — content exceeding these thresholds is compacted
# (LLM-summarised) rather than naively truncated, preserving technical detail.
# The model context (e.g. 262K tokens ≈ 917K chars) is large enough that
# compaction should rarely be needed.
COMPACT_OUTLINE_CHARS = 200_000


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


class BlogWriterAgent:
    """
    Expert agent that generates a blog post draft from a research document and outline,
    following a provided brand and writing style guide.
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
        assert llm_client is not None, "llm_client is required"
        self._model = llm_client
        self._writing_style_prompt = (writing_style_guide_content or "").strip()
        self._brand_spec_prompt = (brand_spec_content or "").strip()
        parts: list[str] = []
        if self._brand_spec_prompt:
            parts.append("--- BRAND SPEC ---\n" + self._brand_spec_prompt)
        if self._writing_style_prompt:
            parts.append("--- WRITING STYLE GUIDE ---\n" + self._writing_style_prompt)
        self._style_prompt = "\n\n".join(parts)

    def _call_agent(self, prompt: str, system_prompt: str = "") -> str:
        """Call a Strands Agent and return the raw text result."""
        agent = Agent(model=self._model, system_prompt=system_prompt or WRITING_SYSTEM_PROMPT)
        result = agent(prompt)
        return str(result).strip()

    def _call_agent_json(self, prompt: str, system_prompt: str = "") -> dict:
        """Call a Strands Agent and parse JSON from the result."""
        raw = self._call_agent(prompt + "\n\nRespond with valid JSON only, no markdown fences.", system_prompt)
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)

    def _assert_guidelines_present(self) -> None:
        """Require both brand and writing guideline inputs before drafting/revising."""
        missing: list[str] = []
        if not self._brand_spec_prompt:
            missing.append("brand guidelines")
        if not self._writing_style_prompt:
            missing.append("writing guidelines")
        if missing:
            raise ValueError(
                "BlogWriterAgent requires both brand and writing guidelines to ensure compliant output. "
                f"Missing: {', '.join(missing)}."
            )

    # ------------------------------------------------------------------
    # Embedded planning (merged from blog_planning_agent)
    # ------------------------------------------------------------------

    @staticmethod
    def _post_validate_plan(plan: ContentPlan, policy: LengthPolicy) -> ContentPlan:
        lo, hi = section_count_bounds_for_profile(policy.content_profile.value)
        n = len(plan.sections)
        ra = plan.requirements_analysis.model_copy(deep=True)
        if n < lo or n > hi:
            ra.plan_acceptable = False
            ra.gaps = [
                *list(ra.gaps),
                f"Section count {n} outside expected range [{lo},{hi}] for profile {policy.content_profile.value}.",
            ]
        return plan.model_copy(update={"requirements_analysis": ra})

    @staticmethod
    def _planning_done(plan: ContentPlan) -> bool:
        ra = plan.requirements_analysis
        return bool(ra.plan_acceptable and ra.scope_feasible)

    @staticmethod
    def _build_generate_plan_prompt(inp: PlanningInput) -> str:
        parts = [
            "Produce the JSON content plan for ONE blog post.",
            "[CONTENT_PLAN_JSON_V1]",
            "",
            "--- BRIEF ---",
            inp.brief.strip(),
            "",
            "--- LENGTH / PROFILE ---",
            inp.length_policy_context.strip(),
        ]
        if inp.audience:
            parts.extend(["", f"Audience: {inp.audience}"])
        if inp.tone_or_purpose:
            parts.append(f"Tone/Purpose: {inp.tone_or_purpose}")
        if inp.series_context_block and inp.series_context_block.strip():
            parts.extend(["", inp.series_context_block.strip()])
        parts.extend(
            [
                "",
                "--- RESEARCH DIGEST (ground the plan in this; flag gaps) ---",
                inp.research_digest.strip(),
            ]
        )
        return "\n".join(parts)

    def _build_refine_plan_prompt(
        self, inp: PlanningInput, previous: ContentPlan, feedback: str
    ) -> str:
        base = self._build_generate_plan_prompt(inp)
        prev_json = previous.model_dump(mode="json")
        return (
            base
            + "\n\n--- PREVIOUS PLAN (JSON) ---\n"
            + json.dumps(prev_json, indent=2)
            + "\n\n--- REFINEMENT FEEDBACK ---\n"
            + feedback
            + "\n\n--- TASK ---\nReturn an improved full JSON plan as specified."
        )

    def _complete_plan_json(
        self,
        prompt: str,
        *,
        system: str,
        on_llm_request: Optional[Callable[[str], None]],
        max_parse_retries: int,
    ) -> tuple[dict[str, Any], int]:
        parse_retries = 0
        last_err: Optional[Exception] = None
        for attempt in range(max_parse_retries):
            if on_llm_request:
                on_llm_request("Planning: generating structured plan...")
            try:
                data = self._call_agent_json(prompt, system_prompt=system)
                if isinstance(data, dict) and data:
                    return data, parse_retries
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                last_err = e
                parse_retries += 1
                logger.warning("JSON parse failed (attempt %s): %s", attempt + 1, e)
            try:
                raw = self._call_agent(
                    prompt + "\n\nRespond with a single JSON object only, no markdown fences.",
                    system_prompt=system)
                data = parse_json_object(raw)
                return data, parse_retries
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                last_err = e
                parse_retries += 1
                logger.warning("parse_json_object failed (attempt %s): %s", attempt + 1, e)
        msg = f"Planning JSON parse failed after {max_parse_retries} attempts"
        if last_err:
            msg += f": {last_err}"
        raise PlanningError(
            msg,
            failure_reason=PlanningFailureReason.PARSE_FAILURE.value,
            cause=last_err,
        )

    def plan_content(
        self,
        planning_input: PlanningInput,
        *,
        length_policy: LengthPolicy,
        on_llm_request: Optional[Callable[[str], None]] = None,
        max_iterations: int = 5,
        max_parse_retries: int = 3,
    ) -> PlanningPhaseResult:
        t0 = time.monotonic()
        total_parse_retries = 0
        last_plan: Optional[ContentPlan] = None
        for iteration in range(1, max_iterations + 1):
            if iteration == 1:
                prompt = self._build_generate_plan_prompt(planning_input)
                system = GENERATE_PLAN_SYSTEM
            else:
                assert last_plan is not None
                feedback = (
                    "The plan is not yet acceptable. "
                    f"requirements_analysis: plan_acceptable={last_plan.requirements_analysis.plan_acceptable}, "
                    f"scope_feasible={last_plan.requirements_analysis.scope_feasible}. "
                    "Fix gaps, scope, and research alignment."
                )
                prompt = self._build_refine_plan_prompt(planning_input, last_plan, feedback)
                system = REFINE_PLAN_SYSTEM
            data, pr = self._complete_plan_json(
                prompt,
                system=system,
                on_llm_request=on_llm_request,
                max_parse_retries=max_parse_retries,
            )
            total_parse_retries += pr
            try:
                plan = ContentPlan.model_validate(data)
            except Exception as e:
                raise PlanningError(
                    f"Invalid content plan schema: {e}",
                    failure_reason=PlanningFailureReason.PARSE_FAILURE.value,
                    cause=e,
                ) from e
            plan = self._post_validate_plan(plan, length_policy)
            if not plan.title_candidates:
                plan = plan.model_copy(
                    update={
                        "title_candidates": [
                            TitleCandidate(
                                title=plan.overarching_topic[:120],
                                probability_of_success=0.5,
                            )
                        ]
                    }
                )
            last_plan = plan.model_copy(update={"plan_version": iteration})
            if self._planning_done(last_plan):
                wall_ms = (time.monotonic() - t0) * 1000.0
                return PlanningPhaseResult(
                    content_plan=last_plan,
                    planning_iterations_used=iteration,
                    parse_retry_count=total_parse_retries,
                    planning_wall_ms_total=wall_ms,
                )
        raise PlanningError(
            f"Planning did not converge after {max_iterations} iterations",
            failure_reason=PlanningFailureReason.MAX_ITERATIONS_REACHED.value,
        )

    # ------------------------------------------------------------------
    # Self-check: deterministic + LLM review
    # ------------------------------------------------------------------

    def _deterministic_self_check(self, draft: str) -> list[str]:
        """Scan draft for mechanical violations. Returns list of violation descriptions."""
        violations: list[str] = []
        draft_lower = draft.lower()
        paragraphs = [p.strip() for p in draft.split("\n\n") if p.strip()]

        # 1. Em/en dashes
        for i, para in enumerate(paragraphs, 1):
            if "\u2014" in para or "\u2013" in para:
                violations.append(f"Em/en dash found in paragraph {i}")

        # 2. Banned phrases
        for phrase in BANNED_PHRASES:
            if phrase.lower() in draft_lower:
                violations.append(f"Banned phrase found: '{phrase}'")

        # 3. Vague citation patterns — only flag if NOT followed by a source/link within ~150 chars
        for pattern in VAGUE_CITATION_PATTERNS:
            for match in re.finditer(pattern, draft):
                after = draft[match.end() : match.end() + 150]
                # Skip if followed by an inline link, [CLAIM:] tag, or URL
                if (
                    re.search(r"\[CLAIM:", after)
                    or re.search(r"https?://", after)
                    or re.search(r"\]\(http", after)
                ):
                    continue
                violations.append(
                    f"Vague citation: '{match.group()}' — add an inline link or name a specific source"
                )

        # 4. Reader address count
        you_count = len(re.findall(r"\byou(?:r|rs|rself)?\b", draft_lower))
        if you_count < 3:
            violations.append(
                f"Reader address 'you/your' appears only {you_count} time(s) — need at least 3"
            )

        # 5. Staccato detection — 3+ consecutive sentences with ≤ 7 words
        for i, para in enumerate(paragraphs, 1):
            if para.startswith("#"):
                continue
            sentences = re.split(r"(?<=[.!?])\s+", para)
            streak = 0
            for sent in sentences:
                word_count = len(sent.split())
                if word_count <= 7:
                    streak += 1
                    if streak >= 3:
                        violations.append(
                            f"Staccato prose in paragraph {i}: {streak}+ consecutive short sentences"
                        )
                        break
                else:
                    streak = 0

        return violations

    def _fix_deterministic_violations(self, draft: str, violations: list[str]) -> str:
        """Call LLM once to fix deterministic violations. Returns cleaned draft."""
        checklist = "\n".join(f"- {v}" for v in violations)
        prompt = (
            "Fix ONLY these specific issues in the draft below. Do not change anything else.\n\n"
            f"ISSUES TO FIX:\n{checklist}\n\n"
            "---\nCURRENT DRAFT:\n---\n"
            f"{draft}\n\n"
            '---\nUse this format: first line {{"draft": 0}}, then ---DRAFT---, '
            "then the full fixed blog post in Markdown."
        )
        try:
            raw = self._call_agent(
                prompt,
                system_prompt=WRITING_SYSTEM_PROMPT)
            fixed = _extract_draft_after_marker(raw)
            if fixed and fixed.strip():
                logger.info("Deterministic self-check: fixed %s violations", len(violations))
                return fixed.strip()
        except Exception as e:
            logger.warning("Deterministic fix LLM call failed: %s", e)
        return draft

    def _llm_self_review(self, draft: str) -> str:
        """Run a focused LLM self-review for subjective violations. Returns cleaned draft."""
        try:
            raw = self._call_agent(
                f"Review this draft:\n\n{draft}",
                system_prompt=SELF_REVIEW_PROMPT)
            cleaned = raw.strip()
            # Extract JSON array
            start = cleaned.find("[")
            end = cleaned.rfind("]") + 1
            if start == -1 or end <= start:
                logger.info("LLM self-review: no issues found (no JSON array)")
                return draft
            issues = json.loads(cleaned[start:end])
            if not issues:
                logger.info("LLM self-review: draft passed all 5 checks")
                return draft

            logger.info("LLM self-review found %s issue(s); applying fixes", len(issues))
            issue_lines = []
            for i, iss in enumerate(issues, 1):
                loc = iss.get("location", "")
                desc = iss.get("issue", "")
                fix = iss.get("fix", "")
                issue_lines.append(f"{i}. [{loc}] {desc}\n   Fix: {fix}")

            fix_prompt = (
                "Fix ONLY these issues found during self-review. Do not change anything else.\n\n"
                "ISSUES:\n" + "\n\n".join(issue_lines) + "\n\n"
                "---\nCURRENT DRAFT:\n---\n" + draft + "\n\n"
                '---\nUse this format: first line {{"draft": 0}}, then ---DRAFT---, '
                "then the full fixed blog post in Markdown."
            )
            raw_fix = self._call_agent(
                fix_prompt,
                system_prompt=WRITING_SYSTEM_PROMPT)
            fixed = _extract_draft_after_marker(raw_fix)
            if fixed and fixed.strip():
                logger.info("LLM self-review: applied fixes, new length=%s", len(fixed.strip()))
                return fixed.strip()
        except Exception as e:
            logger.warning("LLM self-review failed: %s", e)
        return draft

    def _self_review(self, draft: str) -> str:
        """Run deterministic check then LLM self-review. Returns cleaned draft."""
        # Step 1: Deterministic checks
        violations = self._deterministic_self_check(draft)
        if violations:
            logger.info("Deterministic self-check found %s violation(s)", len(violations))
            draft = self._fix_deterministic_violations(draft, violations)

        # Step 2: LLM self-review for subjective issues
        draft = self._llm_self_review(draft)

        return draft

    def run(
        self,
        draft_input: WriterInput,
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
        draft_output_path: Optional[Union[str, Path]] = None,
    ) -> WriterOutput:
        """
        Generate a blog post draft from the approved content plan.

        When draft_output_path is set, writes the draft to that path and logs the path.
        """
        self._assert_guidelines_present()
        outline = draft_input.outline_for_prompt().strip()
        outline = compact_text(outline, COMPACT_OUTLINE_CHARS, self._model, "content plan")
        if not outline:
            logger.warning("Empty content plan; returning minimal draft.")
            return WriterOutput(draft="# Draft\n\nAdd a content plan to generate a draft.")

        style_guide_text = self._style_prompt

        logger.info(
            "Generating draft: outline len=%s, style_guide len=%s",
            len(outline),
            len(style_guide_text),
        )

        brand_section = (
            self._brand_spec_prompt
            if self._brand_spec_prompt
            else "No brand specification was provided. Follow the style guide below."
        )
        prompt_parts = [
            DRAFT_TASK_INSTRUCTIONS,
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
        prompt_parts.extend(
            [
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
            "Before outputting, ensure: no banned phrases; no em dashes or en dashes; 8th grade reading level; "
            "descriptive headings; first-person opening hook from author-provided stories (or placeholder if none "
            "provided, NEVER fabricate); at least one transparent-failure moment from author stories (or placeholder "
            "if none, NEVER fabricate); at least one specific number (dollar figure, percentage, or duration) if the "
            "topic supports it; trade-offs acknowledged; technical concepts introduced through the pain they solve "
            "(not as definitions); one practical next step in the conclusion. "
            "QUALITY CHECK: Does this sound like the author's voice per the brand spec, not an AI? Would a skeptical reader find the "
            "arguments convincing? Is it actionable and valuable to the target audience? Does it flow logically "
            "from intro to conclusion? "
            "FINAL CHECK: scan every 'I' or 'my' sentence, if it describes a specific event not from the "
            "AUTHOR'S PERSONAL STORIES section, replace it with a placeholder."
        )
        if (draft_input.length_guidance or "").strip():
            prompt_parts.append("")
            prompt_parts.append("---")
            prompt_parts.append(draft_input.length_guidance.strip())
        else:
            prompt_parts.append(
                f"TARGET LENGTH: Aim for roughly {draft_input.target_word_count} words "
                f"(acceptable range: {int(draft_input.target_word_count * 0.75)}–{int(draft_input.target_word_count * 1.3)} words). "
                "Hit the intent of the content profile first — do not pad to reach the number, "
                "and do not cut necessary substance to stay under it."
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
        try:
            raw_response = self._call_agent(
                prompt,
                system_prompt=WRITING_SYSTEM_PROMPT)
            draft = _extract_draft_after_marker(raw_response)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("Draft complete() failed: %s; trying complete_json fallback.", e)
            try:
                data = self._call_agent_json(prompt)
                raw_draft = data.get("draft")
                if isinstance(raw_draft, str) and raw_draft.strip():
                    draft = raw_draft.strip()
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        if not draft:
            logger.warning("LLM returned no draft content; returning placeholder.")
            draft = "# Draft\n\nNo draft was generated. Check the model response or try again."

        logger.info("Draft generated: length=%s", len(draft))
        if draft and not draft.startswith("# Draft\n\nNo draft"):
            if on_llm_request:
                on_llm_request("Running self-review...")
            draft = self._self_review(draft)
        if draft_output_path:
            _write_draft_to_path(draft, draft_output_path)
        return WriterOutput(draft=draft)

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
        revision_plan: str,
        style_guide_text: str,
        revise_input: ReviseWriterInput,
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
            revise_input.outline_for_prompt(), COMPACT_OUTLINE_CHARS, self._model, "content plan"
        )
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
                loc = f" [{pi.location}]" if pi.location else ""
                line = f"{i}. [{pi.severity}] {pi.category}{loc} (flagged {pi.occurrence_count} times): {pi.issue}"
                if pi.suggestion:
                    line += f'\n   REQUIRED FIX: "{pi.suggestion}"'
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
            for i, item in enumerate(revise_input.previous_feedback_items[:10], 1):
                loc = f" [{item.location}]" if item.location else ""
                prev_lines.append(f"{i}. [{item.severity}] {item.category}{loc}: {item.issue}")
            prompt_parts.extend(
                [
                    "---",
                    "RECENTLY RESOLVED FEEDBACK (do NOT regress on these):",
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

    def _build_revision_plan_prompt(
        self, draft: str, feedback_items: list[Any], revise_input: ReviseWriterInput
    ) -> str:
        feedback_lines = [
            self._format_feedback_item_line(item, i)
            for i, item in enumerate(feedback_items, start=1)
        ]
        cp = compact_text(
            revise_input.outline_for_prompt(), COMPACT_OUTLINE_CHARS, self._model, "content plan"
        )
        parts = [
            "Analyse ALL feedback items and create a structured revision plan for this draft.",
            "Return valid JSON matching this schema exactly:",
            '{',
            '  "summary": "One-paragraph overview of the revision strategy",',
            '  "changes": [',
            '    {',
            '      "section": "Which section or location this change targets",',
            '      "feedback_ids": [1, 2],',
            '      "action": "rewrite | delete | merge | add | rephrase | restructure",',
            '      "rationale": "Why this change is needed"',
            '    }',
            '  ],',
            '  "risks": ["Potential regressions or trade-offs"]',
            '}',
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

    def _generate_revision_plan(
        self,
        draft: str,
        feedback_items: list[Any],
        revise_input: ReviseWriterInput,
    ) -> RevisionPlan:
        prompt = self._build_revision_plan_prompt(draft, feedback_items, revise_input)
        try:
            data = self._call_agent_json(
                prompt,
                system_prompt=WRITING_SYSTEM_PROMPT)
            if not data or not isinstance(data, dict):
                return RevisionPlan(summary="Planning produced no output.", changes=[], risks=[])
            return RevisionPlan(
                summary=data.get("summary", ""),
                changes=[
                    RevisionPlanChange(**c) for c in (data.get("changes") or []) if isinstance(c, dict)
                ],
                risks=data.get("risks") or [],
            )
        except Exception as e:
            logger.warning("Structured revision planning failed: %s — falling back to unstructured", e)
            # Graceful degradation: try plain-text plan
            try:
                plain = self._call_agent(
                    prompt, system_prompt=WRITING_SYSTEM_PROMPT
                )
                return RevisionPlan(summary=(plain or "").strip(), changes=[], risks=[])
            except Exception:
                return RevisionPlan(summary="Revision planning failed.", changes=[], risks=[])

    def _build_revise_single_item_prompt(
        self,
        draft: str,
        item: Any,
        item_index: int,
        total_items: int,
        style_guide_text: str,
        revise_input: ReviseWriterInput,
    ) -> str:
        """Build a revision prompt for a single feedback item."""
        brand_section = (
            self._brand_spec_prompt
            if self._brand_spec_prompt
            else "No brand specification was provided. Follow the style guide below."
        )
        feedback_line = self._format_feedback_item_line(item, 1)
        cp = compact_text(
            revise_input.outline_for_prompt(), COMPACT_OUTLINE_CHARS, self._model, "content plan"
        )
        prompt_parts = [
            REVISION_TASK_INSTRUCTIONS,
            "",
            f"You are addressing feedback item {item_index}/{total_items}. "
            "Focus ONLY on this one issue. Do not change anything else in the draft.",
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
            "FEEDBACK TO ADDRESS (this is the ONLY change to make):",
            "---",
            feedback_line,
            "",
        ]
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
                ["", "---", "AUTHOR'S PERSONAL STORIES:\n" + revise_input.elicited_stories]
            )
        length_block = (
            revise_input.length_guidance.strip()
            if (revise_input.length_guidance or "").strip()
            else (
                f"TARGET LENGTH: Aim for roughly {revise_input.target_word_count} words "
                f"(acceptable range: {int(revise_input.target_word_count * 0.75)}–{int(revise_input.target_word_count * 1.3)} words)."
            )
        )
        prompt_parts.extend(
            [
                "",
                "---",
                "CURRENT DRAFT:",
                "---",
                draft,
                "",
                "---",
                length_block,
                "",
                "---",
                'Use this format: first line {"draft": 0}, then ---DRAFT---, '
                "then the full revised blog post in Markdown.",
            ]
        )
        return "\n".join(prompt_parts)

    def _revise_single_item(
        self,
        draft: str,
        item: Any,
        item_index: int,
        total_items: int,
        style_guide_text: str,
        revise_input: ReviseWriterInput,
    ) -> str:
        """Apply one feedback item to the draft. Returns revised draft or original on failure."""
        prompt = self._build_revise_single_item_prompt(
            draft, item, item_index, total_items, style_guide_text, revise_input
        )
        for attempt in range(2):
            try:
                raw_response = self._call_agent(
                    prompt,
                    system_prompt=WRITING_SYSTEM_PROMPT)
                revised = _extract_draft_after_marker(raw_response)
                if revised and revised.strip():
                    return revised.strip()
            except Exception:
                logger.warning(
                    "Revise item %s/%s: transient error (attempt %s/2); retrying.",
                    item_index,
                    total_items,
                    attempt + 1,
                )
                time.sleep(2.0 + attempt)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.warning("Revise item %s/%s: %s; retrying.", item_index, total_items, e)
        # Fallback
        try:
            data = self._call_agent_json(
                prompt
            )
            raw_draft = data.get("draft") if data else None
            if isinstance(raw_draft, str) and raw_draft.strip():
                return raw_draft.strip()
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        logger.warning(
            "Revise item %s/%s: could not produce revision; keeping draft as-is.",
            item_index,
            total_items,
        )
        return draft

    def revise(
        self,
        revise_input: ReviseWriterInput,
        *,
        on_llm_request: Optional[Callable[[str], None]] = None,
        draft_output_path: Optional[Union[str, Path]] = None,
        work_dir: Optional[Union[str, Path]] = None,
        iteration: Optional[int] = None,
    ) -> WriterOutput:
        """
        Revise a draft by analysing all feedback, creating a structured revision
        plan, then executing the plan in a single pass.

        Steps:
            1. **Analyse** — review all feedback items at once.
            2. **Plan** — produce a ``RevisionPlan`` (summary, ordered changes, risks).
               Persisted as ``revision_plan_{iteration}.json`` in *work_dir*.
            3. **Execute** — apply the plan to produce the revised draft.
               Persisted as *draft_output_path* (e.g. ``draft_v{iteration}.md``).
        """
        self._assert_guidelines_present()
        draft = revise_input.draft.strip()
        if not draft:
            logger.warning("Empty draft in revise; returning as-is.")
            return WriterOutput(draft=revise_input.draft)
        if not revise_input.feedback_items:
            logger.info("No feedback items; returning draft unchanged.")
            return WriterOutput(draft=draft)

        style_guide_text = self._style_prompt
        items = list(revise_input.feedback_items)
        num_items = len(items)
        logger.info("Revising draft: %s feedback items (plan-first batch revision)", num_items)

        # ── Step 1+2: Analyse feedback and create structured revision plan ──
        if on_llm_request:
            on_llm_request(f"Analysing {num_items} feedback items and creating revision plan...")
        revision_plan: RevisionPlan = self._generate_revision_plan(draft, items, revise_input)
        logger.info(
            "Revision plan: %s planned changes, %s risks identified",
            len(revision_plan.changes),
            len(revision_plan.risks),
        )

        # Persist the plan as a JSON artifact so it's visible to the user
        if work_dir is not None:
            plan_name = f"revision_plan_{iteration}.json" if iteration else "revision_plan.json"
            try:
                from shared.artifacts import write_artifact
                write_artifact(work_dir, plan_name, revision_plan.model_dump(mode="json"))
                logger.info("Persisted %s", plan_name)
            except Exception as e:
                logger.warning("Failed to persist revision plan: %s", e)

        # ── Step 3: Execute the plan ────────────────────────────────────────
        if on_llm_request:
            on_llm_request(f"Executing revision plan ({len(revision_plan.changes)} changes)...")
        # Serialise the structured plan for the LLM prompt
        plan_text = revision_plan.summary
        if revision_plan.changes:
            plan_text += "\n\nPLANNED CHANGES (execute in order):\n"
            for i, ch in enumerate(revision_plan.changes, 1):
                ids = ", ".join(str(fid) for fid in ch.feedback_ids)
                plan_text += f"\n{i}. [{ch.action.upper()}] {ch.section}"
                if ids:
                    plan_text += f"  (feedback #{ids})"
                plan_text += f"\n   {ch.rationale}"
        if revision_plan.risks:
            plan_text += "\n\nRISKS TO WATCH:\n" + "\n".join(f"- {r}" for r in revision_plan.risks)

        prompt = self._build_revise_all_items_prompt(
            draft,
            items,
            plan_text,
            style_guide_text,
            revise_input,
        )
        current_draft = draft
        for attempt in range(3):
            try:
                raw_response = self._call_agent(
                    prompt,
                    system_prompt=WRITING_SYSTEM_PROMPT)
                revised = _extract_draft_after_marker(raw_response)
                if revised and revised.strip():
                    current_draft = revised.strip()
                    break
            except Exception:
                logger.warning(
                    "Batch revise transient error (attempt %s/3); retrying.",
                    attempt + 1,
                )
                time.sleep(2.0 * (2**attempt))
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.warning("Batch revise failed (attempt %s/3): %s", attempt + 1, e)
        if current_draft == draft:
            try:
                data = self._call_agent_json(
                    prompt
                )
                raw_draft = data.get("draft") if data else None
                if isinstance(raw_draft, str) and raw_draft.strip():
                    current_draft = raw_draft.strip()
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        logger.info(
            "Revision complete: %s items addressed, final length=%s", num_items, len(current_draft)
        )
        if draft_output_path:
            _write_draft_to_path(current_draft, draft_output_path)
        return WriterOutput(draft=current_draft)

    # ------------------------------------------------------------------
    # Interactive draft review: user-as-editor methods
    # ------------------------------------------------------------------

    def identify_uncertainty_questions(
        self,
        draft: str,
        content_plan_text: str,
    ) -> list[UncertaintyQuestion]:
        """Scan a draft for areas of high uncertainty that need user input.

        Returns a list of UncertaintyQuestion objects. An empty list means
        the agent is confident in the draft and no user questions are needed.
        """
        prompt = UNCERTAINTY_DETECTION_PROMPT.format(
            content_plan=content_plan_text,
            draft=draft,
        )
        try:
            raw = self._call_agent(
                prompt,
                system_prompt="You are a careful writing assistant that identifies areas of genuine uncertainty.")
            cleaned = raw.strip()
            start = cleaned.find("[")
            end = cleaned.rfind("]") + 1
            if start == -1 or end <= start:
                return []
            items = json.loads(cleaned[start:end])
            if not items:
                return []
            questions = []
            for item in items:
                try:
                    questions.append(
                        UncertaintyQuestion(
                            question_id=item.get("question_id", f"q-{len(questions)}"),
                            question=item["question"],
                            context=item.get("context", ""),
                            section=item.get("section"),
                        )
                    )
                except (KeyError, TypeError) as e:
                    logger.warning("Skipping malformed uncertainty question: %s", e)
            logger.info("Identified %s uncertainty question(s) in draft", len(questions))
            return questions
        except Exception as e:
            logger.warning("Uncertainty detection failed: %s", e)
            return []

    def analyze_user_feedback_for_guideline_updates(
        self,
        user_feedback: str,
        current_guidelines: str,
    ) -> list[WritingGuidelineUpdate]:
        """Analyze user feedback and extract any writing guideline updates.

        When the user/editor gives feedback about tone, cadence, sound, writing
        patterns, content structure, etc., this method extracts those as
        concrete guideline updates that can be persisted to the writing style guide.

        Returns an empty list if the feedback has no guideline-relevant content.
        """
        prompt = ANALYZE_USER_FEEDBACK_FOR_GUIDELINES_PROMPT.format(
            user_feedback=user_feedback,
            current_guidelines=current_guidelines,
        )
        try:
            data = self._call_agent_json(
                prompt)
            if not isinstance(data, dict):
                return []
            if not data.get("has_guideline_updates"):
                logger.info("User feedback contains no guideline updates")
                return []
            updates = []
            for item in data.get("updates", []):
                try:
                    updates.append(
                        WritingGuidelineUpdate(
                            category=item["category"],
                            description=item["description"],
                            guideline_text=item["guideline_text"],
                        )
                    )
                except (KeyError, TypeError) as e:
                    logger.warning("Skipping malformed guideline update: %s", e)
            logger.info("Extracted %s writing guideline update(s) from user feedback", len(updates))
            return updates
        except Exception as e:
            logger.warning("Guideline update analysis failed: %s", e)
            return []

    def revise_from_user_feedback(
        self,
        draft: str,
        user_feedback: str,
        content_plan_text: str,
        *,
        audience: Optional[str] = None,
        tone_or_purpose: Optional[str] = None,
        selected_title: Optional[str] = None,
        elicited_stories: Optional[str] = None,
        target_word_count: int = 1000,
        length_guidance: str = "",
        uncertainty_answers: Optional[dict[str, str]] = None,
        on_llm_request: Optional[Callable[[str], None]] = None,
        draft_output_path: Optional[Union[str, Path]] = None,
    ) -> WriterOutput:
        """Revise a draft based on direct user/editor feedback.

        Unlike ``revise()`` which handles structured copy-editor feedback items,
        this method handles free-form user feedback from the interactive review
        cycle where the user acts as the editor.
        """
        self._assert_guidelines_present()
        if not draft.strip():
            return WriterOutput(draft=draft)

        style_guide_text = self._style_prompt
        brand_section = (
            self._brand_spec_prompt
            if self._brand_spec_prompt
            else "No brand specification was provided. Follow the style guide below."
        )

        prompt_parts = [
            USER_FEEDBACK_REVISION_INSTRUCTIONS.format(user_feedback=user_feedback),
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
            "CONTENT PLAN:",
            "---",
            content_plan_text,
            "",
        ]

        if uncertainty_answers:
            answer_lines = []
            for qid, answer in uncertainty_answers.items():
                answer_lines.append(f"- {qid}: {answer}")
            prompt_parts.extend(
                [
                    "---",
                    "ANSWERS TO PREVIOUSLY ASKED QUESTIONS (incorporate these into the revision):",
                    "---",
                    "\n".join(answer_lines),
                    "",
                ]
            )

        if selected_title:
            prompt_parts.extend(
                ["---", f"AUTHOR-CHOSEN TITLE (preserve this exact H1): {selected_title}", ""]
            )
        if elicited_stories:
            prompt_parts.extend(["---", "AUTHOR'S PERSONAL STORIES:\n" + elicited_stories, ""])
        if audience:
            prompt_parts.append(f"Audience: {audience}")
        if tone_or_purpose:
            prompt_parts.append(f"Tone/Purpose: {tone_or_purpose}")

        length_block = (
            length_guidance.strip()
            if length_guidance.strip()
            else (
                f"TARGET LENGTH: Aim for roughly {target_word_count} words "
                f"(acceptable range: {int(target_word_count * 0.75)}–{int(target_word_count * 1.3)} words)."
            )
        )
        prompt_parts.extend(
            [
                "",
                "---",
                "CURRENT DRAFT:",
                "---",
                draft,
                "",
                "---",
                length_block,
                "",
                "---",
                'Use this format: first line {"draft": 0}, then ---DRAFT---, then the full revised blog post in Markdown.',
            ]
        )
        prompt = "\n".join(prompt_parts)

        if on_llm_request:
            on_llm_request("Revising draft based on editor feedback...")

        current_draft = draft
        for attempt in range(3):
            try:
                raw_response = self._call_agent(
                    prompt,
                    system_prompt=WRITING_SYSTEM_PROMPT)
                revised = _extract_draft_after_marker(raw_response)
                if revised and revised.strip():
                    current_draft = revised.strip()
                    break
            except Exception:
                logger.warning(
                    "User-feedback revision transient error (attempt %s/3); retrying.", attempt + 1
                )
                time.sleep(2.0 * (2**attempt))
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.warning("User-feedback revision failed (attempt %s/3): %s", attempt + 1, e)

        if current_draft == draft:
            try:
                data = self._call_agent_json(prompt)
                raw_draft = data.get("draft") if data else None
                if isinstance(raw_draft, str) and raw_draft.strip():
                    current_draft = raw_draft.strip()
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        logger.info("User-feedback revision complete, final length=%s", len(current_draft))
        if draft_output_path:
            _write_draft_to_path(current_draft, draft_output_path)
        return WriterOutput(draft=current_draft)

    def generate_escalation_summary(
        self,
        revision_count: int,
        latest_feedback_items: list[Any],
        persistent_issues: list[Any],
    ) -> str:
        """Generate a human-readable summary when the copy-edit loop hits the escalation threshold.

        Called when the automated editor has gone through ``revision_count`` iterations
        without approving the draft, to produce a clear explanation for the user about
        what is stuck and what guidance is needed.
        """
        feedback_text = "\n".join(
            f"- [{getattr(item, 'severity', 'unknown')}] {getattr(item, 'category', '')}: {getattr(item, 'issue', '')}"
            for item in latest_feedback_items
        )
        persistent_text = (
            "\n".join(
                f"- [{getattr(item, 'severity', 'unknown')}] {getattr(item, 'category', '')} (flagged {getattr(item, 'occurrence_count', '?')} times): {getattr(item, 'issue', '')}"
                for item in persistent_issues
            )
            if persistent_issues
            else "None"
        )

        prompt = ESCALATION_SUMMARY_PROMPT.format(
            revision_count=revision_count,
            latest_feedback=feedback_text or "No specific feedback items.",
            persistent_issues=persistent_text,
        )
        try:
            summary = self._call_agent(
                prompt)
            return (summary or "").strip()
        except Exception as e:
            logger.warning("Escalation summary generation failed: %s", e)
            return (
                f"The draft has been through {revision_count} automated revision cycles "
                "without reaching approval. Please review the current draft and provide feedback."
            )
