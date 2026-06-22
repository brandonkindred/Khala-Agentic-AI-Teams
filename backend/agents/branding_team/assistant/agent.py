"""Branding assistant agent: two-stage conversation + silent mission extraction.

Stage 1 — Conversation: a Strands `Agent` configured as a senior brand strategist.
It replies in pure natural language (no JSON, no field names). This is what the
user reads.

Stage 2 — Extraction: a second Strands `Agent` reads the same turn plus the
strategist's reply and emits a JSON `mission_update` + `suggested_questions`.
The user never sees this output. Decoupling extraction from conversation means
the user-facing reply is never constrained or contaminated by structured-output
formatting requirements.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Tuple

from branding_team.models import BrandingMission, ColorPalette

from .prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_TEMPLATE,
    SINGLE_CALL_SYSTEM_PROMPT,
    SINGLE_CALL_USER_TEMPLATE,
    SYSTEM_PROMPT,
    USER_TURN_TEMPLATE,
)

logger = logging.getLogger(__name__)

_DEFAULT_SUGGESTIONS = [
    "What's your company or product name?",
    "Who is your target audience?",
    "What 3–5 values define your brand?",
]


def _single_call_enabled() -> bool:
    """True when the opt-in single-LLM-call assistant path is enabled."""
    return os.environ.get("BRANDING_ASSISTANT_SINGLE_CALL", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _parse_single_call(raw: str) -> Tuple[str, Dict[str, Any], List[str]]:
    """Parse the single-call JSON → (reply, mission_update, suggested_questions).

    Tolerates markdown fences / surrounding prose. Returns empty values on any
    parse failure so the caller can substitute a graceful fallback.
    """
    text = (raw or "").strip()
    if not text:
        return "", {}, []
    cleaned = re.sub(r"^```(?:json)?\s*", "", text)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    parsed: Any = None
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except (json.JSONDecodeError, TypeError):
                parsed = None
    if not isinstance(parsed, dict):
        return "", {}, []
    reply = str(parsed.get("reply") or "")
    mission_update = _coerce_mission(parsed.get("mission_update"))
    suggested_questions = _coerce_suggestions(parsed.get("suggested_questions"))
    return reply, mission_update, suggested_questions


def _coerce_suggestions(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(s).strip() for s in value if str(s).strip()][:4]
    return []


def _coerce_mission(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_extraction(raw: str) -> Tuple[Dict[str, Any], List[str]]:
    """Parse the silent extractor's JSON output → (mission_update, suggested_questions).

    Tolerates markdown fences and stray prose around a JSON object. Returns
    empty values on any parse failure — the conversation reply is unaffected.
    """
    text = (raw or "").strip()
    if not text:
        return {}, []

    cleaned = re.sub(r"^```(?:json)?\s*", "", text)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    parsed: Any = None
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except (json.JSONDecodeError, TypeError):
                parsed = None

    if not isinstance(parsed, dict):
        logger.warning("Branding extractor produced unparseable output; treating as no-op")
        return {}, []

    mission_update = _coerce_mission(parsed.get("mission_update"))
    # Schema-drift fallback: when the model omits the ``mission_update``
    # wrapper and emits mission fields at the top level — a common LLM
    # output deviation — promote the top-level mission-shaped keys so the
    # turn's updates aren't silently lost.
    if not mission_update:
        top_level_mission = {k: v for k, v in parsed.items() if k in _MISSION_FIELD_NAMES}
        if top_level_mission:
            mission_update = top_level_mission
    suggested_questions = _coerce_suggestions(parsed.get("suggested_questions"))
    return mission_update, suggested_questions


_MISSION_FIELD_NAMES = {
    "company_name",
    "company_description",
    "target_audience",
    "values",
    "differentiators",
    "desired_voice",
    "existing_brand_material",
    "color_inspiration",
    "color_palettes",
    "selected_palette_index",
    "visual_style",
    "typography_preference",
    "interface_density",
}


def _contains_mission_field(obj: Any) -> bool:
    """Recursively walk ``obj`` looking for any key in ``_MISSION_FIELD_NAMES``.

    Why recurse: mission payloads embed nested structures (e.g.
    ``color_palettes`` is a list of dicts). A top-level-only check would
    miss leaks whose mission keys live one level deep — e.g. an LLM that
    wraps the payload as ``{"mission": {"company_name": "X"}}``.
    """
    if isinstance(obj, dict):
        if _MISSION_FIELD_NAMES.intersection(obj.keys()):
            return True
        return any(_contains_mission_field(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_mission_field(item) for item in obj)
    return False


def _iter_balanced_json_objects(text: str):
    """Yield ``(start, end)`` index pairs for every brace-balanced ``{...}``
    block in ``text``, scanned left-to-right at the top level.

    A simple ``re.finditer(r"\\{[^{}]*\\}", text)`` would match only **flat**
    JSON objects — any nesting inside the candidate causes the regex to
    match an inner object instead of the outer one. That breaks the
    mission-JSON guard for realistic leaks like
    ``{"company_name": "X", "color_palettes": [{"name": "p1"}]}`` where
    the regex would skip the outer block and only see the inner palette
    (which carries no mission keys). Brace counting handles arbitrary
    nesting and skips past each outer object so its interior is not
    re-scanned in isolation.

    When an opening ``{`` cannot be balanced (no matching ``}`` exists in
    the remaining text), the scanner advances by one character and keeps
    looking — a stray ``{`` in prose ahead of a real mission JSON object
    must not silently disqualify the rest of the reply from the leak
    guard. The scanner still terminates (each step strictly advances
    ``i``).
    """
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        end = -1
        for j in range(i, n):
            ch = text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end == -1:
            # Stray unmatched ``{`` — skip just this character and resume
            # scanning. Returning here would let a stray brace upstream of
            # a real mission JSON object suppress the entire guard.
            i += 1
            continue
        yield i, end
        i = end


def _strip_accidental_json(reply: str) -> str:
    """Defensive guard against the conversation LLM ever leaking structured data
    to the user. Returns an empty string in any of the following cases so the
    caller can substitute a graceful fallback prose reply:

    - The whole response is a JSON object (with or without surrounding whitespace).
    - Any brace-balanced JSON object embedded in prose contains mission-field
      keys at any depth (top-level or nested).

    Real prose is returned unchanged.
    """
    stripped = (reply or "").strip()
    if not stripped:
        return ""

    # Whole-response JSON object?
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                logger.warning(
                    "Branding conversation LLM returned raw JSON instead of prose; suppressing. "
                    "Raw response: %r",
                    stripped[:500],
                )
                return ""
        except (json.JSONDecodeError, TypeError):
            pass

    # Embedded JSON object containing mission fields (at any depth)?
    for start, end in _iter_balanced_json_objects(stripped):
        try:
            parsed = json.loads(stripped[start:end])
        except (json.JSONDecodeError, TypeError):
            continue
        if _contains_mission_field(parsed):
            logger.warning(
                "Branding conversation LLM embedded mission JSON inside prose; suppressing. "
                "Raw response: %r",
                stripped[:500],
            )
            return ""

    return stripped


def _merge_mission_update(current: BrandingMission, update: Dict[str, Any]) -> BrandingMission:
    """Merge update dict into current mission; only set keys present and non-empty where applicable."""
    data = current.model_dump()

    for key in (
        "company_name",
        "company_description",
        "target_audience",
        "desired_voice",
        "visual_style",
        "typography_preference",
        "interface_density",
    ):
        if key not in update:
            continue
        val = update[key]
        if isinstance(val, str) and val.strip():
            data[key] = val.strip()

    for key in ("values", "differentiators", "existing_brand_material", "color_inspiration"):
        if key not in update:
            continue
        val = update[key]
        if isinstance(val, list):
            data[key] = [str(x) for x in val if x]

    if "color_palettes" in update:
        raw_palettes = update["color_palettes"]
        if isinstance(raw_palettes, list):
            palettes = []
            for p in raw_palettes:
                if isinstance(p, dict):
                    palettes.append(
                        ColorPalette(
                            name=p.get("name", ""),
                            description=p.get("description", ""),
                            colors=[str(c) for c in p.get("colors", []) if c],
                            sentiment=p.get("sentiment", ""),
                        ).model_dump()
                    )
            if palettes:
                data["color_palettes"] = palettes

    if "selected_palette_index" in update:
        val = update["selected_palette_index"]
        if val is None:
            data["selected_palette_index"] = None
        elif isinstance(val, int) and 0 <= val < len(data.get("color_palettes", [])):
            data["selected_palette_index"] = val

    return BrandingMission(**data)


def _format_brief(mission: BrandingMission) -> Dict[str, Any]:
    return {
        "company_name": mission.company_name or "",
        "company_description": mission.company_description or "",
        "target_audience": mission.target_audience or "",
        "values": mission.values or [],
        "differentiators": mission.differentiators or [],
        "desired_voice": mission.desired_voice or "",
        "existing_brand_material": mission.existing_brand_material or [],
        "color_inspiration": mission.color_inspiration or [],
        "color_palettes": [
            p.model_dump() if hasattr(p, "model_dump") else p
            for p in (mission.color_palettes or [])
        ],
        "selected_palette_index": mission.selected_palette_index,
        "visual_style": mission.visual_style or "",
        "typography_preference": mission.typography_preference or "",
        "interface_density": mission.interface_density or "",
    }


def _history_window() -> int:
    """Max prior turns fed to the LLM (env-tunable, clamped to >= 1).

    Both LLM stages re-send the conversation history every turn, so an
    uncapped history makes each call grow without bound in tokens, latency,
    and memory. Capping to the most recent turns keeps per-turn cost roughly
    constant while preserving the immediate context the strategist needs.
    """
    raw = os.environ.get("BRANDING_ASSISTANT_HISTORY_WINDOW", "20")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 20


def _format_history(messages: List[Tuple[str, str]]) -> str:
    """Render the most recent conversation turns for the LLM prompt.

    Args:
        messages: prior turns as ``(role, content)`` tuples, oldest first.

    Returns:
        A newline-joined ``"Speaker: text"`` transcript of at most the last
        ``_history_window()`` turns, or ``"(No prior messages)"`` when empty.
    """
    if not messages:
        return "(No prior messages)"
    recent = messages[-_history_window() :]
    return "\n".join(
        f"{'Assistant' if role == 'assistant' else 'User'}: {content}" for role, content in recent
    )


class BrandingAssistantAgent:
    """Two-stage branding assistant: conversational strategist + silent mission extractor.

    The user only ever sees the conversation agent's natural-language reply. The
    extractor runs as a separate LLM call and produces the structured
    `mission_update` and `suggested_questions` from the same turn.
    """

    def __init__(self, conversation_llm=None, extraction_llm=None, llm=None, single_call_llm=None):  # noqa: ANN001
        # Backward compatibility: the legacy ``llm=`` argument drives BOTH
        # stages so tests and offline callers that inject a fake never hit a
        # live LLM. New code should pass ``conversation_llm`` and
        # ``extraction_llm`` separately when it needs distinct backends.
        if llm is not None:
            if conversation_llm is None:
                conversation_llm = llm
            if extraction_llm is None:
                extraction_llm = llm

        # Optional single-call agent (one LLM round-trip producing reply +
        # extraction together). Only used when BRANDING_ASSISTANT_SINGLE_CALL
        # is set; built lazily so the default two-stage path is untouched.
        self._single_call_agent = single_call_llm

        if conversation_llm is None or extraction_llm is None:
            from strands import Agent

            from llm_service import get_strands_model

            if conversation_llm is None:
                conversation_llm = Agent(
                    # response_format="text": the conversation stage must
                    # produce free-form prose. Without this opt-in the adapter
                    # would force JSON output on the wire and the user-facing
                    # reply would be empty or JSON-shaped.
                    model=get_strands_model("branding_assistant", response_format="text"),
                    system_prompt=SYSTEM_PROMPT,
                )
            if extraction_llm is None:
                extraction_llm = Agent(
                    # Extractor emits strict JSON — keep the default JSON mode.
                    model=get_strands_model("branding_assistant"),
                    system_prompt=EXTRACTION_SYSTEM_PROMPT,
                )

        self._conversation_agent = conversation_llm
        self._extraction_agent = extraction_llm

    def _get_single_call_agent(self):  # noqa: ANN201
        if self._single_call_agent is None:
            from strands import Agent

            from llm_service import get_strands_model

            self._single_call_agent = Agent(
                model=get_strands_model("branding_assistant"),
                system_prompt=SINGLE_CALL_SYSTEM_PROMPT,
            )
        return self._single_call_agent

    def _respond_single_call(
        self,
        messages: List[Tuple[str, str]],
        current_mission: BrandingMission,
        user_message: str,
    ) -> Tuple[str, BrandingMission, List[str]]:
        """One-round-trip variant: a single LLM call returns the reply plus the
        structured extraction. Opt-in via ``BRANDING_ASSISTANT_SINGLE_CALL`` —
        halves per-turn latency at the cost of constraining the reply to JSON.
        The prose leak-guard still applies to the ``reply`` field.
        """
        brief = _format_brief(current_mission)
        history = _format_history(messages)
        prompt = SINGLE_CALL_USER_TEMPLATE.format(
            conversation_history=history, user_message=user_message, **brief
        )
        try:
            raw = str(self._get_single_call_agent()(prompt)).strip()
        except Exception:
            logger.exception("Branding single-call LLM failed")
            return (
                "I'm here to help build your brand. Could you tell me your company name and what you do?",
                current_mission,
                list(_DEFAULT_SUGGESTIONS),
            )

        reply, mission_update, suggested_questions = _parse_single_call(raw)
        reply_text = _strip_accidental_json(reply)
        if not reply_text:
            reply_text = (
                "Thanks — let me make sure I'm following you. Could you tell me a bit more about "
                "what this brand is for and who you want it to resonate with?"
            )
        if not suggested_questions:
            suggested_questions = list(_DEFAULT_SUGGESTIONS)
        updated_mission = _merge_mission_update(current_mission, mission_update)
        return reply_text, updated_mission, suggested_questions

    def respond(
        self,
        messages: List[Tuple[str, str]],
        current_mission: BrandingMission,
        user_message: str,
    ) -> Tuple[str, BrandingMission, List[str]]:
        """Produce assistant reply, updated mission, and suggested follow-up questions.

        messages: conversation history as list of (role, content) tuples.
        current_mission: mission state before this turn.
        user_message: latest user message.
        """
        if _single_call_enabled():
            return self._respond_single_call(messages, current_mission, user_message)

        brief = _format_brief(current_mission)
        history = _format_history(messages)

        # ── Stage 1: conversational strategist reply ───────────────────────
        conversation_prompt = USER_TURN_TEMPLATE.format(
            conversation_history=history,
            user_message=user_message,
            **brief,
        )

        logger.info(
            "BrandingAssistant two-stage respond: conversation stage starting (history_msgs=%d)",
            len(messages),
        )
        try:
            raw_reply = str(self._conversation_agent(conversation_prompt)).strip()
            logger.info(
                "BrandingAssistant conversation stage raw response (first 300 chars): %r",
                raw_reply[:300],
            )
        except Exception:
            logger.exception("Branding conversation LLM failed")
            return (
                "I'm here to help build your brand. Could you tell me your company name and what you do?",
                current_mission,
                list(_DEFAULT_SUGGESTIONS),
            )

        reply_text = _strip_accidental_json(raw_reply)
        if not reply_text:
            reply_text = (
                "Thanks — let me make sure I'm following you. Could you tell me a bit more about "
                "what this brand is for and who you want it to resonate with?"
            )

        # ── Stage 2: silent extractor ──────────────────────────────────────
        extraction_prompt = EXTRACTION_USER_TEMPLATE.format(
            conversation_history=history,
            user_message=user_message,
            assistant_reply=reply_text,
            **brief,
        )

        mission_update: Dict[str, Any] = {}
        suggested_questions: List[str] = []
        try:
            raw_extraction = str(self._extraction_agent(extraction_prompt)).strip()
            mission_update, suggested_questions = _parse_extraction(raw_extraction)
        except Exception:
            logger.exception("Branding extraction LLM failed; conversation reply unaffected")

        if not suggested_questions:
            suggested_questions = list(_DEFAULT_SUGGESTIONS)

        updated_mission = _merge_mission_update(current_mission, mission_update)
        return reply_text, updated_mission, suggested_questions
