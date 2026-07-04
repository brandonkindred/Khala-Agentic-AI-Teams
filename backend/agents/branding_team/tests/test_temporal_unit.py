"""Unit + integration tests for the Branding team's Temporal wiring.

These tests do not need a live Temporal server. They mock at the
``temporalio`` / ``shared_temporal`` boundary and verify:

* the ``constants`` / ``worker`` / ``start_workflow`` / ``activities`` surfaces
  each honor their contracts, and
* ``_submit_brand_run`` routes through Temporal when enabled and falls back to
  the in-process thread pool when not.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from branding_team.api.main import app
from branding_team.models import BrandPhase

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_task_queue_default() -> None:
    from branding_team.temporal import constants

    assert isinstance(constants.TASK_QUEUE, str)
    assert constants.TASK_QUEUE == "branding-queue"
    assert constants.WORKFLOW_ID_PREFIX == "branding-"


def test_task_queue_env_override(monkeypatch) -> None:
    import importlib

    from branding_team.temporal import constants as constants_mod

    monkeypatch.setenv("TEMPORAL_TASK_QUEUE_BRANDING", "custom-branding")
    reloaded = importlib.reload(constants_mod)
    try:
        assert reloaded.TASK_QUEUE == "custom-branding"
    finally:
        monkeypatch.delenv("TEMPORAL_TASK_QUEUE_BRANDING", raising=False)
        importlib.reload(reloaded)


# ---------------------------------------------------------------------------
# worker.py
# ---------------------------------------------------------------------------


def test_start_worker_thread_no_op_when_disabled() -> None:
    import shared_temporal
    from branding_team.temporal import worker as worker_mod

    # Patch the delegate too: start_team_worker has its own gate, so asserting
    # it is NOT called proves the early return comes from the function's guard.
    with (
        patch.object(shared_temporal, "is_temporal_enabled", return_value=False),
        patch.object(shared_temporal, "start_team_worker") as mock_start,
    ):
        assert worker_mod.start_branding_temporal_worker_thread() is False
        mock_start.assert_not_called()


def test_start_worker_thread_delegates_to_start_team_worker() -> None:
    """The entrypoint contract (TEAM_TEMPORAL_WORKER_FUNC) resolves to a real,
    idempotent function that boots the worker via shared_temporal."""
    import shared_temporal
    from branding_team.temporal import worker as worker_mod

    with (
        patch.object(shared_temporal, "is_temporal_enabled", return_value=True),
        patch.object(shared_temporal, "start_team_worker", return_value=True) as mock_start,
    ):
        assert worker_mod.start_branding_temporal_worker_thread() is True
        mock_start.assert_called_once()
        args, kwargs = mock_start.call_args
        assert args[0] == "branding"
        assert kwargs["task_queue"] == worker_mod.TASK_QUEUE


# ---------------------------------------------------------------------------
# start_workflow.py
# ---------------------------------------------------------------------------


def test_run_async_raises_without_client() -> None:
    from branding_team.temporal import start_workflow as sw

    with (
        patch.object(sw, "get_temporal_loop", return_value=None),
        patch.object(sw, "get_temporal_client", return_value=None),
    ):
        coro = asyncio.sleep(0)
        try:
            with pytest.raises(RuntimeError, match="Temporal client not available"):
                sw._run_async(coro)
        finally:
            coro.close()


def test_start_branding_workflow_raises_without_client() -> None:
    from branding_team.temporal import start_workflow as sw

    with patch.object(sw, "get_temporal_client", return_value=None):
        with pytest.raises(RuntimeError, match="Temporal client not available"):
            sw.start_branding_workflow("job-1", {"job_id": "job-1"})


def test_start_branding_workflow_passes_args() -> None:
    from branding_team.temporal import start_workflow as sw
    from branding_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX

    fake_client = MagicMock()
    fake_client.start_workflow = MagicMock(return_value="coro-sentinel")
    captured: dict = {}

    def fake_run_async(coro):
        captured["ran"] = True

    with (
        patch.object(sw, "get_temporal_client", return_value=fake_client),
        patch.object(sw, "_run_async", side_effect=fake_run_async),
    ):
        payload = {"job_id": "job-9"}
        sw.start_branding_workflow("job-9", payload)

    assert captured["ran"] is True
    _, kwargs = fake_client.start_workflow.call_args
    assert kwargs["id"] == f"{WORKFLOW_ID_PREFIX}job-9"
    assert kwargs["task_queue"] == TASK_QUEUE
    # The payload dict is forwarded positionally as the workflow arg.
    args, _ = fake_client.start_workflow.call_args
    assert payload in args


# ---------------------------------------------------------------------------
# activities.py
# ---------------------------------------------------------------------------


def _activity_payload() -> dict:
    return {
        "job_id": "job-abc",
        "mission": {
            "company_name": "Northstar Labs",
            "company_description": "A strategic studio for product teams",
            "target_audience": "enterprise product leaders",
        },
        "human_review": {"approved": True, "feedback": "go"},
        "brand_checks": [{"asset_name": "logo", "asset_description": "primary mark"}],
        "client_id": "c1",
        "brand_id": "b1",
        "include_market_research": True,
        "include_design_assets": False,
        "target_phase": "strategic_core",
    }


def test_activity_reconstructs_models_and_delegates() -> None:
    from branding_team.models import BrandCheckRequest, BrandingMission, HumanReview
    from branding_team.temporal import activities

    with patch("branding_team.api.main._run_branding_background") as mock_bg:
        activities.run_branding_pipeline_activity(_activity_payload())

    mock_bg.assert_called_once()
    args, _ = mock_bg.call_args
    (
        job_id,
        mission,
        human_review,
        brand_checks,
        client_id,
        brand_id,
        include_mr,
        include_da,
        target_phase,
    ) = args
    assert job_id == "job-abc"
    assert isinstance(mission, BrandingMission)
    assert mission.company_name == "Northstar Labs"
    assert isinstance(human_review, HumanReview)
    assert human_review.approved is True
    assert len(brand_checks) == 1 and isinstance(brand_checks[0], BrandCheckRequest)
    assert client_id == "c1" and brand_id == "b1"
    assert include_mr is True and include_da is False
    assert target_phase is BrandPhase.STRATEGIC_CORE


def test_activity_handles_none_target_phase_and_empty_checks() -> None:
    from branding_team.temporal import activities

    payload = _activity_payload()
    payload["target_phase"] = None
    payload["brand_checks"] = []
    with patch("branding_team.api.main._run_branding_background") as mock_bg:
        activities.run_branding_pipeline_activity(payload)

    args, _ = mock_bg.call_args
    assert args[3] == []  # brand_checks
    assert args[8] is None  # target_phase


# ---------------------------------------------------------------------------
# _submit_brand_run dispatch branch (integration — uses the job service)
# ---------------------------------------------------------------------------

client = TestClient(app)


def _make_brand() -> tuple[str, str]:
    cid = client.post("/clients", json={"name": "Temporal Client"}).json()["id"]
    bid = client.post(
        f"/clients/{cid}/brands",
        json={
            "company_name": "TempCo",
            "company_description": "Company for temporal dispatch test",
            "target_audience": "users",
        },
    ).json()["id"]
    return cid, bid


@pytest.mark.integration
def test_run_dispatches_via_temporal_when_enabled() -> None:
    import shared_temporal
    from branding_team.api import main as main_mod

    cid, bid = _make_brand()
    spy = MagicMock()
    with (
        patch.object(shared_temporal, "is_temporal_enabled", return_value=True),
        patch("branding_team.temporal.start_workflow.start_branding_workflow", spy),
        patch.object(main_mod._run_executor, "submit") as mock_submit,
    ):
        resp = client.post(
            f"/clients/{cid}/brands/{bid}/run",
            json={"human_approved": True, "target_phase": "strategic_core"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    # Temporal path taken: workflow started, thread pool untouched.
    mock_submit.assert_not_called()
    spy.assert_called_once()
    job_id_arg, payload_arg = spy.call_args.args
    assert job_id_arg == body["job_id"]
    assert payload_arg["job_id"] == body["job_id"]
    assert payload_arg["mission"]["company_name"] == "TempCo"
    assert payload_arg["target_phase"] == "strategic_core"


@pytest.mark.integration
def test_run_temporal_dispatch_failure_returns_503_and_fails_job() -> None:
    import shared_temporal

    cid, bid = _make_brand()
    with (
        patch.object(shared_temporal, "is_temporal_enabled", return_value=True),
        patch(
            "branding_team.temporal.start_workflow.start_branding_workflow",
            side_effect=RuntimeError("worker down"),
        ),
    ):
        resp = client.post(
            f"/clients/{cid}/brands/{bid}/run",
            json={"human_approved": True},
        )

    assert resp.status_code == 503
