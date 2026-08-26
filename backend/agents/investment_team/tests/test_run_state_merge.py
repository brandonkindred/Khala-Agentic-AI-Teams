"""Coverage for ``strategy_lab.run_state._merge_and_reconcile_records``.

Standalone fixture-based tests for the merge/reconcile/tolerate-malformed
helper extracted from the near-identical logic duplicated across
``list_strategy_lab_jobs`` and ``list_strategy_lab_runs`` in ``api.main``.
No FastAPI ``TestClient`` and no job-service client stub are needed here --
the helper takes its ``persisted`` records as a plain list, independent of
either HTTP endpoint.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Tuple

import pytest

from investment_team.strategy_lab.run_state import (
    _merge_and_reconcile_records,
    normalize_persisted_job,
)

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _passthrough_normalize(job: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Identity normalizer: ``persisted`` entries are already state-shaped."""
    return job["run_id"], job


def test_in_memory_only_reconciles_non_terminal_runs() -> None:
    active_runs = {"run-1": {"run_id": "run-1", "status": "running"}}
    lock = threading.Lock()
    reconciled: List[str] = []

    result = _merge_and_reconcile_records(
        active_runs=active_runs,
        lock=lock,
        reconcile_fn=reconciled.append,
        terminal_statuses=TERMINAL_STATUSES,
        persisted=[],
        normalize_persisted=_passthrough_normalize,
    )

    assert reconciled == ["run-1"]
    assert result == {"run-1": {"run_id": "run-1", "status": "running"}}


def test_terminal_run_is_not_reconciled_but_is_included() -> None:
    active_runs = {"run-1": {"run_id": "run-1", "status": "completed"}}
    lock = threading.Lock()
    reconciled: List[str] = []

    result = _merge_and_reconcile_records(
        active_runs=active_runs,
        lock=lock,
        reconcile_fn=reconciled.append,
        terminal_statuses=TERMINAL_STATUSES,
        persisted=[],
        normalize_persisted=_passthrough_normalize,
    )

    assert reconciled == []
    assert "run-1" in result


def test_persisted_record_not_in_memory_is_added() -> None:
    active_runs: Dict[str, Dict[str, Any]] = {}
    lock = threading.Lock()
    persisted = [{"job_id": "run-2", "status": "completed", "data": {"total_cycles": 5}}]

    result = _merge_and_reconcile_records(
        active_runs=active_runs,
        lock=lock,
        reconcile_fn=lambda _rid: None,
        terminal_statuses=TERMINAL_STATUSES,
        persisted=persisted,
        normalize_persisted=lambda job: (
            job["job_id"],
            normalize_persisted_job(job, fallback_status="completed"),
        ),
    )

    assert result.keys() == {"run-2"}
    assert result["run-2"]["run_id"] == "run-2"
    assert result["run-2"]["total_cycles"] == 5


def test_in_memory_entry_wins_over_persisted_entry_with_same_id() -> None:
    active_runs = {"run-1": {"run_id": "run-1", "status": "running", "completed_cycles": 3}}
    lock = threading.Lock()
    persisted = [{"job_id": "run-1", "status": "completed", "data": {"completed_cycles": 999}}]

    result = _merge_and_reconcile_records(
        active_runs=active_runs,
        lock=lock,
        reconcile_fn=lambda _rid: None,
        terminal_statuses=TERMINAL_STATUSES,
        persisted=persisted,
        normalize_persisted=lambda job: (
            job["job_id"],
            normalize_persisted_job(job, fallback_status="completed"),
        ),
    )

    assert result.keys() == {"run-1"}
    assert result["run-1"]["completed_cycles"] == 3


def test_malformed_persisted_record_is_skipped_without_dropping_the_rest() -> None:
    active_runs: Dict[str, Dict[str, Any]] = {}
    lock = threading.Lock()
    persisted = [
        {"job_id": "run-good", "status": "completed"},
        {"job_id": "run-bad"},  # deliberately missing what the normalizer needs
    ]

    def _normalize(job: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        if job["job_id"] == "run-bad":
            raise ValueError("simulated malformed persisted record")
        return job["job_id"], job

    result = _merge_and_reconcile_records(
        active_runs=active_runs,
        lock=lock,
        reconcile_fn=lambda _rid: None,
        terminal_statuses=TERMINAL_STATUSES,
        persisted=persisted,
        normalize_persisted=_normalize,
    )

    assert result.keys() == {"run-good"}


def test_active_run_entry_missing_run_id_is_skipped() -> None:
    active_runs = {
        "key-without-run-id": {"status": "running"},
        "run-1": {"run_id": "run-1", "status": "running"},
    }
    lock = threading.Lock()

    result = _merge_and_reconcile_records(
        active_runs=active_runs,
        lock=lock,
        reconcile_fn=lambda _rid: None,
        terminal_statuses=TERMINAL_STATUSES,
        persisted=[],
        normalize_persisted=_passthrough_normalize,
    )

    assert result.keys() == {"run-1"}


def test_empty_inputs_return_empty_result() -> None:
    result = _merge_and_reconcile_records(
        active_runs={},
        lock=threading.Lock(),
        reconcile_fn=lambda _rid: pytest.fail("must not be called with no active runs"),
        terminal_statuses=TERMINAL_STATUSES,
        persisted=[],
        normalize_persisted=_passthrough_normalize,
    )

    assert result == {}


def test_persisted_entry_with_falsy_run_id_from_normalizer_is_dropped() -> None:
    active_runs: Dict[str, Dict[str, Any]] = {}
    lock = threading.Lock()
    persisted = [{"job_id": "", "status": "completed"}]

    result = _merge_and_reconcile_records(
        active_runs=active_runs,
        lock=lock,
        reconcile_fn=lambda _rid: None,
        terminal_statuses=TERMINAL_STATUSES,
        persisted=persisted,
        normalize_persisted=lambda job: (job["job_id"], job),
    )

    assert result == {}
