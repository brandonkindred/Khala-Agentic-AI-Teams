"""Task-graph persist/flush coordinator for the coding-team orchestrator.

Extracted from ``coding_team/orchestrator.py`` (issue: decompose the orchestrator
god-function into named collaborators) — a structural move that preserves behavior.

``GraphPersistCoordinator`` owns the one cohesive concern the entrypoint used to smear
across eight ``nonlocal``-coupled closures: the background single-writer flusher, the task
graph it persists, the last-confirmed ``_persist_state`` bookkeeping, and the live
``phase``/``status_text`` that every write carries. The entrypoint now holds a plain
``coord`` handle and calls ``update()`` / ``persist_sync()``; the graph's own mutators call
``persist_async()`` via the ``persist_callback`` wired at construction.

The single-writer flush protocol (why writes are serialized through the flusher, why the
async path reads phase/status_text live at write time, and why ``_persist_state`` only ever
advances after a confirmed write) is documented on the methods below, next to the code it
governs, rather than in a wall of comments in the caller.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from shared.concurrency import LatestValueFlusher
from software_engineering_team.progress_config import _coding_progress
from software_engineering_team.task_graph import TaskGraphService, create_task_graph

logger = logging.getLogger(__name__)


class GraphPersistCoordinator:
    """Owns the task graph and serializes every job-service write behind one background writer.

    Two write paths share a single ``LatestValueFlusher`` so a graph-snapshot write can never
    land after — and clobber — a fresher direct status write:

    - ``update(**kw)`` — a direct write (phase/status/HITL fields). Uses ``write_now()``, which
      holds the writer's serialization point for the whole call, so a still-queued/in-flight
      background write can never overtake it on the wire, and it keeps ``phase``/``status_text``
      in sync with what it actually wrote.
    - ``persist_async()`` — the graph's ``persist_callback``, invoked synchronously while
      TaskGraphService's RLock is held, so it must never block on I/O: it builds the graph-only
      payload cheaply, then hands a write-and-commit closure to the flusher to run off-thread.

    Invariants:
        - Exactly one flusher daemon thread runs between construction and ``stop()``.
        - ``_persist_state`` reflects the last CONFIRMED-persisted (revision, phase, status_text)
          — it is advanced only after a write actually succeeds, never optimistically at
          enqueue/compute time, so a failed write is retried by the next persist call rather than
          mistaken for delivered.
        - ``phase``/``status_text`` are the live authoritative values as of the most recent
          successful ``update()``; the async path reads them at actual-write time so a background
          write can only ever repeat or advance the authoritative state, never regress it.
    """

    def __init__(
        self,
        job_id: str,
        raw_update: Callable[..., None],
        *,
        progress_base: int,
        progress_span: int,
        phase: str,
        status_text: str,
    ) -> None:
        """Start the flusher and create the persist-wired task graph.

        Preconditions:
            - ``raw_update`` is the SAME store used for the orchestrator's resume read and cancel
              checks (the injected ``update_job_fn`` or the coding_team default), callable with
              the job-record keyword fields.
            - ``0 <= progress_base``, ``0 <= progress_span`` (validated by the caller).

        Postconditions:
            - ``self.graph`` is a fresh ``TaskGraphService`` whose ``persist_callback`` is this
              coordinator's ``persist_async``.
            - The background flusher daemon thread is running; ``stop()`` must eventually be
              called (the orchestrator does so in a ``finally``) so a long-lived process never
              leaks one thread per job.
        """
        self._raw_update = raw_update
        self._progress_base = progress_base
        self._progress_span = progress_span
        self.phase = phase
        self.status_text = status_text
        # Last CONFIRMED-persisted (graph revision, phase, status_text) so a no-op call skips the
        # snapshot + job-service write entirely. "Confirmed" is load-bearing — see the class
        # invariant; every real graph mutation bumps ``graph.revision`` and phase/status changes
        # are part of the key, so every actual state change still writes.
        self._persist_state: Dict[str, Any] = {"revision": -1, "phase": None, "status_text": None}
        # Set by the caller once a CodingTeamSwarm exists (e.g. ``coord.review_cache_export =
        # swarm.export_review_cache``) so persist_sync can include the review verdict cache in the
        # job record. A plain attribute rather than a constructor parameter — this module cannot
        # import CodingTeamSwarm without a circular import (it lives in coding_team_orchestrator.py,
        # which imports this module) — and rather than duck-typing on a swarm object, so the
        # coordinator stays decoupled from the swarm's shape. None during pre-swarm phases (task
        # graph / planning), when review_verdict_cache is correctly absent from the write.
        self.review_cache_export: Optional[Callable[[], List[Dict[str, Any]]]] = None
        self.flusher = LatestValueFlusher(
            lambda write: write(),
            name=f"coding-persist-{job_id}",
            on_error=lambda exc: logger.warning("Task graph background persist failed: %s", exc),
        ).start()
        self.graph: TaskGraphService = create_task_graph(
            job_id, persist_callback=self.persist_async
        )

    def update(self, **kw: Any) -> None:
        """Direct job-service write, serialized through the flusher's ``write_now()``.

        ``_raw_update`` runs first, THEN ``phase``/``status_text`` are committed — never the
        reverse. If ``_raw_update`` raises, this call's phase/status_text must not reach the live
        attributes: a concurrent background persist's live read would otherwise publish this
        call's new phase/status_text alongside the graph snapshot even though the rest of this
        call's write (e.g. HITL pending-question fields on a pause) never made it to the wire.
        Keeping the commit inside the ``write_now()``-serialized closure orders it strictly before
        any concurrent background write can observe it.

        Postconditions:
            - When ``kw`` carried ``phase``/``status_text`` and ``_raw_update`` did not raise,
              the corresponding live attribute equals the value written.
        """

        def _do_write() -> None:
            self._raw_update(**kw)
            if "phase" in kw:
                self.phase = kw["phase"]
            if "status_text" in kw:
                self.status_text = kw["status_text"]

        self.flusher.write_now(_do_write)

    def _graph_payload(self, snap: Dict[str, Any]) -> Dict[str, Any]:
        """The graph-only portion of a persist payload (no phase/status_text).

        Postconditions:
            - Returns ``task_graph_snapshot``/``agent_task_map``/``progress`` derived from
              ``snap``; deliberately excludes phase/status_text so callers layer those in
              themselves (the async path reads them live — see ``persist_async``).
        """
        return {
            "task_graph_snapshot": snap["tasks"],
            "agent_task_map": snap["agent_task_map"],
            "progress": _coding_progress(snap["tasks"], self._progress_base, self._progress_span),
        }

    def _compute_snapshot_if_changed(self) -> "Optional[tuple[Dict[str, Any], int]]":
        """Cheap, revision-gated graph payload for ``persist_async``.

        A graph mutation always bumps ``graph.revision``, so a revision-only check is sufficient
        here; baking today's phase/status_text in at this (enqueue) point is exactly what would
        let a stale value clobber a fresher direct write once the write became a background one —
        so those fields are read live in the enqueued closure instead.

        Postconditions:
            - Returns ``None`` when the graph revision is unchanged since the last confirmed
              persist; otherwise ``(graph_payload, revision)`` for the current revision.
        """
        if self.graph.revision == self._persist_state["revision"]:
            return None
        snap = self.graph.snapshot()
        return self._graph_payload(snap), self.graph.revision

    def persist_async(self) -> None:
        """``persist_callback`` for TaskGraphService — invoked while its RLock is held.

        Must never block on I/O: it does cheap in-memory bookkeeping (build the graph-only
        payload), then hands a write-and-commit closure to the flusher to run off-thread.
        phase/status_text are read LIVE inside that closure, at actual-write time — not baked in
        here at enqueue time. ``write_now()`` (see ``update``) deliberately lets this background
        write land after a racing direct write (e.g. a HITL pause) rather than blocking it; a
        phase/status_text captured here at enqueue time would still describe the pre-pause state
        by the time the write executes and would clobber the pause's phase/status_text via the job
        service's shallow merge even though the pause write itself landed first. Reading live means
        this write always carries whatever phase/status_text is current when it reaches the wire,
        so it can only repeat or advance the authoritative state, never regress it.

        Postconditions:
            - No-op when the graph revision is unchanged; otherwise a background write is enqueued
              that, on success, advances ``_persist_state`` to the written (revision, phase,
              status_text).
        """
        computed = self._compute_snapshot_if_changed()
        if computed is None:
            return
        snap_payload, revision = computed

        def _write_and_commit() -> None:
            live_phase, live_status_text = self.phase, self.status_text
            wire_payload = {
                **snap_payload,
                "phase": live_phase,
                "status_text": live_status_text,
            }
            self._raw_update(**wire_payload)
            self._persist_state.update(
                {"revision": revision, "phase": live_phase, "status_text": live_status_text}
            )

        self.flusher.enqueue(_write_and_commit)

    def persist_sync(self) -> None:
        """Round-boundary / pre-loop durability checkpoint (``persist_fn=``).

        Drains first so a previously queued async write can never land after — and stomp on —
        this write; if that async write had failed, ``_persist_state`` was never advanced, so the
        change check below naturally retries it here instead of silently accepting a stale
        snapshot. Reads phase/status_text directly (not lazily): this path writes synchronously
        with no enqueue-then-execute gap, so there is nothing for a live read to protect against.

        Postconditions:
            - No-op when graph revision, phase, and status_text all match the last confirmed
              persist; otherwise writes the current snapshot synchronously and advances
              ``_persist_state``.
            - When ``review_cache_export`` is set, the write additionally carries
              ``review_verdict_cache`` (that callable's return value); when it is ``None``
              (no swarm attached yet — pre-swarm phases), the key is omitted entirely rather than
              written as ``None``.
        """
        self.flusher.drain()
        if (
            self.graph.revision == self._persist_state["revision"]
            and self.phase == self._persist_state["phase"]
            and self.status_text == self._persist_state["status_text"]
        ):
            return
        snap = self.graph.snapshot()
        wire_payload = {
            **self._graph_payload(snap),
            "phase": self.phase,
            "status_text": self.status_text,
        }
        if self.review_cache_export is not None:
            wire_payload["review_verdict_cache"] = self.review_cache_export()
        self._raw_update(**wire_payload)
        self._persist_state.update(
            {"revision": self.graph.revision, "phase": self.phase, "status_text": self.status_text}
        )

    def stop(self) -> None:
        """Drain any pending write and tear down the flusher's daemon thread.

        Postconditions:
            - The background thread is stopped; safe to call on every orchestrator exit path
              (normal completion, an early return, or an unexpected exception).
        """
        self.flusher.stop()
