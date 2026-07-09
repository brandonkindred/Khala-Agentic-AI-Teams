"""Wire-format helpers for cross-boundary state that isn't already a Pydantic model.

``ConvergenceTracker`` predates the Temporal port and has no JSON-safe
serialization of its own (it holds a ``Counter`` and ``Set[str]`` values,
neither directly JSON-serializable) — these two functions round-trip it
through a plain dict so it can cross a workflow input/output or
activity-result boundary. Kept separate from ``activities.py``/``workflows.py``
so both can import from here without duplicating the shape.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict


def convergence_tracker_to_wire(tracker: Any) -> Dict[str, Any]:
    """Serialize a ``ConvergenceTracker`` to a JSON-safe dict.

    Preconditions:
        ``tracker`` is a ``quality_gates.convergence_tracker.ConvergenceTracker``.
    Postconditions:
        Returns a dict round-trippable by :func:`convergence_tracker_from_wire`
        into an equivalent tracker (same window/history size, signatures,
        failure-mode counts, asset-class history, and trial count).
    """
    return {
        "window_size": tracker._window_size,
        "max_history": tracker._max_history,
        "signatures": [sorted(sig) for sig in tracker._signatures],
        "failure_modes": dict(tracker._failure_modes),
        "asset_class_history": list(tracker._asset_class_history),
        "trial_count": tracker._trial_count,
    }


def convergence_tracker_from_wire(data: Dict[str, Any]) -> Any:
    """Reconstruct a ``ConvergenceTracker`` from :func:`convergence_tracker_to_wire`'s output.

    Preconditions:
        ``data`` is either ``{}`` (fresh tracker) or a dict produced by
        :func:`convergence_tracker_to_wire`.
    Postconditions:
        Returns a ``ConvergenceTracker`` with state equivalent to the one
        that was serialized.
    """
    from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker

    tracker = ConvergenceTracker(
        window_size=data.get("window_size", 5),
        max_history=data.get("max_history", 50),
    )
    tracker._signatures = [set(sig) for sig in data.get("signatures", [])]
    tracker._failure_modes = Counter(data.get("failure_modes", {}))
    tracker._asset_class_history = list(data.get("asset_class_history", []))
    tracker._trial_count = data.get("trial_count", 0)
    return tracker


__all__ = ["convergence_tracker_from_wire", "convergence_tracker_to_wire"]
