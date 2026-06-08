"""Tests for the invoke-time cognition facade."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from agent_cognition import invoke_context
from agent_cognition.models import Rule, RuleMode, RuleSource, RuleStatus

_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _rule(text: str) -> Rule:
    return Rule(
        id="r1",
        agent_id="a",
        text=text,
        mode=RuleMode.ADVISORY,
        status=RuleStatus.ACTIVE,
        source=RuleSource.OPERATOR,
        created_at=_NOW,
        updated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Query extraction
# ---------------------------------------------------------------------------
def test_extract_query_text_variants():
    assert invoke_context.extract_query_text("hello") == "hello"
    assert invoke_context.extract_query_text({"a": "foo", "b": "bar", "n": 3}) == "foo bar"
    assert invoke_context.extract_query_text({"n": 3}) == ""
    assert invoke_context.extract_query_text(123) == ""
    assert invoke_context.extract_query_text(["x"]) == ""


# ---------------------------------------------------------------------------
# build_cognition_context composition
# ---------------------------------------------------------------------------
def _patch(monkeypatch, *, rules, digest, graph):
    monkeypatch.setattr(invoke_context.rules_store, "list_rules", lambda *a, **k: rules)
    monkeypatch.setattr(invoke_context, "build_memory_digest", lambda *a, **k: digest)

    async def _graph(agent_id, query):
        return graph

    monkeypatch.setattr(invoke_context, "build_graph_context", _graph)


def test_build_context_joins_digest_and_graph(monkeypatch):
    _patch(
        monkeypatch,
        rules=[_rule("be nice")],
        digest="## Long-term memory\nx",
        graph="## Knowledge graph\n- y",
    )
    ctx = asyncio.run(invoke_context.build_cognition_context("a", query="hi"))
    assert [r.text for r in ctx.rules] == ["be nice"]
    assert "## Long-term memory" in ctx.memory_digest
    assert "## Knowledge graph" in ctx.memory_digest
    # Joined by a blank line.
    assert "\n\n" in ctx.memory_digest


def test_build_context_omits_empty_blocks(monkeypatch):
    _patch(monkeypatch, rules=[], digest="", graph="## Knowledge graph\n- y")
    ctx = asyncio.run(invoke_context.build_cognition_context("a", query="hi"))
    assert ctx.memory_digest == "## Knowledge graph\n- y"  # no leading blank line
    assert ctx.rules == []


def test_build_context_all_empty(monkeypatch):
    _patch(monkeypatch, rules=[], digest="", graph="")
    ctx = asyncio.run(invoke_context.build_cognition_context("a", query=""))
    assert ctx.memory_digest == ""
    assert ctx.rules == []


def test_build_context_rejects_empty_agent():
    with pytest.raises(AssertionError):
        asyncio.run(invoke_context.build_cognition_context("", query="hi"))
