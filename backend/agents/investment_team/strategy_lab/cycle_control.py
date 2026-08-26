"""``run_cycle``'s directive-gathering and terminal-guard logic, in a module
with zero runtime dependencies beyond ``typing``.

Both functions here are extracted from ``StrategyLabOrchestrator.run_cycle``
(``orchestrator.py``) so thread mode and the Temporal-mode
``StrategyLabCycleWorkflow.run`` (``temporal/workflows.py``, via
``temporal/dto.py``'s adapters of the same names) share one implementation
instead of hand-duplicating it.

This logic deliberately does **not** live in ``_orchestrator_helpers.py``
(the module's other "pure helpers shared by orchestrator.py and its mixins").
That module imports ``..market_data_service``, ``..execution.metrics``, and
``..trading_service.modes.sandbox_compat`` at its own top level for its other,
unrelated functions — the last of those transitively imports
``shared.postgres.client``, whose module scope calls ``threading.Lock()``. The
temporalio workflow sandbox intercepts *every* import reachable from workflow
code, including one deferred inside a function body, and raises
``RestrictedWorkflowAccessError`` the moment that transitive chain reaches
``threading.Lock()`` — so even a deferred import of
``_orchestrator_helpers`` from ``temporal/dto.py`` is unsafe. Only a module
whose own transitive import graph is inert (this one: ``typing`` only, plus
``TYPE_CHECKING``-only references to the real types that
``from __future__ import annotations`` keeps from ever being evaluated at
runtime) is safe to reach from sandboxed workflow code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from ..models import StrategySpec
    from .quality_gates.convergence_tracker import ConvergenceTracker


def gather_convergence_directives(tracker: "ConvergenceTracker") -> List[str]:
    """Gather this cycle's convergence directives from ``tracker``.

    Preconditions:
      - ``tracker`` is a constructed ``ConvergenceTracker``.
    Postconditions:
      - Returns a list containing, in order: the stall directive (if any),
        the diversity directive (if any), then every failure directive.
    Invariants:
      - Pure: only reads ``tracker`` state (counters/history), no I/O, no
        wall-clock or random calls — safe to call from inside a temporalio
        workflow sandbox.
    """
    directives: List[str] = []
    stall_dir = tracker.get_stall_directive()
    if stall_dir:
        directives.append(stall_dir)
    diversity_dir = tracker.get_diversity_directive()
    if diversity_dir:
        directives.append(diversity_dir)
    directives.extend(tracker.get_failure_directives())
    return directives


def require_short_circuit_inputs(
    last_spec: "Optional[StrategySpec]", last_evidence: Optional[str]
) -> None:
    """Guard that re-entry exhaustion captured enough state to short-circuit.

    ``last_spec`` is typed as ``StrategySpec`` for thread-mode ``run_cycle``'s
    call site, but the Temporal-mode workflow calls this with a wire dict
    instead (its re-entry-exhaustion state is JSON-shaped, not a constructed
    model). The guarded check is a plain ``is None``, so it behaves
    identically for either representation.

    Preconditions:
      - Called only after the design-re-entry loop exhausts its re-entry
        bound without returning.
      - ``last_spec`` and ``last_evidence`` are each either the value a
        ``SpecImplementabilityError`` raiser set, or ``None`` if none was
        ever raised (or a future raiser violates that contract) — both are
        legitimately optional inputs, not merely non-``None`` in practice.
    Postconditions:
      - Returns ``None`` when both arguments are non-``None``.
    Invariants:
      - Pure: no I/O, no mutation, no wall-clock or random calls — safe to
        call from inside a temporalio workflow sandbox.

    Raises:
      ``RuntimeError`` when either argument is ``None`` — every
      ``SpecImplementabilityError`` raiser is required to set both
      ``last_spec``/``evidence``, so this is a defensive contract check
      against a future raiser violating that, not an expected runtime path.
    """
    if last_spec is None or last_evidence is None:
        raise RuntimeError(
            "SpecImplementabilityError raised without last_spec/evidence; "
            "cannot build short-circuit record. This is a bug in a refinement "
            "code path; please file an issue with the run logs."
        )
