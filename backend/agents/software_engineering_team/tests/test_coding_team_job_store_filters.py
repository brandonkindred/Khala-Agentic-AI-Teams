"""Tests for job_store list filters."""

from __future__ import annotations

from typing import Any, List, Optional

from software_engineering_team import job_store


class _FakeClient:
    def __init__(self):
        self.calls: List[Any] = []

    def list_jobs(self, statuses: Optional[List[str]] = None):
        self.calls.append(statuses)
        return []


def test_active_only_includes_waiting_for_user(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)

    job_store.list_jobs(active_only=True)
    assert fake.calls[-1] == list(job_store.NON_TERMINAL_STATUSES)
    assert "waiting_for_user" in fake.calls[-1]

    job_store.list_jobs()
    assert fake.calls[-1] is None
