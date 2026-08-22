"""Direct unit tests for GraphPersistCoordinator.

The persist/flush ordering guarantees are exercised end-to-end through the orchestrator in
test_coding_team_orchestrator.py; these cover the coordinator's own contract in isolation —
the revision-gated no-op short-circuit, update()'s phase/status_text commit, and the sync
durability checkpoint — without spinning up the whole swarm.
"""

from __future__ import annotations

from typing import Any, Dict, List

from software_engineering_team.graph_persist import GraphPersistCoordinator


def _make_coord(writes: List[Dict[str, Any]]) -> GraphPersistCoordinator:
    return GraphPersistCoordinator(
        "j1",
        lambda **kw: writes.append(kw),
        progress_base=0,
        progress_span=100,
        phase="task_graph",
        status_text="Building task graph from plan",
    )


def test_update_commits_phase_and_status_text_after_write():
    """update() writes through the flusher, then commits the new phase/status_text as live."""
    writes: List[Dict[str, Any]] = []
    coord = _make_coord(writes)
    try:
        coord.update(phase="coding", status_text="working", status="running")
        assert writes[-1] == {"phase": "coding", "status_text": "working", "status": "running"}
        assert coord.phase == "coding"
        assert coord.status_text == "working"
    finally:
        coord.stop()


def test_persist_async_is_noop_when_revision_unchanged():
    """When the graph revision has not advanced since the last confirmed persist, persist_async
    enqueues nothing — the revision-gated short-circuit that keeps idle rounds off the wire."""
    writes: List[Dict[str, Any]] = []
    coord = _make_coord(writes)
    try:
        # Align the confirmed-persist marker with the current (unmutated) graph revision, so a
        # persist with no intervening mutation is a genuine no-op.
        coord._persist_state["revision"] = coord.graph.revision
        assert coord._compute_snapshot_if_changed() is None
        coord.persist_async()
        coord.flusher.drain()
        assert writes == []
    finally:
        coord.stop()


def test_persist_sync_lands_snapshot_then_noops_when_unchanged():
    """persist_sync surfaces a mutation's snapshot (draining the pending background write or
    writing synchronously), then short-circuits on a second call while nothing has changed."""
    writes: List[Dict[str, Any]] = []
    coord = _make_coord(writes)
    try:
        coord.graph.add_task("t1", title="T1")

        coord.persist_sync()
        graph_writes = [w for w in writes if "task_graph_snapshot" in w]
        assert graph_writes, "the mutation's snapshot must reach the store"
        assert [t["id"] for t in graph_writes[-1]["task_graph_snapshot"]] == ["t1"]
        assert graph_writes[-1]["phase"] == "task_graph"

        # No mutation, no phase/status change → the durability checkpoint is a no-op.
        n = len(writes)
        coord.persist_sync()
        assert len(writes) == n
    finally:
        coord.stop()


def test_persist_sync_omits_review_verdict_cache_when_no_swarm_attached():
    """Pre-swarm phases (review_cache_export unset) never write review_verdict_cache — no
    KeyError/None, the field is simply absent from the wire payload."""
    writes: List[Dict[str, Any]] = []
    coord = _make_coord(writes)
    try:
        coord.graph.add_task("t1", title="T1")
        coord.persist_sync()
        assert "review_verdict_cache" not in writes[-1]
    finally:
        coord.stop()


def test_persist_sync_includes_review_verdict_cache_when_swarm_attached():
    """Once a swarm's export callable is attached, an actual persist_sync write includes its
    return value under review_verdict_cache.

    Deliberately does not mutate the graph first: a graph mutation's persist_callback fires
    synchronously and enqueues an async write on the flusher, which persist_sync's own
    ``drain()`` would land *before* its own no-op check — that async write path never carries
    review_verdict_cache (only persist_sync's own explicit write does, per scope), so it would
    make this test pass or fail on the wrong write. The freshly-constructed coordinator's
    revision/phase/status_text already differ from ``_persist_state``'s initial sentinel, so the
    very first persist_sync() call performs its own write with nothing queued to race it.
    """
    writes: List[Dict[str, Any]] = []
    coord = _make_coord(writes)
    try:
        exported = [{"task_id": "t1", "cache_key": "abc", "verdict": {"approved": True}}]
        coord.review_cache_export = lambda: exported

        coord.persist_sync()

        assert writes[-1]["review_verdict_cache"] == exported
    finally:
        coord.stop()
