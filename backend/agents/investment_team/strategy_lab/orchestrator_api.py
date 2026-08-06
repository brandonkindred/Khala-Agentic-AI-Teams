"""Strategy Lab orchestration API façade (run/cycle helpers).

Target home for Strategy Lab helpers that today still live in
``investment_team.api.main``. This module currently **re-exports** the
Temporal-hot subset only; function bodies have not moved yet.

See ``ORCHESTRATOR_API_BOUNDARIES.md`` for the full helper inventory, call
graph, module ownership, and shared-state access plan.

Preconditions:
    Callers import named helpers from this module (prefer lazy import inside
    Temporal activities so ``api.main`` is not loaded at worker import time).
Postconditions:
    Each public name resolves to the same callable currently defined on
    ``investment_team.api.main`` (lazy attribute lookup). Behavior is
    unchanged from importing that symbol directly from ``api.main``.
Invariants:
    ``__all__`` is the complete public re-export surface for this façade
    step; adding a name requires updating the boundaries note.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from investment_team.api.main import (
        _compute_signal_brief_snapshot,
        _finalize_strategy_lab_cycle_record,
        _is_strategy_lab_run_externally_stopped,
        _persist_run_state,
        _snapshot_prior_records,
        _strategy_lab_external_terminal_status,
    )

__all__ = [
    "_persist_run_state",
    "_snapshot_prior_records",
    "_compute_signal_brief_snapshot",
    "_is_strategy_lab_run_externally_stopped",
    "_strategy_lab_external_terminal_status",
    "_finalize_strategy_lab_cycle_record",
]

_EXPORTS = frozenset(__all__)


def __getattr__(name: str):
    """Resolve a re-exported helper from ``api.main`` on first attribute access.

    Preconditions:
        ``name`` is a non-empty attribute name requested on this module.
    Postconditions:
        Returns ``getattr(investment_team.api.main, name)`` when ``name`` is
        in ``__all__``; otherwise raises ``AttributeError``. Loads ``api.main``
        only when a listed export is first requested.
    """
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from investment_team.api import main as _api_main

    return getattr(_api_main, name)


def __dir__() -> list[str]:
    """Return module attributes including the façade re-exports.

    Preconditions:
        None.
    Postconditions:
        Returns a sorted list that includes every name in ``__all__`` plus
        the module's ordinary attributes.
    """
    return sorted(set(globals()) | _EXPORTS)
