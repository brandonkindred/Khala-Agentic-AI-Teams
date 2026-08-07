"""Tests for ``strategy_lab.orchestrator_api`` implementation and re-exports.

Preconditions:
    ``investment_team.api.main`` is importable (same as activity tests).
Postconditions:
    Asserts moved helpers are locally defined and re-exported by ``api.main``;
    remaining Temporal-hot helpers are lazily resolved from ``api.main``.
"""

from __future__ import annotations

import pytest

from investment_team.api import main as api_main
from investment_team.strategy_lab import orchestrator_api


@pytest.mark.parametrize(
    "name",
    [
        "_persist_run_state",
        "_snapshot_prior_records",
        "_reconcile_run_progress",
        "_job_progress_percent",
        "_delete_jobs_concurrently",
        "_delete_paper_sessions_for_lab_record",
        "_purge_strategy_lab_job_storage",
        "_STRATEGY_LAB_PROGRESS_FIELDS",
        "STRATEGY_LAB_TERMINAL_STATUSES",
        "_PURGE_MAX_WORKERS",
        "_PURGE_TIMEOUT_S",
    ],
)
def test_orchestrator_api_defines_moved_symbols_and_api_main_reexports(name: str) -> None:
    """Moved symbols are defined here and retain identity through ``api.main``."""
    assert name in orchestrator_api.__dict__
    assert getattr(orchestrator_api, name) is getattr(api_main, name)


@pytest.mark.parametrize(
    "name",
    [
        "_compute_signal_brief_snapshot",
        "_is_strategy_lab_run_externally_stopped",
        "_strategy_lab_external_terminal_status",
        "_finalize_strategy_lab_cycle_record",
    ],
)
def test_orchestrator_api_lazy_exports_match_api_main(name: str) -> None:
    """Each remaining lazy export resolves to the same ``api.main`` object."""
    assert name not in orchestrator_api.__dict__
    assert getattr(orchestrator_api, name) is getattr(api_main, name)


def test_orchestrator_api_unknown_attribute_raises() -> None:
    with pytest.raises(AttributeError, match="has no attribute '_not_a_real_export'"):
        getattr(orchestrator_api, "_not_a_real_export")


def test_orchestrator_api_dir_includes_exports() -> None:
    names = dir(orchestrator_api)
    for export in orchestrator_api.__all__:
        assert export in names
