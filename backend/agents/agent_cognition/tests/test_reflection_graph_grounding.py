"""Tests for the additive knowledge-graph grounding in reflection (HITL preserved).

The grounding only enriches the LLM *prompt*; it must never change what reflection
writes (still only ``pending`` proposals via ``create_proposal``) and must degrade
to ungrounded behaviour when the graph is disabled or the agent opted out.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_cognition.models import PeriodSummary, ProposalStatus, Scale
from agent_cognition.rules import reflection
from agent_cognition.testing import FakeGraphiti as _FakeGraphiti

_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _summary() -> PeriodSummary:
    return PeriodSummary(
        id="s1",
        agent_id="a",
        scale=Scale.DAY,
        period_start=_NOW,
        period_end=_NOW,
        summary="did things",
        created_at=_NOW,
    )


class _DummyLLM:
    pass


# ---------------------------------------------------------------------------
# _graph_grounding_block
# ---------------------------------------------------------------------------
def test_grounding_empty_when_neo4j_disabled(monkeypatch):
    monkeypatch.delenv("NEO4J_BOLT_URL", raising=False)
    assert reflection._graph_grounding_block("a", [_summary()]) == ""


def test_grounding_empty_when_opted_out(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    import agent_cognition.manifest_scope as ms

    monkeypatch.setattr(ms, "ground_rule_proposals", lambda a: False)
    assert reflection._graph_grounding_block("a", [_summary()]) == ""


def test_grounding_block_relabeled_when_enabled(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    import agent_cognition.graph.retrieval as retrieval_mod
    import agent_cognition.manifest_scope as ms

    monkeypatch.setattr(ms, "ground_rule_proposals", lambda a: True)

    async def _fake_graph(agent_id, query):
        return "## Knowledge graph\n- Alice knows Bob"

    monkeypatch.setattr(retrieval_mod, "build_graph_context", _fake_graph)
    block = reflection._graph_grounding_block("a", [_summary()])
    assert block.startswith("## Related knowledge (from graph)")
    assert "Alice knows Bob" in block


def test_grounding_empty_on_unexpected_error(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    import agent_cognition.manifest_scope as ms

    def _boom(agent_id):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(ms, "ground_rule_proposals", _boom)
    assert reflection._graph_grounding_block("a", [_summary()]) == ""


def test_grounding_empty_when_graph_returns_nothing(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    import agent_cognition.graph.retrieval as retrieval_mod
    import agent_cognition.manifest_scope as ms

    monkeypatch.setattr(ms, "ground_rule_proposals", lambda a: True)

    async def _empty(agent_id, query):
        return ""

    monkeypatch.setattr(retrieval_mod, "build_graph_context", _empty)
    assert reflection._graph_grounding_block("a", [_summary()]) == ""


# ---------------------------------------------------------------------------
# _propose prepends the grounding block to the prompt
# ---------------------------------------------------------------------------
def test_propose_prepends_graph_block(monkeypatch):
    captured = {}

    def _capture_compact(text, budget, llm, content_description=""):
        captured["text"] = text
        return text

    monkeypatch.setattr(reflection, "compact_text", _capture_compact)
    monkeypatch.setattr(
        reflection,
        "complete_validated",
        lambda *a, **k: reflection._ReflectionResult(proposals=[]),
    )

    reflection._propose(
        [_summary()], [], _DummyLLM(), graph_block="## Related knowledge (from graph)\n- fact"
    )
    assert captured["text"].startswith("## Related knowledge (from graph)\n- fact")


def test_propose_without_graph_block_unchanged(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        reflection,
        "compact_text",
        lambda text, *a, **k: captured.setdefault("text", text) or text,
    )
    monkeypatch.setattr(
        reflection,
        "complete_validated",
        lambda *a, **k: reflection._ReflectionResult(proposals=[]),
    )
    reflection._propose([_summary()], [], _DummyLLM())
    assert not captured["text"].startswith("## Related knowledge")


# ---------------------------------------------------------------------------
# HITL preserved: reflect with grounding still only writes proposals
# ---------------------------------------------------------------------------
def test_reflect_with_grounding_only_creates_proposals(monkeypatch):
    created = []

    def _fetch(agent_id, scale, *, limit=None, exclude_stale=False):
        return [_summary()] if scale is Scale.DAY else []

    monkeypatch.setattr(reflection.memory_store, "fetch_summaries", _fetch)
    monkeypatch.setattr(reflection.rules_store, "list_rules", lambda aid, status=None: [])
    monkeypatch.setattr(reflection.rules_store, "list_proposals", lambda aid, status=None: [])
    monkeypatch.setattr(reflection.rules_store, "create_proposal", lambda aid, p: created.append(p))

    def _boom(*a, **k):
        raise AssertionError("reflection must not activate a rule")

    monkeypatch.setattr(reflection.rules_store, "approve_proposal", _boom)
    monkeypatch.setattr(reflection.rules_store, "create_rule", _boom)

    # Grounding returns a block; _propose returns one canned add proposal.
    grounded = {"called": False}

    def _ground(agent_id, summaries):
        grounded["called"] = True
        return "## Related knowledge (from graph)\n- fact"

    monkeypatch.setattr(reflection, "_graph_grounding_block", _ground)
    monkeypatch.setattr(reflection, "get_client", lambda key: _DummyLLM())

    def _propose(summaries, active_rules, llm, *, graph_block=""):
        assert graph_block.startswith("## Related knowledge")  # block threaded through
        return reflection._ReflectionResult(proposals=[{"action": "add", "text": "derived"}])

    monkeypatch.setattr(reflection, "_propose", _propose)

    report = reflection.reflect("a", _NOW)
    assert grounded["called"] is True
    assert report.proposed == 1
    assert len(created) == 1 and created[0].status.value == "pending"


# ---------------------------------------------------------------------------
# _graph_evidence: bounded, non-load-bearing provenance entry
# ---------------------------------------------------------------------------
def test_graph_evidence_empty_block_is_empty_list():
    assert reflection._graph_evidence("") == []


def test_graph_evidence_marks_grounding_and_omits_stale_keys():
    block = "## Related knowledge (from graph)\n- Alice knows Bob\n- Bob ships code"
    ev = reflection._graph_evidence(block)
    assert ev == [{"source": "graph", "kind": "grounding"}]
    # Must omit the keys the version-staleness SQL reads, or it would make
    # graph-grounded proposals spuriously stale / unapprovable.
    assert "summary_id" not in ev[0] and "version" not in ev[0]


# ---------------------------------------------------------------------------
# Acceptance: a fake Graphiti grounds the prompt AND only create_proposal runs
# ---------------------------------------------------------------------------
def test_reflect_end_to_end_grounds_prompt_and_preserves_hitl(monkeypatch):
    """With a fake Graphiti returning facts: the LLM prompt input carries the graph
    block, the written proposal's evidence carries the graph-provenance entry, and
    only ``create_proposal`` is ever called (HITL gate untouched)."""
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")

    import agent_cognition.graph.retrieval as retrieval_mod
    import agent_cognition.manifest_scope as ms

    monkeypatch.setattr(ms, "ground_rule_proposals", lambda a: True)
    graphiti = _FakeGraphiti(["Alice prefers small PRs"])
    monkeypatch.setattr(retrieval_mod, "get_graphiti", lambda: graphiti)

    def _fetch(agent_id, scale, *, limit=None, exclude_stale=False):
        return [_summary()] if scale is Scale.DAY else []

    created = []
    monkeypatch.setattr(reflection.memory_store, "fetch_summaries", _fetch)
    monkeypatch.setattr(reflection.rules_store, "list_rules", lambda aid, status=None: [])
    monkeypatch.setattr(reflection.rules_store, "list_proposals", lambda aid, status=None: [])
    monkeypatch.setattr(reflection.rules_store, "create_proposal", lambda aid, p: created.append(p))
    monkeypatch.setattr(reflection, "get_client", lambda key: _DummyLLM())

    def _boom(*a, **k):
        raise AssertionError("reflection must not activate a rule")

    monkeypatch.setattr(reflection.rules_store, "approve_proposal", _boom)
    monkeypatch.setattr(reflection.rules_store, "create_rule", _boom)

    captured = {}

    def _capture_compact(text, budget, llm, content_description=""):
        captured["text"] = text
        return text

    monkeypatch.setattr(reflection, "compact_text", _capture_compact)
    monkeypatch.setattr(
        reflection,
        "complete_validated",
        lambda *a, **k: reflection._ReflectionResult(
            proposals=[{"action": "add", "text": "derived"}]
        ),
    )

    report = reflection.reflect("a", _NOW)

    # Graph was queried at the retrieval boundary and grounded the actual prompt.
    assert graphiti.calls and graphiti.calls[0]["group_ids"] == ["a"]
    assert captured["text"].startswith("## Related knowledge (from graph)")
    assert "Alice prefers small PRs" in captured["text"]

    # Only create_proposal ran, the proposal is pending, and its evidence carries
    # both the summary ref and the non-load-bearing graph-provenance entry.
    assert report.proposed == 1
    assert len(created) == 1
    proposal = created[0]
    assert proposal.status == ProposalStatus.PENDING
    assert {"summary_id": "s1", "version": 1} in proposal.evidence
    assert {"source": "graph", "kind": "grounding"} in proposal.evidence


def test_reflect_baseline_evidence_unchanged_when_graph_disabled(monkeypatch):
    """Graph disabled ⇒ no graph-provenance entry ⇒ evidence identical to baseline."""
    monkeypatch.delenv("NEO4J_BOLT_URL", raising=False)

    def _fetch(agent_id, scale, *, limit=None, exclude_stale=False):
        return [_summary()] if scale is Scale.DAY else []

    created = []
    monkeypatch.setattr(reflection.memory_store, "fetch_summaries", _fetch)
    monkeypatch.setattr(reflection.rules_store, "list_rules", lambda aid, status=None: [])
    monkeypatch.setattr(reflection.rules_store, "list_proposals", lambda aid, status=None: [])
    monkeypatch.setattr(reflection.rules_store, "create_proposal", lambda aid, p: created.append(p))
    monkeypatch.setattr(reflection, "get_client", lambda key: _DummyLLM())
    monkeypatch.setattr(reflection, "compact_text", lambda text, *a, **k: text)
    monkeypatch.setattr(
        reflection,
        "complete_validated",
        lambda *a, **k: reflection._ReflectionResult(
            proposals=[{"action": "add", "text": "derived"}]
        ),
    )

    report = reflection.reflect("a", _NOW)

    assert report.proposed == 1
    assert created[0].evidence == [{"summary_id": "s1", "version": 1}]
    assert all(e.get("source") != "graph" for e in created[0].evidence)
