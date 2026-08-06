"""Tests for ``strategy_lab.orchestrator_api`` façade re-exports.

Preconditions:
    ``investment_team.api.main`` is importable (same as activity tests).
Postconditions:
    Asserts that each listed export resolves to the callable on ``api.main``,
    unknown names raise ``AttributeError``, and ``dir()`` includes exports.
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
        "_compute_signal_brief_snapshot",
        "_is_strategy_lab_run_externally_stopped",
        "_strategy_lab_external_terminal_status",
        "_finalize_strategy_lab_cycle_record",
    ],
)
def test_orchestrator_api_reexports_match_api_main(name: str) -> None:
    """Each façade export is the same object currently defined on api.main."""
    assert getattr(orchestrator_api, name) is getattr(api_main, name)


def test_orchestrator_api_unknown_attribute_raises() -> None:
    with pytest.raises(AttributeError, match="has no attribute '_not_a_real_export'"):
        getattr(orchestrator_api, "_not_a_real_export")


def test_orchestrator_api_dir_includes_exports() -> None:
    names = dir(orchestrator_api)
    for export in orchestrator_api.__all__:
        assert export in names
