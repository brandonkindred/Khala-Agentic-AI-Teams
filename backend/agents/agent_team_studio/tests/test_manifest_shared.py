"""Unit tests for :mod:`agent_team_studio.manifest_shared`."""

from __future__ import annotations

from agent_team_studio.manifest_shared import (
    AGENT_ANATOMY_REF,
    DEFAULT_RULE_PACKS,
    GENERATED_AGENT_ENTRYPOINT,
    GENERATED_AGENT_INPUT_REF,
    GENERATED_AGENT_OUTPUT_REF,
    strip_marker_tags,
)


def test_constants_are_non_empty_strings() -> None:
    for value in (
        GENERATED_AGENT_ENTRYPOINT,
        GENERATED_AGENT_INPUT_REF,
        GENERATED_AGENT_OUTPUT_REF,
        AGENT_ANATOMY_REF,
    ):
        assert isinstance(value, str) and value.strip()


def test_default_rule_packs_contains_guardrails() -> None:
    assert DEFAULT_RULE_PACKS == ("default_guardrails",)


def test_strip_marker_tags_removes_only_markers() -> None:
    result = strip_marker_tags(
        ["content", "studio", "seo", "generated"], frozenset({"studio", "generated"})
    )
    assert result == ["content", "seo"]


def test_strip_marker_tags_preserves_order() -> None:
    result = strip_marker_tags(["b", "a", "c"], frozenset())
    assert result == ["b", "a", "c"]


def test_strip_marker_tags_empty_tags() -> None:
    assert strip_marker_tags([], frozenset({"generated"})) == []


def test_strip_marker_tags_all_markers() -> None:
    assert strip_marker_tags(["generated", "studio"], frozenset({"generated", "studio"})) == []


def test_strip_marker_tags_does_not_mutate_input() -> None:
    tags = ["content", "studio"]
    strip_marker_tags(tags, frozenset({"studio"}))
    assert tags == ["content", "studio"]
