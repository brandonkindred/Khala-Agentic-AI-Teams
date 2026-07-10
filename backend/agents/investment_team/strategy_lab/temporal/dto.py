"""Wire-format helpers for cross-boundary state that isn't already a Pydantic model.

``ConvergenceTracker`` predates the Temporal port and holds a ``Counter`` and
``Set[str]`` values that aren't directly JSON-serializable. The actual
serialization now lives on the class itself
(``ConvergenceTracker.to_wire_dict`` / ``from_wire_dict``) so the wire contract
stays co-located with the internal representation it depends on. These two
functions are thin adapters kept for import ergonomics — both ``activities.py``
and ``workflows.py`` import them from here without depending on the tracker
class directly at their module top.
"""

from __future__ import annotations

from typing import Any, Dict


def convergence_tracker_to_wire(tracker: Any) -> Dict[str, Any]:
    """Serialize a ``ConvergenceTracker`` to a JSON-safe dict.

    Preconditions:
        ``tracker`` is a ``quality_gates.convergence_tracker.ConvergenceTracker``.
    Postconditions:
        Returns ``tracker.to_wire_dict()`` — a dict round-trippable by
        :func:`convergence_tracker_from_wire`.
    """
    return tracker.to_wire_dict()


def convergence_tracker_from_wire(data: Dict[str, Any]) -> Any:
    """Reconstruct a ``ConvergenceTracker`` from :func:`convergence_tracker_to_wire`'s output.

    Preconditions:
        ``data`` is either ``{}`` (fresh tracker) or a dict produced by
        :func:`convergence_tracker_to_wire`.
    Postconditions:
        Returns a ``ConvergenceTracker`` with state equivalent to the one that
        was serialized.
    """
    from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker

    return ConvergenceTracker.from_wire_dict(data)


__all__ = ["convergence_tracker_from_wire", "convergence_tracker_to_wire"]
