"""End-to-end (offline) tests for the orchestrator with fake agents + store."""

from __future__ import annotations

import pytest

from job_matching_team.models import JobMatchRequest, JobPosting, RankedJob
from job_matching_team.orchestrator import JobMatchingOrchestrator
from job_matching_team.profile.model import JobSeekerProfile


class FakeQB:
    def build(self, profile, *, max_queries):
        return ["q1", "q2"][:max_queries]


class FakeScanner:
    def __init__(self, postings):
        self.postings = postings
        self.last_skip = None

    def scan(self, queries, *, max_roles, skip_fingerprints=None):
        self.last_skip = skip_fingerprints
        return self.postings[:max_roles]


class FakeRanker:
    def rank(self, postings, profile):
        return [RankedJob(posting=p, score=1.0 - i * 0.1) for i, p in enumerate(postings)]


class RecordingStore:
    def __init__(self, seen=None):
        self.created = None
        self.saved = None
        self.failed = None
        self._seen = seen or set()

    def create_run(self, run_id, profile, request):
        self.created = (run_id, profile, request)

    def save_results(self, run_id, ranked, *, total_found, scanned_fingerprints=None):
        self.saved = (run_id, ranked, total_found)
        self.saved_scanned = scanned_fingerprints

    def mark_failed(self, run_id, error):
        self.failed = (run_id, error)

    def seen_fingerprints(self):
        return self._seen


class _JobIdCapturingRanker:
    """Records the ``job_id`` bound in the attribution context during rank()."""

    def __init__(self) -> None:
        self.job_id: str | None = None

    def rank(self, postings, profile):
        from llm_service.attribution import current_attribution

        self.job_id = current_attribution().job_id
        return [RankedJob(posting=p, score=1.0) for p in postings]


def _make_postings(n):
    return [JobPosting(company=f"C{i}", title="Eng").ensure_fingerprint() for i in range(n)]


def test_run_binds_api_job_id_for_telemetry():
    """The owning API job_id (not the internal run_id) is bound when provided."""
    ranker = _JobIdCapturingRanker()
    orch = JobMatchingOrchestrator(
        query_builder=FakeQB(), scanner=FakeScanner(_make_postings(1)), ranker=ranker, store=None
    )
    orch.run(JobMatchRequest(), profile=JobSeekerProfile(), job_id="api-job-123")
    assert ranker.job_id == "api-job-123"


def test_run_falls_back_to_run_id_without_api_job_id():
    """Direct/sync callers (no API job_id) still attribute to the internal run_id."""
    ranker = _JobIdCapturingRanker()
    orch = JobMatchingOrchestrator(
        query_builder=FakeQB(), scanner=FakeScanner(_make_postings(1)), ranker=ranker, store=None
    )
    orch.run(JobMatchRequest(), profile=JobSeekerProfile())
    assert ranker.job_id  # a non-empty uuid run_id
    assert ranker.job_id != "api-job-123"


def test_run_persists_and_returns_top_n():
    store = RecordingStore()
    orch = JobMatchingOrchestrator(
        query_builder=FakeQB(),
        scanner=FakeScanner(_make_postings(5)),
        ranker=FakeRanker(),
        store=store,
    )
    resp = orch.run(JobMatchRequest(top_n=3), profile=JobSeekerProfile())
    assert len(resp.ranked_jobs) == 3
    assert resp.total_found == 5
    assert resp.total_ranked == 3
    # Sorted best-first.
    assert resp.ranked_jobs[0].score >= resp.ranked_jobs[1].score
    assert store.created[0] == resp.run_id
    assert store.saved[0] == resp.run_id
    assert store.saved[2] == 5
    # All 5 scanned postings' fingerprints are persisted for exclude_seen,
    # even though only the top 3 are returned/stored as ranked rows.
    assert store.saved_scanned is not None
    assert len(store.saved_scanned) == 5


def test_overrides_merged_into_snapshot():
    orch = JobMatchingOrchestrator(
        query_builder=FakeQB(),
        scanner=FakeScanner([]),
        ranker=FakeRanker(),
        persist=False,
    )
    resp = orch.run(
        JobMatchRequest(profile_overrides={"salary_min": 999}),
        profile=JobSeekerProfile(salary_min=1),
    )
    assert resp.profile_snapshot.salary_min == 999


def test_exclude_seen_passes_skip_set():
    scanner = FakeScanner(_make_postings(2))
    store = RecordingStore(seen={"abc"})
    orch = JobMatchingOrchestrator(
        query_builder=FakeQB(), scanner=scanner, ranker=FakeRanker(), store=store
    )
    orch.run(JobMatchRequest(exclude_seen=True), profile=JobSeekerProfile())
    assert scanner.last_skip == {"abc"}


def test_no_persist_skips_store():
    orch = JobMatchingOrchestrator(
        query_builder=FakeQB(),
        scanner=FakeScanner(_make_postings(1)),
        ranker=FakeRanker(),
        persist=False,
    )
    resp = orch.run(JobMatchRequest(), profile=JobSeekerProfile())
    assert resp.total_ranked == 1


def test_failure_marks_run_failed_and_reraises():
    class BoomScanner:
        def scan(self, *a, **k):
            raise RuntimeError("scan exploded")

    store = RecordingStore()
    orch = JobMatchingOrchestrator(
        query_builder=FakeQB(), scanner=BoomScanner(), ranker=FakeRanker(), store=store
    )
    with pytest.raises(RuntimeError, match="scan exploded"):
        orch.run(JobMatchRequest(), profile=JobSeekerProfile())
    assert store.failed[0] is not None
    assert "scan exploded" in store.failed[1]


def test_save_failure_marks_run_failed_but_still_returns():
    class BadSaveStore(RecordingStore):
        def save_results(self, *a, **k):
            raise RuntimeError("disk full")

    store = BadSaveStore()
    orch = JobMatchingOrchestrator(
        query_builder=FakeQB(),
        scanner=FakeScanner(_make_postings(2)),
        ranker=FakeRanker(),
        store=store,
    )
    # The scan still returns results to the caller...
    resp = orch.run(JobMatchRequest(), profile=JobSeekerProfile())
    assert resp.total_ranked == 2
    # ...but the run row is marked failed rather than left stuck in RUNNING.
    assert store.failed is not None
    assert store.failed[0] == resp.run_id


def test_create_run_failure_degrades_gracefully():
    class BadCreateStore(RecordingStore):
        def create_run(self, *a, **k):
            raise RuntimeError("db down")

    orch = JobMatchingOrchestrator(
        query_builder=FakeQB(),
        scanner=FakeScanner(_make_postings(2)),
        ranker=FakeRanker(),
        store=BadCreateStore(),
    )
    # Scan still completes even though persistence is unavailable.
    resp = orch.run(JobMatchRequest(), profile=JobSeekerProfile())
    assert resp.total_ranked == 2
