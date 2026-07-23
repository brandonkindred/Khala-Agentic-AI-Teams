"""Prove team models re-export the shared HumanReview type (identity, not a copy)."""

from __future__ import annotations

from shared.hitl import HumanReview as SharedHumanReview


def test_branding_reexports_shared_human_review():
    from branding_team.models import HumanReview

    assert HumanReview is SharedHumanReview


def test_social_media_marketing_reexports_shared_human_review():
    from social_media_marketing_team.models import HumanReview

    assert HumanReview is SharedHumanReview


def test_market_research_reexports_shared_human_review():
    from market_research_team.models import HumanReview

    assert HumanReview is SharedHumanReview
