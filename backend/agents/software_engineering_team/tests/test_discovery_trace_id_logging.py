"""Verify Discovery-phase (``discovery.py``) logger calls carry the job's bound
trace id via ``extra={"trace_id": ...}`` rather than plain string interpolation.

Reuses the ``product_delivery.get_store`` patching pattern from
``test_orchestrator_sprint_path.py`` to reach ``resolve_spec_source``'s
sprint-not-found error path, which logs via ``logger.error("Sprint %s not
found: %s", ...)``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from shared.observability import bind_trace_id
from software_engineering_team import discovery


@pytest.fixture
def patch_product_delivery(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch the lazy ``from product_delivery import get_store`` lookup so the
    sprint-not-found path can be exercised without a running Postgres."""
    state: dict[str, Any] = {"store": None}

    import product_delivery as pd_mod

    def _fake_get_store() -> Any:
        return state["store"]

    monkeypatch.setattr(pd_mod, "get_store", _fake_get_store)
    return state


class _StubStore:
    def get_sprint_with_stories(self, sprint_id: str):
        return None  # unknown sprint


def test_resolve_spec_source_sprint_not_found_logs_bound_trace_id(
    patch_product_delivery, caplog
) -> None:
    """The 'Sprint ... not found' error log carries the job's bound trace id."""
    patch_product_delivery["store"] = _StubStore()
    updates: list[dict] = []

    caplog.set_level(logging.ERROR)
    with bind_trace_id("discovery-trace-id"):
        result = discovery.resolve_spec_source(
            "job-x",
            "/tmp/does-not-matter",
            sprint_id="missing-sprint",
            spec_content_override=None,
            update_job_fn=lambda job_id, **kw: updates.append(kw),
        )

    assert result is None
    assert updates and updates[0]["status"] == discovery.JOB_STATUS_FAILED

    failure_records = [r for r in caplog.records if "not found" in r.message]
    assert failure_records, "expected the sprint-not-found log to be emitted"
    assert failure_records[-1].trace_id == "discovery-trace-id"
