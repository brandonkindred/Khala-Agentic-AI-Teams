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

from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
from agents.blogging.blog_plan_critic_agent import BlogPlanCriticAgent
from agents.blogging.blog_planning_agent.prompts import GENERATE_PLAN_SYSTEM, REFINE_PLAN_SYSTEM
from agents.blogging.shared.agent_base import _BlogAgentBase
from agents.blogging.shared.content_plan import PlanningInput, PlanningPhaseResult
from agents.blogging.shared.content_planning_loop import (
    complete_plan_json,
    run_content_planning_loop,
)
from agents.blogging.shared.content_profile import LengthPolicy
from agents.blogging.shared.json_retry import call_json_with_retry
from strands import Agent
from strands.types.exceptions import EventLoopException

from llm_service import (
    LLMError,
    LLMJsonParseError,
    LLMRateLimitError,
    LLMTemporaryError,
    compact_text,
    extract_json_from_response,
)

from .feedback_tracker import MAX_PREVIOUS_FEEDBACK_ITEMS, PersistentFeedbackItem
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

BATCH_EXECUTE_MAX_RETRIES = 3
BATCH_EXECUTE_BACKOFF_BASE_SECONDS = 2.0

_PLACEHOLDER_DRAFT = "# Draft\n\nNo draft was generated. Check the model response or try again."

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


def _extract_draft_after_marker(raw_response: Optional[str]) -> str:
    """
    Extract draft content from model output that uses the hybrid format:
    first line {\"draft\": 0}, then ---DRAFT---, then the full blog post in Markdown.
    Falls back to scanning the response for extractable JSON (whole-response,
    fenced, or prose-wrapped, via ``extract_json_from_response``) and returning
    the value of its \"draft\" key.
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
        data = extract_json_from_response(text)
        if isinstance(data, dict):
            d = data.get("draft")
            if isinstance(d, str) and d.strip():
                return d.strip()
    except LLMJsonParseError:
        pass
    return ""


def _extract_json_array_from_text(
    text: str, *, required_keys: tuple[str, ...] = ()
) -> Optional[list]:
    """Parse a JSON array of objects from ``text``, including when prefixed by prose.

    Preconditions:
        - ``text`` is a string (may be empty).
        - ``required_keys``, if given, are the keys used to recognize the real
          payload (e.g. ``("issue",)`` for self-review issues, ``("question",)``
          for uncertainty questions): at least one element of a candidate array
          must contain all of them. This rejects an unrelated dict array (e.g. a
          ``references`` list salvaged from surrounding prose) that would
          otherwise pass a bare "is it a list of dicts" check, while still
          tolerating a real payload where some items are individually malformed
          (the caller's own per-item validation skips those).
    Postconditions:
        - Returns the dict elements of the first decoded JSON array containing at
          least one dict with every key in ``required_keys``, found by scanning
          for ``[`` and using ``json.JSONDecoder.raw_decode``. Non-dict elements
          in that array (e.g. a stray string) are dropped rather than rejecting
          the whole array — callers already tolerate individually malformed dict
          items via their own per-item validation.
        - A syntactically valid but schema-mismatched non-empty array (e.g. a
          numeric citation like ``[1]``, or a dict array none of whose elements
          have ``required_keys``) does not short-circuit the scan; scanning
          continues past it toward the real payload.
        - If no matching array of dicts is found, returns the first syntactically
          valid empty ``[]`` encountered — this cannot be distinguished from a
          literally empty Markdown link ``[]()`` (an empty pair of brackets is
          valid JSON), so a response containing only such a link and no real
          array-of-dicts payload also returns ``[]`` here. A Markdown link with
          non-empty text, e.g. ``[label](url)``, is not valid JSON at that
          ``[`` and is simply skipped like any other non-match. Returns
          ``None`` if no array matched at all.

    Limitation: the scan looks for a literal ``[`` anywhere in ``text``,
    including inside a JSON string value (e.g. an object field whose value is
    the literal text ``"[{...}]"``), so it can extract an array nested inside
    a string rather than only a true top-level/prose array. This has not been
    observed in practice for the reviewer/uncertainty response shapes this is
    used for, but is a known edge case if a future prompt's schema puts
    JSON-looking text inside a string field.
    """
    decoder = json.JSONDecoder()
    search_from = 0
    empty_fallback = None
    while True:
        i = text.find("[", search_from)
        if i == -1:
            break
        try:
            value, _end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            search_from = i + 1
            continue
        if isinstance(value, list):
            dict_elements = [el for el in value if isinstance(el, dict)]
            if dict_elements and any(all(k in el for k in required_keys) for el in dict_elements):
                return dict_elements
            if not value and empty_fallback is None:
                empty_fallback = value
        search_from = i + 1
    return empty_fallback


def _looks_like_top_level_json_object(text: str) -> bool:
    """Return True when ``text``'s JSON payload appears to be a top-level object.

    Preconditions:
        - ``text`` is a string (may be empty).
    Postconditions:
        - Returns True only when the entire stripped response is a JSON object;
          prose and fenced snippets are not treated as top-level objects.
    """
    candidate = text.strip()
    if not candidate.startswith("{"):
        return False
    try:
        value, end = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError:
        return False
    return isinstance(value, dict) and not candidate[end:].strip()


def _write_draft_to_path(draft: str, path: Union[str, Path]) -> None:
    """Write draft content to path; create parent dirs if needed. Log the saved path.

    Preconditions:
        - ``draft`` must be a string (may be empty).
        - ``path`` must be a ``str`` or ``pathlib.Path``.
    Postconditions:
        - Parent directories of ``path`` exist.
        - The resolved path contains ``draft`` as UTF-8 text.
        - A success log records the resolved path.
    """
    if not isinstance(draft, str):
        raise TypeError(f"draft must be a string, got {type(draft).__name__}")
    if not isinstance(path, (str, Path)):
        raise TypeError(f"path must be a str or Path, got {type(path).__name__}")
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(draft, encoding="utf-8")
    logger.info("Draft written to %s", p)


class BlogWriterAgent(_BlogAgentBase):
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

        Raises:
            ValueError: if ``llm_client`` is ``None``.
        """
        if llm_client is None:
            raise ValueError("llm_client must not be None")
        super().__init__(llm_client)
        # ``_call_text`` produces the ``---DRAFT---`` hybrid format (JSON
        # marker line + Markdown body), which only works when the underlying
        # adapter is in text mode — JSON mode would force a single JSON object
        # on the wire and the marker pattern would disappear. Derive a
        # text-mode sibling from the injected model when possible; fall back
        # to the passed model so test fixtures (MagicMock, fakes) continue to
        # work. ``_call_json_raw`` / ``_call_agent_json`` use ``self._model``
        # directly for structured helpers.
        try:
            from llm_service.strands_adapter import LLMClientModel  # noqa: PLC0415

            if isinstance(llm_client, LLMClientModel):
                self._text_model = llm_client.clone(response_format="text")
            else:
                self._text_model = llm_client
        except ImportError:
            self._text_model = llm_client
        self._writing_style_prompt = (writing_style_guide_content or "").strip()
        self._brand_spec_prompt = (brand_spec_content or "").strip()
        parts: list[str] = []
        if self._brand_spec_prompt:
            parts.append("--- BRAND SPEC ---\n" + self._brand_spec_prompt)
        if self._writing_style_prompt:
            parts.append("--- WRITING STYLE GUIDE ---\n" + self._writing_style_prompt)
        self._style_prompt = "\n\n".join(parts)

    def _call_agent(self, model: Any, prompt: str, system_prompt: str = "") -> str:
        """Construct a Strands Agent, invoke it, and return stripped text.

        Shared invocation path for ``_call_text`` and ``_call_json_raw``, which
        differ only in which model they pass.

        Preconditions:
            - ``model`` is a configured LLM client/model object.
            - ``prompt`` is a non-empty string.
        Postconditions:
            - Returns the agent's response as a stripped string.
        """
        agent = Agent(model=model, system_prompt=system_prompt or WRITING_SYSTEM_PROMPT)
        return str(agent(prompt)).strip()

    def _call_text(self, prompt: str, system_prompt: str = "") -> str:
        """Call the text-mode Strands Agent and return its stripped text output.

        Used for drafting and revision paths that emit the ``---DRAFT---``
        marker + Markdown hybrid format. The text-mode sibling avoids forcing
        ``response_format=json_object`` on the wire so the marker survives.
        """
        return self._call_agent(self._text_model, prompt, system_prompt)

    def _call_json_raw(self, prompt: str, system_prompt: str = "") -> str:
        """Invoke the injected model via Strands and return its stripped assistant text.

        Uses ``self._model`` as supplied by the caller (typically already configured
        for structured/JSON-oriented completions). Does not clone or force
        ``response_format=json_object`` here — callers that need a specific wire
        format must configure that on the injected client. Prefer this over
        parsing when a caller needs to extract JSON itself (e.g. planning paths
        that call ``extract_json_from_response``).
        """
        return self._call_agent(self._model, prompt, system_prompt)

    def _call_agent_json(self, prompt: str, system_prompt: str = "") -> dict:
        """Invoke the injected model via Strands and parse JSON from the result.

        Appends a soft JSON-only instruction and runs ``extract_json_from_response``
        as defensive cleanup if the model wraps the object. Relies on ``self._model``
        already being suitable for structured replies; this method does not force
        ``response_format=json_object`` on the wire.

        Raises:
            ``LLMJsonParseError`` when the response contains no extractable JSON,
            or when the extracted JSON parses to something other than a dict.
        """
        raw = self._call_json_raw(
            prompt + "\n\nRespond with valid JSON only, no markdown fences.",
            system_prompt,
        )
        data = extract_json_from_response(raw)
        if not isinstance(data, dict):
            raise LLMJsonParseError(f"Expected a JSON object, got {type(data).__name__}")
        return data

    def _fallback_draft_via_json(self, prompt: str, system_prompt: str = "") -> Optional[str]:
        """Parse a revised draft via shared JSON retry when the text path fails.

        Preconditions:
            - ``prompt`` is a non-empty string (same prompt used for the text path).
            - ``system_prompt``, if given, mirrors the one used for the failed
              text-path call; falls back to ``WRITING_SYSTEM_PROMPT`` when empty,
              matching ``_call_text``/``_call_json_raw``.
        Postconditions:
            - Returns a non-empty stripped draft string on success.
            - Returns ``None`` when JSON cannot yield a usable draft (caller keeps
              the prior draft).
            - Transient LLM transport errors (``LLMRateLimitError`` /
              ``LLMTemporaryError``), including when strands wraps them in
              ``EventLoopException``, propagate unwrapped from
              ``call_json_with_retry`` so the draft-stage retry funnel can catch them.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

        soft_json_instruction = "\n\nRespond with valid JSON only, no markdown fences."
        strict_json_suffix = (
            "\n\nRespond with a single JSON object only (no markdown, no code fence). "
            'Keys: "draft" (string — the full revised blog post in Markdown).'
        )

        def _agent_factory():
            # Construct Agent inside the invoker so TypeError/ValueError from a
            # bad model/config land in call_json_with_retry's try block and hit
            # on_unexpected_error (preserving the prior keep-original behavior).
            model = self._model

            def _invoke(prompt: str):
                return Agent(model=model, system_prompt=system_prompt or WRITING_SYSTEM_PROMPT)(
                    prompt
                )

            return _invoke

        def _unwrap(exc: Exception) -> Exception:
            return exc.original_exception if isinstance(exc, EventLoopException) else exc

        def _empty_fallback(_exc: Exception) -> dict:
            return {}

        data = call_json_with_retry(
            _agent_factory,
            prompt + soft_json_instruction,
            max_attempts=2,
            strict_json_suffix=strict_json_suffix,
            unwrap_exception=_unwrap,
            on_exhausted=_empty_fallback,
            on_unexpected_error=_empty_fallback,
            logger=logger,
        )
        raw_draft = data.get("draft") if isinstance(data, dict) else None
        if isinstance(raw_draft, str) and raw_draft.strip():
            return raw_draft.strip()
        return None

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
    # Planning (delegates to shared.content_planning_loop; also used by
    # blog_planning_agent.BlogPlanningAgent, which delegates identically)
    # ------------------------------------------------------------------

    def _complete_plan_json(
        self,
        prompt: str,
        *,
        system: str,
        on_llm_request: Optional[Callable[[str], None]],
        max_parse_retries: int,
    ) -> tuple[dict[str, Any], int]:
        """Delegate to ``shared.content_planning_loop.complete_plan_json``, wiring this
        agent's ``_call_agent_json`` / ``_call_json_raw`` as the JSON and raw-text callers.

        Postconditions:
            - Returns the parsed plan dict and the number of parse retries consumed,
              per ``complete_plan_json``'s contract.
        """
        return complete_plan_json(
            prompt,
            system=system,
            on_llm_request=on_llm_request,
            max_parse_retries=max_parse_retries,
            call_json_fn=lambda p, s: self._call_agent_json(p, system_prompt=s),
            call_raw_fn=lambda p, s: self._call_json_raw(p, system_prompt=s),
        )

    def plan_content(
        self,
        planning_input: PlanningInput,
        *,
        length_policy: LengthPolicy,
        on_llm_request: Optional[Callable[[str], None]] = None,
        max_iterations: int = 5,
        max_parse_retries: int = 3,
        plan_critic: Optional[BlogPlanCriticAgent] = None,
        work_dir: Optional[Union[str, Path]] = None,
    ) -> PlanningPhaseResult:
        """Generate and refine a ContentPlan until the planner (and optional critic) agree.

        When ``plan_critic`` is supplied, its verdict is authoritative: the loop
        terminates only when the planner's self-eval is done AND the critic
        approves. Refine feedback comes from the critic's structured violations
        instead of a generic string. When absent, legacy planner-self-eval only.

        Args:
            planning_input: Brief/context the planner drafts and refines against.
            length_policy: Target-length policy passed through to the loop.
            on_llm_request: Optional progress callback invoked before each LLM call.
            max_iterations: Cap on generate/refine loop iterations before giving up.
            max_parse_retries: Cap on JSON-parse retries per LLM call within the loop.
            plan_critic: Optional critic agent whose approval is required for the
                loop to terminate; omit for legacy planner-self-eval-only behavior.
            work_dir: Optional directory to persist intermediate plan artifacts to.

        Returns:
            The final ``PlanningPhaseResult`` (content plan plus loop metadata),
            per ``run_content_planning_loop``'s contract.
        """
        return run_content_planning_loop(
            planning_input,
            length_policy=length_policy,
            on_llm_request=on_llm_request,
            max_iterations=max_iterations,
            max_parse_retries=max_parse_retries,
            plan_critic=plan_critic,
            brand_spec_prompt=self._brand_spec_prompt,
            writing_guidelines=self._writing_style_prompt,
            work_dir=work_dir,
            generate_system=GENERATE_PLAN_SYSTEM,
            refine_system=REFINE_PLAN_SYSTEM,
            complete_plan_json_fn=self._complete_plan_json,
        )

    # ------------------------------------------------------------------
    # Self-check: deterministic + LLM review
    # ------------------------------------------------------------------

    def _deterministic_self_check(self, draft: str) -> list[str]:
        """Scan draft for mechanical violations. Returns list of violation descriptions.

        Checks: em/en dashes, banned phrases (``BANNED_PHRASES``), vague citation
        patterns not followed by a source/link, reader-address (``you``/``your``)
        count below 3, and staccato prose (3+ consecutive short sentences).
        """
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
                    or re.search(r"\]\(https?://", after)
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
        """Call LLM once to fix deterministic violations. Returns cleaned draft.

        Preconditions:
            - ``draft`` is a non-empty string when callers intend a real fix (empty is allowed).
            - ``violations`` is a list of human-readable violation strings (may be empty).
        Postconditions:
            - On success with extractable fixed draft, returns that stripped draft.
            - On soft-fail (``LLMError`` excluding types re-raised below, or
              ``json.JSONDecodeError`` / ``TypeError`` / ``ValueError`` / ``AttributeError``),
              logs with traceback via ``logger.exception`` and returns the original ``draft``.
            - ``LLMRateLimitError`` and ``LLMTemporaryError`` (including when wrapped in
              ``EventLoopException``) propagate as the unwrapped cause.
            - Unexpected exceptions propagate unchanged.
        """
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
            raw = self._call_text(prompt, system_prompt=WRITING_SYSTEM_PROMPT)
            fixed = _extract_draft_after_marker(raw)
            if fixed and fixed.strip():
                logger.info("Deterministic self-check: fixed %s violations", len(violations))
                return fixed.strip()
        except Exception as e:
            cause = _unwrap_llm_cause(e)
            if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                raise cause
            if isinstance(
                cause, (LLMError, json.JSONDecodeError, TypeError, ValueError, AttributeError)
            ):
                logger.exception("Deterministic fix LLM call failed")
            else:
                raise
        return draft

    def _llm_self_review(self, draft: str) -> str:
        """Run a focused LLM self-review for subjective violations. Returns cleaned draft.

        Preconditions:
            - ``draft`` is a string (may be empty).
        Postconditions:
            - On success, returns the reviewed/fixed draft or the original when no issues.
            - On soft-fail (``LLMError`` excluding types re-raised below, or
              ``json.JSONDecodeError`` / ``TypeError`` / ``ValueError`` / ``AttributeError``),
              logs with traceback via ``logger.exception`` and returns the original ``draft``.
            - ``LLMRateLimitError`` and ``LLMTemporaryError`` (including when wrapped in
              ``EventLoopException``) propagate as the unwrapped cause.
            - Unexpected exceptions propagate unchanged.
        """
        try:
            raw = self._call_text(
                f"Review this draft:\n\n{draft}", system_prompt=SELF_REVIEW_PROMPT
            )
            cleaned = raw.strip()
            # Prefer the shared extractor for fenced / whole-response JSON.
            # Fall back to a Markdown-safe array scan only when extraction fails,
            # or when a non-list parse looks like salvage from prose (not a
            # top-level object response — those must not be rescanned for
            # nested arrays).
            try:
                parsed = extract_json_from_response(cleaned)
            except LLMJsonParseError:
                issues = _extract_json_array_from_text(cleaned, required_keys=("issue",))
            else:
                if isinstance(parsed, list):
                    issues = parsed
                elif _looks_like_top_level_json_object(cleaned):
                    logger.info("LLM self-review: no issues found (response was not a JSON array)")
                    return draft
                else:
                    issues = _extract_json_array_from_text(cleaned, required_keys=("issue",))
            if issues is None:
                logger.info("LLM self-review: no issues found (response was not a JSON array)")
                return draft
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
            raw_fix = self._call_text(fix_prompt, system_prompt=WRITING_SYSTEM_PROMPT)
            fixed = _extract_draft_after_marker(raw_fix)
            if fixed and fixed.strip():
                logger.info("LLM self-review: applied fixes, new length=%s", len(fixed.strip()))
                return fixed.strip()
        except Exception as e:
            cause = _unwrap_llm_cause(e)
            if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                raise cause
            if isinstance(
                cause, (LLMError, json.JSONDecodeError, TypeError, ValueError, AttributeError)
            ):
                logger.exception("LLM self-review failed")
            else:
                raise
        return draft

    def _self_review(self, draft: str) -> str:
        """Run deterministic check then LLM self-review. Returns cleaned draft.

        Both sub-steps (``_fix_deterministic_violations``, ``_llm_self_review``)
        already return the original draft on their own soft-fail paths, so this
        method has no additional failure handling of its own.
        """
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

        Preconditions:
            - Brand and writing guidelines are present (enforced by
              ``_assert_guidelines_present``).
            - ``draft_input`` is a valid ``WriterInput``.
        Postconditions:
            - Returns a ``WriterOutput`` with a non-empty draft string.
            - Expected LLM parse failures (``LLMJsonParseError``, including when
              Strands wraps them in ``EventLoopException``) soft-fail into a JSON
              fallback, then a placeholder if both paths yield no content.
            - Any other exception from the LLM call path propagates unchanged —
              this includes non-transient LLM errors such as
              ``LLMPermanentError``/``LLMRateLimitError``/``LLMTemporaryError``,
              not only unexpected programming errors.
        Invariants:
            - The agent's configuration, style guide, and brand spec are not mutated.
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
        # Soft-fail only on expected LLM parse failures (unwrap Strands EventLoopException);
        # programming bugs (TypeError/ValueError/etc.) propagate.
        draft = ""
        try:
            raw_response = self._call_text(prompt, system_prompt=WRITING_SYSTEM_PROMPT)
            draft = _extract_draft_after_marker(raw_response)
        except Exception as e:
            cause = _unwrap_llm_cause(e)
            if not isinstance(cause, LLMJsonParseError):
                raise
            logger.warning(
                "Draft text completion failed: %s; trying JSON fallback.",
                cause,
            )
            try:
                data = self._call_agent_json(prompt)
                if isinstance(data, dict):
                    raw_draft = data.get("draft")
                    if isinstance(raw_draft, str) and raw_draft.strip():
                        draft = raw_draft.strip()
            except Exception as e2:
                cause2 = _unwrap_llm_cause(e2)
                if not isinstance(cause2, LLMJsonParseError):
                    raise
                logger.warning("JSON draft fallback also failed: %s", cause2)

        if not draft:
            logger.warning("LLM returned no draft content; returning placeholder.")
            draft = _PLACEHOLDER_DRAFT

        logger.info("Draft generated: length=%s", len(draft))
        if draft != _PLACEHOLDER_DRAFT:
            if on_llm_request:
                on_llm_request("Running self-review...")
            draft = self._self_review(draft)
        if draft_output_path:
            _write_draft_to_path(draft, draft_output_path)
        return WriterOutput(draft=draft)

    def _format_feedback_item_line(self, item: Any, index: int) -> str:
        """One numbered feedback line (+ optional suggestion) for batch revise prompts.

        Preconditions:
            ``index`` is a positive int. ``item`` exposes ``severity``, ``category``,
            and ``issue`` (via attribute or duck typing); empty/missing values are
            rejected. ``location`` and ``suggestion`` are optional.
        Postconditions:
            Returns a numbered feedback line; includes a location bracket and a
            suggestion sub-line when those optional fields are present.
        """
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

    def _build_revise_all_items_prompt(
        self,
        draft: str,
        feedback_items: list[Any],
        revision_plan: str,
        style_guide_text: str,
        revise_input: ReviseWriterInput,
    ) -> str:
        """Build one revision prompt that applies every copy-editor feedback item.

        Postconditions:
            - Returns a prompt string embedding the brand/style sections, the
              content plan, every feedback item formatted via
              ``_format_feedback_item_line``, ``revision_plan`` as planning
              context, and the current draft.
        """
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
        if revise_input.audience:  # pragma: no cover - prompt-assembly branch when audience is supplied; covered by integration tests.
            prompt_parts.insert(0, f"Audience: {revise_input.audience}\n")
        if revise_input.tone_or_purpose:  # pragma: no cover - prompt-assembly branch when tone_or_purpose is supplied; covered by integration tests.
            prompt_parts.insert(0, f"Tone/Purpose: {revise_input.tone_or_purpose}\n")
        if revise_input.selected_title:  # pragma: no cover - prompt-assembly branch when selected_title is supplied; covered by integration tests.
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
        """Build a prompt that asks the LLM for a structured revision plan.

        Preconditions:
            - ``draft`` is the current Markdown draft text.
            - ``feedback_items`` is a sequence of items that each expose
              ``severity``, ``category``, and ``issue`` (and optionally
              ``location`` / ``suggestion``) for ``_format_feedback_item_line``.
            - ``revise_input`` provides the content plan via
              ``outline_for_prompt()``.
        Postconditions:
            - Returns a prompt string that instructs the model to return JSON
              matching the ``RevisionPlan`` schema (``summary``, ordered
              ``changes`` with ``section`` / ``feedback_ids`` / ``action`` /
              ``rationale``, and ``risks``), with feedback referenced by
              1-based index and ``must_fix`` severity prioritized.
        """
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

    def _generate_revision_plan(
        self,
        draft: str,
        feedback_items: list[Any],
        revise_input: ReviseWriterInput,
    ) -> RevisionPlan:
        """Build a structured revision plan, with a plain-text fallback.

        Calls the JSON-oriented LLM path first and converts its response to a
        ``RevisionPlan``. Any non-transient failure — including an unexpected
        programming error, not only LLM/structured-call failures — falls back to
        a plain-text plan; transient LLM errors are unwrapped and re-raised.
        """
        prompt = self._build_revision_plan_prompt(draft, feedback_items, revise_input)
        try:
            data = self._call_agent_json(prompt, system_prompt=WRITING_SYSTEM_PROMPT)
            if not data or not isinstance(data, dict):
                return RevisionPlan(summary="Planning produced no output.", changes=[], risks=[])
            changes: list[RevisionPlanChange] = []
            for c in data.get("changes") or []:
                if not isinstance(c, dict):
                    continue
                try:
                    changes.append(RevisionPlanChange(**c))
                except (TypeError, ValueError) as change_exc:
                    logger.debug("Skipping malformed revision plan change: %s", change_exc)
                    continue
            return RevisionPlan(
                summary=data.get("summary", ""),
                changes=changes,
                risks=data.get("risks") or [],
            )
        except Exception as e:
            cause = _unwrap_llm_cause(e)
            if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                raise cause
            logger.warning(
                "Structured revision planning failed: %s — falling back to unstructured", e
            )
            # Graceful degradation: try plain-text plan
            try:
                plain = self._call_text(prompt, system_prompt=WRITING_SYSTEM_PROMPT)
                return RevisionPlan(summary=(plain or "").strip(), changes=[], risks=[])
            except Exception as fallback_exc:
                fallback_cause = _unwrap_llm_cause(fallback_exc)
                if isinstance(fallback_cause, (LLMRateLimitError, LLMTemporaryError)):
                    raise fallback_cause
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
        """Build a revision prompt for a single feedback item.

        Postconditions:
            - Returns a prompt string embedding the brand/style sections, the
              content plan, the single feedback item formatted via
              ``_format_feedback_item_line``, and the current draft, instructing
              the model to change only that one issue.
        """
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
                raw_response = self._call_text(prompt, system_prompt=WRITING_SYSTEM_PROMPT)
                revised = _extract_draft_after_marker(raw_response)
                if revised and revised.strip():
                    return revised.strip()
            # The underlying Strands Agent call can surface LLMJsonParseError; retry
            # without the transient backoff sleep (covered by test_writer_interactive.py).
            except LLMJsonParseError as e:
                logger.warning("Revise item %s/%s: %s; retrying.", item_index, total_items, e)
                if attempt == 0:
                    time.sleep(0.5)
            except Exception as e:
                cause = _unwrap_llm_cause(e)
                if isinstance(cause, LLMJsonParseError):
                    logger.warning(
                        "Revise item %s/%s: %s; retrying.", item_index, total_items, cause
                    )
                    if attempt == 0:
                        time.sleep(0.5)
                    continue
                if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                    logger.warning(
                        "Revise item %s/%s: transient error (attempt %s/2); retrying.",
                        item_index,
                        total_items,
                        attempt + 1,
                    )
                    time.sleep(2.0 + attempt)
                    continue
                raise
        # Fallback — keep original on unexpected failure; re-raise transient LLM
        # errors so the draft-stage retry funnel can own backoff.
        try:
            fallback = self._fallback_draft_via_json(prompt, system_prompt=WRITING_SYSTEM_PROMPT)
            if fallback:
                return fallback
        except (LLMRateLimitError, LLMTemporaryError):
            raise
        except Exception as e:
            logger.warning(
                "Revise item %s/%s: JSON fallback failed: %s; keeping draft as-is.",
                item_index,
                total_items,
                e,
            )
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
               Persisted in *work_dir* as ``revision_plan_{iteration}.json`` when
               *iteration* is a positive int, otherwise ``revision_plan.json``.
            3. **Execute** — apply the plan to produce the revised draft.
               Persisted as *draft_output_path* (e.g. ``draft_v{iteration}.md``).

        Preconditions:
            - ``revise_input`` is a ``ReviseWriterInput``.
            - Brand and writing guidelines have both been loaded
              (``_assert_guidelines_present``).
        Postconditions:
            - Strips leading/trailing whitespace from ``revise_input.draft`` before
              revision. If the result is empty, returns ``revise_input.draft``
              unchanged (preserves the caller's original whitespace-only text).
            - Otherwise returns a ``WriterOutput`` whose draft is the revised text,
              the stripped draft when feedback is empty, or the stripped draft when
              the text path and JSON fallback both fail to produce a usable draft.
            - During batch execute retries, unwrapped ``LLMJsonParseError``
              (including ``EventLoopException`` wrappers) retries without a
              backoff sleep, and unwrapped ``LLMRateLimitError`` /
              ``LLMTemporaryError`` (including wrappers) retry with backoff;
              unexpected exceptions propagate immediately.
        """
        self._assert_guidelines_present()
        original_draft = revise_input.draft or ""
        draft = original_draft.strip()
        if not draft:
            logger.warning("Empty draft in revise; returning as-is.")
            return WriterOutput(draft=original_draft)
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
            plan_name = (
                f"revision_plan_{iteration}.json"
                if iteration is not None and iteration > 0
                else "revision_plan.json"
            )
            try:
                from agents.blogging.shared.artifacts import write_artifact

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
        primary_succeeded = False
        for attempt in range(BATCH_EXECUTE_MAX_RETRIES):
            try:
                raw_response = self._call_text(prompt, system_prompt=WRITING_SYSTEM_PROMPT)
                revised = _extract_draft_after_marker(raw_response)
                if revised and revised.strip():
                    current_draft = revised.strip()
                    primary_succeeded = True
                    break
            # See _revise_one_item: LLMJsonParseError from the Strands Agent call
            # retries without the transient backoff sleep.
            except LLMJsonParseError as e:
                logger.warning(
                    "Batch revise failed (attempt %s/%s): %s",
                    attempt + 1,
                    BATCH_EXECUTE_MAX_RETRIES,
                    e,
                )
            except Exception as e:
                cause = _unwrap_llm_cause(e)
                if isinstance(cause, LLMJsonParseError):
                    logger.warning(
                        "Batch revise failed (attempt %s/%s): %s",
                        attempt + 1,
                        BATCH_EXECUTE_MAX_RETRIES,
                        cause,
                    )
                    continue
                if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                    logger.warning(
                        "Batch revise transient error (attempt %s/%s); retrying.",
                        attempt + 1,
                        BATCH_EXECUTE_MAX_RETRIES,
                    )
                    time.sleep(BATCH_EXECUTE_BACKOFF_BASE_SECONDS * (2**attempt))
                    continue
                raise
        if not primary_succeeded:
            try:
                fallback = self._fallback_draft_via_json(
                    prompt, system_prompt=WRITING_SYSTEM_PROMPT
                )
                if fallback:
                    current_draft = fallback
            except (LLMRateLimitError, LLMTemporaryError):
                raise
            except Exception as e:
                logger.warning("Batch revise JSON fallback failed: %s; keeping original draft.", e)

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

        Returns a list of UncertaintyQuestion objects. An empty list means the
        agent is confident in the draft, the model returned no questions, or any
        other non-transient failure (LLM/parse error or unexpected exception) was
        soft-failed after logging. Transient ``LLMRateLimitError`` /
        ``LLMTemporaryError`` (including when wrapped in ``EventLoopException``)
        propagate so Temporal can retry the draft stage.
        """
        prompt = UNCERTAINTY_DETECTION_PROMPT.format(
            content_plan=content_plan_text,
            draft=draft,
        )
        try:
            # NOTE: use ``_call_text`` (not ``_call_agent_json``). The prompt asks
            # for a top-level JSON *array*, but JSON-mode adapters constrain
            # output to a single object, so a JSON-mode call can wrap or empty
            # the array. ``_extract_json_array_from_text`` extracts ``[...]``
            # from prose, skipping Markdown links and other non-array ``[``.
            raw = self._call_text(
                prompt,
                system_prompt="You are a careful writing assistant that identifies areas of genuine uncertainty.",
            )
            cleaned = raw.strip()
            items = _extract_json_array_from_text(cleaned, required_keys=("question",))
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
            cause = _unwrap_llm_cause(e)
            if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                raise cause
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

        Returns an empty list when the feedback has no guideline-relevant content,
        the response is malformed / non-dict, or any non-transient ``LLMError``
        (including ``LLMJsonParseError`` and ``LLMPermanentError``) is soft-failed
        with a logged traceback — this is an optional analysis step, and a
        non-transient LLM failure here should not abort the draft stage.
        Transient ``LLMRateLimitError`` / ``LLMTemporaryError`` (including when
        wrapped in ``EventLoopException``) propagate so Temporal can retry the
        draft stage. An unexpected programming error (not an ``LLMError``)
        propagates rather than being swallowed.
        """
        prompt = ANALYZE_USER_FEEDBACK_FOR_GUIDELINES_PROMPT.format(
            user_feedback=user_feedback,
            current_guidelines=current_guidelines,
        )
        try:
            data = self._call_agent_json(prompt)
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
            cause = _unwrap_llm_cause(e)
            if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                raise cause
            if not isinstance(cause, LLMError):
                raise
            logger.exception("Guideline update analysis failed: %s", cause)
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

        Postconditions:
            - Returns ``draft`` unchanged when it is blank.
            - Otherwise retries the text-completion path up to 3 times: an
              unwrapped ``LLMJsonParseError`` (including ``EventLoopException``
              wrappers) retries without a backoff sleep, and an unwrapped
              ``LLMRateLimitError`` / ``LLMTemporaryError`` (including wrappers)
              retries with backoff. If all attempts fail, falls back to
              ``_fallback_draft_via_json``; if both paths fail to produce a
              usable draft, returns the original ``draft`` unchanged.
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
            USER_FEEDBACK_REVISION_INSTRUCTIONS.replace("{user_feedback}", user_feedback),
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
        primary_succeeded = False
        for attempt in range(BATCH_EXECUTE_MAX_RETRIES):
            try:
                raw_response = self._call_text(prompt, system_prompt=WRITING_SYSTEM_PROMPT)
                revised = _extract_draft_after_marker(raw_response)
                if revised and revised.strip():
                    current_draft = revised.strip()
                    primary_succeeded = True
                    break
            # See _revise_one_item: LLMJsonParseError from the Strands Agent call
            # retries without the transient backoff sleep (test_revise_from_user_feedback_json_parse_error_skips_sleep).
            except LLMJsonParseError as e:
                logger.warning(
                    "User-feedback revision failed (attempt %s/%s): %s",
                    attempt + 1,
                    BATCH_EXECUTE_MAX_RETRIES,
                    e,
                )
            except Exception as e:
                cause = _unwrap_llm_cause(e)
                if isinstance(cause, LLMJsonParseError):
                    logger.warning(
                        "User-feedback revision failed (attempt %s/%s): %s",
                        attempt + 1,
                        BATCH_EXECUTE_MAX_RETRIES,
                        cause,
                    )
                    continue
                if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                    logger.warning(
                        "User-feedback revision transient error (attempt %s/%s); retrying.",
                        attempt + 1,
                        BATCH_EXECUTE_MAX_RETRIES,
                    )
                    time.sleep(BATCH_EXECUTE_BACKOFF_BASE_SECONDS * (2**attempt))
                    continue
                raise

        if not primary_succeeded:
            try:
                fallback = self._fallback_draft_via_json(
                    prompt, system_prompt=WRITING_SYSTEM_PROMPT
                )
                if fallback:
                    current_draft = fallback
            except Exception as e:
                cause = _unwrap_llm_cause(e)
                if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                    raise cause
                logger.warning(
                    "User-feedback JSON fallback failed after retries; keeping original draft: %s",
                    e,
                )

        logger.info("User-feedback revision complete, final length=%s", len(current_draft))
        if draft_output_path:
            _write_draft_to_path(current_draft, draft_output_path)
        return WriterOutput(draft=current_draft)

    def generate_escalation_summary(
        self,
        revision_count: int,
        latest_feedback_items: list[FeedbackItem],
        persistent_issues: list[PersistentFeedbackItem],
    ) -> str:
        """Generate a human-readable summary when the copy-edit loop hits the escalation threshold.

        Called when the automated editor has gone through ``revision_count`` iterations
        without approving the draft, to produce a clear explanation for the user about
        what is stuck and what guidance is needed.

        Transient ``LLMRateLimitError`` / ``LLMTemporaryError`` (including when wrapped
        in ``EventLoopException``) propagate so Temporal can retry. Other LLM failures
        fall back to a generic summary string.
        """
        feedback_text = "\n".join(
            f"- [{item.severity}] {item.category}: {item.issue}" for item in latest_feedback_items
        )
        persistent_text = (
            "\n".join(
                f"- [{item.severity}] {item.category} "
                f"(flagged {item.occurrence_count} times): {item.issue}"
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
            summary = self._call_text(prompt)
            return (summary or "").strip()
        except Exception as e:
            cause = _unwrap_llm_cause(e)
            if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
                raise cause
            if not isinstance(cause, LLMError):
                raise
            logger.warning("Escalation summary generation failed: %s", e)
            return (
                f"The draft has been through {revision_count} automated revision cycles "
                "without reaching approval. Please review the current draft and provide feedback."
            )
