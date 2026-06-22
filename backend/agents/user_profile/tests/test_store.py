"""Unit tests for ``user_profile.store`` against the dict-backed fake Postgres."""

from __future__ import annotations

import pytest

from user_profile import store as up_store
from user_profile.models import UserProfileUpdate
from user_profile.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def db(monkeypatch):
    return install_fake_postgres(monkeypatch)


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
    with pytest.raises(AssertionError):
        up_store.record_association("", "branding", "x")


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


def test_record_association_async_dispatches(monkeypatch, db):
    """The async wrapper runs record_association_safe on a background worker."""
    captured: list = []
    monkeypatch.setattr(up_store, "record_association_safe", lambda *a, **k: captured.append((a, k)))

    f1 = up_store.record_association_async("brand", "branding", "b1", label="x")
    f2 = up_store.record_association_async("project", "coding_team", "j2")  # reuses the executor
    assert f1 is not None and f2 is not None
    f1.result(timeout=5)
    f2.result(timeout=5)

    assert len(captured) == 2
    assert (("brand", "branding", "b1"), {"user_id": "default", "label": "x", "role": "owner"}) in captured


def test_record_association_async_dispatch_failure_returns_none(monkeypatch):
    """If dispatch itself fails, the wrapper logs and returns None (never raises)."""

    def _boom():
        raise RuntimeError("no executor")

    monkeypatch.setattr(up_store, "_get_assoc_executor", _boom)
    assert up_store.record_association_async("brand", "branding", "b1") is None


def test_ts_helper_renders_values():
    from datetime import datetime, timezone

    assert up_store._ts(None) == ""
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
