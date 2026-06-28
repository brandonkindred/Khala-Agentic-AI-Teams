"""Coverage for strategy-lab run lifecycle routes in ``api.main``.

Targets the routes that interact with the in-memory ``_active_runs``
dict and the ``_get_lab_run_job_client`` shim:

* ``run_strategy_lab`` — 409 when a run is already active.
* ``run_strategy_lab`` — happy path (worker thread is stubbed so the
  route can return immediately).
* ``resume_strategy_lab_run`` — 404 + 400 + 409 + happy paths.
* ``restart_strategy_lab_run`` — 404 + 400 + 409 + happy paths.
* ``list_strategy_lab_runs`` — terminal-status reconciliation + persisted
  job merge.
* ``list_strategy_lab_jobs`` — persisted-job merge.
* ``get_strategy_lab_run_status`` — terminal-status reconciliation +
  load-from-job-service fallback.
* ``stream_strategy_lab_run`` — terminal-state short-circuit + 404.

Every test patches the JobService shim and the worker so no real
strategy-lab cycles execute.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest


class _InMemoryDict:
    def __init__(self) -> None:
        self._d: Dict[str, Any] = {}

    def __setitem__(self, k, v):
        self._d[k] = v

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)

    def __contains__(self, k):
        return k in self._d

    def __delitem__(self, k):
        self._d.pop(k, None)

    def pop(self, k, *args):
        if args:
            return self._d.pop(k, args[0])
        return self._d.pop(k)

    def values(self):
        return list(self._d.values())


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from investment_team.api import main as api_main

    for attr in (
        "_profiles",
        "_proposals",
        "_strategies",
        "_validations",
        "_backtests",
        "_strategy_lab_records",
        "_paper_trading_sessions",
        "_advisor_sessions",
    ):
        monkeypatch.setattr(api_main, attr, _InMemoryDict())

    monkeypatch.setattr(api_main, "_active_runs", {})

    # Stop real threads from spawning.
    monkeypatch.setattr(api_main, "_strategy_lab_worker", lambda *a, **k: None)

    # Stub the persistence calls so they don't try to reach the job service.
    monkeypatch.setattr(api_main, "_persist_run_state", lambda *a, **k: None)

    return TestClient(api_main.app)


class _StubLabClient:
    """Fake JobServiceClient for /strategy-lab/runs/* endpoints."""

    def __init__(self, jobs: Optional[List[Dict[str, Any]]] = None) -> None:
        self.jobs = list(jobs or [])
        self.by_id: Dict[str, Dict[str, Any]] = {j["job_id"]: j for j in self.jobs if "job_id" in j}
        self.deleted: List[str] = []

    def list_jobs(self, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if statuses:
            return [j for j in self.jobs if j.get("status") in statuses]
        return list(self.jobs)

    def get_job(self, jid: str) -> Optional[Dict[str, Any]]:
        return dict(self.by_id[jid]) if jid in self.by_id else None

    def delete_job(self, jid: str) -> bool:
        self.deleted.append(jid)
        return jid in self.by_id


# ---------------------------------------------------------------------------
# run_strategy_lab
# ---------------------------------------------------------------------------


def test_run_strategy_lab_returns_409_when_already_running(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["existing"] = {"run_id": "existing", "status": "running"}
    resp = api_client.post("/strategy-lab/run", json={})
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["detail"]


def test_run_strategy_lab_starts_run_when_idle(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    resp = api_client.post(
        "/strategy-lab/run",
        json={"batch_size": 2, "batch_count": 1, "max_parallel": 1, "paper_trading_enabled": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"].startswith("run-")
    assert body["total_cycles"] == 2
    # The run was registered.
    assert body["run_id"] in api_main._active_runs


# ---------------------------------------------------------------------------
# resume_strategy_lab_run + restart_strategy_lab_run
# ---------------------------------------------------------------------------


def _resumable_state(run_id: str = "run-r1", **overrides: Any) -> Dict[str, Any]:
    base = {
        "run_id": run_id,
        "status": "interrupted",
        "started_at": "2024-01-01T00:00:00Z",
        "total_cycles": 4,
        "completed_cycles": 2,
        "contiguous_cycles": 2,
        "request_payload": {
            "start_date": "2021-01-01",
            "end_date": "2024-12-31",
            "initial_capital": 100_000.0,
            "benchmark_symbol": "SPY",
            "transaction_cost_bps": 5.0,
            "slippage_bps": 2.0,
            "batch_size": 2,
            "batch_count": 2,
            "max_parallel": 1,
            "paper_trading_enabled": False,
            "paper_trading_lookback_days": 365,
        },
    }
    base.update(overrides)
    return base


def test_resume_strategy_lab_run_404_when_missing(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _StubLabClient())
    resp = api_client.post("/strategy-lab/runs/nope/resume")
    assert resp.status_code == 404


def test_resume_strategy_lab_run_400_when_state_not_resumable(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["run-a"] = {
        "run_id": "run-a",
        "status": "completed",  # not in RESUMABLE_STATUSES
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _StubLabClient())
    resp = api_client.post("/strategy-lab/runs/run-a/resume")
    assert resp.status_code == 400


def test_resume_strategy_lab_run_400_when_payload_missing(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["run-b"] = _resumable_state("run-b", request_payload=None)
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _StubLabClient())
    resp = api_client.post("/strategy-lab/runs/run-b/resume")
    assert resp.status_code == 400


def test_resume_strategy_lab_run_409_when_another_active(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["run-c"] = _resumable_state("run-c")
    api_main._active_runs["other"] = {"run_id": "other", "status": "running"}
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _StubLabClient())
    resp = api_client.post("/strategy-lab/runs/run-c/resume")
    assert resp.status_code == 409


def test_resume_strategy_lab_run_happy_path(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["run-d"] = _resumable_state("run-d")
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _StubLabClient())
    resp = api_client.post("/strategy-lab/runs/run-d/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-d"
    assert "resumed from cycle" in body["message"]


def test_restart_strategy_lab_run_404(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _StubLabClient())
    resp = api_client.post("/strategy-lab/runs/nope/restart")
    assert resp.status_code == 404


def test_restart_strategy_lab_run_400_when_payload_missing(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["run-e"] = {
        "run_id": "run-e",
        "status": "completed",
        "request_payload": None,
    }
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _StubLabClient())
    resp = api_client.post("/strategy-lab/runs/run-e/restart")
    assert resp.status_code == 400


def test_restart_strategy_lab_run_409_when_other_active(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["run-f"] = {
        "run_id": "run-f",
        "status": "completed",
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }
    api_main._active_runs["other"] = {"run_id": "other", "status": "running"}
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _StubLabClient())
    resp = api_client.post("/strategy-lab/runs/run-f/restart")
    assert resp.status_code == 409


def test_restart_strategy_lab_run_happy_path(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["run-g"] = {
        "run_id": "run-g",
        "status": "completed_with_errors",  # extended restartable set
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _StubLabClient())
    resp = api_client.post("/strategy-lab/runs/run-g/restart")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-g"
    assert "restarted" in body["message"]


# ---------------------------------------------------------------------------
# list_strategy_lab_runs — reconciliation + persisted merge
# ---------------------------------------------------------------------------


def test_list_strategy_lab_runs_reconciles_terminal_job_service_status(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    # In-memory says "running" but job service has the run as "cancelled".
    api_main._active_runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "started_at": "2024-01-01T00:00:00Z",
        "total_cycles": 5,
    }
    stub = _StubLabClient(
        jobs=[
            {"job_id": "run-1", "status": "cancelled", "error": "user request"},
        ]
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.get("/strategy-lab/runs")
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) == 1
    # Status was reconciled to cancelled.
    assert runs[0]["status"] == "cancelled"
    assert "user request" in (runs[0]["error"] or "")


def test_list_strategy_lab_runs_merges_persisted_only_runs(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs.clear()
    stub = _StubLabClient(
        jobs=[
            {
                "job_id": "run-x",
                "status": "running",
                "data": {
                    "started_at": "2024-01-01T00:00:00Z",
                    "total_cycles": 3,
                    "completed_cycles": 1,
                    "batch_size": 1,
                    "batch_count": 3,
                },
            }
        ]
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    resp = api_client.get("/strategy-lab/runs")
    body = resp.json()
    assert any(r["run_id"] == "run-x" for r in body["runs"])


def test_list_strategy_lab_runs_falls_back_when_job_service_broken(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["mem-only"] = {
        "run_id": "mem-only",
        "status": "running",
        "started_at": "2024-01-01T00:00:00Z",
        "total_cycles": 2,
    }

    class _Broken:
        def get_job(self, *a, **k):
            raise RuntimeError("backend down")

        def list_jobs(self, *a, **k):
            raise RuntimeError("backend down")

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _Broken())
    resp = api_client.get("/strategy-lab/runs")
    body = resp.json()
    assert any(r["run_id"] == "mem-only" for r in body["runs"])


# ---------------------------------------------------------------------------
# list_strategy_lab_jobs — persisted merge + running filter
# ---------------------------------------------------------------------------


def test_list_strategy_lab_jobs_merges_persisted_completed_runs(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    # In-memory running run.
    api_main._active_runs["mem-r"] = {
        "run_id": "mem-r",
        "status": "running",
        "total_cycles": 4,
        "completed_cycles": 1,
        "started_at": "2024-01-01T00:00:00Z",
        "current_cycle": {"phase": "ideation", "strategy": {"hypothesis": "test"}},
    }
    stub = _StubLabClient(
        jobs=[
            {
                "job_id": "persisted-c",
                "status": "completed",
                "data": {
                    "started_at": "2024-01-01T00:00:00Z",
                    "total_cycles": 2,
                    "completed_cycles": 2,
                },
            }
        ]
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    resp = api_client.get("/strategy-lab/jobs")
    body = resp.json()
    ids = {j["job_id"] for j in body["jobs"]}
    assert "mem-r" in ids
    assert "persisted-c" in ids

    # running_only filter.
    resp2 = api_client.get("/strategy-lab/jobs?running_only=true")
    body2 = resp2.json()
    ids2 = {j["job_id"] for j in body2["jobs"]}
    assert "mem-r" in ids2
    assert "persisted-c" not in ids2


# ---------------------------------------------------------------------------
# get_strategy_lab_run_status — reconciliation + load fallback
# ---------------------------------------------------------------------------


def test_get_strategy_lab_run_status_reconciles_terminal(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["run-r"] = {
        "run_id": "run-r",
        "status": "running",
        "started_at": "2024-01-01T00:00:00Z",
        "total_cycles": 3,
    }
    stub = _StubLabClient(jobs=[{"job_id": "run-r", "status": "failed", "error": "boom"}])
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    resp = api_client.get("/strategy-lab/runs/run-r/status")
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"] == "boom"


def test_get_strategy_lab_run_status_loads_from_job_service_when_absent(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs.clear()
    monkeypatch.setattr(
        api_main,
        "_load_run_from_job_service",
        lambda rid: {
            "run_id": rid,
            "status": "completed",
            "started_at": "2024-01-01T00:00:00Z",
            "total_cycles": 1,
        },
    )
    resp = api_client.get("/strategy-lab/runs/loaded/status")
    body = resp.json()
    assert body["status"] == "completed"


# ---------------------------------------------------------------------------
# delete_strategy_lab_run — 404 already covered; happy path now.
# ---------------------------------------------------------------------------


def test_delete_strategy_lab_run_success(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["delete-me"] = {"run_id": "delete-me", "status": "completed"}
    stub = _StubLabClient(jobs=[{"job_id": "delete-me", "status": "completed"}])
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    resp = api_client.delete("/strategy-lab/runs/delete-me")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    # And the in-memory entry was popped.
    assert "delete-me" not in api_main._active_runs


# ---------------------------------------------------------------------------
# stream_strategy_lab_run — terminal short-circuit + 404
# ---------------------------------------------------------------------------


def test_stream_strategy_lab_run_404(monkeypatch: pytest.MonkeyPatch, api_client) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs.clear()
    monkeypatch.setattr(api_main, "_load_run_from_job_service", lambda rid: None)
    resp = api_client.get("/strategy-lab/runs/nope/stream")
    assert resp.status_code == 404


def test_stream_strategy_lab_run_terminal_short_circuit(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["done"] = {
        "run_id": "done",
        "status": "completed",
        "started_at": "2024-01-01T00:00:00Z",
        "total_cycles": 1,
        "completed_cycles": 1,
    }
    resp = api_client.get("/strategy-lab/runs/done/stream")
    # Terminal runs return a complete SSE response synchronously.
    assert resp.status_code == 200
    body = resp.text
    assert "snapshot" in body
    assert "done" in body


# ---------------------------------------------------------------------------
# stream_strategy_lab_run — active (non-terminal) event_generator path
# ---------------------------------------------------------------------------
#
# The generator subscribes to the per-job event bus, yields an initial
# "snapshot", drains any buffered events, and terminates as soon as it sees a
# "complete" or "error" event (followed by a "done" sentinel). These tests
# pre-load the subscription's deque so the loop drains and returns on the
# first pass, never sleeping. Runtime is bounded by both the synchronous
# pre-load and a 2s ``read`` timeout on the TestClient stream so a regression
# can't hang CI.


def _wait_for_terminal_sse(body_iter, *, max_chunks: int = 50, timeout_seconds: float = 2.0) -> str:
    """Read SSE chunks until the terminal ``data: {"type": "done"}`` line.

    Preconditions:
        * ``body_iter`` is an iterator over UTF-8 string chunks (TestClient
          ``iter_text()``).
        * ``max_chunks`` and ``timeout_seconds`` are positive.

    Postconditions:
        * Returns the concatenated body up to and including the ``done`` line.
        * Raises ``AssertionError`` if the terminal line is not seen within
          ``max_chunks`` chunks or ``timeout_seconds`` wall-clock seconds.
    """
    import time as _time

    assert max_chunks > 0
    assert timeout_seconds > 0
    buf = ""
    deadline = _time.monotonic() + timeout_seconds
    seen = 0
    for chunk in body_iter:
        buf += chunk
        seen += 1
        if '"type": "done"' in buf or '"type":"done"' in buf:
            return buf
        assert seen <= max_chunks, f"SSE stream exceeded {max_chunks} chunks without terminating"
        assert _time.monotonic() < deadline, "SSE stream did not terminate within timeout"
    return buf


def test_stream_strategy_lab_run_emits_snapshot_update_and_terminates(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Drive the live event_generator path end-to-end.

    Pre-loads the subscription deque with one ``progress`` update and one
    ``complete`` terminal so the generator drains and exits on its first
    iteration, never reaching ``await asyncio.sleep``.
    """
    from collections import deque

    from investment_team.api import job_event_bus
    from investment_team.api import main as api_main

    api_main._active_runs["active"] = {
        "run_id": "active",
        "status": "running",
        "started_at": "2024-01-01T00:00:00Z",
        "total_cycles": 2,
        "completed_cycles": 0,
    }

    pre_events = deque(
        [
            {"type": "progress", "phase": "design", "cycle_index": 1},
            {"type": "complete", "summary": "ok"},
        ]
    )

    class _Sub:
        def __init__(self) -> None:
            self.events = pre_events

        def touch(self) -> None:  # reaper-liveness signal, no-op for the fake
            pass

    sub_holder = {"sub": None, "unsubscribed": False}

    def _fake_subscribe(rid: str):
        assert rid == "active"
        sub_holder["sub"] = _Sub()
        return sub_holder["sub"]

    def _fake_unsubscribe(rid: str, sub) -> None:
        sub_holder["unsubscribed"] = True

    monkeypatch.setattr(job_event_bus, "subscribe", _fake_subscribe)
    monkeypatch.setattr(job_event_bus, "unsubscribe", _fake_unsubscribe)

    with api_client.stream("GET", "/strategy-lab/runs/active/stream", timeout=2.0) as resp:
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        body = _wait_for_terminal_sse(resp.iter_text())

    # Snapshot, the in-flight progress update, the terminal "complete",
    # and the final "done" sentinel must all have been streamed.
    assert '"type": "snapshot"' in body
    assert '"type": "progress"' in body
    assert '"type": "complete"' in body
    assert '"type": "done"' in body
    # ``finally`` branch must have run, releasing the bus subscription.
    assert sub_holder["unsubscribed"] is True


def test_stream_strategy_lab_run_terminates_on_error_event(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """An ``error`` event must also trigger the terminal ``done`` sentinel."""
    from collections import deque

    from investment_team.api import job_event_bus
    from investment_team.api import main as api_main

    api_main._active_runs["boom"] = {
        "run_id": "boom",
        "status": "running",
        "started_at": "2024-01-01T00:00:00Z",
        "total_cycles": 1,
        "completed_cycles": 0,
    }

    pre_events = deque([{"type": "error", "error": "kaboom"}])

    class _Sub:
        def __init__(self) -> None:
            self.events = pre_events

        def touch(self) -> None:  # reaper-liveness signal, no-op for the fake
            pass

    monkeypatch.setattr(job_event_bus, "subscribe", lambda rid: _Sub())
    monkeypatch.setattr(job_event_bus, "unsubscribe", lambda rid, sub: None)

    with api_client.stream("GET", "/strategy-lab/runs/boom/stream", timeout=2.0) as resp:
        assert resp.status_code == 200
        body = _wait_for_terminal_sse(resp.iter_text())

    assert '"type": "snapshot"' in body
    assert '"type": "error"' in body
    assert '"type": "done"' in body
