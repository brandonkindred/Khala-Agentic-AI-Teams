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
