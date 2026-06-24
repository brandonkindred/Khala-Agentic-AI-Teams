"""Unit tests for ``user_profile.store`` against the dict-backed fake Postgres."""

from __future__ import annotations

import pytest

from user_profile import store as up_store
from user_profile.models import UserProfileUpdate
from user_profile.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def db(monkeypatch):
    return install_fake_postgres(monkeypatch)


def test_now_iso_returns_tz_aware_iso_string():
    """_now_iso must always yield a timezone-aware ISO-8601 string."""
    result = up_store._now_iso()
    assert "T" in result  # date/time separator
    assert result.endswith("+00:00") or result.endswith("Z")  # explicit UTC offset
    # Round-trips back to an aware datetime.
    from datetime import datetime

    parsed = datetime.fromisoformat(result)
    assert parsed.tzinfo is not None


def test_get_profile_autocreates_default(db):
    profile = up_store.get_profile()
    assert profile.user_id == "default"
    assert profile.display_name == ""
    assert profile.preferences == {}
    # Row now exists; a second read returns the same identity without a new row.
    again = up_store.get_profile()
    assert again.user_id == "default"
    assert list(db["profiles"].keys()) == ["default"]


def test_get_profile_rejects_empty_user_id(db):
    with pytest.raises(AssertionError):
        up_store.get_profile("")


def test_upsert_profile_rejects_empty_user_id(db):
    with pytest.raises(AssertionError):
        up_store.upsert_profile(UserProfileUpdate(display_name="x"), user_id="")


def test_list_associations_rejects_empty_user_id(db):
    with pytest.raises(AssertionError):
        up_store.list_associations(user_id="")


def test_get_profile_lost_insert_race_rereads_winner(monkeypatch):
    """When a concurrent writer wins the insert, get_profile re-reads its row.

    Models the race the synthesized-row fast path guards against: our SELECT
    misses, but by the time our ``INSERT ... ON CONFLICT DO NOTHING`` runs the
    row already exists (rowcount 0), so we must re-SELECT the winner's row rather
    than synthesize our own.
    """
    from contextlib import contextmanager

    winner_row = {
        "user_id": "default",
        "display_name": "Winner",
        "email": "",
        "bio": "",
        "profile_json": {},
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }

    class _RaceCursor:
        def __init__(self):
            self.rowcount = 0
            self._one = None
            self._selects = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=()):
            norm = " ".join(sql.split()).lower()
            if norm.startswith("select") and "from user_profiles" in norm:
                self._selects += 1
                # First SELECT misses; the post-insert SELECT finds the winner.
                self._one = None if self._selects == 1 else dict(winner_row)
            elif norm.startswith("insert into user_profiles"):
                self.rowcount = 0  # a concurrent writer already inserted the row
            else:  # pragma: no cover - no other SQL on this path
                raise AssertionError(sql)

        def fetchone(self):
            return self._one

    class _RaceConn:
        def cursor(self, row_factory=None):
            return _RaceCursor()

    @contextmanager
    def _fake_get_conn(database=None):
        yield _RaceConn()

    monkeypatch.setattr(up_store, "get_conn", _fake_get_conn)

    profile = up_store.get_profile()
    assert profile.display_name == "Winner"


def test_upsert_profile_partial_update(db):
    up_store.get_profile()
    updated = up_store.upsert_profile(UserProfileUpdate(display_name="Brandon", bio="hi"))
    assert updated.display_name == "Brandon"
    assert updated.bio == "hi"
    assert updated.email == ""  # untouched

    # A second update leaves unspecified fields intact.
    updated2 = up_store.upsert_profile(UserProfileUpdate(email="b@example.com"))
    assert updated2.email == "b@example.com"
    assert updated2.display_name == "Brandon"


def test_upsert_profile_preferences_roundtrip(db):
    updated = up_store.upsert_profile(UserProfileUpdate(preferences={"theme": "dark"}))
    assert updated.preferences == {"theme": "dark"}


def test_record_association_is_idempotent(db):
    a = up_store.record_association("brand", "branding", "brand_1", label="Acme")
    assert a is not None
    b = up_store.record_association("brand", "branding", "brand_1", label="Acme v2")
    # Same logical link — no duplicate row, label refreshed.
    items = up_store.list_associations()
    assert len(items) == 1
    assert items[0].label == "Acme v2"
    assert a.artifact_id == b.artifact_id == "brand_1"


def test_record_association_validates_inputs(db):
    # Each of artifact_type, team, artifact_id is a non-empty precondition.
    with pytest.raises(AssertionError):
        up_store.record_association("", "branding", "x")
    with pytest.raises(AssertionError):
        up_store.record_association("brand", "", "x")
    with pytest.raises(AssertionError):
        up_store.record_association("brand", "branding", "")


def test_record_association_requires_user_id(db):
    with pytest.raises(AssertionError):
        up_store.record_association("brand", "branding", "x", user_id="")


def test_remove_association_requires_user_id(db):
    with pytest.raises(AssertionError):
        up_store.remove_association("assoc_1", user_id="")


def test_record_association_safe_skips_empty_user_id(monkeypatch, db):
    called = False

    def _spy(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(up_store, "record_association", _spy)
    up_store.record_association_safe("brand", "branding", "brand_1", user_id="")
    assert called is False


def test_list_associations_filters_by_type(db):
    up_store.record_association("brand", "branding", "brand_1", label="Acme")
    up_store.record_association("blog_post", "blogging", "job_1", label="Post")
    up_store.record_association("project", "coding_team", "job_2", label="Repo")

    assert len(up_store.list_associations()) == 3
    brands = up_store.list_associations(artifact_type="brand")
    assert len(brands) == 1
    assert brands[0].team == "branding"


def test_remove_association(db):
    a = up_store.record_association("brand", "branding", "brand_1")
    assert up_store.remove_association(a.id) is True
    assert up_store.list_associations() == []
    # Removing again is a no-op.
    assert up_store.remove_association(a.id) is False


def test_ts_helper_renders_values():
    from datetime import datetime, timezone

    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert up_store._ts(dt) == dt.isoformat()
    assert up_store._ts("2026-01-02T00:00:00+00:00") == "2026-01-02T00:00:00+00:00"


def test_record_association_safe_swallows_errors(monkeypatch):
    """The best-effort wrapper never raises even when the store blows up."""

    def _boom(*args, **kwargs):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(up_store, "record_association", _boom)
    # Must not raise.
    up_store.record_association_safe("brand", "branding", "brand_1")


def test_record_association_safe_skips_empty_inputs(monkeypatch, db):
    """Empty fields are a caller bug: skipped+logged, never passed to the strict
    record_association (whose precondition would assert)."""
    called = False

    def _spy(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(up_store, "record_association", _spy)
    up_store.record_association_safe("", "branding", "brand_1")
    up_store.record_association_safe("brand", "", "brand_1")
    up_store.record_association_safe("brand", "branding", "")
    assert called is False
