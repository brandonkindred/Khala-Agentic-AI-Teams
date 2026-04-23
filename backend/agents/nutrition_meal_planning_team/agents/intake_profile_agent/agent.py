"""Intake profile agent: validates and completes client profile for nutrition/meal planning."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from strands import Agent

from llm_service import extract_json_from_response

from ...models import ClientProfile, ProfileUpdateRequest

# Re-export the fallback merger under its historical private name so
# the existing import surface stays stable. Pure-logic lives in
# ``structural`` (no strands dependency) per SPEC-002 W4.
from .restriction_hook import apply_resolver as _apply_resolver
from .structural import merge_profile_structural as _merge_profile_structural  # noqa: F401

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an expert intake specialist for a personal nutrition and meal planning service.
Your job is to take partial or full client information and produce a complete, consistent client profile as JSON.

The profile must include:
- household: number_of_people (int), description (e.g. "solo", "couple", "family of 4"), ages_if_relevant (list of strings), members (optional list of {name, age_or_role, dietary_needs, allergies, notes} per person)
- dietary_needs: list of strings (e.g. vegetarian, vegan, keto, low-sodium, diabetic-friendly)
- allergies_and_intolerances: list of strings (e.g. nuts, shellfish, gluten)
- lifestyle: max_cooking_time_minutes (int or null), lunch_context ("office" or "remote"), equipment_constraints (list), other_constraints (string)
- preferences: cuisines_liked, cuisines_disliked, ingredients_disliked (lists), preferences_free_text (string)
- goals: goal_type (e.g. maintain, lose_weight, gain_weight, muscle), target_weight_kg (number or null), rate_kg_per_week (number 0..1 or null), started_at (ISO string or null), notes (string)
- biometrics: sex ("female"|"male"|"other"|"unspecified"), age_years (integer 2..120 or null), height_cm (number 50..260 or null), weight_kg (number 20..400 or null), body_fat_pct (number 3..75 or null), activity_level ("sedentary"|"light"|"moderate"|"active"|"very_active"), timezone (IANA zone), preferred_units ("metric"|"imperial")
- clinical: conditions (list of canonical tags, e.g. "hypertension", "t2_diabetes"; unknown items belong in conditions_freetext), medications (class tags, e.g. "warfarin", "ssri"; unknown items belong in medications_freetext), reproductive_state ("none"|"pregnant_t1"|"pregnant_t2"|"pregnant_t3"|"lactating"|"postpartum"), ed_history_flag (boolean)

UNIT RULES — strict:
- Height is always in centimeters. Never accept or emit feet / inches.
- Weight is always in kilograms. Never accept or emit pounds.
- If the user clearly specified imperial units, the upstream API has already converted them; trust what you receive.

If the user did not specify something, infer sensible defaults or leave empty lists/empty strings. Never invent allergies or medical conditions. Never invent biometrics — leave them null if the user has not provided them.
Output only valid JSON matching the structure above, with no markdown or explanation."""


class IntakeProfileAgent:
    """Validates and completes client profile from structured input using LLM."""

    def __init__(self, model: Any) -> None:
        self._agent = Agent(model=model, system_prompt=SYSTEM_PROMPT)

    def run(
        self,
        client_id: str,
        update: Optional[ProfileUpdateRequest] = None,
        current_profile: Optional[ClientProfile] = None,
    ) -> ClientProfile:
        """
        Merge current profile with update (if any), ask LLM to validate/complete, return ClientProfile.
        """
        profile = self._llm_merge(client_id, update, current_profile)
        return _apply_resolver(profile)

    def _llm_merge(
        self,
        client_id: str,
        update: Optional[ProfileUpdateRequest],
        current_profile: Optional[ClientProfile],
    ) -> ClientProfile:
        current_dict: Dict[str, Any] = {}
        if current_profile:
            current_dict = current_profile.model_dump()
        update_dict: Dict[str, Any] = {}
        if update:
            update_dict = update.model_dump(exclude_none=True)

        prompt = (
            "Current profile (may be empty):\n"
            + json.dumps(current_dict, indent=2)
            + "\n\nRequested updates (partial):\n"
            + json.dumps(update_dict, indent=2)
            + "\n\nProduce a single complete client profile JSON. Preserve any existing fields not being updated; merge updates; fill missing with defaults."
            + "\n\nRespond with valid JSON only, no markdown fences."
        )

        try:
            result = self._agent(prompt)
            raw = str(result).strip()
            # Use llm_service's canonical JSON extractor (SPEC-002 W4):
            # tolerates code fences, trailing text, and common model quirks
            # without the regex-strip-and-hope approach this module used
            # previously.
            data = extract_json_from_response(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("Intake profile JSON extraction failed: %s", e)
            return _merge_profile_structural(client_id, current_profile, update)
        except Exception as e:
            logger.warning("Intake profile LLM call failed, using structural merge: %s", e)
            return _merge_profile_structural(client_id, current_profile, update)

        data["client_id"] = client_id
        try:
            return ClientProfile.model_validate(data)
        except Exception as e:
            # Pydantic rejected the LLM's output (e.g. implausible
            # weight, bad activity_level). Fall back to structural
            # merge so the user's write still persists.
            logger.warning("Intake profile LLM output failed schema validation: %s", e)
            return _merge_profile_structural(client_id, current_profile, update)
