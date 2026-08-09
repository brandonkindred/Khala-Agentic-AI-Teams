"""Unit tests for the shared Manifest projection mechanics."""

from __future__ import annotations

from agent_registry.manifest_projection import filter_marker_tags, hash_suffix, revalidate
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


def test_hash_suffix_is_deterministic() -> None:
    assert hash_suffix("Router Agent", 8) == hash_suffix("Router Agent", 8)


def test_hash_suffix_respects_length() -> None:
    digest = hash_suffix("Router Agent", 16)
    assert len(digest) == 16


def test_hash_suffix_differs_for_different_inputs() -> None:
    assert hash_suffix("Router Agent", 8) != hash_suffix("Resolution Agent", 8)


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
