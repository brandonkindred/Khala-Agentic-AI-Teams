"""Unit tests for :mod:`agent_team_studio.manifest_shared`."""

from __future__ import annotations

from agent_team_studio.manifest_shared import (
    AGENT_ANATOMY_REF,
    DEFAULT_RULE_PACKS,
    default_cognition_block,
    strip_marker_tags,
)


def test_anatomy_ref_is_a_non_empty_string() -> None:
    assert isinstance(AGENT_ANATOMY_REF, str) and AGENT_ANATOMY_REF.strip()


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


def test_default_cognition_block_is_batteries_included() -> None:
    block = default_cognition_block()
    assert block.rule_packs == ["default_guardrails"]
    assert block.memory.retention_days_events == 90
    assert block.tools == []
    assert block.requires_idempotency_key is False
    assert block.knowledge_graph.enabled is True


def test_default_cognition_block_returns_a_fresh_instance_each_call() -> None:
    # Callers (e.g. build_studio_agent_manifest) mutate a copy via model_copy;
    # a shared/cached instance would let one caller's override leak to another.
    a = default_cognition_block()
    b = default_cognition_block()
    assert a is not b
    assert a == b
