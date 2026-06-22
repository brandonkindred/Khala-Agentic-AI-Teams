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


def test_create_job_records_profile_association(monkeypatch):
    """create_job links the new project to the default user profile (best-effort)."""
    from user_profile import ArtifactType

    class _CreateClient:
        def create_job(self, job_id, status="pending", **data):
            pass

    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: _CreateClient())
    calls: List[Any] = []
    monkeypatch.setattr(job_store, "record_association_safe", lambda *a, **k: calls.append((a, k)))

    job_store.create_job("job_1", "my/repo")
    assert calls == [((ArtifactType.PROJECT, "coding_team", "job_1"), {"label": "my/repo"})]


def test_active_only_includes_waiting_for_user(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)

    job_store.list_jobs(active_only=True)
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


def test_release_swallows_store_transport_error(monkeypatch):
    """Release is best-effort cleanup: a job-store transport error must be swallowed, not raised —
    callers promise 'never raises' or call it from an except block re-raising a prior error, and the
    lease self-heals via its TTL anyway."""

    class _BoomClient:
        def update_job(self, *a, **k):
            raise RuntimeError("store down")

    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: _BoomClient())
    # Must not raise.
    assert job_store.release_resume_claim("j1") is None


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


def test_claim_tolerates_small_forward_clock_skew(monkeypatch):
    """A claim stamp a few seconds in the future (NTP drift on the stamping host) must still
    be treated as fresh so the checking worker doesn't immediately re-claim an active lease
    and allow two orchestrators to run against the same checkout."""
    from datetime import datetime, timedelta, timezone

    future_stamp = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    fake = _AtomicFakeClient(
        {"job_id": "j1", "resume_claim_seq": 1, "resume_claim_at": future_stamp}
    )
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)
    assert job_store.claim_resume("j1") is False  # 5s ahead is within tolerance → still fresh


def test_claim_implausibly_future_stamp_is_not_fresh(monkeypatch):
    """A stamp far in the future (beyond _CLAIM_CLOCK_SKEW_TOLERANCE_S) is corruption or
    severe misconfiguration — treat it as expired so the lease can be re-claimed rather
    than wedged indefinitely."""
    from datetime import datetime, timedelta, timezone

    far_future = (
        datetime.now(timezone.utc) + timedelta(seconds=job_store._CLAIM_CLOCK_SKEW_TOLERANCE_S + 30)
    ).isoformat()
    fake = _AtomicFakeClient({"job_id": "j1", "resume_claim_seq": 1, "resume_claim_at": far_future})
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)
    assert job_store.claim_resume("j1") is True  # far-future stamp re-claimable
