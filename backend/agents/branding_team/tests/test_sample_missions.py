"""Unit tests pinning the sample mission eval corpus.

Preconditions:
    - ``branding_team.scripts.eval_fixtures.sample_missions`` is importable.
Postconditions:
    - Assertions guard against accidental corpus shrinkage, type drift, or
      loss of industry/completeness diversity.
"""

from __future__ import annotations

from branding_team.models import BrandingMission
from branding_team.scripts.eval_fixtures.sample_missions import SAMPLE_MISSIONS


def test_corpus_has_at_least_three_missions() -> None:
    assert len(SAMPLE_MISSIONS) >= 3


def test_corpus_entries_are_valid_branding_missions() -> None:
    for mission in SAMPLE_MISSIONS:
        assert isinstance(mission, BrandingMission)


def test_corpus_covers_diverse_target_audiences() -> None:
    audiences = {mission.target_audience for mission in SAMPLE_MISSIONS}
    assert len(audiences) >= 3


def test_corpus_includes_a_minimal_mission() -> None:
    assert any(not mission.values and not mission.differentiators for mission in SAMPLE_MISSIONS)


def test_corpus_includes_a_full_mission() -> None:
    """At least one mission populates every optional BrandingMission field."""
    assert any(
        mission.values
        and mission.differentiators
        and mission.existing_brand_material
        and mission.wiki_path
        and mission.color_inspiration
        and mission.color_palettes
        and mission.selected_palette_index is not None
        and mission.visual_style
        and mission.typography_preference
        and mission.interface_density
        for mission in SAMPLE_MISSIONS
    )
