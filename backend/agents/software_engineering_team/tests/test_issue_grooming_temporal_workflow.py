"""The issue grooming Temporal module is a Pattern-A export scaffold.

Guards the scaffold's contract: importing it never boots a worker or touches
``TEMPORAL_ADDRESS``, it exports ``WORKFLOWS``/``ACTIVITIES``, its typed
request payload validates, and its stub activity/workflow bodies raise
``NotImplementedError`` rather than silently doing nothing.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from software_engineering_team.temporal.issue_grooming_workflow import (
    ACTIVITIES,
    WORKFLOWS,
    IssueGroomingRunRequest,
    IssueGroomingRunResult,
    IssueGroomingWorkflow,
    run_issue_grooming_activity,
)

_VALID_REQUEST = {"job_id": "j1", "owner": "acme", "repo": "widgets", "issue_number": 42}


def test_import_does_not_require_temporal_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the module must not depend on ``TEMPORAL_ADDRESS`` being set.

    Preconditions: none.
    Postconditions: re-importing the already-loaded module raises nothing,
    proving no import-time worker boot or env read is load-bearing.
    """
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    import importlib

    import software_engineering_team.temporal.issue_grooming_workflow as mod

    importlib.reload(mod)


def test_exports_workflows_and_activities() -> None:
    assert WORKFLOWS == [IssueGroomingWorkflow]
    assert ACTIVITIES == [run_issue_grooming_activity]


def test_run_request_validates_required_fields() -> None:
    IssueGroomingRunRequest.model_validate(_VALID_REQUEST)
    with pytest.raises(ValidationError):
        IssueGroomingRunRequest.model_validate({"owner": "acme", "repo": "widgets"})


def test_run_result_defaults() -> None:
    result = IssueGroomingRunResult(status="pending")
    assert result.phase is None
    assert result.grooming is None


def test_activity_stub_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        run_issue_grooming_activity(_VALID_REQUEST)


def test_activity_stub_validates_before_raising() -> None:
    with pytest.raises(ValidationError):
        run_issue_grooming_activity({"owner": "acme"})


def test_workflow_stub_raises_not_implemented() -> None:
    workflow_obj = IssueGroomingWorkflow()
    with pytest.raises(NotImplementedError):
        asyncio.run(workflow_obj.run(_VALID_REQUEST))


def test_workflow_stub_validates_before_raising() -> None:
    workflow_obj = IssueGroomingWorkflow()
    with pytest.raises(ValidationError):
        asyncio.run(workflow_obj.run({"owner": "acme"}))
