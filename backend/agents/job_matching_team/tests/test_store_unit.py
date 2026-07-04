"""Unit tests for the Postgres store with a mocked connection (no real DB)."""

from __future__ import annotations

from job_matching_team import store as store_mod
from job_matching_team.models import JobMatchRequest, JobPosting, RankedJob, SubScores
from job_matching_team.profile.model import JobSeekerProfile
from job_matching_team.store import JobMatchingStore, get_store


class FakeCursor:
    def __init__(self, fetchone=None, fetchall=None):
        self._fetchone = list(fetchone or [])
        self._fetchall = list(fetchall or [])
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None

    def fetchall(self):
        return self._fetchall.pop(0) if self._fetchall else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, row_factory=None):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_conn(monkeypatch, cursor):
    monkeypatch.setattr(store_mod, "get_conn", lambda: FakeConn(cursor))


def _ranked(company="Acme", score=0.9):
    posting = JobPosting(company=company, title="Eng", location="NYC").ensure_fingerprint()
    return RankedJob(
        posting=posting,
        score=score,
        sub_scores=SubScores(title_fit=0.8),
        recommendation="apply",
        rationale="fit",
        concerns=["c1"],
    )


def test_create_run_executes_insert(monkeypatch):
    cur = FakeCursor()
    _patch_conn(monkeypatch, cur)
    JobMatchingStore().create_run("r1", JobSeekerProfile(), JobMatchRequest(top_n=7))
    assert any("INSERT INTO job_matching_runs" in sql for sql, _ in cur.executed)


def test_save_results_inserts_rows_and_completes(monkeypatch):
    cur = FakeCursor()
    _patch_conn(monkeypatch, cur)
    JobMatchingStore().save_results(
        "r1",
        [_ranked("A"), _ranked("B")],
        total_found=4,
        scanned_fingerprints=["a", "b", "c", "a", ""],
    )
    inserts = [s for s, _ in cur.executed if "INSERT INTO job_matching_ranked_jobs" in s]
    updates = [s for s, p in cur.executed if "UPDATE job_matching_runs" in s]
    assert len(inserts) == 2
    assert len(updates) == 1
    # The UPDATE persists the de-duplicated, non-empty scanned fingerprint set.
    update_params = next(p for s, p in cur.executed if "UPDATE job_matching_runs" in s)
    seen_json = update_params[3]
    assert sorted(seen_json.obj) == ["a", "b", "c"]


def test_mark_failed_truncates(monkeypatch):
    cur = FakeCursor()
    _patch_conn(monkeypatch, cur)
    JobMatchingStore().mark_failed("r1", "x" * 5000)
    _, params = cur.executed[0]
    assert len(params[1]) <= 2000


def test_list_runs_maps_rows(monkeypatch):
    rows = [
        {
            "run_id": "r1",
            "status": "completed",
            "total_found": 3,
            "total_ranked": 2,
            "created_at": "2026-01-01T00:00:00+00:00",
            "completed_at": None,
        }
    ]
    cur = FakeCursor(fetchall=[rows])
    _patch_conn(monkeypatch, cur)
    out = JobMatchingStore().list_runs(limit=10)
    assert out[0].run_id == "r1"
    assert out[0].total_found == 3


def test_get_run_maps_detail_and_jobs(monkeypatch):
    run_row = {
        "run_id": "r1",
        "status": "completed",
        "total_found": 2,
        "total_ranked": 1,
        "error": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T01:00:00+00:00",
    }
    job_rows = [
        {
            "score": 0.9,
            "sub_scores": {"title_fit": 0.8},
            "posting": {"company": "Acme", "title": "Eng"},
            "recommendation": "apply",
            "rationale": "good",
            "concerns": ["c1"],
        }
    ]
    cur = FakeCursor(fetchone=[run_row], fetchall=[job_rows])
    _patch_conn(monkeypatch, cur)
    detail = JobMatchingStore().get_run("r1")
    assert detail.run_id == "r1"
    assert detail.ranked_jobs[0].posting.company == "Acme"
    assert detail.ranked_jobs[0].sub_scores.title_fit == 0.8


def test_get_run_missing_returns_none(monkeypatch):
    cur = FakeCursor(fetchone=[None])
    _patch_conn(monkeypatch, cur)
    assert JobMatchingStore().get_run("nope") is None


def test_seen_fingerprints_unions_run_and_ranked(monkeypatch):
    # First query: run-level scanned fingerprints; second: ranked-job rows.
    cur = FakeCursor(fetchall=[[("scanned1",), ("scanned2",)], [("fp1",), ("fp2",)]])
    _patch_conn(monkeypatch, cur)
    assert JobMatchingStore().seen_fingerprints() == {"scanned1", "scanned2", "fp1", "fp2"}


def _listing_row(fingerprint="fp1", status="new"):
    return {
        "fingerprint": fingerprint,
        "run_id": "r1",
        "score": 0.9,
        "sub_scores": {"title_fit": 0.8},
        "posting": {"company": "Acme", "title": "Eng", "fingerprint": fingerprint},
        "recommendation": "apply",
        "rationale": "fit",
        "concerns": ["c1"],
        "created_at": "2026-01-01T00:00:00+00:00",
        "times_seen": 2,
        "status": status,
        "notes": None,
        "status_updated_at": None,
    }


def test_list_listings_maps_rows_and_counts(monkeypatch):
    cur = FakeCursor(
        fetchall=[[_listing_row()], [{"status": "new", "n": 3}, {"status": "favorite", "n": 1}]]
    )
    _patch_conn(monkeypatch, cur)
    out = JobMatchingStore().list_listings(status="active", limit=50)
    assert out.total == 1
    assert out.listings[0].fingerprint == "fp1"
    assert out.listings[0].times_seen == 2
    assert out.listings[0].posting.company == "Acme"
    assert out.counts == {"new": 3, "favorite": 1}
    listing_sql, listing_params = cur.executed[0]
    # Latest snapshot per fingerprint + inbox filter + limit.
    assert "DISTINCT ON (fingerprint)" in listing_sql
    assert "NOT IN ('archived', 'not_interested')" in listing_sql
    assert listing_params == (50,)


def test_list_listings_specific_status_filters_exactly(monkeypatch):
    cur = FakeCursor(fetchall=[[_listing_row(status="favorite")], []])
    _patch_conn(monkeypatch, cur)
    out = JobMatchingStore().list_listings(status="favorite", limit=10)
    listing_sql, listing_params = cur.executed[0]
    assert "COALESCE(s.status, 'new') = %s" in listing_sql
    assert listing_params == ("favorite", 10)
    assert out.listings[0].status == "favorite"


def test_list_listings_all_disables_filtering(monkeypatch):
    cur = FakeCursor(fetchall=[[], []])
    _patch_conn(monkeypatch, cur)
    JobMatchingStore().list_listings(status="all")
    listing_sql, _ = cur.executed[0]
    assert "WHERE COALESCE" not in listing_sql


def test_list_listings_rejects_invalid_filter(monkeypatch):
    _patch_conn(monkeypatch, FakeCursor())
    try:
        JobMatchingStore().list_listings(status="bogus")
    except AssertionError:
        return
    raise AssertionError("invalid filter must be rejected")


def test_update_listing_state_upserts_and_returns_listing(monkeypatch):
    from job_matching_team.models import ListingStateUpdate

    cur = FakeCursor(fetchone=[{"ok": 1}, _listing_row(status="archived")])
    _patch_conn(monkeypatch, cur)
    out = JobMatchingStore().update_listing_state("fp1", ListingStateUpdate(status="archived"))
    assert out is not None
    assert out.status == "archived"
    sqls = [s for s, _ in cur.executed]
    assert any("SELECT 1 FROM job_matching_ranked_jobs" in s for s in sqls)
    upsert = next(s for s in sqls if "INSERT INTO job_matching_listing_states" in s)
    # notes=None must preserve previously stored notes.
    assert "COALESCE(EXCLUDED.notes, job_matching_listing_states.notes)" in upsert


def test_update_listing_state_unknown_fingerprint_returns_none(monkeypatch):
    from job_matching_team.models import ListingStateUpdate

    cur = FakeCursor(fetchone=[None])
    _patch_conn(monkeypatch, cur)
    out = JobMatchingStore().update_listing_state("nope", ListingStateUpdate(status="favorite"))
    assert out is None
    # Nothing was written after the failed existence check.
    assert not any("INSERT INTO job_matching_listing_states" in s for s, _ in cur.executed)


def test_iso_helper_handles_non_datetime():
    assert store_mod._iso(None) is None
    assert store_mod._iso("2026-01-01") == "2026-01-01"


def test_get_store_is_singleton():
    assert get_store() is get_store()
