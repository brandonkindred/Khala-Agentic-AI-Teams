"""Unit tests for the SPEC-015 pantry store.

Verifies CRUD round-trip, increment-on-conflict semantics, sort modes,
and near-expiry windowing. Gated on Postgres like the other nutrition
store tests; conftest.py (one level up) handles schema registration and
per-test truncation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from nutrition_meal_planning_team.models import ClientProfile
from nutrition_meal_planning_team.pantry import (
    InvalidQuantity,
    PantryItemNotFound,
    PantryStore,
    get_pantry_store,
)
from nutrition_meal_planning_team.pantry.version import PANTRY_VERSION
from nutrition_meal_planning_team.shared.client_profile_store import save_profile
from shared_postgres import is_postgres_enabled

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="Pantry store requires Postgres (set POSTGRES_HOST).",
)


def _today() -> date:
    return datetime.now(tz=timezone.utc).date()


@pytest.fixture
def client_id() -> str:
    """Insert a profile row so the pantry FK constraint is satisfied."""
    cid = "pantry-test-client"
    save_profile(cid, ClientProfile(client_id=cid))
    return cid


@pytest.fixture
def store() -> PantryStore:
    return PantryStore()


class TestVersion:
    def test_pantry_version_exposed(self):
        assert PANTRY_VERSION == "1.0.0"


class TestAddAndList:
    def test_add_returns_item(self, store, client_id):
        item = store.add_or_increment_item(
            client_id,
            "olive_oil",
            quantity_grams=500.0,
            display_qty=0.5,
            display_unit="L",
        )
        assert item.client_id == client_id
        assert item.canonical_id == "olive_oil"
        assert item.quantity_grams == 500.0
        assert item.display_qty == 0.5
        assert item.display_unit == "L"
        assert item.added_at
        assert item.updated_at

    def test_re_add_increments_quantity(self, store, client_id):
        store.add_or_increment_item(client_id, "tomato", 100.0)
        again = store.add_or_increment_item(client_id, "tomato", 250.0)
        assert again.quantity_grams == 350.0
        # Only one row should exist for the canonical id.
        assert len(store.list_items(client_id)) == 1

    def test_re_add_preserves_existing_display_when_not_provided(self, store, client_id):
        store.add_or_increment_item(client_id, "milk", 1000.0, display_qty=1.0, display_unit="L")
        # Second add with no display fields — existing values should stick.
        again = store.add_or_increment_item(client_id, "milk", 500.0)
        assert again.quantity_grams == 1500.0
        assert again.display_qty == 1.0
        assert again.display_unit == "L"

    def test_rejects_non_positive_quantity(self, store, client_id):
        with pytest.raises(InvalidQuantity):
            store.add_or_increment_item(client_id, "x", 0.0)
        with pytest.raises(InvalidQuantity):
            store.add_or_increment_item(client_id, "x", -5.0)


class TestGetAndUpdate:
    def test_get_missing_returns_none(self, store, client_id):
        assert store.get_item(client_id, "does-not-exist") is None

    def test_update_replaces_quantity(self, store, client_id):
        store.add_or_increment_item(client_id, "rice", 500.0)
        updated = store.update_item(client_id, "rice", quantity_grams=200.0)
        assert updated.quantity_grams == 200.0  # replaced, not summed

    def test_update_missing_raises(self, store, client_id):
        with pytest.raises(PantryItemNotFound):
            store.update_item(client_id, "ghost", quantity_grams=100.0)

    def test_update_partial_fields(self, store, client_id):
        store.add_or_increment_item(client_id, "yogurt", 200.0, display_unit="g")
        new_expiry = _today() + timedelta(days=5)
        updated = store.update_item(
            client_id, "yogurt", expires_on=new_expiry, notes="back of fridge"
        )
        assert updated.quantity_grams == 200.0  # untouched
        assert updated.display_unit == "g"  # untouched
        assert updated.expires_on == new_expiry
        assert updated.notes == "back of fridge"

    def test_update_rejects_non_positive_quantity(self, store, client_id):
        store.add_or_increment_item(client_id, "flour", 100.0)
        with pytest.raises(InvalidQuantity):
            store.update_item(client_id, "flour", quantity_grams=0)

    def test_update_clears_nullable_fields_on_explicit_none(self, store, client_id):
        store.add_or_increment_item(
            client_id,
            "meat",
            100.0,
            display_qty=0.5,
            display_unit="kg",
            expires_on=_today() + timedelta(days=2),
            notes="thawing",
        )
        updated = store.update_item(
            client_id,
            "meat",
            display_qty=None,
            display_unit=None,
            expires_on=None,
            notes=None,
        )
        assert updated.display_qty is None
        assert updated.display_unit is None
        assert updated.expires_on is None
        assert updated.notes == ""  # _row_to_item coerces NULL notes to ""

    def test_update_without_passing_nullable_fields_keeps_them(self, store, client_id):
        expiry = _today() + timedelta(days=5)
        store.add_or_increment_item(
            client_id, "yogurt", 200.0, display_unit="g", expires_on=expiry, notes="back of fridge"
        )
        # No nullable kwargs passed — all four should stay intact.
        updated = store.update_item(client_id, "yogurt", quantity_grams=250.0)
        assert updated.quantity_grams == 250.0
        assert updated.display_unit == "g"
        assert updated.expires_on == expiry
        assert updated.notes == "back of fridge"


class TestDelete:
    def test_delete_returns_true_when_present(self, store, client_id):
        store.add_or_increment_item(client_id, "onion", 200.0)
        assert store.delete_item(client_id, "onion") is True
        assert store.get_item(client_id, "onion") is None

    def test_delete_returns_false_when_absent(self, store, client_id):
        assert store.delete_item(client_id, "ghost") is False


class TestListSortModes:
    def test_sort_expiring_puts_soonest_first_and_null_last(self, store, client_id):
        today = _today()
        store.add_or_increment_item(client_id, "a_no_expiry", 100.0)
        store.add_or_increment_item(
            client_id, "b_far", 100.0, expires_on=today + timedelta(days=30)
        )
        store.add_or_increment_item(
            client_id, "c_soon", 100.0, expires_on=today + timedelta(days=2)
        )
        items = store.list_items(client_id, sort="expiring")
        assert [i.canonical_id for i in items] == ["c_soon", "b_far", "a_no_expiry"]

    def test_sort_name(self, store, client_id):
        for cid in ("zucchini", "apple", "mango"):
            store.add_or_increment_item(client_id, cid, 100.0)
        items = store.list_items(client_id, sort="name")
        assert [i.canonical_id for i in items] == ["apple", "mango", "zucchini"]

    def test_sort_added_desc(self, store, client_id):
        store.add_or_increment_item(client_id, "first", 100.0)
        store.add_or_increment_item(client_id, "second", 100.0)
        store.add_or_increment_item(client_id, "third", 100.0)
        items = store.list_items(client_id, sort="added_desc")
        # Most recently added comes first. Insertion order = added_at order.
        assert [i.canonical_id for i in items][0] == "third"


class TestListExpiring:
    def test_returns_only_items_within_window(self, store, client_id):
        today = _today()
        store.add_or_increment_item(client_id, "no_expiry", 100.0)
        store.add_or_increment_item(client_id, "far", 100.0, expires_on=today + timedelta(days=30))
        store.add_or_increment_item(
            client_id, "inside_window", 100.0, expires_on=today + timedelta(days=2)
        )
        store.add_or_increment_item(
            client_id, "already_expired", 100.0, expires_on=today - timedelta(days=1)
        )
        expiring = store.list_expiring(client_id, days=3)
        names = {i.canonical_id for i in expiring}
        assert names == {"inside_window", "already_expired"}

    def test_boundary_day_is_included(self, store, client_id):
        today = _today()
        store.add_or_increment_item(
            client_id, "exactly_on_boundary", 100.0, expires_on=today + timedelta(days=3)
        )
        expiring = store.list_expiring(client_id, days=3)
        assert [i.canonical_id for i in expiring] == ["exactly_on_boundary"]

    def test_day_after_boundary_excluded(self, store, client_id):
        today = _today()
        store.add_or_increment_item(
            client_id, "just_past", 100.0, expires_on=today + timedelta(days=4)
        )
        assert store.list_expiring(client_id, days=3) == []

    def test_rejects_negative_days(self, store, client_id):
        with pytest.raises(ValueError):
            store.list_expiring(client_id, days=-1)


class TestProfileCascade:
    def test_pantry_rows_cascade_when_profile_deleted(self, store, client_id):
        from shared_postgres import get_conn

        store.add_or_increment_item(client_id, "apple", 200.0)
        assert len(store.list_items(client_id)) == 1
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM nutrition_profiles WHERE client_id = %s", (client_id,))
        assert store.list_items(client_id) == []


class TestSingleton:
    def test_get_pantry_store_is_idempotent(self):
        s1 = get_pantry_store()
        s2 = get_pantry_store()
        assert s1 is s2


class TestProfileAutoDebitFlag:
    def test_pantry_auto_debit_defaults_off(self):
        profile = ClientProfile(client_id="defaults-test")
        assert profile.pantry_auto_debit is False

    def test_pantry_auto_debit_round_trips(self, client_id):
        from nutrition_meal_planning_team.shared.client_profile_store import (
            get_profile,
        )

        save_profile(client_id, ClientProfile(client_id=client_id, pantry_auto_debit=True))
        loaded = get_profile(client_id)
        assert loaded is not None
        assert loaded.pantry_auto_debit is True
