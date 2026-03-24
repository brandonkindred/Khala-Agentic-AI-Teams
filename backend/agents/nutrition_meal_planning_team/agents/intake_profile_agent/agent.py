"""Intake profile agent: validates and completes client profile for nutrition/meal planning."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from llm_service import LLMClient, LLMError, LLMJsonParseError

from ...models import ClientProfile, ProfileUpdateRequest

logger = logging.getLogger(__name__)


def _merge_profile_structural(
    client_id: str,
    current: Optional[ClientProfile],
    update: Optional[ProfileUpdateRequest],
) -> ClientProfile:
    """Apply update onto current without LLM (fallback when the model is unavailable)."""
    data: Dict[str, Any] = (
        current.model_dump() if current else ClientProfile(client_id=client_id).model_dump()
    )
    if not update:
        data["client_id"] = client_id
        return ClientProfile.model_validate(data)
    patch = update.model_dump(exclude_none=True)
    for key in ("dietary_needs", "allergies_and_intolerances"):
        if key in patch:
            data[key] = patch[key]
    for key in ("household", "lifestyle", "preferences", "goals"):
        if key not in patch:
            continue
        sub = patch[key]
        if sub is None:
            continue
        existing = data.get(key) or {}
        if isinstance(existing, dict) and isinstance(sub, dict):
            data[key] = {**existing, **sub}
        else:
            data[key] = sub
    data["client_id"] = client_id
    return ClientProfile.model_validate(data)


SYSTEM_PROMPT = """You are an expert intake specialist for a personal nutrition and meal planning service.
Your job is to take partial or full client information and produce a complete, consistent client profile as JSON.

The profile must include:
- household: number_of_people (int), description (e.g. "solo", "couple", "family of 4"), ages_if_relevant (list of strings), members (optional list of {name, age_or_role, dietary_needs, allergies, notes} per person)
- dietary_needs: list of strings (e.g. vegetarian, vegan, keto, low-sodium, diabetic-friendly)
- allergies_and_intolerances: list of strings (e.g. nuts, shellfish, gluten)
- lifestyle: max_cooking_time_minutes (int or null), lunch_context ("office" or "remote"), equipment_constraints (list), other_constraints (string)
- preferences: cuisines_liked, cuisines_disliked, ingredients_disliked (lists), preferences_free_text (string)
- goals: goal_type (e.g. maintain, lose_weight, gain_weight, muscle), notes (string)

If the user did not specify something, infer sensible defaults or leave empty lists/empty strings. Never invent allergies.
Output only valid JSON matching the structure above, with no markdown or explanation."""


class IntakeProfileAgent:
    """Validates and completes client profile from structured input using LLM."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(
        self,
        client_id: str,
        update: Optional[ProfileUpdateRequest] = None,
        current_profile: Optional[ClientProfile] = None,
    ) -> ClientProfile:
        """
        Merge current profile with update (if any), ask LLM to validate/complete, return ClientProfile.
        """
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
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.2,
                system_prompt=SYSTEM_PROMPT,
                expected_keys=[
                    "household",
                    "dietary_needs",
                    "allergies_and_intolerances",
                    "lifestyle",
                    "preferences",
                    "goals",
                ],
            )
        except LLMJsonParseError as e:
            logger.warning("Intake profile JSON extraction failed: %s", e)
            return _merge_profile_structural(client_id, current_profile, update)
        except LLMError as e:
            logger.warning("Intake profile LLM call failed, using structural merge: %s", e)
            return _merge_profile_structural(client_id, current_profile, update)

        data["client_id"] = client_id
        return ClientProfile.model_validate(data)
