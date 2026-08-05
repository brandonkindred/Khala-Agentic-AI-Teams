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

from collections.abc import MutableMapping
from typing import Any, Dict, Iterator, List, Optional

import pytest

from investment_team.api.main import _dispatch_strategy_lab_run as _real_dispatch_strategy_lab_run
from investment_team.api.main import _persist_run_state as _real_persist_run_state


class _InMemoryDict(MutableMapping):
    """Plain-dict stand-in for monkeypatching api.main's module-level record
    stores. Subclasses MutableMapping (rather than hand-rolling the dict
    protocol) so it gets correct semantics -- including __iter__/__len__/
    keys/items/update/setdefault and a real KeyError on deleting a missing
    key -- for free, matching what production code calling these stores
    would see from an actual dict."""

    def __init__(self) -> None:
        self._d: Dict[str, Any] = {}

    def __setitem__(self, k, v):
        self._d[k] = v

    def __getitem__(self, k):
        return self._d[k]

    def __delitem__(self, k):
        del self._d[k]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)


def test_in_memory_dict_matches_real_dict_protocol() -> None:
    """Regression: the hand-rolled predecessor of this MutableMapping-based
    test double was missing __iter__/__len__/keys/items/update/setdefault,
    and its __delitem__ silently no-op'd on a missing key instead of
    raising KeyError like a real dict -- gaps that could mask a bug in
    production code exercising the full mapping protocol against these
    monkeypatched stores."""
    d = _InMemoryDict()
    d["a"] = 1
    d.setdefault("b", 2)
    d.update({"c": 3})
    assert len(d) == 3
    assert set(iter(d)) == {"a", "b", "c"}
    assert dict(d.items()) == {"a": 1, "b": 2, "c": 3}
    assert set(d.keys()) == {"a", "b", "c"}
    with pytest.raises(KeyError):
        del d["missing"]


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
        existed = jid in self.by_id
        self.by_id.pop(jid, None)
        self.jobs = [j for j in self.jobs if j.get("job_id") != jid]
        return existed

    def apply_and_get(
        self,
        jid: str,
        *,
        merge_fields: Optional[Dict[str, Any]] = None,
        merge_nested: Optional[Dict[str, Any]] = None,
        append_to: Optional[Dict[str, List[Any]]] = None,
        increment: Optional[Dict[str, int]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Minimal stand-in for JobServiceClient.apply_and_get's increment-and-read-back.

        Auto-vivifies an entry for jid rather than requiring get_job/create_job to have
        been called first, so tests exercising restart's generation mint don't need to
        separately seed the job store. Only increment and merge_fields are honored;
        merge_nested and append_to are accepted (to match the real client's signature)
        but not implemented -- no current test relies on either. create_job isn't
        implemented (route tests that hit this path stub _persist_run_state to a
        no-op instead); update_job is implemented separately below.
        """
        record = self.by_id.setdefault(jid, {"job_id": jid})
        if increment:
            for key, delta in increment.items():
                record[key] = (record.get(key) or 0) + delta
        if merge_fields:
            record.update(merge_fields)
        self.jobs = list(self.by_id.values())
        return dict(record)

    def update_job(self, jid: str, *, heartbeat: bool = True, **fields: Any) -> None:
        """Minimal stand-in for JobServiceClient.update_job's partial merge.

        Merges only the explicitly-provided fields into the record (matching
        job_service.db.update_job's "merge fields into the job's data" partial
        semantics, not a full replace) -- a field _persist_run_state's
        exclude_fields omitted from this call is left untouched, exactly as
        the real job service behaves. ``heartbeat`` is accepted for signature
        compatibility with the real JobServiceClient.update_job but is
        intentionally ignored in this stub.
        """
        record = self.by_id.setdefault(jid, {"job_id": jid})
        record.update(fields)
        self.jobs = list(self.by_id.values())


@pytest.fixture
def lab_job_client(monkeypatch: pytest.MonkeyPatch) -> "_StubLabClient":
    """Patch ``_get_lab_run_job_client`` to return a fresh, empty ``_StubLabClient``.

    Centralizes the ``monkeypatch.setattr(api_main, "_get_lab_run_job_client",
    lambda: _StubLabClient())`` boilerplate repeated across tests that only
    need Temporal-adjacent code paths to avoid touching a real job-service
    client and don't care about pre-seeded jobs. Tests that need specific
    persisted jobs should construct their own ``_StubLabClient(jobs=[...])``
    and patch it directly instead of using this fixture.

    Also patches ``run_state.get_lab_run_job_client`` (the *same* stub
    instance) -- ``run_state.load_run_from_job_service`` builds its own
    client via that module-level function, independent of ``api.main``'s.
    Since ``load_run_from_job_service`` no longer swallows job-service
    errors (see run_state.py), leaving that source unpatched would make a
    route that falls through to it (e.g. resume/restart's 404-when-missing
    check) attempt a real network call and raise a connection error instead
    of the empty-store 404 these tests intend to exercise.
    """
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state as _run_state

    stub = _StubLabClient()
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    monkeypatch.setattr(_run_state, "get_lab_run_job_client", lambda: stub)
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


def test_run_strategy_lab_initial_state_has_generation_one(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A fresh run's initial state carries generation 1 — the default incarnation
    a restart later mints a superseding value against."""
    from investment_team.api import main as api_main

    resp = api_client.post(
        "/strategy-lab/run",
        json={"batch_size": 2, "batch_count": 1, "max_parallel": 1, "paper_trading_enabled": False},
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    assert api_main._active_runs[run_id]["generation"] == 1


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


def test_resume_strategy_lab_run_carries_forward_generation_unchanged(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A resume continues the same incarnation rather than superseding one, so it
    must carry the current generation forward unchanged (unlike restart, which
    mints a new one) -- read from the DURABLE store, not the in-memory
    snapshot (see test below for why that distinction matters)."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-i"] = _resumable_state("run-i", generation=4)
    stub = _StubLabClient(jobs=[{"job_id": "run-i", "generation": 4}])
    mint_calls: List[Any] = []
    real_apply_and_get = stub.apply_and_get
    monkeypatch.setattr(
        stub,
        "apply_and_get",
        lambda *a, **k: (mint_calls.append((a, k)), real_apply_and_get(*a, **k))[1],
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    resp = api_client.post("/strategy-lab/runs/run-i/resume")
    assert resp.status_code == 200
    assert api_main._active_runs["run-i"]["generation"] == 4
    # Resume must carry the generation forward, never mint a new one --
    # only restart is allowed to call the atomic-increment mint path.
    assert mint_calls == []


def test_resume_strategy_lab_run_uses_durable_generation_not_stale_local_cache(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Regression: in a multi-process/multi-replica deployment, a restart handled
    by a different process already minted a newer generation durably, while
    this process's in-memory active_runs snapshot may still show the old one.
    Resume must carry forward the DURABLE value, not the stale local one --
    otherwise it would regress the durable high-water mark and un-fence
    everything the restart just fenced out."""
    from investment_team.api import main as api_main

    # In-memory snapshot is stale (generation 1); the durable store already
    # has generation 3 from a restart this process never observed.
    api_main._active_runs["run-j"] = _resumable_state("run-j", generation=1)
    stub = _StubLabClient(jobs=[{"job_id": "run-j", "generation": 3}])
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.post("/strategy-lab/runs/run-j/resume")

    assert resp.status_code == 200
    assert api_main._active_runs["run-j"]["generation"] == 3
    assert stub.by_id["run-j"]["generation"] == 3  # not regressed back to 1


def test_resume_strategy_lab_run_write_does_not_regress_generation_minted_mid_request(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Regression: even after reading the durable generation, resume's own
    _persist_run_state write must not clobber a NEWER durable value minted by
    a concurrent restart on another process/replica in the gap between the
    initial read and this write. Simulated by bumping the stub's durable
    generation only on the FIRST read (mimicking a same-request race window
    that happens once, between the initial read and the write) and then
    leaving the durable store alone -- so the revalidation read and the
    final assertion both observe whatever the write actually left behind,
    genuinely proving the write didn't regress it back down to the stale
    snapshot. (A prior version of this test re-bumped the durable value on
    EVERY read, including the one after the write; that would have forced
    the final assertion to pass even if the write itself had incorrectly
    regressed the durable generation in between, since the post-write read
    would silently re-stamp it back to 5 regardless.)"""
    from investment_team.api import main as api_main

    api_main._active_runs["run-race"] = _resumable_state("run-race", generation=1)
    stub = _StubLabClient(jobs=[{"job_id": "run-race", "generation": 1}])
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    # Override the api_client fixture's blanket _persist_run_state no-op
    # stub: this test needs the real durable-write behavior (including its
    # exclude_fields handling) to observe the regression it claims to catch.
    monkeypatch.setattr(api_main, "_persist_run_state", _real_persist_run_state)

    real_get_job = stub.get_job
    read_calls: List[str] = []

    def _get_job_with_concurrent_restart_on_first_read(jid: str):
        read_calls.append(jid)
        result = real_get_job(jid)
        if len(read_calls) == 1:
            # Simulate a restart on another replica minting generation 5 in
            # the durable store immediately after this resume's first read
            # of it -- a one-time race, not re-injected on later reads.
            stub.by_id[jid]["generation"] = 5
        return result

    monkeypatch.setattr(stub, "get_job", _get_job_with_concurrent_restart_on_first_read)

    resp = api_client.post("/strategy-lab/runs/run-race/resume")

    assert resp.status_code == 200
    assert read_calls == ["run-race", "run-race"]  # confirms both reads happened
    # The durable value must still be 5 -- resume's write must not have
    # regressed it back down to the stale 1 it read moments earlier. Nothing
    # re-injects 5 after the write, so this genuinely reflects the write's
    # own exclude_fields behavior, not a test-side reset.
    assert stub.by_id["run-race"]["generation"] == 5


def test_resume_strategy_lab_run_dispatches_with_revalidated_generation_not_stale_snapshot(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Regression: a concurrent restart on another process/replica can mint (and
    dispatch under) a newer generation between resume's initial durable read
    and its dispatch call. Dispatching this resume's workflow under the
    earlier, now-stale snapshot would permanently fence out its own
    activities (the only live workflow for this run) the moment they tried
    to persist anything -- so the value actually handed to
    _dispatch_strategy_lab_run must reflect a revalidated read taken as
    close to dispatch as possible, not the earlier snapshot."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-revalidate"] = _resumable_state("run-revalidate", generation=1)
    stub = _StubLabClient(jobs=[{"job_id": "run-revalidate", "generation": 1}])
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    real_get_job = stub.get_job
    read_calls: List[str] = []

    def _get_job_bumping_generation_after_first_read(jid: str):
        read_calls.append(jid)
        result = real_get_job(jid)
        # Simulate a concurrent restart minting (and dispatching under)
        # generation 5 durably, right after this resume's *first* read of
        # the generation (used to build/persist its own state) but before
        # its second, pre-dispatch revalidation read.
        if len(read_calls) == 1:
            stub.by_id[jid]["generation"] = 5
        return result

    monkeypatch.setattr(stub, "get_job", _get_job_bumping_generation_after_first_read)

    captured = {}
    monkeypatch.setattr(
        api_main,
        "_dispatch_strategy_lab_run",
        lambda run_id, request, *, generation, allow_already_started=True: captured.update(
            generation=generation
        ),
    )

    resp = api_client.post("/strategy-lab/runs/run-revalidate/resume")

    assert resp.status_code == 200
    assert read_calls == ["run-revalidate", "run-revalidate"]  # both reads happened
    assert captured["generation"] == 5  # revalidated value, not the stale snapshot of 1
    assert api_main._active_runs["run-revalidate"]["generation"] == 5


def test_resume_strategy_lab_run_returns_503_when_revalidation_detects_generation_regression(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """The generation field is meant to be strictly monotonic (only ever
    advanced via an atomic job-service increment). If the pre-dispatch
    revalidation read ever comes back LOWER than the earlier snapshot --
    which cannot represent a legitimate concurrent mint -- that indicates a
    corrupted or otherwise inconsistent durable record. Must fail closed
    rather than silently dispatch under the lower, invariant-violating
    value."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-regress"] = _resumable_state("run-regress", generation=1)
    stub = _StubLabClient(jobs=[{"job_id": "run-regress", "generation": 5}])

    real_get_job = stub.get_job
    read_calls: List[str] = []

    def _get_job_regressing_after_first_read(jid: str):
        read_calls.append(jid)
        result = real_get_job(jid)
        if len(read_calls) == 1:
            # Simulate the durable record becoming corrupted/inconsistent
            # between the initial read (5) and the revalidation read (2) --
            # a real decrease, not a legitimate concurrent mint.
            stub.by_id[jid]["generation"] = 2
        return result

    monkeypatch.setattr(stub, "get_job", _get_job_regressing_after_first_read)
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.post("/strategy-lab/runs/run-regress/resume")

    assert resp.status_code == 503
    assert "regress" in resp.json()["detail"].lower()
    assert api_main._active_runs["run-regress"]["status"] == "failed"


def test_resume_strategy_lab_run_returns_503_when_generation_lookup_fails(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    from investment_team.api import main as api_main

    api_main._active_runs["run-k"] = _resumable_state("run-k", generation=1)

    class _RaisingClient(_StubLabClient):
        def get_job(self, jid):
            raise ConnectionError("connection refused")

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _RaisingClient())

    resp = api_client.post("/strategy-lab/runs/run-k/resume")

    assert resp.status_code == 503
    assert "generation" in resp.json()["detail"].lower()
    assert api_main._active_runs["run-k"]["status"] == "interrupted"  # unchanged, no partial resume


def test_resume_strategy_lab_run_returns_503_when_pre_dispatch_revalidation_fails(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """The pre-dispatch revalidation read (added alongside the initial
    carry-forward read) must also fail closed -- and, unlike the initial
    read's failure (which happens before any state mutation), this failure
    happens after resume already wrote "running" state, so it must also
    mark the run "failed" rather than leaving it wedged as "running" with
    no workflow ever dispatched."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-revalidate-fail"] = _resumable_state("run-revalidate-fail", generation=1)
    stub = _StubLabClient(jobs=[{"job_id": "run-revalidate-fail", "generation": 1}])

    real_get_job = stub.get_job
    read_calls: List[str] = []

    def _get_job_failing_on_second_read(jid: str):
        read_calls.append(jid)
        if len(read_calls) == 2:
            raise ConnectionError("connection refused")
        return real_get_job(jid)

    monkeypatch.setattr(stub, "get_job", _get_job_failing_on_second_read)
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.post("/strategy-lab/runs/run-revalidate-fail/resume")

    assert resp.status_code == 503
    assert "generation" in resp.json()["detail"].lower()
    # Three get_job reads: the initial carry-forward read, the pre-dispatch
    # revalidation that's made to fail here, and _fail_strategy_lab_run's own
    # durable-generation check before it writes the "failed" status.
    assert read_calls == ["run-revalidate-fail"] * 3
    assert api_main._active_runs["run-revalidate-fail"]["status"] == "failed"


def test_resume_strategy_lab_run_revalidation_failure_does_not_regress_concurrently_minted_generation(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Regression: if the pre-dispatch revalidation read itself hits a
    transient job-service failure after a concurrent replica has already
    minted (and dispatched under) a newer generation, the resulting
    _fail_strategy_lab_run call must not durably regress that generation.
    Marking a run "failed" is a status/error update, not a fencing
    decision -- writing this request's stale in-memory generation back to
    the durable record would re-enable an incarnation generation fencing
    has already superseded and stop the legitimate newer one."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-revalidate-fail2"] = _resumable_state("run-revalidate-fail2", generation=1)
    stub = _StubLabClient(jobs=[{"job_id": "run-revalidate-fail2", "generation": 1}])

    real_get_job = stub.get_job
    read_calls: List[str] = []

    def _get_job_bumping_then_failing(jid: str):
        read_calls.append(jid)
        if len(read_calls) == 1:
            result = real_get_job(jid)
            # Concurrent replica mints (and dispatches under) generation 5
            # immediately after this resume's first read.
            stub.by_id[jid]["generation"] = 5
            return result
        raise ConnectionError("connection refused")  # the pre-dispatch revalidation read

    monkeypatch.setattr(stub, "get_job", _get_job_bumping_then_failing)
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    # Override the api_client fixture's blanket _persist_run_state no-op
    # stub: this test needs the real durable-write behavior (including
    # _fail_strategy_lab_run's own persist call) to observe the regression.
    monkeypatch.setattr(api_main, "_persist_run_state", _real_persist_run_state)

    resp = api_client.post("/strategy-lab/runs/run-revalidate-fail2/resume")

    assert resp.status_code == 503
    assert api_main._active_runs["run-revalidate-fail2"]["status"] == "failed"
    # The legitimate, concurrently minted generation 5 must survive --
    # marking this request's run "failed" must not regress it back to the
    # stale value (1) this request read moments earlier.
    assert stub.by_id["run-revalidate-fail2"]["generation"] == 5


def test_fail_strategy_lab_run_does_not_clobber_concurrently_resumed_incarnation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: if a resume/restart replaces `_active_runs[run_id]` with a
    newer incarnation while `_fail_strategy_lab_run` is between its durable
    generation read and re-acquiring the lock to write, the stale fail-write
    must not land on that newer incarnation. The durable read itself can
    return a generation that still matches this call's stale in-memory
    snapshot (e.g. the concurrent restart hasn't persisted its mint yet),
    so the earlier durable-generation guard alone does not catch this --
    only re-checking the in-memory generation right before the write does."""
    from investment_team.api import main as api_main

    run_id = "run-fail-race"
    api_main._active_runs[run_id] = _resumable_state(run_id, generation=1, status="running")

    def _fake_get_run_generation_strict(run_id_arg, client=None):
        # Simulate a concurrent restart replacing the in-memory entry with a
        # newer incarnation while this durable read is "in flight", but the
        # durable read still returns the stale generation this request
        # already knows about.
        api_main._active_runs[run_id] = _resumable_state(run_id, generation=2, status="running")
        return 1

    monkeypatch.setattr(api_main, "_get_run_generation_strict", _fake_get_run_generation_strict)
    persisted_calls: List[Any] = []
    monkeypatch.setattr(
        api_main,
        "_persist_run_state",
        lambda rid, state, **kw: persisted_calls.append((rid, dict(state))),
    )

    api_main._fail_strategy_lab_run(run_id, "boom")

    assert api_main._active_runs[run_id]["status"] == "running"
    assert api_main._active_runs[run_id]["generation"] == 2
    assert persisted_calls == []


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
    # lab_job_client already patches _get_lab_run_job_client to a single
    # shared _StubLabClient instance: restart calls _get_lab_run_job_client()
    # multiple times per request (bootstrap check, mint, pre-dispatch
    # revalidation), and each call must observe the same durable state --
    # a fresh instance per call would make the revalidation read see an
    # empty store and misreport a generation regression.
    resp = api_client.post("/strategy-lab/runs/run-g/restart")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-g"
    assert "restarted" in body["message"]


def test_restart_strategy_lab_run_mints_new_generation(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Restart mints a fresh generation (atomically incremented, not just reset to
    a fixed value) so the new incarnation's writes fence out any stale activity
    still in flight from the terminated one."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-gen"] = {
        "run_id": "run-gen",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 3,
    }
    stub = _StubLabClient(jobs=[{"job_id": "run-gen", "generation": 3}])
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.post("/strategy-lab/runs/run-gen/restart")

    assert resp.status_code == 200
    assert api_main._active_runs["run-gen"]["generation"] == 4
    assert stub.by_id["run-gen"]["generation"] == 4


def test_restart_strategy_lab_run_write_does_not_regress_concurrently_minted_generation(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Regression: apply_and_get already durably persists the freshly minted
    generation atomically -- this restart's own subsequent full-state
    persist write is redundant for that field. If a DIFFERENT restart on
    another process/replica mints (and dispatches under) an even newer
    generation in the gap between this restart's own mint and its own
    write, that write must not regress the durable value back down to this
    request's now-stale mint (a non-colliding restart, unlike the dispatch-
    collision rollback path, which already has its own dedicated test)."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-race3"] = {
        "run_id": "run-race3",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 1,
    }
    stub = _StubLabClient(jobs=[{"job_id": "run-race3", "generation": 1}])
    real_apply_and_get = stub.apply_and_get

    def _apply_and_get_then_concurrent_mint(jid, **kwargs):
        result = real_apply_and_get(jid, **kwargs)
        # Simulate a different restart on another replica minting (and
        # dispatching under) generation 3 immediately after this restart's
        # own mint (to 2) above.
        stub.by_id[jid]["generation"] = 3
        return result

    monkeypatch.setattr(stub, "apply_and_get", _apply_and_get_then_concurrent_mint)
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    # Override the api_client fixture's blanket _persist_run_state no-op
    # stub: this test needs the real durable-write behavior to observe the
    # regression.
    monkeypatch.setattr(api_main, "_persist_run_state", _real_persist_run_state)

    resp = api_client.post("/strategy-lab/runs/run-race3/restart")

    assert resp.status_code == 200
    # The concurrently minted generation 3 must survive this restart's own
    # (non-colliding) persist write untouched.
    assert stub.by_id["run-race3"]["generation"] == 3


def test_restart_strategy_lab_run_dispatches_with_the_minted_generation(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Regression: the generation passed into _dispatch_strategy_lab_run must be
    the exact value restart just minted -- passed through explicitly, not
    re-derived by build_strategy_lab_batch_input via a separate read that could
    transiently fail or diverge (see _dispatch_strategy_lab_run's precondition)."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-dispatch-gen"] = {
        "run_id": "run-dispatch-gen",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 4,
    }
    stub = _StubLabClient(jobs=[{"job_id": "run-dispatch-gen", "generation": 4}])
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    captured = {}
    monkeypatch.setattr(
        api_main,
        "_dispatch_strategy_lab_run",
        lambda run_id, request, *, generation, allow_already_started=True: captured.update(
            generation=generation
        ),
    )

    resp = api_client.post("/strategy-lab/runs/run-dispatch-gen/restart")

    assert resp.status_code == 200
    assert captured["generation"] == 5  # minted (4 -> 5); matches the persisted value below
    assert api_main._active_runs["run-dispatch-gen"]["generation"] == 5


def test_restart_strategy_lab_run_dispatches_with_revalidated_generation_not_stale_snapshot(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Regression: a DIFFERENT restart on another process/replica can mint
    (and dispatch under) an even newer generation immediately after this
    restart's own mint. Dispatching this restart's workflow under the
    earlier, now-stale minted value would permanently fence out its own
    activities if it wins the workflow-id race -- so the value actually
    handed to _dispatch_strategy_lab_run must reflect a revalidated read
    taken as close to dispatch as possible, not this restart's own earlier
    mint."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-restart-revalidate"] = {
        "run_id": "run-restart-revalidate",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 1,
    }
    stub = _StubLabClient(jobs=[{"job_id": "run-restart-revalidate", "generation": 1}])
    real_apply_and_get = stub.apply_and_get

    def _apply_and_get_then_concurrent_mint(jid, **kwargs):
        result = real_apply_and_get(jid, **kwargs)
        # Simulate a different restart on another replica minting (and
        # dispatching under) generation 9 immediately after this restart's
        # own mint above.
        stub.by_id[jid]["generation"] = 9
        return result

    monkeypatch.setattr(stub, "apply_and_get", _apply_and_get_then_concurrent_mint)
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    captured = {}
    monkeypatch.setattr(
        api_main,
        "_dispatch_strategy_lab_run",
        lambda run_id, request, *, generation, allow_already_started=True: captured.update(
            generation=generation
        ),
    )

    resp = api_client.post("/strategy-lab/runs/run-restart-revalidate/restart")

    assert resp.status_code == 200
    assert captured["generation"] == 9  # revalidated value, not this restart's own stale mint (2)
    assert api_main._active_runs["run-restart-revalidate"]["generation"] == 9


def test_restart_strategy_lab_run_returns_503_when_revalidation_detects_generation_regression(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Mirrors the equivalent resume regression test: the durable generation
    can only legitimately advance (atomic increments only), so if the
    pre-dispatch revalidation read comes back LOWER than this restart's own
    just-minted value, that's a corrupted/inconsistent durable record, not a
    concurrent mint -- must fail closed rather than dispatch under it."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-restart-regress"] = {
        "run_id": "run-restart-regress",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 1,
    }
    stub = _StubLabClient(jobs=[{"job_id": "run-restart-regress", "generation": 1}])
    real_apply_and_get = stub.apply_and_get

    def _apply_and_get_then_corrupt_lower(jid, **kwargs):
        result = real_apply_and_get(jid, **kwargs)
        # This restart mints 2 (1 -> 2); simulate the durable record then
        # becoming corrupted/inconsistent, reporting a lower value on the
        # subsequent revalidation read.
        stub.by_id[jid]["generation"] = 0
        return result

    monkeypatch.setattr(stub, "apply_and_get", _apply_and_get_then_corrupt_lower)
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.post("/strategy-lab/runs/run-restart-regress/restart")

    assert resp.status_code == 503
    assert "regress" in resp.json()["detail"].lower()
    assert api_main._active_runs["run-restart-regress"]["status"] == "failed"


def test_restart_strategy_lab_run_returns_503_when_pre_dispatch_revalidation_fails(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """The pre-dispatch revalidation read (added alongside the mint-time
    bootstrap check) must also fail closed -- and, unlike the bootstrap
    check's failure, this failure happens after restart already wrote
    "running" state, so it must also mark the run "failed" rather than
    leaving it wedged as "running" with no workflow ever dispatched."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-restart-revalidate-fail"] = {
        "run_id": "run-restart-revalidate-fail",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 1,
    }
    stub = _StubLabClient(jobs=[{"job_id": "run-restart-revalidate-fail", "generation": 1}])

    real_get_job = stub.get_job
    read_calls: List[str] = []

    def _get_job_failing_on_second_read(jid: str):
        read_calls.append(jid)
        if len(read_calls) == 2:
            raise ConnectionError("connection refused")
        return real_get_job(jid)

    monkeypatch.setattr(stub, "get_job", _get_job_failing_on_second_read)
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.post("/strategy-lab/runs/run-restart-revalidate-fail/restart")

    assert resp.status_code == 503
    assert "generation" in resp.json()["detail"].lower()
    # Three get_job reads: the mint-time legacy-bootstrap check, the
    # pre-dispatch revalidation that's made to fail here, and
    # _fail_strategy_lab_run's own durable-generation check before it writes
    # the "failed" status.
    assert read_calls == ["run-restart-revalidate-fail"] * 3
    assert api_main._active_runs["run-restart-revalidate-fail"]["status"] == "failed"


def test_restart_strategy_lab_run_bootstraps_legacy_run_generation_above_one(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A run created before generation fencing shipped has no generation field in
    its persisted record at all. Its first post-upgrade restart must mint
    generation 2, not 1 -- 1 is what a pre-upgrade in-flight activity (which
    omits generation entirely) is treated as presenting, and
    check_fencing_token accepts equal tokens, so minting exactly 1 would fence
    nothing for that stale activity."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-legacy"] = {
        "run_id": "run-legacy",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        # No "generation" key at all -- simulates a run created before this change.
    }
    stub = _StubLabClient(jobs=[{"job_id": "run-legacy"}])  # likewise no "generation" field
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.post("/strategy-lab/runs/run-legacy/restart")

    assert resp.status_code == 200
    assert api_main._active_runs["run-legacy"]["generation"] == 2
    assert stub.by_id["run-legacy"]["generation"] == 2


def test_restart_strategy_lab_run_bootstrap_check_read_failure_still_succeeds(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A get_job failure during restart's legacy-generation bootstrap check
    (the read used only to decide +1 vs +2 for the mint) must not block the
    restart -- it's explicitly best-effort: falling back to the legacy/+2
    branch is safe regardless of why this read failed, since the actual
    fencing-critical write is the atomic apply_and_get increment right
    after, which is unaffected by this read's outcome."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-bootstrapreadfail"] = {
        "run_id": "run-bootstrapreadfail",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 3,
    }

    stub = _StubLabClient(jobs=[{"job_id": "run-bootstrapreadfail", "generation": 3}])
    real_get_job = stub.get_job
    read_calls: List[str] = []

    def _get_job_failing_on_first_read(jid: str):
        read_calls.append(jid)
        if len(read_calls) == 1:
            raise ConnectionError("connection refused")
        return real_get_job(jid)

    monkeypatch.setattr(stub, "get_job", _get_job_failing_on_first_read)
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.post("/strategy-lab/runs/run-bootstrapreadfail/restart")

    assert resp.status_code == 200
    # The bootstrap read failed, so durable_data fell back to {} -- treated
    # the same as a genuinely legacy/fieldless record, bootstrapping by +2
    # (3 -> 5) rather than the ordinary +1, per
    # _legacy_generation_bootstrap_increment's documented safety margin.
    assert api_main._active_runs["run-bootstrapreadfail"]["generation"] == 5
    assert stub.by_id["run-bootstrapreadfail"]["generation"] == 5


def test_restart_strategy_lab_run_bootstraps_from_durable_record_not_stale_in_memory_generation(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Regression: resuming a legacy run (no durable "generation" field)
    populates the in-memory active_runs entry with a default generation=1
    (get_run_generation_strict's fallback), while resume deliberately
    excludes "generation" from its own durable write -- so the durable
    record stays legacy. A restart of the SAME run in the SAME process
    afterward must still recognize the durable record as legacy and mint
    generation 2, not defer to the in-memory entry (which now has a
    "generation" key) and mint only +1 -- landing on durable generation 1,
    exactly what a still-in-flight legacy activity (which omits generation
    entirely, defaulting to 1) presents, which check_fencing_token accepts
    as current rather than fencing out."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-legacy2"] = {
        "run_id": "run-legacy2",
        "status": "interrupted",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "completed_cycles": 0,
        "contiguous_cycles": 0,
        # No "generation" key -- simulates a run created before this change.
    }
    stub = _StubLabClient(jobs=[{"job_id": "run-legacy2"}])  # durable: also no "generation" field
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resume_resp = api_client.post("/strategy-lab/runs/run-legacy2/resume")
    assert resume_resp.status_code == 200
    # Confirms the race is set up correctly: resume's in-memory default
    # must not have reached the durable record.
    assert "generation" not in stub.by_id["run-legacy2"]
    assert api_main._active_runs["run-legacy2"]["generation"] == 1

    # Simulate the resumed run later reaching a restartable terminal status,
    # still carrying that stale in-memory generation=1.
    api_main._active_runs["run-legacy2"]["status"] = "completed_with_errors"

    restart_resp = api_client.post("/strategy-lab/runs/run-legacy2/restart")

    assert restart_resp.status_code == 200
    assert stub.by_id["run-legacy2"]["generation"] == 2
    assert api_main._active_runs["run-legacy2"]["generation"] == 2


def test_restart_strategy_lab_run_rollback_does_not_regress_concurrently_minted_generation(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Regression: two replicas restarting the same terminal run concurrently
    don't serialize against each other (the per-run_id transition lock is
    process-local). If replica A mints generation 2, then replica B mints and
    successfully dispatches generation 3 before A's own dispatch, A's dispatch
    collides (409) and rolls back -- that rollback must NOT durably overwrite
    B's legitimate generation 3 with A's stale minted value of 2."""
    from temporalio.exceptions import WorkflowAlreadyStartedError

    import shared.temporal
    from investment_team.api import main as api_main
    from investment_team.strategy_lab.temporal import start_workflow as sl_sw

    api_main._active_runs["run-race2"] = {
        "run_id": "run-race2",
        "status": "cancelled",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 1,
    }
    stub = _StubLabClient(jobs=[{"job_id": "run-race2", "generation": 1}])
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    monkeypatch.setattr(shared.temporal, "terminate_and_await_workflow_sync", lambda *a, **k: None)
    # Override the api_client fixture's blanket _dispatch_strategy_lab_run
    # no-op stub with the real function: this test needs its actual
    # WorkflowAlreadyStartedError -> 409-and-rollback handling, only with
    # the inner start_strategy_lab_batch_workflow call replaced below.
    monkeypatch.setattr(api_main, "_dispatch_strategy_lab_run", _real_dispatch_strategy_lab_run)

    def _dispatch_collides_after_a_concurrent_replica_wins(rid, req, generation):
        # Simulate replica B concurrently restarting the same run: it mints
        # generation 3 (durably, via the same apply_and_get idiom) and
        # successfully dispatches, all while this (replica A's) request is
        # mid-dispatch. A's own attempt then collides with B's fresh workflow.
        stub.by_id[rid]["generation"] = 3
        raise WorkflowAlreadyStartedError(
            workflow_id=f"strategy-lab-{rid}", run_id="prior-run", workflow_type="X"
        )

    monkeypatch.setattr(
        sl_sw, "start_strategy_lab_batch_workflow", _dispatch_collides_after_a_concurrent_replica_wins
    )

    resp = api_client.post("/strategy-lab/runs/run-race2/restart")

    assert resp.status_code == 409
    # B's legitimate generation 3 must survive A's rollback untouched.
    assert stub.by_id["run-race2"]["generation"] == 3
    assert api_main._active_runs["run-race2"]["generation"] == 3


def test_restart_strategy_lab_run_returns_503_when_generation_mint_fails(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A job-service hiccup during the generation mint must not silently restart
    without a fenced generation — it should fail loudly (503) instead."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-mintfail"] = {
        "run_id": "run-mintfail",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 1,
    }

    class _NoApplyClient(_StubLabClient):
        def apply_and_get(self, jid, **kwargs):  # simulates job-service unavailability
            return None

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _NoApplyClient())

    resp = api_client.post("/strategy-lab/runs/run-mintfail/restart")

    assert resp.status_code == 503
    assert "generation" in resp.json()["detail"].lower()
    # State was not overwritten by a partial restart.
    assert api_main._active_runs["run-mintfail"]["status"] == "completed_with_errors"
    assert api_main._active_runs["run-mintfail"]["generation"] == 1


def test_restart_strategy_lab_run_returns_503_when_generation_mint_raises(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A real job-service transport failure (connection refused, timeout, ...)
    raises from apply_and_get rather than returning None -- must still map to
    the documented 503, not escape as an unhandled 500."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-mintraise"] = {
        "run_id": "run-mintraise",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 1,
    }

    class _RaisingClient(_StubLabClient):
        def apply_and_get(self, jid, **kwargs):
            raise ConnectionError("connection refused")

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _RaisingClient())

    resp = api_client.post("/strategy-lab/runs/run-mintraise/restart")

    assert resp.status_code == 503
    assert "generation" in resp.json()["detail"].lower()
    assert api_main._active_runs["run-mintraise"]["status"] == "completed_with_errors"
    assert api_main._active_runs["run-mintraise"]["generation"] == 1


def test_restart_strategy_lab_run_returns_503_when_mint_response_is_malformed(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """apply_and_get returning a truthy record whose "generation" field is
    missing or not an int is a malformed mint response, not a legitimate
    absent-field case -- must map to the same documented 503 rather than
    propagate a raw ValueError/TypeError/KeyError as an unhandled 500."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-mintmalformed"] = {
        "run_id": "run-mintmalformed",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 1,
    }

    class _MalformedMintClient(_StubLabClient):
        def apply_and_get(self, jid, **kwargs):
            return {"job_id": jid, "generation": "not-a-number"}

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _MalformedMintClient())

    resp = api_client.post("/strategy-lab/runs/run-mintmalformed/restart")

    assert resp.status_code == 503
    assert "generation" in resp.json()["detail"].lower()
    assert api_main._active_runs["run-mintmalformed"]["status"] == "completed_with_errors"
    assert api_main._active_runs["run-mintmalformed"]["generation"] == 1


def test_restart_strategy_lab_run_returns_503_when_mint_response_is_non_positive(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """apply_and_get returning a truthy record whose "generation" field is a
    well-typed but non-positive int (a corrupt durable record, since the
    applied increment is always positive) must map to the same documented
    503 as a missing/non-int value -- a non-positive generation could match
    a stale/legacy activity's default token and defeat fencing if allowed
    through."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-mintnonpositive"] = {
        "run_id": "run-mintnonpositive",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 1,
    }

    class _NonPositiveMintClient(_StubLabClient):
        def apply_and_get(self, jid, **kwargs):
            return {"job_id": jid, "generation": 0}

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _NonPositiveMintClient())

    resp = api_client.post("/strategy-lab/runs/run-mintnonpositive/restart")

    assert resp.status_code == 503
    assert "generation" in resp.json()["detail"].lower()
    assert api_main._active_runs["run-mintnonpositive"]["status"] == "completed_with_errors"
    assert api_main._active_runs["run-mintnonpositive"]["generation"] == 1


@pytest.mark.parametrize("non_int_generation", [2.7, True, False])
def test_restart_strategy_lab_run_returns_503_when_mint_response_is_non_integer(
    monkeypatch: pytest.MonkeyPatch, api_client, non_int_generation
) -> None:
    """apply_and_get returning a "generation" that's a float or bool (an int
    subclass in Python, so `isinstance(True, int)` is True) must be rejected
    as a malformed mint response, not silently coerced via int(...) --
    a truncated float or a bool-derived 0/1 could produce a fencing token
    that doesn't match the durable job-service value."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-mintnonint"] = {
        "run_id": "run-mintnonint",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 1,
    }

    class _NonIntMintClient(_StubLabClient):
        def apply_and_get(self, jid, **kwargs):
            return {"job_id": jid, "generation": non_int_generation}

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _NonIntMintClient())

    resp = api_client.post("/strategy-lab/runs/run-mintnonint/restart")

    assert resp.status_code == 503
    assert "generation" in resp.json()["detail"].lower()
    assert api_main._active_runs["run-mintnonint"]["status"] == "completed_with_errors"
    assert api_main._active_runs["run-mintnonint"]["generation"] == 1


def test_restart_generation_fences_stale_activity_from_terminated_incarnation(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """End-to-end regression (the literal acceptance criterion): a stale activity
    from the pre-restart incarnation — captured with the OLD generation before the
    restart happened — must be rejected by both persist_run_state_activity and
    finalize_cycle_record_activity after the restart, proving it can no longer
    corrupt the freshly restarted run's state."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state
    from investment_team.strategy_lab.temporal import activities as act

    api_main._active_runs["run-stale"] = {
        "run_id": "run-stale",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
        "generation": 1,
    }
    stub = _StubLabClient(jobs=[{"job_id": "run-stale", "generation": 1}])
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    # get_run_generation_strict (used by the fencing checks below) reads the
    # durable store via run_state.get_lab_run_job_client directly, not
    # api.main's alias -- both must point at the same backing store for this
    # test to represent one consistent durable store.
    monkeypatch.setattr(run_state, "get_lab_run_job_client", lambda: stub)

    # Capture the pre-restart generation, as a stale in-flight activity would have.
    stale_generation = run_state.get_run_generation_strict("run-stale")
    assert stale_generation == 1

    resp = api_client.post("/strategy-lab/runs/run-stale/restart")
    assert resp.status_code == 200
    assert api_main._active_runs["run-stale"]["generation"] == 2

    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError) as persist_exc:
        act.persist_run_state_activity(
            "run-stale", {"status": "running"}, generation=stale_generation
        )
    assert persist_exc.value.type == "StaleFencingTokenError"

    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )
    with pytest.raises(ApplicationError) as finalize_exc:
        act.finalize_cycle_record_activity(
            {
                "run_id": "run-stale",
                "generation": stale_generation,
                "record": {"lab_record_id": "stale-record"},
            }
        )
    assert finalize_exc.value.type == "StaleFencingTokenError"

    # The stale record never got as far as being persisted.
    assert "stale-record" not in api_main._active_runs["run-stale"].get("completed_record_ids", [])


def test_restart_strategy_lab_run_rollback_persist_failure_does_not_mask_409(
    lab_job_client, api_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart that collides with a still-running old execution (409) must
    still surface that 409 even when the best-effort rollback persist itself
    fails -- a job-service failure in that cleanup step must not replace the
    more actionable conflict response with an unrelated error (issue #4150)."""
    from fastapi import HTTPException

    from investment_team.api import main as api_main

    api_main._active_runs["run-rollback"] = {
        "run_id": "run-rollback",
        "status": "completed_with_errors",
        "request_payload": {"batch_size": 1, "batch_count": 1},
    }

    def _raise_409(*args, **kwargs):
        raise HTTPException(status_code=409, detail="still winding down")

    monkeypatch.setattr(api_main, "_dispatch_strategy_lab_run", _raise_409)

    # The route's own primary persist (before dispatch) must still succeed --
    # only the rollback persist (after the 409) is the one under test here.
    persist_calls = {"n": 0}

    def _persist_fails_after_first_call(*args, **kwargs):
        persist_calls["n"] += 1
        if persist_calls["n"] > 1:
            raise RuntimeError("job service down")

    monkeypatch.setattr(api_main, "_persist_run_state", _persist_fails_after_first_call)

    resp = api_client.post("/strategy-lab/runs/run-rollback/restart")
    assert resp.status_code == 409


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
    # lab_job_client provides a single shared instance -- see
    # test_restart_strategy_lab_run_happy_path for why a fresh instance per
    # _get_lab_run_job_client() call breaks the pre-dispatch revalidation read.

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


def test_list_strategy_lab_runs_merged_entry_null_data_does_not_500(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A persisted job whose ``"data"`` key is present but ``None`` must not
    500 -- regression test for issue #4325. Before the fix,
    ``normalize_persisted_job`` did ``data = job.get("data", job)``, which
    only falls back to ``job`` when the key is *absent*; a present-but-null
    value passed straight through and crashed on the next line's
    ``data["run_id"] = ...``. That ``TypeError`` was swallowed by the outer
    ``except Exception`` around the whole reconcile+merge block, silently
    dropping every persisted running/pending job from the response (not just
    this malformed one) and falling back to the in-memory-only snapshot.
    """
    from investment_team.api import main as api_main

    api_main._active_runs.clear()
    stub = _StubLabClient(
        jobs=[
            {
                "job_id": "run-null-data",
                "status": "running",
                "data": None,
            }
        ]
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)
    resp = api_client.get("/strategy-lab/runs")
    assert resp.status_code == 200
    assert any(r["run_id"] == "run-null-data" for r in resp.json()["runs"])


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


def test_job_progress_percent_clamps_out_of_range_values() -> None:
    """``completed`` exceeding ``total`` or negative can't yield an out-of-range percentage."""
    from investment_team.api import main as api_main

    assert api_main._job_progress_percent(5, 4) == 100  # completed exceeds total
    assert api_main._job_progress_percent(-1, 4) == 0  # negative completed


@pytest.mark.parametrize(
    "completed,total",
    [("not-a-number", 4), (4, "not-a-number"), (None, 4), ([], 4), ({}, {})],
)
def test_job_progress_percent_tolerates_non_numeric_inputs(completed, total) -> None:
    """Regression: malformed in-memory or persisted state (e.g. a
    non-numeric string from durable-record corruption) must not raise
    TypeError -- list_strategy_lab_jobs's in-memory loop calls this
    unguarded by any try/except, unlike the persisted-job merge loop, so a
    raise here would crash the whole endpoint despite its own documented
    "always returns 200" contract."""
    from investment_team.api import main as api_main

    assert api_main._job_progress_percent(completed, total) == 0


def test_list_strategy_lab_jobs_tolerates_malformed_in_memory_progress_fields(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """Regression: a malformed in-memory run (non-numeric completed_cycles/
    total_cycles, e.g. from corrupted durable state merged in via
    normalize_persisted_job) must not crash the endpoint -- it must degrade
    to a 0% progress entry instead, honoring the documented "always returns
    200" contract for the in-memory path, not just the persisted-job path."""
    from investment_team.api import main as api_main

    api_main._active_runs["run-malformed-progress"] = {
        "run_id": "run-malformed-progress",
        "status": "completed",
        "completed_cycles": "not-a-number",
        "total_cycles": "also-not-a-number",
        "started_at": "2024-01-01T00:00:00Z",
    }

    resp = api_client.get("/strategy-lab/jobs")

    assert resp.status_code == 200
    job = next(j for j in resp.json()["jobs"] if j["job_id"] == "run-malformed-progress")
    assert job["progress"] == 0


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


def test_list_strategy_lab_jobs_same_id_reconciles_terminal_and_dedupes(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """When the same run/job id exists in both stores, it appears exactly once,
    reconciled against the persisted (terminal) job-service record.

    Regression test for the double-lock TOCTOU bug (dedup by id, no
    duplicates) combined with stale-progress reconciliation: the persisted
    record for this id is terminal (``completed``), so
    ``_reconcile_run_progress`` flips the in-memory entry's status/progress
    to match it before the response is built -- the in-memory entry no
    longer wins with stale values, but its identity still dedupes the merge
    to a single row. ``current_phase`` is untouched because the persisted
    stub's ``data`` has no ``current_cycle`` key (the field-presence guard
    in ``_reconcile_run_progress`` leaves absent fields alone).
    """
    from investment_team.api import main as api_main

    shared_id = "dup-run"
    api_main._active_runs[shared_id] = {
        "run_id": shared_id,
        "status": "running",
        "total_cycles": 4,
        "completed_cycles": 1,
        "started_at": "2024-01-01T00:00:00Z",
        "current_cycle": {"phase": "ideation", "strategy": {"hypothesis": "in-memory wins"}},
    }
    stub = _StubLabClient(
        jobs=[
            {
                "job_id": shared_id,
                "status": "completed",
                "data": {
                    "started_at": "2024-01-01T00:00:00Z",
                    "total_cycles": 4,
                    "completed_cycles": 4,
                },
            }
        ]
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.get("/strategy-lab/jobs")

    assert resp.status_code == 200
    body = resp.json()
    matches = [j for j in body["jobs"] if j["job_id"] == shared_id]
    assert len(matches) == 1
    entry = matches[0]
    assert entry["status"] == "completed"
    assert entry["progress"] == 100
    assert entry["current_phase"] == "ideation"


def test_list_strategy_lab_jobs_reconciles_progress_while_non_terminal(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A non-terminal in-memory run's progress is refreshed from the job
    service without its status being touched.

    Regression test for issue #4299: unlike ``list_strategy_lab_runs``,
    ``get_strategy_lab_run_status``, and the SSE snapshot, this endpoint
    used to build summaries straight from ``_active_runs`` without ever
    calling ``_reconcile_run_progress``, so dispatch-time progress counters
    could go stale while a run was still active. Both records are
    ``running`` here -- only ``completed_cycles`` differs -- confirming the
    reconciliation is not gated on a terminal transition.
    """
    from investment_team.api import main as api_main

    run_id = "prog-run"
    api_main._active_runs[run_id] = {
        "run_id": run_id,
        "status": "running",
        "total_cycles": 4,
        "completed_cycles": 1,
        "started_at": "2024-01-01T00:00:00Z",
    }
    stub = _StubLabClient(
        jobs=[
            {
                "job_id": run_id,
                "status": "running",
                "data": {
                    "started_at": "2024-01-01T00:00:00Z",
                    "total_cycles": 4,
                    "completed_cycles": 3,
                },
            }
        ]
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.get("/strategy-lab/jobs")

    assert resp.status_code == 200
    body = resp.json()
    matches = [j for j in body["jobs"] if j["job_id"] == run_id]
    assert len(matches) == 1
    entry = matches[0]
    assert entry["status"] == "running"
    assert entry["progress"] == 75
    assert api_main._active_runs[run_id]["completed_cycles"] == 3


def test_list_strategy_lab_jobs_tolerates_malformed_current_cycle(
    monkeypatch: pytest.MonkeyPatch, api_client
) -> None:
    """A non-dict ``current_cycle``/``strategy`` reconciled from job-service
    data doesn't 500 the whole endpoint.

    Regression test for issue #4261: ``current_cycle`` is never populated by
    any first-party writer, but it does pass through an unvalidated
    boundary -- ``_reconcile_run_progress`` copies it verbatim from the raw
    job-service record's ``data`` into a live ``_active_runs`` entry with no
    shape check. Simulate that by seeding a persisted record whose
    ``current_cycle`` is a plain string (a stand-in for corrupted/foreign
    data), and confirm the endpoint degrades to a fallback label/phase
    instead of raising ``AttributeError``.
    """
    from investment_team.api import main as api_main

    run_id = "malformed-run"
    api_main._active_runs[run_id] = {
        "run_id": run_id,
        "status": "running",
        "total_cycles": 4,
        "completed_cycles": 1,
        "started_at": "2024-01-01T00:00:00Z",
        "current_cycle": None,
    }
    stub = _StubLabClient(
        jobs=[
            {
                "job_id": run_id,
                "status": "running",
                "data": {
                    "started_at": "2024-01-01T00:00:00Z",
                    "total_cycles": 4,
                    "completed_cycles": 1,
                    "current_cycle": "not-a-dict",
                },
            }
        ]
    )
    monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: stub)

    resp = api_client.get("/strategy-lab/jobs")

    assert resp.status_code == 200
    body = resp.json()
    matches = [j for j in body["jobs"] if j["job_id"] == run_id]
    assert len(matches) == 1
    entry = matches[0]
    assert entry["current_phase"] is None
    assert entry["label"] == "Strategy batch (1/4)"


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


def test_delete_strategy_lab_run_409_when_transition_lock_held(
    monkeypatch: pytest.MonkeyPatch, lab_job_client, api_client
) -> None:
    """A delete request for a run_id whose transition lock is already held is
    rejected with 409 without touching the job service or _active_runs."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state as _run_state

    run_id = "run-lock-held-delete"
    api_main._active_runs[run_id] = {"run_id": run_id, "status": "completed"}

    held_lock = _run_state.acquire_run_transition_lock(run_id)
    assert held_lock is not None
    try:
        resp = api_client.delete(f"/strategy-lab/runs/{run_id}")
        assert resp.status_code == 409
        assert "Another transition" in resp.json()["detail"]
        assert lab_job_client.deleted == []
        assert run_id in api_main._active_runs
    finally:
        held_lock.release()


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
          ``max_chunks`` chunks, within ``timeout_seconds`` (measured via
          ``time.monotonic()``, not wall-clock time), or before
          ``body_iter`` is exhausted (a route regression that stops
          emitting before the terminal event would otherwise silently
          return an incomplete body instead of failing the test).
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
    raise AssertionError("SSE stream ended without a terminal done line")


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
