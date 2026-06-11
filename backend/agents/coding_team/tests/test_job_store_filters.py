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
    """Minimal in-memory client modeling the job service's atomic, row-locked increment."""

    def __init__(self, job: Dict[str, Any]):
        self.job = job

    def apply_and_get(self, job_id, *, increment=None, **_kw):
        if increment:
            for field, delta in increment.items():
                self.job[field] = self.job.get(field, 0) + delta
        return dict(self.job)

    def update_job(self, job_id, *, heartbeat=True, **fields):
        self.job.update(fields)


def test_claim_resume_first_caller_wins_others_lose(monkeypatch):
    fake = _AtomicFakeClient({"job_id": "j1"})  # no resume_claim yet → starts at 0
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)

    assert job_store.claim_resume("j1") is True  # 0 -> 1 wins
    assert job_store.claim_resume("j1") is False  # 1 -> 2 loses
    assert job_store.claim_resume("j1") is False  # 2 -> 3 loses


def test_release_resets_claim_so_a_later_caller_can_win(monkeypatch):
    fake = _AtomicFakeClient({"job_id": "j1", "resume_claim": 3})
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)

    job_store.release_resume_claim("j1")
    assert fake.job["resume_claim"] == 0
    assert job_store.claim_resume("j1") is True  # 0 -> 1 wins again


def test_claim_resume_returns_false_for_missing_job(monkeypatch):
    class _NoneClient:
        def apply_and_get(self, *a, **k):
            return None

    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: _NoneClient())
    assert job_store.claim_resume("ghost") is False
