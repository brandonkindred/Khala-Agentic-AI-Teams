"""Tests for job_store list filters and the cross-worker resume claim."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from coding_team import job_store


class _FakeClient:
    def __init__(self):
        self.calls: List[Any] = []

    def list_jobs(self, statuses: Optional[List[str]] = None):
        self.calls.append(statuses)
        return []


def test_running_only_includes_waiting_for_user(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)

    job_store.list_jobs(running_only=True)
    assert fake.calls[-1] == list(job_store.NON_TERMINAL_STATUSES)
    assert "waiting_for_user" in fake.calls[-1]

    job_store.list_jobs()
    assert fake.calls[-1] is None


class _AtomicFakeClient:
    """Minimal in-memory client modeling the job service's atomic, row-locked apply + get_job."""

    def __init__(self, job: Dict[str, Any]):
        self.job = job

    def get_job(self, job_id):
        return dict(self.job)

    def apply_and_get(self, job_id, *, increment=None, merge_fields=None, **_kw):
        if increment:
            for field, delta in increment.items():
                self.job[field] = (self.job.get(field) or 0) + delta
        if merge_fields:
            self.job.update(merge_fields)
        return dict(self.job)

    def update_job(self, job_id, *, heartbeat=True, **fields):
        self.job.update(fields)


def _stamp(seconds_ago: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_claim_resume_first_caller_wins_when_lease_free(monkeypatch):
    fake = _AtomicFakeClient({"job_id": "j1"})  # no lease yet
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)
    assert job_store.claim_resume("j1") is True  # acquires the free lease
    # Now a fresh lease is held → a subsequent claim declines.
    assert job_store.claim_resume("j1") is False


def test_claim_resume_declines_while_lease_fresh(monkeypatch):
    fake = _AtomicFakeClient({"job_id": "j1", "resume_claim_seq": 5, "resume_claim_at": _stamp(1)})
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)
    assert job_store.claim_resume("j1") is False  # a live worker holds it


def test_claim_resume_reclaims_expired_lease_after_worker_death(monkeypatch):
    # Winner died: the lease stamp is older than the TTL and the seq stayed nonzero.
    fake = _AtomicFakeClient(
        {
            "job_id": "j1",
            "resume_claim_seq": 1,
            "resume_claim_at": _stamp(job_store.RESUME_CLAIM_TTL_S + 10),
        }
    )
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)
    assert job_store.claim_resume("j1") is True  # recoverable — not wedged
    assert fake.job["resume_claim_seq"] == 2


def test_claim_resume_future_stamp_is_not_fresh(monkeypatch):
    # A future-dated stamp (skew/corruption) must not block the claim forever.
    fake = _AtomicFakeClient(
        {"job_id": "j1", "resume_claim_seq": 1, "resume_claim_at": _stamp(-3600)}
    )
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)
    assert job_store.claim_resume("j1") is True


def test_release_clears_stamp_so_a_later_caller_can_win(monkeypatch):
    fake = _AtomicFakeClient({"job_id": "j1", "resume_claim_seq": 3, "resume_claim_at": _stamp(1)})
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)
    assert job_store.claim_resume("j1") is False  # fresh lease blocks
    job_store.release_resume_claim("j1")
    assert fake.job["resume_claim_at"] is None
    assert fake.job["resume_claim_seq"] == 3  # seq untouched (monotonic)
    assert job_store.claim_resume("j1") is True  # now re-claimable


def test_concurrent_claim_only_one_wins(monkeypatch):
    """Two callers that both read the same prior seq before either writes: only the one whose
    increment yields prior+1 wins (optimistic compare-and-set)."""
    fake = _AtomicFakeClient({"job_id": "j1", "resume_claim_seq": 5})
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)
    # Simulate both reading seq=5 before either increments by snapshotting get_job.
    snapshot = dict(fake.job)
    fake.get_job = lambda job_id: dict(snapshot)  # both callers see seq=5, no fresh stamp
    results = [job_store.claim_resume("j1"), job_store.claim_resume("j1")]
    assert results.count(True) == 1  # exactly one winner
    assert fake.job["resume_claim_seq"] == 7  # both incremented (5→6→7), one matched 6


def test_claim_resume_returns_false_for_missing_job(monkeypatch):
    class _NoneClient:
        def get_job(self, *a, **k):
            return None

    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: _NoneClient())
    assert job_store.claim_resume("ghost") is False
