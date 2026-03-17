"""Branding assistant agent: conversational flow and mission extraction."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from branding_team.models import BrandingMission, ColorPalette

from .prompts import SYSTEM_PROMPT, USER_TURN_TEMPLATE


def _parse_mission_and_suggestions(response: str) -> Tuple[str, Dict[str, Any], List[str]]:
    """Extract reply text, mission JSON object, and suggestions array from LLM response."""
    reply_text = response.strip()
    mission_update: Dict[str, Any] = {}
    suggested_questions: List[str] = []

    # Find ```mission ... ``` block
    mission_match = re.search(r"```mission\s*\n?(.*?)```", response, re.DOTALL | re.IGNORECASE)
    if mission_match:
        try:
            mission_update = json.loads(mission_match.group(1).strip())
            if not isinstance(mission_update, dict):
                mission_update = {}
        except (json.JSONDecodeError, TypeError):
            pass
        # Reply is everything before the mission block
        reply_text = response[: mission_match.start()].strip()
    else:
        # Try generic ```json for mission
        json_match = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*```", response, re.DOTALL)
        if json_match:
            try:
                mission_update = json.loads(json_match.group(1).strip())
                if isinstance(mission_update, dict):
                    reply_text = response[: json_match.start()].strip()
            except (json.JSONDecodeError, TypeError):
                pass

    # Find ```suggestions ... ``` block
    sugg_match = re.search(r"```suggestions\s*\n?(.*?)```", response, re.DOTALL | re.IGNORECASE)
    if sugg_match:
        try:
            raw = sugg_match.group(1).strip()
            suggested_questions = json.loads(raw)
            if isinstance(suggested_questions, list):
                suggested_questions = [str(s) for s in suggested_questions if s][:4]
            else:
                suggested_questions = []
        except (json.JSONDecodeError, TypeError):
            pass

    if not reply_text:
        reply_text = response.strip() or "I'm here to help you build your brand. Tell me a bit about your company."
    if not suggested_questions:
        suggested_questions = [
            "What's your company or product name?",
            "Who is your target audience?",
            "What 3–5 values define your brand?",
        ]
    return reply_text, mission_update, suggested_questions


def _merge_mission_update(current: BrandingMission, update: Dict[str, Any]) -> BrandingMission:
    """Merge update dict into current mission; only set keys that are present and non-empty where we care."""
    data = current.model_dump()

    # String fields
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

    # List-of-string fields
    for key in ("values", "differentiators", "existing_brand_material", "color_inspiration"):
        if key not in update:
            continue
        val = update[key]
        if isinstance(val, list):
            data[key] = [str(x) for x in val if x]

    # Color palettes — list of palette objects
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

    # Selected palette index — int or None
    if "selected_palette_index" in update:
        val = update["selected_palette_index"]
        if val is None:
            data["selected_palette_index"] = None
        elif isinstance(val, int) and 0 <= val < len(data.get("color_palettes", [])):
            data["selected_palette_index"] = val

    return BrandingMission(**data)


class BrandingAssistantAgent:
    """Conversational agent that guides the user through brand creation and emits structured mission updates."""

    def __init__(self, llm=None):  # noqa: ANN001
        if llm is None:
            from llm_service import get_client
            self._llm = get_client("branding_assistant")
        else:
            self._llm = llm

    def respond(
        self,
        messages: List[Tuple[str, str]],
        current_mission: BrandingMission,
        user_message: str,
    ) -> Tuple[str, BrandingMission, List[str]]:
        """
        Produce assistant reply, updated mission, and suggested follow-up questions.

        messages: list of (role, content) in order (e.g. [("assistant", "Hi..."), ("user", "Hi"), ...]).
        current_mission: current mission state.
        user_message: latest user message.

        Returns (reply_text, updated_mission, suggested_questions).
        """
        conversation_lines = []
        for role, content in messages:
            prefix = "Assistant: " if role == "assistant" else "User: "
            conversation_lines.append(f"{prefix}{content}")
        conversation_history = "\n".join(conversation_lines) if conversation_lines else "(No prior messages)"

        prompt = USER_TURN_TEMPLATE.format(
            company_name=current_mission.company_name or "",
            company_description=current_mission.company_description or "",
            target_audience=current_mission.target_audience or "",
            values=current_mission.values or [],
            differentiators=current_mission.differentiators or [],
            desired_voice=current_mission.desired_voice or "",
            existing_brand_material=current_mission.existing_brand_material or [],
            color_inspiration=current_mission.color_inspiration or [],
            color_palettes=[p.model_dump() if hasattr(p, "model_dump") else p for p in (current_mission.color_palettes or [])],
            selected_palette_index=current_mission.selected_palette_index,
            visual_style=current_mission.visual_style or "",
            typography_preference=current_mission.typography_preference or "",
            interface_density=current_mission.interface_density or "",
            conversation_history=conversation_history,
            user_message=user_message,
        )

        try:
            raw = self._llm.complete(
                prompt,
                temperature=0.5,
                system_prompt=SYSTEM_PROMPT,
            )
        except Exception:
            reply_text = "I'm here to help build your brand. Could you tell me your company name and what you do?"
            suggested_questions = [
                "What's your company name?",
                "Who is your target audience?",
                "What values matter most?",
            ]
            return reply_text, current_mission, suggested_questions

        reply_text, mission_update, suggested_questions = _parse_mission_and_suggestions(raw)
        updated_mission = _merge_mission_update(current_mission, mission_update)
        return reply_text, updated_mission, suggested_questions
