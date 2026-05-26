"""Edge branches in ``shared.content_profile``."""

from __future__ import annotations

from shared.content_profile import (
    ContentProfile,
    SeriesContext,
    build_planning_length_context,
    build_review_length_context,
    resolve_length_policy,
    resolve_length_policy_from_request_dict,
    series_context_block,
)


def test_resolve_length_policy_normalizes_soft_max_and_min() -> None:
    """When explicit target overrides preset, soft_min/soft_max are clamped."""
    policy = resolve_length_policy(
        content_profile=ContentProfile.standard_article,
        explicit_target_word_count=50,  # well below preset soft_min/soft_max
    )
    assert policy.soft_max_words >= policy.target_word_count
    assert policy.soft_min_words <= policy.target_word_count


def test_series_context_block_passthrough() -> None:
    ctx = SeriesContext(
        series_title="My Series",
        post_position=2,
        prior_posts=["Post 1"],
    )
    block = series_context_block(ctx)
    assert block is not None
    assert "My Series" in block


def test_series_context_block_none_returns_none() -> None:
    assert series_context_block(None) is None


def test_resolve_length_policy_includes_series_context() -> None:
    ctx = SeriesContext(series_title="My Series", post_position=2, prior_posts=[])
    policy = resolve_length_policy(
        content_profile=ContentProfile.standard_article,
        series_context=ctx,
    )
    assert "My Series" in policy.length_guidance


def test_resolve_length_policy_appends_length_notes() -> None:
    policy = resolve_length_policy(
        content_profile=ContentProfile.standard_article,
        length_notes="keep tight",
    )
    assert "Author notes" in policy.length_guidance
    assert "keep tight" in policy.length_guidance


def test_build_review_length_context_is_alias_for_planning() -> None:
    policy = resolve_length_policy(content_profile=ContentProfile.standard_article)
    assert build_review_length_context(policy) == build_planning_length_context(policy)


def test_resolve_length_policy_from_request_dict_with_series_instance() -> None:
    """``series_context`` may be a SeriesContext instance — not just dict."""
    ctx = SeriesContext(series_title="S", post_position=1, prior_posts=[])
    request_dict = {
        "content_profile": "standard_article",
        "series_context": ctx,
    }
    policy = resolve_length_policy_from_request_dict(request_dict)
    assert "S" in policy.length_guidance


def test_resolve_length_policy_from_request_dict_with_profile_instance() -> None:
    """``content_profile`` may be a ContentProfile enum instance directly."""
    policy = resolve_length_policy_from_request_dict(
        {"content_profile": ContentProfile.short_listicle},
    )
    assert policy.content_profile == ContentProfile.short_listicle


def test_resolve_length_policy_from_request_dict_with_target_word_count_string() -> None:
    """``target_word_count`` may arrive as a string from JSON."""
    policy = resolve_length_policy_from_request_dict(
        {"content_profile": "standard_article", "target_word_count": "1500"},
    )
    assert policy.target_word_count == 1500


def test_resolve_length_policy_from_request_dict_with_blank_length_notes() -> None:
    """Blank length notes are normalized to None (no Author notes block)."""
    policy = resolve_length_policy_from_request_dict(
        {"content_profile": "standard_article", "length_notes": "   "},
    )
    assert "Author notes" not in policy.length_guidance


def test_resolve_length_policy_from_request_dict_empty_string_target() -> None:
    """Empty-string target_word_count is treated as absent."""
    policy = resolve_length_policy_from_request_dict(
        {"content_profile": "standard_article", "target_word_count": ""},
    )
    assert policy.target_word_count > 0
