"""Wire-format helpers for cross-boundary state that isn't already a Pydantic model.

``ConvergenceTracker`` predates the Temporal port and holds a ``Counter`` and
``Set[str]`` values that aren't directly JSON-serializable. The actual
serialization now lives on the class itself
(``ConvergenceTracker.to_wire_dict`` / ``from_wire_dict``) so the wire contract
stays co-located with the internal representation it depends on. These
functions are thin adapters kept for import ergonomics — both ``activities.py``
and ``workflows.py`` import them from here without depending on the tracker
class (or, for ``gather_convergence_directives`` /
``require_short_circuit_inputs``, ``_orchestrator_helpers``) directly at their
module top: each adapter below defers its real import to inside the function
body, exactly like ``convergence_tracker_from_wire`` already does, so
``workflows.py`` never drags ``orchestrator.py``'s or ``_orchestrator_helpers.py``'s
transitive import graph into the temporalio sandbox's restricted re-import of
the workflow module.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


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


def gather_convergence_directives(tracker: Any) -> List[str]:
    """Adapter onto ``_orchestrator_helpers.gather_convergence_directives``.

    Lets ``StrategyLabCycleWorkflow.run`` share the exact same
    directive-gathering implementation as thread-mode ``run_cycle`` instead of
    hand-duplicating it, without importing ``_orchestrator_helpers`` (and its
    heavier transitive graph) at ``workflows.py``'s module top.

    Preconditions:
        ``tracker`` is a ``quality_gates.convergence_tracker.ConvergenceTracker``
        (e.g. from :func:`convergence_tracker_from_wire`).
    Postconditions:
        Returns the same ordered directive list
        ``_orchestrator_helpers.gather_convergence_directives`` would.
    """
    from investment_team.strategy_lab._orchestrator_helpers import (
        gather_convergence_directives as _gather_convergence_directives,
    )

    return _gather_convergence_directives(tracker)


def require_short_circuit_inputs(last_spec: Optional[Any], last_evidence: Optional[str]) -> None:
    """Adapter onto ``_orchestrator_helpers.require_short_circuit_inputs``.

    Lets ``StrategyLabCycleWorkflow.run`` share the exact same terminal-guard
    implementation as thread-mode ``run_cycle`` instead of hand-duplicating it,
    without importing ``_orchestrator_helpers`` at ``workflows.py``'s module
    top. ``last_spec`` is typed loosely (``Any``, not ``StrategySpec``) because
    the workflow's re-entry-exhaustion state is a JSON-shaped dict, not a
    constructed model; the guarded check is a plain ``is None``, so it behaves
    identically for either representation.

    Preconditions:
        Called only after the workflow's design-re-entry loop exhausts its
        re-entry bound without returning a record.
    Postconditions:
        Returns ``None`` when both arguments are non-``None``.
    Raises:
        ``RuntimeError`` (same message as thread mode) when either argument is
        ``None``.
    """
    from investment_team.strategy_lab._orchestrator_helpers import (
        require_short_circuit_inputs as _require_short_circuit_inputs,
    )

    _require_short_circuit_inputs(last_spec, last_evidence)


__all__ = [
    "convergence_tracker_from_wire",
    "convergence_tracker_to_wire",
    "gather_convergence_directives",
    "require_short_circuit_inputs",
]
