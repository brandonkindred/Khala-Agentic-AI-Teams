"""Progress reporting adapter for Graph/Swarm execution.

Translates graph lifecycle events into ``job_updater`` or
``JobServiceClient`` callbacks so the UI can track execution progress.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class GraphProgressReporter:
    """Reports graph/swarm execution progress to a job tracking callback.

    Parameters
    ----------
    job_updater:
        A callable ``(phase: str, detail: str, pct: float) -> None`` that
        pushes progress to the job store / UI.
    total_nodes:
        Total number of graph nodes (used to compute percentage).
    base_phase:
        Human-readable phase prefix (e.g. ``"SOC2 Audit"``).
    """

    def __init__(
        self,
        job_updater: Callable[..., Any],
        total_nodes: int,
        base_phase: str = "Graph Execution",
    ) -> None:
        self._updater = job_updater
        self._total = max(total_nodes, 1)
        self._completed = 0
        self._base_phase = base_phase

    def on_node_start(self, node_id: str) -> None:
        """Call when a graph node begins execution."""
        pct = self._completed / self._total
        try:
            self._updater(self._base_phase, f"Running {node_id}...", pct)
        except Exception:
            logger.debug("Progress update failed for node_start %s", node_id, exc_info=True)

    def on_node_complete(self, node_id: str) -> None:
        """Call when a graph node finishes execution."""
        self._completed += 1
        pct = self._completed / self._total
        try:
            self._updater(self._base_phase, f"Completed {node_id}", pct)
        except Exception:
            logger.debug("Progress update failed for node_complete %s", node_id, exc_info=True)

    def on_done(self) -> None:
        """Call when the entire graph execution is finished."""
        try:
            self._updater(self._base_phase, "Complete", 1.0)
        except Exception:
            logger.debug("Progress update failed for on_done", exc_info=True)
