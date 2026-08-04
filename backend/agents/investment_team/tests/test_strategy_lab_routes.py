"""Coverage for strategy-lab run lifecycle routes in ``api.main``.

Targets the routes that interact with the in-memory ``_active_runs``
dict and the ``_get_lab_run_job_client`` shim:

* ``run_strategy_lab`` — 409 when a run is already active.
* ``run_strategy_lab`` — happy path (Temporal dispatch is stubbed so the
  route can return immediately).
* ``resume_strategy_lab_run`` — 404 + 400 + 409 + happy paths.
* ``restart_strategy_lab_run`` — 404 + 400 + 409 + happy paths.
* run/resume/restart — same-run_id transition-lock serialization (#4028).
* ``list_strategy_lab_runs`` — terminal-status reconciliation + persisted
  job merge.
* ``list_strategy_lab_jobs`` — persisted-job merge.
* ``get_strategy_lab_run_status`` — terminal-status reconciliation +
  load-from-job-service fallback.
* ``stream_strategy_lab_run`` — terminal-state short-circuit + 404.

Every test patches the JobService shim and the Temporal dispatch so no real
strategy-lab cycles execute.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest


class _InMemoryDict:
    """Minimal dict-like stand-in used to monkeypatch module-level storage dicts.

    Implements the full mapping protocol (``__iter__``, ``__len__``, ``keys``,
    ``items``, ``update``, ``setdefault``, ...) so it's a safe substitute
    anywhere the production code treats ``_profiles``/``_proposals``/etc. as a
    plain dict, including iteration or ``len()``.
    """

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
        del self._d[k]

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)

    def pop(self, k, *args):
        if args:
            return self._d.pop(k, args[0])
        return self._d.pop(k)

    def keys(self):
        return self._d.keys()

    def items(self):
        return self._d.items()

    def values(self):
        return list(self._d.values())

    def update(self, *args, **kwargs):
        self._d.update(*args, **kwargs)

    def setdefault(self, k, default=None):
        return self._d.setdefault(k, default)


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch):
    """Return a ``TestClient`` wired to a fresh, isolated strategy-lab API state.

    Rebinds every module-level storage dict (``_profiles``, ``_active_runs``,
    the transition-lock registry, ...) to per-test instances and stubs out
    Temporal dispatch/termination and job-service persistence, so no test
    leaks state into another or requires a real Temporal worker / job service.
    """
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

    # Rebind the shared run store to a fresh dict for test isolation. Patch both
    # the ``api.main`` alias and the source module attribute to the *same* object,
    # so direct reads/writes (routes) and ``_get_run_state`` (which closes over
    # ``run_state.active_runs``) observe one consistent store.
    from investment_team.strategy_lab import run_state as _run_state

    shared_runs: Dict[str, Any] = {}
    monkeypatch.setattr(api_main, "_active_runs", shared_runs)
    monkeypatch.setattr(_run_state, "active_runs", shared_runs)

    # Reset the per-run_id transition-lock registry too, so a test that
    # deliberately pre-holds a lock to simulate contention can't leak it into
    # a later test that happens to reuse the same run_id string.
    monkeypatch.setattr(_run_state, "_run_transition_locks", {})

    # Stub the Temporal dispatch so no real workflow start is attempted.
    monkeypatch.setattr(api_main, "_dispatch_strategy_lab_run", lambda *a, **k: None)

    # restart_strategy_lab_run calls _require_temporal() and
    # terminate_and_await_workflow_sync() directly (to resolve any prior
    # execution before resetting state), independent of the dispatch stub
    # above — stub those too so restart's happy path doesn't need a real
    # Temporal worker either.
    import shared.temporal

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(shared.temporal, "terminate_and_await_workflow_sync", lambda *a, **k: None)

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
        if jid not in self.by_id:
            return False
        self.by_id.pop(jid)
        self.jobs = [j for j in self.jobs if j.get("job_id") != jid]
        return True


@pytest.fixture
def lab_job_client(monkeypatch: pytest.MonkeyPatch) -> "_StubLabClient":
    """Patch ``_get_lab_run_job_client`` to return a fresh, empty ``_StubLabClient``.

    Centralizes the ``monkeypatch.setattr(api_main, "_get_lab_run_job_client",
    lambda: _StubLabClient())`` boilerplate repeated across tests that only
    need Temporal-adjacent code paths to avoid touching a real job-service
    client and don't care about pre-seeded jobs. Tests that need specific
    persisted jobs should construct their own ``_StubLabClient(jobs=[...])``
    and patch it directly instead of using this fixture.
    """
    from investment_team.api import main as api_main

    stub = _StubLabClient()
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    return stub


# ---------------------------------------------------------------------------
# _StubLabClient.get_job contract
# ---------------------------------------------------------------------------


def test_stub_lab_client_get_job_returns_none_for_unknown_id() -> None:
    """``get_job`` returns ``None`` for a job_id that was never seeded."""
    stub = _StubLabClient()
    assert stub.get_job("missing-id") is None


def test_stub_lab_client_get_job_returns_copy_for_known_id() -> None:
    """``get_job`` returns an equal but distinct copy, not the stored object itself."""
    job = {"job_id": "run-1", "status": "completed", "data": {"total_cycles": 2}}
    stub = _StubLabClient(jobs=[job])
    got = stub.get_job("run-1")
    assert got == job
    assert got is not job
    assert got is not stub.by_id["run-1"]


# ---------------------------------------------------------------------------
# run_strategy_lab
# ---------------------------------------------------------------------------


def test_run_strategy_lab_returns_409_when_already_running(api_client) -> None:
    """Starting a run while another is already active is rejected with 409."""
    from investment_team.api import main as api_main

    api_main._active_runs["existing"] = {"run_id": "existing", "status": "running"}
    resp = api_client.post("/strategy-lab/run", json={})
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["detail"]


def test_run_strategy_lab_starts_run_when_idle(api_client) -> None:
    """A run started while idle mints a run_id, registers it, and returns 200."""
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
    """Return a baseline ``_active_runs`` entry for an interrupted, resumable run.

    The returned dict represents a run interrupted after 2 of 4 cycles, with a
    complete ``request_payload`` so resume/restart can rebuild dispatch state.
    Caller-supplied ``overrides`` are shallow-merged on top of the base dict.
    """
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


def test_resume_strategy_lab_run_404_when_missing(lab_job_client, api_client) -> None:
    """Resuming a run_id with no in-memory or persisted state returns 404."""
    resp = api_client.post("/strategy-lab/runs/nope/resume")
    assert resp.status_code == 404


def test_resume_strategy_lab_run_400_when_state_not_resumable(lab_job_client, api_client) -> None:
    """A run whose status isn't in ``RESUMABLE_STATUSES`` (e.g. ``completed``) returns 400."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-a"] = {
        "run_id": "run-a",
        "status": "completed",  # not in RESUMABLE_STATUSES
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }
    resp = api_client.post("/strategy-lab/runs/run-a/resume")
    assert resp.status_code == 400


def test_resume_strategy_lab_run_400_when_payload_missing(lab_job_client, api_client) -> None:
    """A resumable run with no stored ``request_payload`` to rebuild dispatch from returns 400."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-b"] = _resumable_state("run-b", request_payload=None)
    resp = api_client.post("/strategy-lab/runs/run-b/resume")
    assert resp.status_code == 400


def test_resume_strategy_lab_run_409_when_another_active(lab_job_client, api_client) -> None:
    """Resuming is rejected with 409 while a different run_id is already active."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-c"] = _resumable_state("run-c")
    api_main._active_runs["other"] = {"run_id": "other", "status": "running"}
    resp = api_client.post("/strategy-lab/runs/run-c/resume")
    assert resp.status_code == 409


def test_resume_strategy_lab_run_happy_path(lab_job_client, api_client) -> None:
    """A resumable run with no conflicting active run resumes and reports its cycle offset."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-d"] = _resumable_state("run-d")
    resp = api_client.post("/strategy-lab/runs/run-d/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-d"
    assert "resumed from cycle" in body["message"]


def test_resume_strategy_lab_run_carries_forward_tracker_merge_error_count(
    lab_job_client, api_client
) -> None:
    """Regression: resume must not silently reset tracker_merge_error_count to 0
    while errored_cycles/errored_details (which include the same tracker-merge
    entries) carry forward — that would reintroduce the double-count bug the
    counter exists to fix, but only for resumed runs."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-h"] = _resumable_state(
        "run-h",
        errored_cycles=3,
        errored_details=[{"cycle_index": 1, "error": "merge boom", "reason": "tracker_merge_failed"}],
        tracker_merge_error_count=3,
    )
    resp = api_client.post("/strategy-lab/runs/run-h/resume")
    assert resp.status_code == 200
    assert api_main._active_runs["run-h"]["tracker_merge_error_count"] == 3
    assert api_main._active_runs["run-h"]["errored_cycles"] == 3


def test_restart_strategy_lab_run_404(lab_job_client, api_client) -> None:
    """Restarting a run_id with no in-memory or persisted state returns 404."""
    resp = api_client.post("/strategy-lab/runs/nope/restart")
    assert resp.status_code == 404


def test_restart_strategy_lab_run_400_when_payload_missing(lab_job_client, api_client) -> None:
    """A restartable run with no stored ``request_payload`` to rebuild dispatch from returns 400."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-e"] = {
        "run_id": "run-e",
        "status": "completed",
        "request_payload": None,
    }
    resp = api_client.post("/strategy-lab/runs/run-e/restart")
    assert resp.status_code == 400


def test_restart_strategy_lab_run_409_when_other_active(lab_job_client, api_client) -> None:
    """Restarting is rejected with 409 while a different run_id is already active."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-f"] = {
        "run_id": "run-f",
        "status": "completed",
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }
    api_main._active_runs["other"] = {"run_id": "other", "status": "running"}
    resp = api_client.post("/strategy-lab/runs/run-f/restart")
    assert resp.status_code == 409


def test_restart_strategy_lab_run_happy_path(lab_job_client, api_client) -> None:
    """A run in the extended restartable set (``completed_with_errors``) restarts from scratch."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-g"] = {
        "run_id": "run-g",
        "status": "completed_with_errors",  # extended restartable set
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }
    resp = api_client.post("/strategy-lab/runs/run-g/restart")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-g"
    assert "restarted" in body["message"]


# ---------------------------------------------------------------------------
# run/resume/restart — same-run_id transition-lock serialization (#4028)
# ---------------------------------------------------------------------------


def test_run_strategy_lab_returns_409_when_transition_lock_held_for_minted_run_id(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A freshly-minted run_id whose transition lock is already held (a vanishingly
    unlikely uuid4 collision, forced here) is rejected with 409 and never
    registered in ``_active_runs``."""
    import uuid as uuid_module

    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state as _run_state

    fixed_uuid = uuid_module.UUID("12345678-1234-5678-1234-567812345678")
    monkeypatch.setattr(api_main.uuid, "uuid4", lambda: fixed_uuid)
    run_id = f"run-{fixed_uuid.hex[:8]}"

    held_lock = _run_state.acquire_run_transition_lock(run_id)
    assert held_lock is not None
    try:
        resp = api_client.post("/strategy-lab/run", json={})
        assert resp.status_code == 409
        assert "Another transition" in resp.json()["detail"]
        assert run_id not in api_main._active_runs
    finally:
        held_lock.release()


def test_resume_strategy_lab_run_returns_409_when_transition_lock_held(
    monkeypatch: pytest.MonkeyPatch, lab_job_client, api_client
) -> None:
    """A resume request for a run_id whose transition lock is already held is
    rejected with 409 without ever reaching dispatch."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state as _run_state

    run_id = "run-lock-held-resume"
    api_main._active_runs[run_id] = _resumable_state(run_id)
    dispatch_calls: List[Any] = []
    monkeypatch.setattr(api_main, "_dispatch_strategy_lab_run", lambda *a, **k: dispatch_calls.append(a))

    held_lock = _run_state.acquire_run_transition_lock(run_id)
    assert held_lock is not None
    try:
        resp = api_client.post(f"/strategy-lab/runs/{run_id}/resume")
        assert resp.status_code == 409
        assert "Another transition" in resp.json()["detail"]
        assert dispatch_calls == []
    finally:
        held_lock.release()


def test_resume_strategy_lab_run_dispatches_using_state_read_after_lock_not_before(
    monkeypatch: pytest.MonkeyPatch, lab_job_client, api_client
) -> None:
    """Regression: resume must derive its dispatched counters/payload from
    state read AFTER acquiring the transition lock, not from a snapshot
    taken beforehand — a concurrent transition for the same run_id could
    otherwise complete (write its own state, dispatch, even reach a
    terminal status) between an earlier read and this request's lock
    acquisition, at which point _ensure_no_active_run() no longer blocks
    it, and a resume built from the stale snapshot would rebuild the run
    from outdated counters and dispatch duplicate work.

    Simulated via a stateful _get_run_state stub keyed on call count (not
    real threads): the fixed code calls it twice (a cheap existence check,
    then the real read inside the lock) — only the second call's result
    should drive the dispatch. The old code called it exactly once, before
    the lock, so this test would have used the stale snapshot against it."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state as _run_state

    run_id = "run-resume-uses-post-lock-state"
    stale_state = _resumable_state(run_id, contiguous_cycles=2, completed_cycles=2)
    fresh_state = _resumable_state(run_id, contiguous_cycles=5, completed_cycles=5)
    api_main._active_runs[run_id] = stale_state

    call_count = {"n": 0}

    def _stateful_get_run_state(rid: str):
        call_count["n"] += 1
        if call_count["n"] > 1:
            # Simulate a concurrent transition completing and overwriting
            # state between the first (existence) read and the real one.
            api_main._active_runs[rid] = fresh_state
        return _run_state.get_run_state(rid)

    monkeypatch.setattr(api_main, "_get_run_state", _stateful_get_run_state)

    resp = api_client.post(f"/strategy-lab/runs/{run_id}/resume")
    assert resp.status_code == 200
    assert call_count["n"] >= 2
    body = resp.json()
    # 5 + 1 (fresh), not 2 + 1 (stale).
    assert "resumed from cycle 6" in body["message"]
    assert api_main._active_runs[run_id]["contiguous_cycles"] == 5


def test_restart_strategy_lab_run_returns_409_when_transition_lock_held(
    monkeypatch: pytest.MonkeyPatch, lab_job_client, api_client
) -> None:
    """A restart request for a run_id whose transition lock is already held is
    rejected with 409 without ever reaching Temporal termination."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state as _run_state

    run_id = "run-lock-held-restart"
    api_main._active_runs[run_id] = {
        "run_id": run_id,
        "status": "completed",
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }

    terminate_calls: List[Any] = []
    import shared.temporal

    monkeypatch.setattr(
        shared.temporal, "terminate_and_await_workflow_sync", lambda *a, **k: terminate_calls.append(a)
    )

    held_lock = _run_state.acquire_run_transition_lock(run_id)
    assert held_lock is not None
    try:
        resp = api_client.post(f"/strategy-lab/runs/{run_id}/restart")
        assert resp.status_code == 409
        assert "Another transition" in resp.json()["detail"]
        # Rejected purely by the lock — never reached Temporal at all.
        assert terminate_calls == []
    finally:
        held_lock.release()


def test_restart_strategy_lab_run_returns_409_not_400_when_racing_transition_wrote_running(
    lab_job_client, api_client
) -> None:
    """Regression: status must be read INSIDE the transition lock, not
    before it. "running" is deliberately excluded from RESTARTABLE_STATUSES
    (a genuinely still-running run can't be restarted without stopping it
    first) — but a concurrent in-flight restart for this same run_id
    transiently writes "running" too, while still holding the lock. Reading
    state before attempting the lock would misread that transient write as
    a permanently invalid status and 400 instead of the promised retryable
    409, breaking the "retry shortly" contract this whole guard exists to
    provide."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state as _run_state

    run_id = "run-racing-restart-wrote-running"
    # Simulates the state a concurrent in-flight restart already wrote —
    # non-restartable on its face, but only because a transition, not a
    # genuine long-running execution, currently owns this run_id.
    api_main._active_runs[run_id] = {
        "run_id": run_id,
        "status": "running",
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }

    held_lock = _run_state.acquire_run_transition_lock(run_id)
    assert held_lock is not None
    try:
        resp = api_client.post(f"/strategy-lab/runs/{run_id}/restart")
        assert resp.status_code == 409
        assert "Another transition" in resp.json()["detail"]
    finally:
        held_lock.release()


def test_restart_strategy_lab_run_404_does_not_allocate_transition_lock_entry(
    lab_job_client, api_client
) -> None:
    """Regression: a 404 for a nonexistent run_id must be rejected by the
    cheap existence check before the transition lock is ever touched — a
    barrage of restart requests for run_ids that don't exist must not grow
    the (never-evicted) transition-lock registry."""
    from investment_team.strategy_lab import run_state as _run_state

    run_id = "run-never-existed"

    resp = api_client.post(f"/strategy-lab/runs/{run_id}/restart")
    assert resp.status_code == 404
    assert run_id not in _run_state._run_transition_locks


def test_run_strategy_lab_409_when_already_running_does_not_allocate_transition_lock_entry(
    api_client,
) -> None:
    """Regression: run_strategy_lab mints a fresh uuid4 run_id every call, so
    if the global 409 guard ran after minting + acquiring the transition
    lock, every rejected /run request during a long run would leak one
    throwaway Lock into the (never-evicted) registry forever. The global
    check must run first."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state as _run_state

    api_main._active_runs["existing"] = {"run_id": "existing", "status": "running"}
    before = len(_run_state._run_transition_locks)

    resp = api_client.post("/strategy-lab/run", json={})

    assert resp.status_code == 409
    assert len(_run_state._run_transition_locks) == before


def test_resume_strategy_lab_run_returns_409_when_restart_transition_lock_held_for_same_run_id(
    lab_job_client, api_client
) -> None:
    """Cross-endpoint case named explicitly in #4028: a resume racing a
    restart for the same run_id must not proceed just because it's a
    different route — the lock is keyed on run_id, not on endpoint."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state as _run_state

    run_id = "run-cross-endpoint"
    api_main._active_runs[run_id] = _resumable_state(run_id)

    # Simulate a restart already in flight for this run_id.
    held_lock = _run_state.acquire_run_transition_lock(run_id)
    assert held_lock is not None
    try:
        resp = api_client.post(f"/strategy-lab/runs/{run_id}/resume")
        assert resp.status_code == 409
        assert "Another transition" in resp.json()["detail"]
    finally:
        held_lock.release()


def test_restart_strategy_lab_run_serializes_concurrent_restarts_for_same_run_id(
    monkeypatch: pytest.MonkeyPatch, lab_job_client, api_client
) -> None:
    """The literal #4028 acceptance criterion: two concurrent restart calls
    for the same run_id must not both proceed to terminate/dispatch. Only
    one wins the transition lock; the other is rejected with 409 before it
    ever touches Temporal — a deterministic block-and-signal setup, not a
    ``sys.setswitchinterval`` race (the fix makes the contended path
    non-blocking, so no actual race window needs to be forced)."""
    import threading

    from fastapi import HTTPException

    from investment_team.api import main as api_main

    run_id = "run-concurrent-restart"
    api_main._active_runs[run_id] = {
        "run_id": run_id,
        "status": "completed",
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }

    entered = threading.Event()
    release = threading.Event()
    terminate_calls: List[Any] = []
    dispatch_calls: List[Any] = []

    def _slow_terminate(*args: Any, **kwargs: Any) -> None:
        terminate_calls.append((args, kwargs))
        entered.set()
        assert release.wait(timeout=5.0), "release event was never set"

    import shared.temporal

    monkeypatch.setattr(shared.temporal, "terminate_and_await_workflow_sync", _slow_terminate)
    monkeypatch.setattr(
        api_main, "_dispatch_strategy_lab_run", lambda *a, **k: dispatch_calls.append(a)
    )

    result_a: List[Any] = []

    def _call_a() -> None:
        try:
            result_a.append(api_main.restart_strategy_lab_run(run_id))
        except BaseException as exc:  # pragma: no cover - surfaced via assertion below
            result_a.append(exc)

    thread_a = threading.Thread(target=_call_a)
    thread_a.start()
    try:
        assert entered.wait(timeout=5.0), "request A never entered terminate_and_await_workflow_sync"

        # Request B races in while A still holds the transition lock inside
        # the (stubbed) blocking termination call.
        with pytest.raises(HTTPException) as exc_info:
            api_main.restart_strategy_lab_run(run_id)
        assert exc_info.value.status_code == 409
        assert "Another transition" in exc_info.value.detail
        # B was rejected purely by the lock — it never reached Temporal.
        assert len(terminate_calls) == 1
    finally:
        release.set()
        thread_a.join(timeout=5.0)
    assert not thread_a.is_alive()

    assert len(result_a) == 1
    assert not isinstance(result_a[0], BaseException), result_a[0]
    assert result_a[0].run_id == run_id

    # Exactly one dispatch/termination sequence executed overall.
    assert len(terminate_calls) == 1
    assert len(dispatch_calls) == 1
    assert api_main._active_runs[run_id]["status"] == "running"
    assert api_main._active_runs[run_id]["contiguous_cycles"] == 0


# ---------------------------------------------------------------------------
# list_strategy_lab_runs — reconciliation + persisted merge
# ---------------------------------------------------------------------------


def test_list_strategy_lab_runs_reconciles_terminal_job_service_status(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """An in-memory run marked "running" is reconciled to the job service's
    terminal status (and its error message) in the listing."""
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


def test_list_strategy_lab_runs_reconciles_progress_while_non_terminal(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Mid-run progress must reach the client even while the job service still
    reports a non-terminal status -- not just status/error at completion."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-prog2"] = {
        "run_id": "run-prog2",
        "status": "running",
        "started_at": "2024-01-01T00:00:00Z",
        "total_cycles": 10,
        "completed_cycles": 0,
        "skipped_cycles": 0,
        "errored_cycles": 0,
        "current_batch": None,
        "completed_record_ids": [],
    }
    stub = _StubLabClient(
        jobs=[
            {
                "job_id": "run-prog2",
                "status": "running",
                "data": {
                    "completed_cycles": 5,
                    "skipped_cycles": 2,
                    "errored_cycles": 1,
                    "current_batch": 2,
                    "contiguous_cycles": 5,
                    "completed_record_ids": ["rec-1", "rec-2"],
                },
            }
        ]
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.get("/strategy-lab/runs")
    assert resp.status_code == 200
    run = next(r for r in resp.json()["runs"] if r["run_id"] == "run-prog2")
    # Status stays "running" -- the persisted status is itself non-terminal --
    # but progress counters are still reconciled from the job service.
    assert run["status"] == "running"
    assert run["completed_cycles"] == 5
    assert run["skipped_cycles"] == 2
    assert run["errored_cycles"] == 1
    assert run["current_batch"] == 2
    assert run["completed_record_ids"] == ["rec-1", "rec-2"]
    # contiguous_cycles is intentionally absent from the response schema
    # (internal resume-offset math only); assert it landed in _active_runs.
    assert api_main._active_runs["run-prog2"]["contiguous_cycles"] == 5


def test_list_strategy_lab_runs_merges_persisted_only_runs(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A run present only in the job service (not in ``_active_runs``) still
    appears in the listing, merged in from the persisted record."""
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


def test_list_strategy_lab_runs_merged_entry_missing_keys_does_not_500(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A persisted job whose data lacks ``started_at``/``total_cycles`` must not 500."""
    from investment_team.api import main as api_main

    api_main._active_runs.clear()
    stub = _StubLabClient(
        jobs=[
            {
                "job_id": "run-sparse",
                "status": "running",
                "data": {},
            }
        ]
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    resp = api_client.get("/strategy-lab/runs")
    assert resp.status_code == 200
    run = next(r for r in resp.json()["runs"] if r["run_id"] == "run-sparse")
    assert run["started_at"] == ""
    assert run["total_cycles"] == 0


def test_list_strategy_lab_runs_falls_back_when_job_service_broken(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A job-service outage during reconciliation must not hide in-memory runs
    from the listing — the endpoint falls back to the in-memory snapshot."""
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
# _job_progress_percent
# ---------------------------------------------------------------------------


def test_job_progress_percent_guards_non_positive_total() -> None:
    """A zero or negative ``total`` returns 0 instead of raising ``ZeroDivisionError``."""
    from investment_team.api import main as api_main

    assert api_main._job_progress_percent(0, 0) == 0
    assert api_main._job_progress_percent(5, 0) == 0
    assert api_main._job_progress_percent(5, -1) == 0


def test_job_progress_percent_computes_normal_ratio() -> None:
    """A positive ``total`` computes the expected integer percentage."""
    from investment_team.api import main as api_main

    assert api_main._job_progress_percent(0, 4) == 0
    assert api_main._job_progress_percent(1, 4) == 25
    assert api_main._job_progress_percent(4, 4) == 100


# ---------------------------------------------------------------------------
# list_strategy_lab_jobs — persisted merge + running filter
# ---------------------------------------------------------------------------


def test_list_strategy_lab_jobs_merges_persisted_completed_runs(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """The jobs listing merges in-memory and persisted-only jobs, and
    ``running_only=true`` filters out the persisted-completed one."""
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


def test_list_strategy_lab_jobs_handles_explicit_zero_total_cycles(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A persisted ``total_cycles: 0`` must not raise ``ZeroDivisionError``.

    Covers both the in-memory ``_active_runs`` branch and the persisted
    job-service branch of ``list_strategy_lab_jobs``.
    """
    from investment_team.api import main as api_main

    api_main._active_runs["mem-zero"] = {
        "run_id": "mem-zero",
        "status": "running",
        "total_cycles": 0,
        "completed_cycles": 0,
        "started_at": "2024-01-01T00:00:00Z",
    }
    stub = _StubLabClient(
        jobs=[
            {
                "job_id": "persisted-zero",
                "status": "completed",
                "data": {
                    "started_at": "2024-01-01T00:00:00Z",
                    "total_cycles": 0,
                    "completed_cycles": 0,
                },
            }
        ]
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.get("/strategy-lab/jobs")

    assert resp.status_code == 200
    body = resp.json()
    by_id = {j["job_id"]: j for j in body["jobs"]}
    assert by_id["mem-zero"]["progress"] == 0
    assert by_id["persisted-zero"]["progress"] == 0


def test_list_strategy_lab_jobs_survives_concurrent_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list_strategy_lab_jobs`` must not tear its read while a run is popped.

    Reproduces the race the lock fix guards: a background cleanup (mirroring the
    worker ``finally``'s ``_cleanup`` body) pops from the run store while
    ``list_strategy_lab_jobs`` iterates it. Before the fix, the unlocked
    ``in_memory_ids`` comprehension raised ``RuntimeError: dictionary changed
    size during iteration`` — but that error is swallowed by the function's own
    ``except Exception`` around the persisted-merge, so the *observable* symptom
    is silent: the whole persisted block is skipped and persisted-only jobs
    vanish from the result. This asserts the persisted job is never dropped.
    """
    import sys
    import threading

    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state as _run_state

    # Use the real shared store + lock so the fix's ``with _lock:`` actually
    # serializes reader vs. popper (a per-test dict would defeat the guard).
    shared_runs: Dict[str, Any] = {}
    monkeypatch.setattr(api_main, "_active_runs", shared_runs)
    monkeypatch.setattr(_run_state, "active_runs", shared_runs)

    # A persisted-only job that is NOT in ``_active_runs`` — a correct read always
    # merges it in; a torn read skips the whole persisted block and drops it.
    persisted_id = "persisted-keep"
    stub = _StubLabClient(
        jobs=[
            {
                "job_id": persisted_id,
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

    def _make_state(rid: str) -> Dict[str, Any]:
        return {
            "run_id": rid,
            "status": "running",
            "total_cycles": 4,
            "completed_cycles": 1,
            "started_at": "2024-01-01T00:00:00Z",
            "current_cycle": None,
        }

    run_ids = [f"run-{i}" for i in range(1000)]
    for rid in run_ids:
        shared_runs[rid] = _make_state(rid)

    # Force frequent thread switches so the reader is reliably preempted
    # mid-iteration (the default 5ms interval almost never collides on a fast
    # comprehension, masking the regression). A moderate 1e-4s interval is
    # enough to trigger the race across 2000 iterations without the
    # excessive scheduling overhead (and consequent CI flakiness/slowness)
    # of a 1e-6s interval. Restored in ``finally``.
    prev_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-4)

    stop = threading.Event()
    churn_errors: List[BaseException] = []

    def _churn() -> None:
        # Mirror the worker ``finally``'s ``_cleanup`` body: pop under the lock,
        # then re-insert — hammering the same keys the reader iterates so the
        # dict size oscillates continuously.
        try:
            while not stop.is_set():
                for rid in run_ids:
                    with _run_state.lock:
                        shared_runs.pop(rid, None)
                    with _run_state.lock:
                        shared_runs[rid] = _make_state(rid)
        except BaseException as exc:  # pragma: no cover - only on regression
            churn_errors.append(exc)

    popper = threading.Thread(target=_churn, name="cleanup-churn", daemon=True)
    popper.start()
    try:
        for _ in range(2000):
            resp = api_main.list_strategy_lab_jobs()
            ids = {j.job_id for j in resp.jobs}
            # The persisted job must survive every read; its absence means the
            # persisted-merge block was skipped by a torn in-memory iteration.
            assert persisted_id in ids
    finally:
        stop.set()
        popper.join(timeout=5.0)
        sys.setswitchinterval(prev_interval)

    assert not churn_errors, f"cleanup churn raised: {churn_errors[0]!r}"


# ---------------------------------------------------------------------------
# get_strategy_lab_run_status — reconciliation + load fallback
# ---------------------------------------------------------------------------


def test_get_strategy_lab_run_status_reconciles_terminal(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """An in-memory "running" status is reconciled to the job service's
    terminal status/error in the single-run status response."""
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


def test_get_strategy_lab_run_status_reconciles_progress_while_non_terminal(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Mid-run progress must reach the client even while the job service still
    reports a non-terminal status -- not just status/error at completion."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-prog"] = {
        "run_id": "run-prog",
        "status": "running",
        "started_at": "2024-01-01T00:00:00Z",
        "total_cycles": 10,
        "completed_cycles": 0,
        "skipped_cycles": 0,
        "errored_cycles": 0,
        "current_batch": None,
    }
    stub = _StubLabClient(
        jobs=[
            {
                "job_id": "run-prog",
                "status": "running",
                "data": {
                    "completed_cycles": 4,
                    "skipped_cycles": 1,
                    "errored_cycles": 2,
                    "current_batch": 3,
                    "contiguous_cycles": 4,
                },
            }
        ]
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.get("/strategy-lab/runs/run-prog/status")
    assert resp.status_code == 200
    body = resp.json()
    # Status stays "running" -- the persisted status is itself non-terminal --
    # but progress counters are still reconciled from the job service.
    assert body["status"] == "running"
    assert body["completed_cycles"] == 4
    assert body["skipped_cycles"] == 1
    assert body["errored_cycles"] == 2
    assert body["current_batch"] == 3
    # contiguous_cycles is intentionally absent from the response schema
    # (internal resume-offset math only); assert it landed in _active_runs.
    assert api_main._active_runs["run-prog"]["contiguous_cycles"] == 4


def test_get_strategy_lab_run_status_logs_reconciliation_failure(
    monkeypatch: pytest.MonkeyPatch, api_client, caplog: pytest.LogCaptureFixture
) -> None:
    """A job-service failure during reconciliation is logged (at DEBUG) and the
    endpoint still returns 200 with the last-known in-memory status."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-broken"] = {
        "run_id": "run-broken",
        "status": "running",
        "started_at": "2024-01-01T00:00:00Z",
        "total_cycles": 3,
    }

    class _Broken:
        def get_job(self, *a, **k):
            raise RuntimeError("backend down")

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _Broken())

    with caplog.at_level("DEBUG", logger=api_main.logger.name):
        resp = api_client.get("/strategy-lab/runs/run-broken/status")

    body = resp.json()
    assert body["status"] == "running"
    assert any("run-broken" in record.getMessage() for record in caplog.records)


def test_get_strategy_lab_run_status_loads_from_job_service_when_absent(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A run_id with no in-memory entry falls back to loading the status
    straight from the persisted job-service record."""
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
    """Deleting a run removes it from both the in-memory store and the job service."""
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
    """Streaming a run_id with neither in-memory nor persisted state returns 404."""
    from investment_team.api import main as api_main

    api_main._active_runs.clear()
    monkeypatch.setattr(api_main, "_load_run_from_job_service", lambda rid: None)
    resp = api_client.get("/strategy-lab/runs/nope/stream")
    assert resp.status_code == 404


def test_stream_strategy_lab_run_terminal_short_circuit(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A run already terminal in-memory gets an immediate snapshot + done SSE
    response instead of subscribing to the live event bus."""
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


def test_stream_strategy_lab_run_terminal_short_circuit_completed_with_errors(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """``completed_with_errors`` must also be treated as terminal so a
    reconnecting client gets snapshot + done instead of hanging in 'running'."""
    from investment_team.api import main as api_main

    api_main._active_runs["done-with-errors"] = {
        "run_id": "done-with-errors",
        "status": "completed_with_errors",
        "started_at": "2024-01-01T00:00:00Z",
        "total_cycles": 1,
        "completed_cycles": 1,
        "errored_cycles": 1,
    }
    resp = api_client.get("/strategy-lab/runs/done-with-errors/stream")
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
    assert '"type": "done"' in buf or '"type":"done"' in buf, (
        "SSE stream ended without terminal done marker"
    )
    return buf


def test_stream_strategy_lab_run_emits_snapshot_update_and_terminates(
    monkeypatch: pytest.MonkeyPatch, lab_job_client, api_client
) -> None:
    """Drive the live event_generator path end-to-end.

    Pre-loads the subscription deque with one ``progress`` update and one
    ``complete`` terminal so the generator drains and exits on its first
    iteration, never reaching ``await asyncio.sleep``.
    """
    from collections import deque

    from investment_team.api import job_event_bus
    from investment_team.api import main as api_main

    monkeypatch.setitem(
        api_main._active_runs,
        "active",
        {
            "run_id": "active",
            "status": "running",
            "started_at": "2024-01-01T00:00:00Z",
            "total_cycles": 2,
            "completed_cycles": 0,
        },
    )

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
    monkeypatch: pytest.MonkeyPatch, lab_job_client, api_client
) -> None:
    """An ``error`` event must also trigger the terminal ``done`` sentinel."""
    from collections import deque

    from investment_team.api import job_event_bus
    from investment_team.api import main as api_main

    monkeypatch.setitem(
        api_main._active_runs,
        "boom",
        {
            "run_id": "boom",
            "status": "running",
            "started_at": "2024-01-01T00:00:00Z",
            "total_cycles": 1,
            "completed_cycles": 0,
        },
    )

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


def test_stream_strategy_lab_run_snapshot_reconciles_progress(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """The initial SSE snapshot must reflect job-service-reconciled progress,
    not the stale in-memory values that were current at connect time."""
    import json
    from collections import deque

    from investment_team.api import job_event_bus
    from investment_team.api import main as api_main

    api_main._active_runs["stream-prog"] = {
        "run_id": "stream-prog",
        "status": "running",
        "started_at": "2024-01-01T00:00:00Z",
        "total_cycles": 10,
        "completed_cycles": 0,
        "skipped_cycles": 0,
        "errored_cycles": 0,
        "current_batch": None,
    }
    stub = _StubLabClient(
        jobs=[
            {
                "job_id": "stream-prog",
                "status": "running",
                "data": {
                    "completed_cycles": 6,
                    "skipped_cycles": 1,
                    "errored_cycles": 1,
                    "current_batch": 4,
                    "contiguous_cycles": 5,
                },
            }
        ]
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    pre_events = deque([{"type": "complete", "summary": "ok"}])

    class _Sub:
        def __init__(self) -> None:
            self.events = pre_events

        def touch(self) -> None:  # reaper-liveness signal, no-op for the fake
            pass

    monkeypatch.setattr(job_event_bus, "subscribe", lambda rid: _Sub())
    monkeypatch.setattr(job_event_bus, "unsubscribe", lambda rid, sub: None)

    with api_client.stream("GET", "/strategy-lab/runs/stream-prog/stream", timeout=2.0) as resp:
        assert resp.status_code == 200
        body = _wait_for_terminal_sse(resp.iter_text())

    segments = [s for s in body.split("\n\n") if s.strip()]
    snapshot_seg = next(s for s in segments if '"type": "snapshot"' in s)
    snapshot = json.loads(snapshot_seg[len("data: ") :])
    assert snapshot["completed_cycles"] == 6
    assert snapshot["skipped_cycles"] == 1
    assert snapshot["errored_cycles"] == 1
    assert snapshot["current_batch"] == 4
    # contiguous_cycles is intentionally absent from the response schema
    # (internal resume-offset math only); assert it landed in _active_runs.
    assert api_main._active_runs["stream-prog"]["contiguous_cycles"] == 5
