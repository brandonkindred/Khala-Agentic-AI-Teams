"""Generic off-hot-path deferred-id sweep primitive.

:class:`ResetSweepState` is the reusable mechanism behind moving a per-call
"reset this id" write off a caller's hot path: a caller enqueues an id (pure
Python, no I/O) and a lazily-started :class:`~shared_concurrency.heartbeat.BackgroundHeartbeat`
drains the pending set on an interval, invoking an injected callback for each
id off-thread. Mirrors the batching half of
``software_engineering_team/shared/trace_flusher.py``, generalized: this module
has no knowledge of Postgres, provider config, or any other owning domain — the
reset callback and the interval are both supplied by the caller, so it can be
unit-tested fully in isolation and reused by any "defer this write" caller.

The sole current caller is :mod:`llm_service.provider_store`, which constructs
one process-wide :class:`ResetSweepState` with ``reset_fn=reset_entry`` and
``interval_fn`` reading ``LLM_PROVIDER_RESET_SWEEP_INTERVAL_S`` — see that
module for the concrete wiring and the env var's documentation.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from shared_concurrency.heartbeat import BackgroundHeartbeat

# Default floor applied to the resolved interval before starting the
# heartbeat: a 0 (or near-0) interval would busy-loop the sweep thread.
_DEFAULT_MIN_INTERVAL_S = 0.1


class ResetSweepState:
    """Encapsulates one background "enqueue id -> drain via callback" sweep.

    A caller enqueues an id (pure Python, no I/O) instead of performing its
    write synchronously; a lazily-started ``BackgroundHeartbeat`` drains
    ``pending_ids`` on an interval and invokes ``reset_fn`` for each id
    off-thread.

    Invariants:
        - ``_lock`` guards ``pending_ids`` only; ``_start_lock`` guards the
          ``started``/``heartbeat`` pair (double-checked locking in
          :meth:`_ensure_started`).
        - At most one ``BackgroundHeartbeat`` runs for this instance at a time.

    No shutdown/drain hook (intentional, not an oversight): this is a
    self-contained primitive with no knowledge of whether its owning module
    has a shared lifecycle hook to attach one to (``llm_service``, the sole
    current caller, has none — see ``provider_store``'s docstring). If the
    process exits with ids still in ``pending_ids``, the caller decides how
    to interpret that; for the current caller (provider resets) it is bounded
    eventual consistency, not data loss, since the next selection call
    re-detects and re-enqueues.
    """

    def __init__(
        self,
        reset_fn: Callable[[int], None],
        interval_fn: Callable[[], float],
        *,
        min_interval_s: float = _DEFAULT_MIN_INTERVAL_S,
        name: str = "reset-sweep",
    ) -> None:
        self._reset_fn = reset_fn
        self._interval_fn = interval_fn
        self._min_interval_s = min_interval_s
        self._name = name
        self.pending_ids: set[int] = set()
        self._lock = threading.Lock()
        self.heartbeat: Optional[BackgroundHeartbeat] = None
        self.started = False
        self._start_lock = threading.Lock()

    def enqueue(self, entry_id: int) -> None:
        """Queue an id for background reset; zero I/O on the call path.

        The actual ``reset_fn`` call happens on the next :meth:`tick`, at most
        ``interval_fn()`` (floored at ``min_interval_s``) later.

        Postconditions: ``entry_id`` is present in :attr:`pending_ids`; the
            background sweep is running. Never raises.
        """
        with self._lock:
            self.pending_ids.add(entry_id)
        self._ensure_started()

    def tick(self) -> None:
        """Drain the pending id set and call ``reset_fn`` for each, off the hot path.

        Snapshots and clears :attr:`pending_ids` under the lock, then calls
        ``reset_fn`` for each id *outside* the lock so a slow callback never
        blocks a concurrent enqueue.

        Postconditions: the pending set is empty when this returns (new ids
            may have been added concurrently and are picked up on the next
            tick). Never raises unless ``reset_fn`` itself raises.
        """
        with self._lock:
            if not self.pending_ids:
                return
            ids = list(self.pending_ids)
            self.pending_ids.clear()
        for entry_id in ids:
            self._reset_fn(entry_id)

    def _ensure_started(self) -> None:
        """Lazily start the background sweep heartbeat (idempotent).

        Postconditions: exactly one ``BackgroundHeartbeat`` daemon thread is
            running for this instance after this returns. Never raises.
        """
        if self.started:
            return
        with self._start_lock:
            if self.started:
                return
            self.heartbeat = BackgroundHeartbeat(
                self.tick,
                max(self._interval_fn(), self._min_interval_s),
                name=self._name,
            )
            self.heartbeat.start()
            self.started = True

    def reset_for_test(self) -> None:
        """Test-only: clear pending ids and sweep-started state between tests.

        Stops any heartbeat that was started (best-effort, never raises) so
        daemon threads/fakes don't leak state across tests. Mirrors
        ``trace_flusher._reset_for_test``.
        """
        hb = self.heartbeat
        self.heartbeat = None
        self.started = False
        with self._lock:
            self.pending_ids.clear()
        if hb is not None:
            try:
                hb.stop()
            except Exception:  # pragma: no cover - defensive only, stop() should not raise
                pass
