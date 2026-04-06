"""Unit tests for the branding adapter validation and extraction logic."""

import pytest

from social_media_marketing_team.adapters.branding import (
    BrandContext,
    BrandIncompleteError,
    validate_brand_for_social_marketing,
)


def _full_brand_data() -> dict:
    """Return a realistic brand API response with both required phases."""
    return {
        "id": "brand_1",
        "client_id": "client_1",
        "name": "Acme Corp",
        "current_phase": "narrative_messaging",
        "mission": {
            "company_name": "Acme Corp",
            "company_description": "Developer tools",
            "target_audience": "B2B engineering leaders",
            "desired_voice": "clear, confident, human",
            "values": ["transparency", "simplicity"],
            "differentiators": ["ease of use"],
        },
        "latest_output": {
            "strategic_core": {
                "brand_purpose": "Empower developers to ship faster",
                "mission_statement": "Make developer tools that just work",
                "vision_statement": "A world where dev tools disappear into the background",
                "brand_promise": "You'll ship in half the time",
                "positioning_statement": "The simplest way to build and deploy",
                "core_values": [
                    {
                        "value": "Simplicity",
                        "behavioral_definition": "Remove unnecessary complexity",
                    },
                    {"value": "Speed", "behavioral_definition": "Ship fast, iterate faster"},
                ],
                "target_audience_segments": [
                    {
                        "name": "Startup CTOs",
                        "description": "Technical founders at seed-stage companies",
                    },
                    {
                        "name": "Platform Engineers",
                        "description": "Infra engineers at mid-size companies",
                    },
                ],
                "differentiation_pillars": [
                    {
                        "pillar": "Ease of use",
                        "competitive_context": "Others require 10x more config",
                    },
                ],
            },
            "narrative_messaging": {
                "brand_story": "Acme was born from frustration with overcomplicated tools.",
                "tagline": "Developer tools that just work",
                "brand_archetypes": [
                    {
                        "archetype": "Creator",
                        "personality_traits": ["inventive", "expressive", "bold"],
                    },
                ],
                "messaging_framework": [
                    {
                        "pillar": "Developer empowerment",
                        "key_message": "Ship faster",
                        "proof_points": [],
                    },
                    {
                        "pillar": "Simplicity first",
                        "key_message": "Less config, more code",
                        "proof_points": [],
                    },
                ],
                "audience_message_maps": [
                    {
                        "audience_segment": "Startup CTOs",
                        "primary_message": "Move fast without breaking things",
                        "tone_adjustments": "Direct and pragmatic",
                    },
                ],
                "elevator_pitches": [],
                "boilerplate_variants": [],
                "persona_profiles": [],
            },
        },
    }


class TestValidateBrandHappyPath:
    def test_extracts_brand_name(self) -> None:
        ctx = validate_brand_for_social_marketing(_full_brand_data(), "c1", "b1")
        assert ctx.brand_name == "Acme Corp"

    def test_extracts_target_audience_with_segments(self) -> None:
        ctx = validate_brand_for_social_marketing(_full_brand_data(), "c1", "b1")
        assert "B2B engineering leaders" in ctx.target_audience
        assert "Startup CTOs" in ctx.target_audience
        assert "Platform Engineers" in ctx.target_audience

    def test_extracts_voice_and_tone(self) -> None:
        ctx = validate_brand_for_social_marketing(_full_brand_data(), "c1", "b1")
        assert "clear, confident, human" in ctx.voice_and_tone
        assert "inventive" in ctx.voice_and_tone

    def test_extracts_brand_guidelines(self) -> None:
        ctx = validate_brand_for_social_marketing(_full_brand_data(), "c1", "b1")
        assert "Positioning:" in ctx.brand_guidelines
        assert "Simplicity" in ctx.brand_guidelines
        assert "Ease of use" in ctx.brand_guidelines
        assert "Direct and pragmatic" in ctx.brand_guidelines

    def test_extracts_brand_objectives(self) -> None:
        ctx = validate_brand_for_social_marketing(_full_brand_data(), "c1", "b1")
        assert "Purpose:" in ctx.brand_objectives
        assert "Mission:" in ctx.brand_objectives
        assert "Vision:" in ctx.brand_objectives
        assert "Promise:" in ctx.brand_objectives

    def test_extracts_messaging_pillars(self) -> None:
        ctx = validate_brand_for_social_marketing(_full_brand_data(), "c1", "b1")
        assert ctx.messaging_pillars == ["Developer empowerment", "Simplicity first"]

    def test_extracts_brand_story_and_tagline(self) -> None:
        ctx = validate_brand_for_social_marketing(_full_brand_data(), "c1", "b1")
        assert "frustration" in ctx.brand_story
        assert ctx.tagline == "Developer tools that just work"

    def test_returns_brand_context_type(self) -> None:
        ctx = validate_brand_for_social_marketing(_full_brand_data(), "c1", "b1")
        assert isinstance(ctx, BrandContext)


class TestValidateBrandMissingPhases:
    def test_raises_when_latest_output_is_none(self) -> None:
        data = _full_brand_data()
        data["latest_output"] = None
        with pytest.raises(BrandIncompleteError) as exc_info:
            validate_brand_for_social_marketing(data, "c1", "b1")
        assert set(exc_info.value.missing_phases) == {"strategic_core", "narrative_messaging"}

    def test_raises_when_latest_output_missing(self) -> None:
        data = _full_brand_data()
        del data["latest_output"]
        with pytest.raises(BrandIncompleteError) as exc_info:
            validate_brand_for_social_marketing(data, "c1", "b1")
        assert len(exc_info.value.missing_phases) == 2

    def test_raises_when_strategic_core_missing(self) -> None:
        data = _full_brand_data()
        del data["latest_output"]["strategic_core"]
        with pytest.raises(BrandIncompleteError) as exc_info:
            validate_brand_for_social_marketing(data, "c1", "b1")
        assert exc_info.value.missing_phases == ["strategic_core"]

    def test_raises_when_narrative_messaging_missing(self) -> None:
        data = _full_brand_data()
        del data["latest_output"]["narrative_messaging"]
        with pytest.raises(BrandIncompleteError) as exc_info:
            validate_brand_for_social_marketing(data, "c1", "b1")
        assert exc_info.value.missing_phases == ["narrative_messaging"]

    def test_raises_when_phase_is_non_dict(self) -> None:
        data = _full_brand_data()
        data["latest_output"]["strategic_core"] = "not a dict"
        with pytest.raises(BrandIncompleteError) as exc_info:
            validate_brand_for_social_marketing(data, "c1", "b1")
        assert "strategic_core" in exc_info.value.missing_phases

    def test_raises_when_phase_is_boolean(self) -> None:
        data = _full_brand_data()
        data["latest_output"]["narrative_messaging"] = True
        with pytest.raises(BrandIncompleteError) as exc_info:
            validate_brand_for_social_marketing(data, "c1", "b1")
        assert "narrative_messaging" in exc_info.value.missing_phases

    def test_carries_current_phase(self) -> None:
        data = _full_brand_data()
        data["current_phase"] = "strategic_core"
        del data["latest_output"]["narrative_messaging"]
        with pytest.raises(BrandIncompleteError) as exc_info:
            validate_brand_for_social_marketing(data, "c1", "b1")
        assert exc_info.value.current_phase == "strategic_core"


class TestValidateBrandEdgeCases:
    def test_empty_mission_produces_defaults(self) -> None:
        data = _full_brand_data()
        data["mission"] = {}
        ctx = validate_brand_for_social_marketing(data, "c1", "b1")
        assert ctx.brand_name == "Acme Corp"  # falls back to data["name"]
        assert ctx.voice_and_tone  # should still have archetype traits

    def test_empty_segments_and_archetypes(self) -> None:
        data = _full_brand_data()
        data["latest_output"]["strategic_core"]["target_audience_segments"] = []
        data["latest_output"]["narrative_messaging"]["brand_archetypes"] = []
        data["mission"]["desired_voice"] = ""
        ctx = validate_brand_for_social_marketing(data, "c1", "b1")
        assert ctx.voice_and_tone == "professional, clear, and human"

    def test_missing_name_falls_back_to_mission_company_name(self) -> None:
        data = _full_brand_data()
        del data["name"]
        ctx = validate_brand_for_social_marketing(data, "c1", "b1")
        assert ctx.brand_name == "Acme Corp"

    def test_missing_name_and_mission_falls_back_to_brand(self) -> None:
        data = _full_brand_data()
        del data["name"]
        data["mission"] = {}
        ctx = validate_brand_for_social_marketing(data, "c1", "b1")
        assert ctx.brand_name == "Brand"


class TestBrandContextToGoals:
    def test_to_brand_goals_maps_all_fields(self) -> None:
        ctx = validate_brand_for_social_marketing(_full_brand_data(), "c1", "b1")
        goals = ctx.to_brand_goals(goals=["engagement"], cadence_posts_per_day=3, duration_days=7)
        assert goals.brand_name == ctx.brand_name
        assert goals.target_audience == ctx.target_audience
        assert goals.voice_and_tone == ctx.voice_and_tone
        assert goals.brand_guidelines == ctx.brand_guidelines
        assert goals.brand_objectives == ctx.brand_objectives
        assert goals.messaging_pillars == ctx.messaging_pillars
        assert goals.brand_story == ctx.brand_story
        assert goals.tagline == ctx.tagline
        assert goals.goals == ["engagement"]
        assert goals.cadence_posts_per_day == 3
        assert goals.duration_days == 7
