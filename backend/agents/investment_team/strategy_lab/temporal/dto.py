"""Wire-format helpers for cross-boundary state that isn't already a Pydantic model.

``ConvergenceTracker`` predates the Temporal port and holds a ``Counter`` and
``Set[str]`` values that aren't directly JSON-serializable. The actual
serialization now lives on the class itself
(``ConvergenceTracker.to_wire_dict`` / ``from_wire_dict``) so the wire contract
stays co-located with the internal representation it depends on. These
functions are thin adapters kept for import ergonomics — both ``activities.py``
and ``workflows.py`` import them from here without depending on the tracker
class (or, for ``gather_convergence_directives`` / ``require_short_circuit_inputs``,
``cycle_control``) directly at their module top: each adapter below defers its
real import to inside the function body, exactly like
``convergence_tracker_from_wire`` already does, so ``workflows.py``'s own
top-level code (which runs inside the temporalio workflow sandbox) never
imports anything beyond ``temporalio`` and its own ``temporal/`` siblings.

Note that ``gather_convergence_directives`` / ``require_short_circuit_inputs``
specifically live in ``strategy_lab/cycle_control.py``, NOT
``strategy_lab/_orchestrator_helpers.py`` (where the rest of ``run_cycle``'s
extracted pure helpers live): ``_orchestrator_helpers.py`` imports
``trading_service.modes.sandbox_compat`` at its own top level, which
transitively reaches ``shared.postgres.client``'s module-scope
``threading.Lock()`` call — a genuinely restricted call under the temporalio
sandbox, tripped even by a deferred import like the ones below. See
``cycle_control.py``'s module docstring for the full explanation.
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
    """Adapter onto ``cycle_control.gather_convergence_directives``.

    Lets ``StrategyLabCycleWorkflow.run`` share the exact same
    directive-gathering implementation as thread-mode ``run_cycle`` instead of
    hand-duplicating it, without importing ``cycle_control`` at
    ``workflows.py``'s module top.

    Preconditions:
        ``tracker`` is a ``quality_gates.convergence_tracker.ConvergenceTracker``
        (e.g. from :func:`convergence_tracker_from_wire`).
    Postconditions:
        Returns the same ordered directive list
        ``cycle_control.gather_convergence_directives`` would.
    """
    from investment_team.strategy_lab.cycle_control import (
        gather_convergence_directives as _gather_convergence_directives,
    )

    return _gather_convergence_directives(tracker)


def require_short_circuit_inputs(last_spec: Any, last_evidence: Optional[str]) -> None:
    """Adapter onto ``cycle_control.require_short_circuit_inputs``.

    Lets ``StrategyLabCycleWorkflow.run`` share the exact same terminal-guard
    implementation as thread-mode ``run_cycle`` instead of hand-duplicating it,
    without importing ``cycle_control`` at ``workflows.py``'s module top.
    ``last_spec`` is typed loosely (``Any``, not ``StrategySpec``) because the
    workflow's re-entry-exhaustion state is a JSON-shaped dict, not a
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
    from investment_team.strategy_lab.cycle_control import (
        require_short_circuit_inputs as _require_short_circuit_inputs,
    )

    _require_short_circuit_inputs(last_spec, last_evidence)


__all__ = [
    "convergence_tracker_from_wire",
    "convergence_tracker_to_wire",
    "gather_convergence_directives",
    "require_short_circuit_inputs",
]
