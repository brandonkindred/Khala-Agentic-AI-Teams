"""Tests for content profile / length policy resolution."""

import pytest
from shared.content_profile import (
    ContentProfile,
    LengthPolicy,
    SeriesContext,
    build_draft_length_instruction,
    resolve_length_policy,
    resolve_length_policy_from_request_dict,
)


def test_resolve_default_standard_article_1000_words() -> None:
    policy = resolve_length_policy()
    assert policy.content_profile == ContentProfile.standard_article
    assert policy.target_word_count == 1000
    assert policy.soft_min_words <= policy.target_word_count <= policy.soft_max_words
    assert policy.editor_must_fix_over_ratio == pytest.approx(1.30)


def test_resolve_short_listicle_preset() -> None:
    policy = resolve_length_policy(content_profile=ContentProfile.short_listicle)
    assert policy.target_word_count == 750
    assert policy.soft_max_words >= policy.target_word_count
    assert policy.editor_must_fix_over_ratio > policy.editor_should_fix_over_ratio


def test_resolve_explicit_target_overrides_numeric() -> None:
    policy = resolve_length_policy(
        content_profile=ContentProfile.short_listicle,
        explicit_target_word_count=500,
    )
    assert policy.target_word_count == 500
    assert policy.soft_min_words <= 500 <= policy.soft_max_words


def test_resolve_explicit_target_clamped() -> None:
    policy = resolve_length_policy(explicit_target_word_count=50)
    assert policy.target_word_count == 100
    policy2 = resolve_length_policy(explicit_target_word_count=99999)
    assert policy2.target_word_count == 10000


def test_resolve_length_notes_appended() -> None:
    policy = resolve_length_policy(length_notes="Focus on EU readers only.")
    assert "EU readers" in policy.length_guidance


def test_series_context_in_guidance() -> None:
    ctx = SeriesContext(
        series_title="Observability 101",
        part_number=2,
        planned_parts=5,
        instalment_scope="Metrics cardinality",
    )
    policy = resolve_length_policy(
        content_profile=ContentProfile.series_instalment,
        series_context=ctx,
    )
    assert "Observability 101" in policy.length_guidance
    assert "Part 2" in policy.length_guidance
    assert "Metrics cardinality" in policy.length_guidance


def test_build_draft_length_instruction_includes_target_band() -> None:
    policy = resolve_length_policy(content_profile=ContentProfile.technical_deep_dive)
    block = build_draft_length_instruction(policy)
    assert "technical deep dive" in block.lower()
    assert str(policy.target_word_count) in block
    assert str(policy.soft_min_words) in block


def test_resolve_from_request_dict_empty() -> None:
    policy = resolve_length_policy_from_request_dict({})
    assert isinstance(policy, LengthPolicy)
    assert policy.target_word_count == 1000


def test_resolve_from_request_dict_with_profile() -> None:
    policy = resolve_length_policy_from_request_dict(
        {"content_profile": "technical_deep_dive"},
    )
    assert policy.content_profile == ContentProfile.technical_deep_dive
    assert policy.target_word_count == 2200


def test_resolve_from_request_dict_explicit_target() -> None:
    policy = resolve_length_policy_from_request_dict(
        {"content_profile": "short_listicle", "target_word_count": 900},
    )
    assert policy.target_word_count == 900
