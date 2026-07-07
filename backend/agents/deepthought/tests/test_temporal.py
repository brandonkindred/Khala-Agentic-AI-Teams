"""Tests for the deepthought Temporal wiring.

Covers the three pieces the runtime needs to actually dispatch through
Temporal instead of leaving the workflow as dead code:

1. ``temporal/__init__.py`` — the activity threads ``job_id`` through and owns
   the same job-store status transitions as the thread path, and the package
   no longer self-boots a worker at import.
2. ``temporal/worker.py`` — the no-arg boot function the team_service
   entrypoint calls via ``TEAM_TEMPORAL_WORKER_MODULE`` / ``_FUNC``.
3. ``temporal/start_workflow.py`` — the sync→async dispatch helper.
4. ``api/main.py`` — ``/deepthought/ask`` branches on ``is_temporal_enabled()``.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from deepthought.models import AgentResult, DeepthoughtResponse


def _sample_response() -> DeepthoughtResponse:
    return DeepthoughtResponse(
        answer="42",
        agent_tree=AgentResult(
            agent_id="root",
            agent_name="general_analyst",
            depth=0,
            focus_question="q",
            answer="42",
            confidence=0.9,
            child_results=[],
            was_decomposed=False,
        ),
        total_agents_spawned=1,
        max_depth_reached=0,
    )


# --------------------------------------------------------------------------- #
# temporal/__init__.py — activity job-store writeback + Pattern A shape
# --------------------------------------------------------------------------- #


def test_activity_writes_running_then_completed():
    """Happy path: activity flips RUNNING then COMPLETED with the result dump."""
    from deepthought.temporal import run_pipeline_activity

    mock_orch = MagicMock()
    mock_orch.process_message.return_value = _sample_response()

    with (
        patch("deepthought.orchestrator.DeepthoughtOrchestrator", return_value=mock_orch),
        patch("deepthought.shared.job_store.is_job_cancelled", return_value=False),
        patch("deepthought.shared.job_store.update_job") as mock_update,
    ):
        result = run_pipeline_activity("job-1", {"message": "q"})

    assert result["answer"] == "42"
    statuses = [c.kwargs.get("status") for c in mock_update.call_args_list]
    assert statuses == ["running", "completed"]
    # The completed write carries the serialized result.
    assert mock_update.call_args_list[-1].kwargs["result"]["answer"] == "42"


def test_activity_writes_failed_and_reraises():
    """On orchestrator error the activity records FAILED then re-raises."""
    from deepthought.temporal import run_pipeline_activity

    mock_orch = MagicMock()
    mock_orch.process_message.side_effect = RuntimeError("boom")

    with (
        patch("deepthought.orchestrator.DeepthoughtOrchestrator", return_value=mock_orch),
        patch("deepthought.shared.job_store.is_job_cancelled", return_value=False),
        patch("deepthought.shared.job_store.update_job") as mock_update,
        pytest.raises(RuntimeError, match="boom"),
    ):
        run_pipeline_activity("job-2", {"message": "q"})

    statuses = [c.kwargs.get("status") for c in mock_update.call_args_list]
    assert statuses == ["running", "failed"]
    assert mock_update.call_args_list[-1].kwargs["error"] == "boom"


def test_activity_short_circuits_when_cancelled():
    """A job cancelled before the activity runs is never dispatched."""
    from deepthought.temporal import run_pipeline_activity

    with (
        patch("deepthought.orchestrator.DeepthoughtOrchestrator") as mock_cls,
        patch("deepthought.shared.job_store.is_job_cancelled", return_value=True),
        patch("deepthought.shared.job_store.update_job") as mock_update,
    ):
        result = run_pipeline_activity("job-3", {"message": "q"})

    assert result == {}
    mock_cls.assert_not_called()
    mock_update.assert_not_called()


def test_activity_signature_takes_job_id_and_request():
    """Regression guard: the workflow passes (job_id, request)."""
    from deepthought.temporal import run_pipeline_activity

    params = list(inspect.signature(run_pipeline_activity).parameters)
    assert params == ["job_id", "request"]


def test_pattern_a_exports_present():
    import deepthought.temporal as dt

    assert dt.WORKFLOWS == [dt.DeepthoughtWorkflow]
    assert dt.ACTIVITIES == [dt.run_pipeline_activity]
    assert dt.TASK_QUEUE == "deepthought-queue"
    assert dt.WORKFLOW_ID_PREFIX == "deepthought-"


def test_importing_temporal_package_does_not_start_worker():
    """The package must not self-boot a worker at import (boot is worker.py)."""
    import shared_temporal

    for name in list(sys.modules):
        if name == "deepthought.temporal" or name.startswith("deepthought.temporal."):
            del sys.modules[name]

    with patch.object(shared_temporal, "start_team_worker") as patched:
        importlib.import_module("deepthought.temporal")
        assert patched.call_count == 0


# --------------------------------------------------------------------------- #
# temporal/worker.py
# --------------------------------------------------------------------------- #


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from deepthought.temporal.worker import start_deepthought_temporal_worker_thread

    assert start_deepthought_temporal_worker_thread() is False


def test_worker_start_delegates_when_enabled():
    from deepthought.temporal import worker

    with (
        patch.object(worker, "is_temporal_enabled", return_value=True),
        patch.object(worker, "start_team_worker", return_value=True) as mock_start,
    ):
        assert worker.start_deepthought_temporal_worker_thread() is True

    args, kwargs = mock_start.call_args
    assert args[0] == "deepthought"
    assert kwargs["task_queue"] == "deepthought-queue"


def test_worker_module_exposes_entrypoint_func():
    """team_service entrypoint resolves this name from the module by string."""
    from deepthought.temporal import worker

    assert callable(getattr(worker, "start_deepthought_temporal_worker_thread", None))


# --------------------------------------------------------------------------- #
# temporal/start_workflow.py
# --------------------------------------------------------------------------- #


def test_start_workflow_dispatches_with_prefixed_id():
    from deepthought.temporal import start_workflow as sw

    with patch.object(sw, "start_workflow_sync") as mock_start:
        sw.start_deepthought_workflow("abc", {"message": "q"})

    args, kwargs = mock_start.call_args
    assert args[0] is sw.DeepthoughtWorkflow.run
    assert args[1] == "abc"
    assert args[2] == {"message": "q"}
    assert kwargs["workflow_id"] == "deepthought-abc"
    assert kwargs["task_queue"] == "deepthought-queue"


# --------------------------------------------------------------------------- #
# api/main.py — dispatch branch
# --------------------------------------------------------------------------- #


def test_ask_uses_temporal_when_enabled():
    """With Temporal enabled, ``ask`` dispatches the workflow (no thread)."""
    from deepthought.api import main

    with (
        patch("shared_temporal.is_temporal_enabled", return_value=True),
        patch("deepthought.temporal.start_workflow.start_deepthought_workflow") as mock_start,
        patch.object(main, "create_job") as mock_create,
        patch("threading.Thread") as mock_thread,
    ):
        client = TestClient(main.app)
        resp = client.post("/deepthought/ask", json={"message": "q"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    mock_create.assert_called_once()
    mock_start.assert_called_once()
    mock_thread.assert_not_called()


def test_ask_falls_back_to_thread_when_temporal_disabled():
    """With Temporal disabled, ``ask`` keeps the existing thread path."""
    from deepthought.api import main

    with (
        patch("shared_temporal.is_temporal_enabled", return_value=False),
        patch.object(main, "create_job"),
        patch.object(main, "_run_deepthought_background"),
        patch("threading.Thread") as mock_thread,
    ):
        client = TestClient(main.app)
        resp = client.post("/deepthought/ask", json={"message": "q"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    mock_thread.assert_called_once()
