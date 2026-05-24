"""Tests for agent_provisioning_team API endpoints."""

import threading
import time
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from agent_provisioning_team.api import main as api_main
from agent_provisioning_team.api.main import app

client = TestClient(app)


def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"


def test_root_endpoint():
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert "service" in data


def test_list_jobs_empty():
    with patch("agent_provisioning_team.api.main.list_jobs", return_value=[]):
        resp = client.get("/provision/jobs")
    assert resp.status_code == 200
    assert resp.json()["jobs"] == []


def test_get_status_not_found():
    with patch("agent_provisioning_team.api.main.get_job", return_value={}):
        resp = client.get("/provision/status/nonexistent-job")
    assert resp.status_code == 404


def test_start_provision_submits_to_executor():
    """/provision submits to the bounded executor instead of spawning a raw thread."""
    with (
        patch("agent_provisioning_team.api.main.create_job"),
        patch("agent_provisioning_team.api.main._ensure_executor") as mock_ensure,
    ):
        mock_executor = MagicMock()
        mock_future = MagicMock()
        mock_executor.submit.return_value = mock_future
        mock_ensure.return_value = mock_executor

        resp = client.post("/provision", json={"agent_id": "test-agent-001"})

    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data and len(data["job_id"]) > 0
    # The submission was routed through the executor, not threading.Thread.
    assert mock_executor.submit.called
    submitted_fn = mock_executor.submit.call_args[0][0]
    assert submitted_fn is api_main._run_provisioning_background


def test_bounded_concurrency_and_429(monkeypatch):
    """When the pending queue exceeds PROVISION_MAX_QUEUE_DEPTH, /provision returns 429
    without creating a job row. Concurrency never exceeds max_workers."""
    from concurrent.futures import ThreadPoolExecutor

    # Tight limits so we can saturate quickly.
    small_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="provision-test")
    monkeypatch.setattr(api_main, "_executor", small_executor)
    monkeypatch.setattr(api_main, "PROVISION_MAX_QUEUE_DEPTH", 2)

    gate = threading.Event()
    max_observed = [0]
    current = [0]
    lock = threading.Lock()

    def blocking_run(*args, **kwargs):
        with lock:
            current[0] += 1
            max_observed[0] = max(max_observed[0], current[0])
        gate.wait(timeout=5)
        with lock:
            current[0] -= 1

    monkeypatch.setattr(api_main, "_run_provisioning_background", blocking_run)
    create_job_calls = []
    monkeypatch.setattr(api_main, "create_job", lambda **kw: create_job_calls.append(kw))

    try:
        statuses = []
        # 6 requests: 2 run immediately, 2 queue, 2 should 429.
        for _ in range(6):
            resp = client.post("/provision", json={"agent_id": "load-test"})
            statuses.append(resp.status_code)
            # Let the executor pick up the first two so they start running.
            time.sleep(0.02)

        # Release all work so the executor can drain.
        gate.set()
        small_executor.shutdown(wait=True)

        assert statuses.count(429) == 2, f"expected 2 × 429, got {statuses}"
        assert statuses.count(200) == 4, f"expected 4 × 200, got {statuses}"
        assert max_observed[0] <= 2, f"concurrency exceeded max_workers: {max_observed[0]}"
        # 429s must not persist job rows.
        assert len(create_job_calls) == 4
    finally:
        small_executor.shutdown(wait=True)
        # Reset the module-level executor for subsequent tests.
        monkeypatch.setattr(api_main, "_executor", None)


def test_graceful_shutdown_compensates_inflight(monkeypatch):
    """On lifespan shutdown, any job still marked running gets `_compensate()`-ed
    and `mark_all_running_jobs_failed` is called as a backstop."""
    compensate_calls = []
    mark_failed_calls = []

    monkeypatch.setattr(
        api_main.orchestrator,
        "_compensate",
        lambda agent_id, tool_results: compensate_calls.append(agent_id),
    )
    monkeypatch.setattr(
        api_main,
        "list_jobs",
        lambda running_only=False: [{"agent_id": "stuck-agent-1", "status": "running"}],
    )
    monkeypatch.setattr(
        api_main,
        "mark_all_running_jobs_failed",
        lambda reason: mark_failed_calls.append(reason),
    )

    # Entering the TestClient context manager runs lifespan startup; exiting runs shutdown.
    with TestClient(app) as _c:
        pass

    assert compensate_calls == ["stuck-agent-1"]
    assert mark_failed_calls == ["shutdown"]


def test_compensate_timeout_does_not_block_shutdown(monkeypatch):
    """A slow `_compensate()` must not hold up graceful shutdown beyond
    COMPENSATE_TIMEOUT_S."""
    monkeypatch.setattr(api_main, "COMPENSATE_TIMEOUT_S", 0.2)

    def slow_compensate(agent_id, tool_results):
        time.sleep(5)  # would block shutdown if not timeout-wrapped

    monkeypatch.setattr(api_main.orchestrator, "_compensate", slow_compensate)
    monkeypatch.setattr(
        api_main,
        "list_jobs",
        lambda running_only=False: [{"agent_id": "slow-agent", "status": "running"}],
    )
    monkeypatch.setattr(api_main, "mark_all_running_jobs_failed", lambda reason: None)

    start = time.monotonic()
    with TestClient(app) as _c:
        pass
    elapsed = time.monotonic() - start

    # Shutdown must return well before the 5s slow_compensate would have finished.
    assert elapsed < 2.0, f"shutdown was blocked for {elapsed:.2f}s"


def test_deprovision_runs_via_orchestrator():
    from agent_provisioning_team.models import DeprovisionResponse

    mock_resp = DeprovisionResponse(agent_id="nonexistent-agent", success=False, error="not found")
    with patch("agent_provisioning_team.api.main.orchestrator") as mock_orch:
        mock_orch.deprovision.return_value = mock_resp
        resp = client.delete("/environments/nonexistent-agent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False


def test_cancel_job_not_found():
    with patch("agent_provisioning_team.api.main.get_job", return_value={}):
        resp = client.post("/provision/job/nonexistent/cancel")
    assert resp.status_code == 404
