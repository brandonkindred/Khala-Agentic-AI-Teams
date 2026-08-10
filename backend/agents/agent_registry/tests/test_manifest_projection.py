"""Unit tests for the shared Manifest projection mechanics."""

from __future__ import annotations

import pytest

from agent_registry.manifest_projection import filter_marker_tags, hash_suffix, revalidate, slug
from agent_registry.models import AgentManifest, SourceInfo


def _manifest(**overrides) -> AgentManifest:
    base = dict(
        id="team.agent",
        team="team",
        name="Agent",
        summary="Does things",
        tags=["real", "generated"],
        source=SourceInfo(entrypoint="x:run"),
    )
    base.update(overrides)
    return AgentManifest(**base)


def test_slug_lowercases_and_hyphenates() -> None:
    assert slug("My Cool Agent") == "my-cool-agent"


def test_slug_falls_back_to_agent_for_all_symbol_input() -> None:
    assert slug("!!!") == "agent"


def test_slug_falls_back_to_agent_for_empty_input() -> None:
    assert slug("") == "agent"
    assert slug(None) == "agent"


def test_slug_truncates_to_max_len() -> None:
    result = slug("a very long agent name that exceeds the bound", max_len=10)
    assert result == "a-very-lon"
    assert len(result) <= 10


def test_slug_strips_dangling_hyphen_after_truncation() -> None:
    assert slug("abc-def-ghi", max_len=4) == "abc"
    assert slug("ab-cd-ef", max_len=3) == "ab"


def test_slug_rejects_non_positive_max_len() -> None:
    with pytest.raises(ValueError):
        slug("Agent", max_len=0)
    with pytest.raises(ValueError):
        slug("Agent", max_len=-1)


def test_hash_suffix_is_deterministic() -> None:
    assert hash_suffix("Router Agent", 8) == hash_suffix("Router Agent", 8)


def test_hash_suffix_respects_length() -> None:
    digest = hash_suffix("Router Agent", 16)
    assert len(digest) == 16


def test_hash_suffix_differs_for_different_inputs() -> None:
    assert hash_suffix("Router Agent", 8) != hash_suffix("Resolution Agent", 8)


def test_hash_suffix_rejects_out_of_range_length() -> None:
    with pytest.raises(ValueError):
        hash_suffix("Agent", 0)
    with pytest.raises(ValueError):
        hash_suffix("Agent", -1)
    with pytest.raises(ValueError):
        hash_suffix("Agent", 65)


def test_filter_marker_tags_removes_markers_and_preserves_order() -> None:
    tags = ["content", "studio", "generated", "seo"]
    assert filter_marker_tags(tags, frozenset({"studio", "generated"})) == ["content", "seo"]


def test_filter_marker_tags_handles_none() -> None:
    assert filter_marker_tags(None, frozenset({"generated"})) == []


def test_filter_marker_tags_no_markers_matched_is_no_op() -> None:
    tags = ["content", "seo"]
    assert filter_marker_tags(tags, frozenset({"generated"})) == tags


def test_filter_marker_tags_all_matched_yields_empty() -> None:
    tags = ["studio", "generated"]
    assert filter_marker_tags(tags, frozenset({"studio", "generated"})) == []


def test_revalidate_round_trips_to_an_equal_manifest() -> None:
    manifest = _manifest()
    result = revalidate(manifest)
    assert result == manifest
    assert result is not manifest
