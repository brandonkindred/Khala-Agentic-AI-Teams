"""Tests for ``strategy_lab.orchestrator_api`` ownership and façade identity.

Preconditions:
    ``investment_team.api.main`` is importable.
Postconditions:
    Moved symbols are defined on ``orchestrator_api`` (not only via ``__getattr__``)
    and match ``api.main`` aliases. ``api.main`` has no top-level function bodies for
    moved callables (assignment aliases only). Deferred Temporal-hot symbols still
    resolve via ``__getattr__`` to ``api.main`` until a later extract moves them.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from investment_team.api import main as api_main
from investment_team.strategy_lab import orchestrator_api

_MOVED = (
    "STRATEGY_LAB_TERMINAL_STATUSES",
    "_persist_run_state",
    "_reconcile_run_progress",
    "_run_state_to_response",
    "_build_run_state",
    "_job_progress_percent",
    "_delete_jobs_concurrently",
    "_delete_paper_sessions_for_lab_record",
    "_purge_strategy_lab_job_storage",
    "_fail_strategy_lab_run",
    "_dispatch_strategy_lab_run",
    "_no_active_run_locked",
    "_ensure_no_active_run",
    "_require_run_transition_lock",
)

_MOVED_CALLABLES = tuple(
    name for name in _MOVED if name != "STRATEGY_LAB_TERMINAL_STATUSES"
)

_DEFERRED = (
    "_snapshot_prior_records",
    "_compute_signal_brief_snapshot",
    "_is_strategy_lab_run_externally_stopped",
    "_strategy_lab_external_terminal_status",
    "_finalize_strategy_lab_cycle_record",
)


@pytest.mark.parametrize("name", _MOVED)
def test_moved_symbol_defined_on_orchestrator_api(name: str) -> None:
    """Moved helpers must be real module attributes, not lazy ``__getattr__`` only."""
    assert name in orchestrator_api.__dict__, f"{name} missing from orchestrator_api.__dict__"
    assert getattr(orchestrator_api, name) is getattr(api_main, name)


@pytest.mark.parametrize("name", _DEFERRED)
def test_deferred_symbol_still_aliases_api_main(name: str) -> None:
    assert getattr(orchestrator_api, name) is getattr(api_main, name)


def test_orchestrator_api_unknown_attribute_raises() -> None:
    with pytest.raises(AttributeError, match="has no attribute '_not_a_real_export'"):
        getattr(orchestrator_api, "_not_a_real_export")


def test_orchestrator_api_dir_includes_exports() -> None:
    names = dir(orchestrator_api)
    for export in orchestrator_api.__all__:
        assert export in names


def test_progress_fields_and_purge_constants_exported() -> None:
    assert "completed_cycles" in orchestrator_api._STRATEGY_LAB_PROGRESS_FIELDS
    assert orchestrator_api._PURGE_MAX_WORKERS == 16
    assert orchestrator_api._PURGE_TIMEOUT_S == 120.0


def test_api_main_has_no_moved_helper_function_bodies() -> None:
    """Moved orchestrator callables must not regain ``def`` bodies in ``api.main``.

    Preconditions:
        ``api_main`` is the loaded ``investment_team.api.main`` module.
    Postconditions:
        No top-level ``FunctionDef`` / ``AsyncFunctionDef`` in ``api.main``'s
        source is named in ``_MOVED_CALLABLES``. Assignment aliases remain allowed.
    """
    source = inspect.getsource(api_main)
    tree = ast.parse(source)
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    leaked = sorted(defined & set(_MOVED_CALLABLES))
    assert leaked == [], (
        "Moved Strategy Lab orchestrator helpers must not have function bodies "
        f"in api.main; found def(s): {leaked}"
    )
