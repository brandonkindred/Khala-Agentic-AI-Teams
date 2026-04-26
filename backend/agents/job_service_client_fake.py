"""In-memory ``JobServiceClient`` substitute for unit tests.

Tests that exercise team code which talks to the job service should use the
``fake_job_client`` pytest fixture exposed from this module instead of the
real ``JobServiceClient``.  The fake stores everything in a per-instance
dict, so tests get isolation by constructing a fresh fake (typical case:
one instance per test, monkey-patched into the team module under test).

Why a separate module?  Some teams (e.g. ``software_engineering_team``)
override pytest's rootdir via their own ``pyproject.toml``, which means
``backend/conftest.py`` is **not** auto-discovered for their tests.  By
shipping the fake + fixture as an importable module on the standard agents
``pythonpath``, any team's ``tests/conftest.py`` can pull it in with a
single one-liner::

    from job_service_client_fake import fake_job_client  # noqa: F401
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

_ACTIVE_STATUSES = frozenset({"pending", "running"})
# Mirrors backend/job_service/db.py:411-413 / 446-448 / 471-473.
_WAITING_FIELDS = (
    "waiting_for_answers",
    "waiting_for_title_selection",
    "waiting_for_story_input",
    "waiting_for_draft_feedback",
)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class FakeJobServiceClient:
    """Minimal in-memory implementation of :class:`JobServiceClient`."""

    def __init__(self, team: str = "test", base_url: str | None = None) -> None:
        self.team = team
        self._jobs: dict[str, dict[str, Any]] = {}

    # -- internal -----------------------------------------------------------

    def _stamp(self, job: dict[str, Any], *, heartbeat: bool = True) -> None:
        now = _now_iso()
        job["updated_at"] = now
        if heartbeat:
            job["last_heartbeat_at"] = now

    # -- core CRUD ----------------------------------------------------------

    def create_job(self, job_id: str, *, status: str = "pending", **fields: Any) -> None:
        now = _now_iso()
        self._jobs[job_id] = {
            "job_id": job_id,
            "team": self.team,
            "status": status,
            "created_at": now,
            "updated_at": now,
            "last_heartbeat_at": now,
            "events": [],
            **fields,
        }

    def replace_job(self, job_id: str, payload: dict[str, Any]) -> None:
        self._jobs[job_id] = dict(payload)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        return dict(job) if job is not None else None

    def delete_job(self, job_id: str) -> bool:
        return self._jobs.pop(job_id, None) is not None

    def list_jobs(self, *, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        jobs = [dict(j) for j in self._jobs.values()]
        if statuses:
            jobs = [j for j in jobs if j.get("status") in statuses]
        return jobs

    def update_job(self, job_id: str, *, heartbeat: bool = True, **fields: Any) -> None:
        # Mirror production: ``PATCH /jobs/{team}/{job_id}`` runs a bare
        # SQL UPDATE.  If no row matches, it silently does nothing — it
        # does NOT auto-create the job.  Tests that need a job present
        # must call ``create_job`` first.
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.update(fields)
        self._stamp(job, heartbeat=heartbeat)

    # -- events / heartbeat -------------------------------------------------

    def append_event(
        self,
        job_id: str,
        *,
        action: str,
        outcome: str | None = None,
        details: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        events = job.setdefault("events", [])
        events.append(
            {
                "timestamp": _now_iso(),
                "action": action,
                "outcome": outcome,
                "details": details,
            }
        )
        if status is not None:
            job["status"] = status
        self._stamp(job)

    def heartbeat(self, job_id: str) -> None:
        # Mirror production: ``POST /jobs/{team}/{job_id}/heartbeat`` returns
        # 404 when the job is missing, which ``JobServiceClient.heartbeat``
        # surfaces as ``httpx.HTTPStatusError``.  Raise the same exception
        # type so unit tests catching it behave like integration tests do.
        job = self._jobs.get(job_id)
        if job is None:
            request = httpx.Request("POST", f"/jobs/{self.team}/{job_id}/heartbeat")
            response = httpx.Response(404, request=request, json={"detail": "Job not found"})
            raise httpx.HTTPStatusError("Job not found", request=request, response=response)
        self._stamp(job)

    # -- bulk lifecycle -----------------------------------------------------

    def mark_stale_active_jobs_failed(
        self,
        *,
        stale_after_seconds: float,
        reason: str,
        waiting_field: str = "waiting_for_answers",
    ) -> list[str]:
        cutoff = datetime.now(tz=timezone.utc).timestamp() - stale_after_seconds
        # Mirror production (``backend/job_service/db.py:404-413``): the
        # caller-supplied ``waiting_field`` is excluded *in addition to* the
        # other paused-for-user states.  A unit test that paused a job via,
        # say, ``waiting_for_title_selection`` must not see it failed here.
        waiting_fields = {waiting_field, *_WAITING_FIELDS}
        failed: list[str] = []
        for job in self._jobs.values():
            if job.get("status") not in _ACTIVE_STATUSES:
                continue
            if any(job.get(wf) for wf in waiting_fields):
                continue
            hb = job.get("last_heartbeat_at") or job.get("updated_at") or job.get("created_at")
            try:
                hb_ts = datetime.fromisoformat(hb).timestamp() if hb else 0.0
            except (TypeError, ValueError):
                hb_ts = 0.0
            if hb_ts < cutoff:
                job["status"] = "failed"
                job["error"] = reason
                self._stamp(job, heartbeat=False)
                failed.append(job["job_id"])
        return failed

    def mark_all_active_jobs_failed(self, reason: str, **_: Any) -> list[str]:
        return self._mark_all("failed", reason)

    def mark_all_active_jobs_interrupted(self, reason: str, **_: Any) -> list[str]:
        return self._mark_all("interrupted", reason)

    def _mark_all(self, target_status: str, reason: str) -> list[str]:
        ids: list[str] = []
        for job in self._jobs.values():
            if job.get("status") not in _ACTIVE_STATUSES:
                continue
            if any(job.get(wf) for wf in _WAITING_FIELDS):
                continue
            job["status"] = target_status
            job["error"] = reason
            self._stamp(job, heartbeat=False)
            ids.append(job["job_id"])
        return ids

    # -- atomic patch helpers ----------------------------------------------

    def merge_nested(self, job_id: str, path: str, data: dict[str, Any]) -> None:
        self.atomic_update(job_id, merge_nested={path: data})

    def append_to_list(self, job_id: str, field: str, items: list[Any]) -> None:
        self.atomic_update(job_id, append_to={field: items})

    def increment_field(self, job_id: str, field: str, delta: int = 1) -> None:
        self.atomic_update(job_id, increment={field: delta})

    def atomic_update(
        self,
        job_id: str,
        *,
        merge_fields: dict[str, Any] | None = None,
        merge_nested: dict[str, Any] | None = None,
        append_to: dict[str, list[Any]] | None = None,
        increment: dict[str, int] | None = None,
    ) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        if merge_fields:
            job.update(merge_fields)
        if merge_nested:
            for dotted, value in merge_nested.items():
                target = job
                parts = dotted.split(".")
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                leaf = parts[-1]
                existing = target.get(leaf, {})
                if isinstance(existing, dict) and isinstance(value, dict):
                    existing.update(value)
                    target[leaf] = existing
                else:
                    target[leaf] = value
        if append_to:
            for field, items in append_to.items():
                existing = job.get(field, [])
                if not isinstance(existing, list):
                    existing = []
                existing.extend(items)
                job[field] = existing
        if increment:
            for field, delta in increment.items():
                current = job.get(field, 0)
                if not isinstance(current, (int, float)):
                    current = 0
                job[field] = current + delta
        self._stamp(job)


@pytest.fixture
def fake_job_client() -> FakeJobServiceClient:
    """Function-scoped in-memory ``JobServiceClient`` substitute.

    Tests opt in by requesting this fixture and using ``monkeypatch`` to bind
    it into the team module under test (typically by patching the team's
    ``_client`` factory or the module-level ``_job_manager`` reference).
    """
    return FakeJobServiceClient(team="test")
