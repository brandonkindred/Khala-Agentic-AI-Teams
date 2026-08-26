"""Branding assistant agent: two-stage conversation + silent mission extraction.

Stage 1 — Conversation: a Strands `Agent` configured as a senior brand strategist.
It replies in pure natural language (no JSON, no field names). This is what the
user reads.

Stage 2 — Extraction: a second Strands `Agent`, built with
`structured_output=MissionUpdate`, reads the same turn plus the strategist's
reply and returns a validated `MissionUpdate` (mission deltas +
`suggested_questions`). The user never sees this output. Decoupling
extraction from conversation means the user-facing reply is never
constrained or contaminated by structured-output formatting requirements.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Tuple

from branding_team.models import BrandingMission
from shared.env_config import env_int

from .models import MissionUpdate
from .prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_TEMPLATE,
    SYSTEM_PROMPT,
    USER_TURN_TEMPLATE,
)

logger = logging.getLogger(__name__)

_DEFAULT_SUGGESTIONS = [
    "What's your company or product name?",
    "Who is your target audience?",
    "What 3–5 values define your brand?",
]


def _coerce_suggestions(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(s).strip() for s in value if str(s).strip()][:4]
    return []


# Single source of truth for the mission fields the assistant reads/writes,
# grouped by how they merge. ``_brief_value``, ``_merge_mission_update`` and
# ``_MISSION_FIELD_NAMES`` all derive from these so a new field is added once.
_MISSION_STR_FIELDS = (
    "company_name",
    "company_description",
    "target_audience",
    "desired_voice",
    "visual_style",
    "typography_preference",
    "interface_density",
)
_MISSION_LIST_FIELDS = (
    "values",
    "differentiators",
    "existing_brand_material",
    "color_inspiration",
)
# Structured fields with bespoke handling (not plain str/list).
_MISSION_STRUCTURED_FIELDS = ("color_palettes", "selected_palette_index")

_MISSION_FIELD_NAMES = (
    set(_MISSION_STR_FIELDS) | set(_MISSION_LIST_FIELDS) | set(_MISSION_STRUCTURED_FIELDS)
)

# Display order for the brief block rendered into the prompt templates.
_MISSION_BRIEF_ORDER = (
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
)


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
                    stripped,
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
                stripped,
            )
            return ""

    return stripped


def _merge_mission_update(current: BrandingMission, update: MissionUpdate) -> BrandingMission:
    """Merge one turn's ``MissionUpdate`` deltas into ``current``.

    Only fields the extractor actually populated are applied; an unset
    (``None``) field on ``update`` means "nothing new this turn" and leaves
    the corresponding ``current`` field unchanged.
    """
    data = current.model_dump()

    for key in _MISSION_STR_FIELDS:
        val = getattr(update, key)
        if isinstance(val, str) and val.strip():
            data[key] = val.strip()

    for key in _MISSION_LIST_FIELDS:
        val = getattr(update, key)
        if isinstance(val, list):
            # The extractor emits the complete list each turn, so we replace
            # (not extend) — that already bounds growth. Dedupe while keeping
            # first-seen order so a model that repeats an item doesn't put
            # duplicates in the mission brief.
            data[key] = list(dict.fromkeys(str(x) for x in val if x))

    if isinstance(update.color_palettes, list) and update.color_palettes:
        data["color_palettes"] = [p.model_dump() for p in update.color_palettes]

    if isinstance(update.selected_palette_index, int) and 0 <= update.selected_palette_index < len(
        data.get("color_palettes", [])
    ):
        data["selected_palette_index"] = update.selected_palette_index

    return BrandingMission(**data)


def _brief_value(mission: BrandingMission, field: str) -> Any:
    """Display value for one mission *field* in the prompt brief.

    Preconditions:
        ``field`` is a member of ``_MISSION_BRIEF_ORDER``.
    Postconditions:
        Returns the field's value normalized for display — ``""`` for empty
        strings, ``[]`` for empty lists, dumped dicts for color palettes, and
        the raw value (possibly None) for ``selected_palette_index``.
    """
    value = getattr(mission, field)
    if field == "color_palettes":
        return [p.model_dump() if hasattr(p, "model_dump") else p for p in (value or [])]
    if field == "selected_palette_index":
        return value
    if field in _MISSION_LIST_FIELDS:
        return value or []
    return value or ""


def _format_brief_block(mission: BrandingMission) -> str:
    """Render the mission brief as the ``- field: value`` block the prompt
    templates interpolate via ``{brief_block}``.

    Preconditions:
        ``mission`` is a BrandingMission.
    Postconditions:
        Returns a newline-joined block with exactly one line per field in
        ``_MISSION_BRIEF_ORDER`` (the shared order, so it can't drift from
        ``_merge_mission_update``), without allocating an intermediate dict.
    """
    return "\n".join(f"- {field}: {_brief_value(mission, field)}" for field in _MISSION_BRIEF_ORDER)


def _history_window() -> int:
    """Max prior turns fed to the LLM (env-tunable, clamped to >= 1).

    Both LLM stages re-send the conversation history every turn, so an
    uncapped history makes each call grow without bound in tokens, latency,
    and memory. Capping to the most recent turns keeps per-turn cost roughly
    constant while preserving the immediate context the strategist needs.
    """
    return env_int("BRANDING_ASSISTANT_HISTORY_WINDOW", 20, floor=1)


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

    def __init__(self, conversation_llm=None, extraction_llm=None, llm=None):  # noqa: ANN001
        # Backward compatibility: the legacy ``llm=`` argument drives BOTH
        # stages so tests and offline callers that inject a fake never hit a
        # live LLM. New code should pass ``conversation_llm`` and
        # ``extraction_llm`` separately when it needs distinct backends.
        if llm is not None:
            if conversation_llm is None:
                conversation_llm = llm
            if extraction_llm is None:
                extraction_llm = llm

        if conversation_llm is None or extraction_llm is None:
            if conversation_llm is None:
                from branding_team.graphs.shared import build_agent

                # output_mode="text": the conversation stage must produce
                # free-form prose. Without this opt-in the adapter would
                # force JSON output on the wire and the user-facing reply
                # would be empty or JSON-shaped.
                conversation_llm = build_agent(
                    name="conversation",
                    agent_key="branding_assistant",
                    output_mode="text",
                    system_prompt=SYSTEM_PROMPT,
                )
            if extraction_llm is None:
                from branding_team.graphs.shared import build_agent

                # structured_output=MissionUpdate: the Strands adapter routes
                # through its tool-calling flow and validates the response
                # into a MissionUpdate instance before we ever see it.
                extraction_llm = build_agent(
                    name="extraction",
                    agent_key="branding_assistant",
                    system_prompt=EXTRACTION_SYSTEM_PROMPT,
                    structured_output=MissionUpdate,
                )

        self._conversation_agent = conversation_llm
        self._extraction_agent = extraction_llm

    def respond(
        self,
        messages: List[Tuple[str, str]],
        current_mission: BrandingMission,
        user_message: str,
    ) -> Tuple[str, BrandingMission, List[str], bool]:
        """Produce assistant reply, updated mission, suggested follow-up questions, and a degradation flag.

        messages: conversation history as list of (role, content) tuples.
        current_mission: mission state before this turn.
        user_message: latest user message.

        The returned ``degraded`` flag is ``True`` iff this turn's mission
        extraction was lost — either the extractor call raised, or its
        result didn't carry a valid ``MissionUpdate`` — so the caller can
        surface that degradation instead of it being indistinguishable from
        the extractor legitimately having nothing new to report.
        """
        brief_block = _format_brief_block(current_mission)
        history = _format_history(messages)

        # ── Stage 1: conversational strategist reply ───────────────────────
        conversation_prompt = USER_TURN_TEMPLATE.format(
            brief_block=brief_block,
            conversation_history=history,
            user_message=user_message,
        )

        logger.info(
            "BrandingAssistant two-stage respond: conversation stage starting (history_msgs=%d)",
            len(messages),
        )
        try:
            raw_reply = str(self._conversation_agent(conversation_prompt)).strip()
            logger.info(
                "BrandingAssistant conversation stage raw response: %r",
                raw_reply,
            )
        except Exception:
            logger.exception("Branding conversation LLM failed")
            return (
                "I'm here to help build your brand. Could you tell me your company name and what you do?",
                current_mission,
                list(_DEFAULT_SUGGESTIONS),
                True,
            )

        reply_text = _strip_accidental_json(raw_reply)
        if not reply_text:
            reply_text = (
                "Thanks — let me make sure I'm following you. Could you tell me a bit more about "
                "what this brand is for and who you want it to resonate with?"
            )

        # ── Stage 2: silent extractor ──────────────────────────────────────
        extraction_prompt = EXTRACTION_USER_TEMPLATE.format(
            brief_block=brief_block,
            conversation_history=history,
            user_message=user_message,
            assistant_reply=reply_text,
        )

        mission_update = MissionUpdate()
        suggested_questions: List[str] = []
        degraded = False
        try:
            extraction_result = self._extraction_agent(extraction_prompt)
            structured = getattr(extraction_result, "structured_output", None)
            if not isinstance(structured, MissionUpdate):
                raise ValueError(
                    "extraction agent did not return a MissionUpdate "
                    f"(got {type(structured).__name__ if structured is not None else 'None'})"
                )
            mission_update = structured
            suggested_questions = _coerce_suggestions(mission_update.suggested_questions)
        except Exception:
            logger.exception("Branding extraction LLM failed; conversation reply unaffected")
            degraded = True

        if not suggested_questions:
            suggested_questions = list(_DEFAULT_SUGGESTIONS)

        updated_mission = _merge_mission_update(current_mission, mission_update)
        return reply_text, updated_mission, suggested_questions, degraded
