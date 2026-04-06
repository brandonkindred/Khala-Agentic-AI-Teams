"""Adapter to fetch and validate a Brand from the branding team API."""

from __future__ import annotations

import logging
import os
from typing import List, Optional

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BrandNotFoundError(Exception):
    """Raised when the requested brand does not exist."""

    def __init__(self, client_id: str, brand_id: str):
        self.client_id = client_id
        self.brand_id = brand_id
        super().__init__(f"Brand '{brand_id}' not found for client '{client_id}'.")


class BrandIncompleteError(Exception):
    """Raised when the brand exists but required phases are not yet complete."""

    def __init__(
        self, client_id: str, brand_id: str, missing_phases: List[str], current_phase: str
    ):
        self.client_id = client_id
        self.brand_id = brand_id
        self.missing_phases = missing_phases
        self.current_phase = current_phase
        super().__init__(
            f"Brand '{brand_id}' is missing required phases: {', '.join(missing_phases)}. "
            f"Current phase: {current_phase}."
        )


# ---------------------------------------------------------------------------
# BrandContext — the subset of brand data social marketing needs
# ---------------------------------------------------------------------------


class BrandContext(BaseModel):
    """Brand data extracted and synthesized for social marketing use."""

    brand_name: str
    target_audience: str
    voice_and_tone: str
    brand_guidelines: str
    brand_objectives: str
    messaging_pillars: List[str] = []
    brand_story: str = ""
    tagline: str = ""

    def to_brand_goals(
        self, *, goals: List[str], cadence_posts_per_day: int, duration_days: int
    ) -> BrandGoals:  # noqa: F821
        """Convert to a ``BrandGoals`` instance, merging campaign-specific fields."""
        from social_media_marketing_team.models import BrandGoals

        return BrandGoals(
            brand_name=self.brand_name,
            target_audience=self.target_audience,
            voice_and_tone=self.voice_and_tone,
            brand_guidelines=self.brand_guidelines,
            brand_objectives=self.brand_objectives,
            messaging_pillars=self.messaging_pillars,
            brand_story=self.brand_story,
            tagline=self.tagline,
            goals=goals,
            cadence_posts_per_day=cadence_posts_per_day,
            duration_days=duration_days,
        )


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

_REQUIRED_PHASES = ["strategic_core", "narrative_messaging"]


def _base_url() -> Optional[str]:
    return os.environ.get("UNIFIED_API_BASE_URL") or os.environ.get("SOCIAL_MARKETING_BRANDING_URL")


def fetch_brand(client_id: str, brand_id: str) -> dict:
    """Fetch a brand from the branding team API. Returns the raw JSON dict.

    Raises ``BrandNotFoundError`` when the brand or client does not exist.
    Raises ``RuntimeError`` on network / unexpected errors.
    """
    base = _base_url()
    if not base:
        raise RuntimeError(
            "Branding API URL not configured. Set UNIFIED_API_BASE_URL or SOCIAL_MARKETING_BRANDING_URL."
        )
    url = f"{base.rstrip('/')}/api/branding/clients/{client_id}/brands/{brand_id}"
    try:
        with httpx.Client(timeout=30.0) as client_http:
            resp = client_http.get(url)
            if resp.status_code == 404:
                raise BrandNotFoundError(client_id, brand_id)
            resp.raise_for_status()
            return resp.json()
    except BrandNotFoundError:
        raise
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        raise RuntimeError(f"Failed to fetch brand from branding API: {e}") from e


# ---------------------------------------------------------------------------
# Validation + extraction
# ---------------------------------------------------------------------------


def validate_brand_for_social_marketing(
    brand_data: dict, client_id: str, brand_id: str
) -> BrandContext:
    """Validate that a brand has the required phases and extract a ``BrandContext``.

    Raises ``BrandIncompleteError`` if Phase 1 or Phase 2 outputs are missing.
    """
    latest_output = brand_data.get("latest_output")
    if not latest_output or not isinstance(latest_output, dict):
        raise BrandIncompleteError(
            client_id, brand_id, missing_phases=list(_REQUIRED_PHASES), current_phase="draft"
        )

    missing = [
        phase for phase in _REQUIRED_PHASES if not isinstance(latest_output.get(phase), dict)
    ]
    if missing:
        current_phase = brand_data.get("current_phase", "unknown")
        raise BrandIncompleteError(
            client_id, brand_id, missing_phases=missing, current_phase=current_phase
        )

    mission = brand_data.get("mission") or {}
    strategic_core = latest_output["strategic_core"]
    narrative = latest_output["narrative_messaging"]

    return _extract_brand_context(brand_data, mission, strategic_core, narrative)


def _extract_brand_context(
    brand_data: dict, mission: dict, strategic_core: dict, narrative: dict
) -> BrandContext:
    """Synthesize a ``BrandContext`` from raw branding API data."""
    brand_name = brand_data.get("name", mission.get("company_name", "Brand"))

    # Target audience -- combine mission + segment details
    audience_parts = [mission.get("target_audience", "")]
    for seg in strategic_core.get("target_audience_segments", []):
        if isinstance(seg, dict):
            name = seg.get("name", "")
            desc = seg.get("description", "")
            if name:
                audience_parts.append(f"{name}: {desc}" if desc else name)
    target_audience = "; ".join(p for p in audience_parts if p)

    # Voice and tone -- combine mission voice + archetype traits
    voice_parts = [mission.get("desired_voice", "")]
    for archetype in narrative.get("brand_archetypes", []):
        if isinstance(archetype, dict):
            traits = archetype.get("personality_traits", [])
            if traits:
                voice_parts.append(", ".join(traits[:5]))
    voice_and_tone = "; ".join(p for p in voice_parts if p) or "professional, clear, and human"

    # Brand guidelines -- synthesize from strategic core + narrative
    guideline_parts = []
    if strategic_core.get("positioning_statement"):
        guideline_parts.append(f"Positioning: {strategic_core['positioning_statement']}")
    for val in strategic_core.get("core_values", []):
        if isinstance(val, dict) and val.get("value"):
            guideline_parts.append(
                f"Value -- {val['value']}: {val.get('behavioral_definition', '')}"
            )
    for pillar in strategic_core.get("differentiation_pillars", []):
        if isinstance(pillar, dict) and pillar.get("pillar"):
            guideline_parts.append(
                f"Differentiator -- {pillar['pillar']}: {pillar.get('competitive_context', '')}"
            )
    for aud_map in narrative.get("audience_message_maps", []):
        if isinstance(aud_map, dict) and aud_map.get("tone_adjustments"):
            guideline_parts.append(
                f"Tone for {aud_map.get('audience_segment', 'audience')}: {aud_map['tone_adjustments']}"
            )
    brand_guidelines = "\n".join(guideline_parts)

    # Brand objectives -- from strategic core purpose/mission/vision
    objective_parts = []
    if strategic_core.get("brand_purpose"):
        objective_parts.append(f"Purpose: {strategic_core['brand_purpose']}")
    if strategic_core.get("mission_statement"):
        objective_parts.append(f"Mission: {strategic_core['mission_statement']}")
    if strategic_core.get("vision_statement"):
        objective_parts.append(f"Vision: {strategic_core['vision_statement']}")
    if strategic_core.get("brand_promise"):
        objective_parts.append(f"Promise: {strategic_core['brand_promise']}")
    brand_objectives = "\n".join(objective_parts)

    # Messaging pillars
    messaging_pillars = []
    for mp in narrative.get("messaging_framework", []):
        if isinstance(mp, dict) and mp.get("pillar"):
            messaging_pillars.append(mp["pillar"])

    return BrandContext(
        brand_name=brand_name,
        target_audience=target_audience,
        voice_and_tone=voice_and_tone,
        brand_guidelines=brand_guidelines,
        brand_objectives=brand_objectives,
        messaging_pillars=messaging_pillars,
        brand_story=narrative.get("brand_story", ""),
        tagline=narrative.get("tagline", ""),
    )
