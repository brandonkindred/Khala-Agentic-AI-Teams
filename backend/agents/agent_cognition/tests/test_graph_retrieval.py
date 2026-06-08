"""Tests for build_graph_context — faked Graphiti, no live Neo4j."""

from __future__ import annotations

import asyncio
import types

import pytest

from agent_cognition.graph import retrieval


class _FakeGraphiti:
    def __init__(self, facts=None, error: Exception | None = None):
        self._facts = facts or []
        self._error = error
        self.calls: list[dict] = []

    async def search(self, *, query, group_ids, num_results):
        self.calls.append({"query": query, "group_ids": group_ids, "num_results": num_results})
        if self._error is not None:
            raise self._error
        return [types.SimpleNamespace(fact=f) for f in self._facts]


def _enable(monkeypatch, graphiti):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    monkeypatch.setattr(retrieval, "get_graphiti", lambda: graphiti)


def test_top_k_env(monkeypatch):
    monkeypatch.delenv("AGENT_COGNITION_GRAPH_SEARCH_TOP_K", raising=False)
    assert retrieval._search_top_k() == 10
    monkeypatch.setenv("AGENT_COGNITION_GRAPH_SEARCH_TOP_K", "3")
    assert retrieval._search_top_k() == 3
    monkeypatch.setenv("AGENT_COGNITION_GRAPH_SEARCH_TOP_K", "0")
    assert retrieval._search_top_k() == 10
    monkeypatch.setenv("AGENT_COGNITION_GRAPH_SEARCH_TOP_K", "junk")
    assert retrieval._search_top_k() == 10


def test_empty_when_disabled(monkeypatch):
    monkeypatch.delenv("NEO4J_BOLT_URL", raising=False)
    assert asyncio.run(retrieval.build_graph_context("a", "q")) == ""


def test_empty_when_query_blank(monkeypatch):
    _enable(monkeypatch, _FakeGraphiti(facts=["x"]))
    assert asyncio.run(retrieval.build_graph_context("a", "   ")) == ""


def test_empty_when_no_results(monkeypatch):
    _enable(monkeypatch, _FakeGraphiti(facts=[]))
    assert asyncio.run(retrieval.build_graph_context("a", "q")) == ""


def test_empty_on_search_error(monkeypatch):
    _enable(monkeypatch, _FakeGraphiti(error=RuntimeError("boom")))
    assert asyncio.run(retrieval.build_graph_context("a", "q")) == ""


def test_renders_facts_scoped_to_agent(monkeypatch):
    graphiti = _FakeGraphiti(facts=["Alice knows Bob", "Bob owns Acme"])
    _enable(monkeypatch, graphiti)
    out = asyncio.run(retrieval.build_graph_context("agent-7", "who is bob"))
    assert out.startswith("## Knowledge graph")
    assert "- Alice knows Bob" in out
    assert "- Bob owns Acme" in out
    assert graphiti.calls[0]["group_ids"] == ["agent-7"]


def test_precondition_rejects_empty_agent(monkeypatch):
    with pytest.raises(AssertionError):
        asyncio.run(retrieval.build_graph_context("", "q"))
