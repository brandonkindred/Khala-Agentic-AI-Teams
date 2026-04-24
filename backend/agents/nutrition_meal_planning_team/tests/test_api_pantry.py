"""API tests for SPEC-015 W5 pantry endpoints.

Covers the feature-flag gate, wire-contract semantics
(XOR identity, server-side gram derivation, increment-on-duplicate,
sentinel-based PUT), and per-route error mapping.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from shared_postgres import is_postgres_enabled

# Ensure agents dir is on path for direct invocation.
_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_agents_dir))

from fastapi.testclient import TestClient  # noqa: E402

from nutrition_meal_planning_team.api.main import app  # noqa: E402
from nutrition_meal_planning_team.models import ClientProfile  # noqa: E402
from nutrition_meal_planning_team.shared.client_profile_store import save_profile  # noqa: E402

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="Pantry API requires Postgres (set POSTGRES_HOST).",
)


@pytest.fixture
def client():
    # Context-manage to run Starlette lifespan (schema registration).
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_id() -> str:
    """Insert a profile so the pantry FK constraint is satisfied."""
    cid = "pantry-api-client"
    save_profile(cid, ClientProfile(client_id=cid))
    return cid


@pytest.fixture
def pantry_on(monkeypatch):
    """Enable the NUTRITION_PANTRY flag for happy-path tests."""
    monkeypatch.setenv("NUTRITION_PANTRY", "1")
    yield


# --- Flag gating (one per route) ----------------------------------------


def _flag_off(monkeypatch):
    monkeypatch.delenv("NUTRITION_PANTRY", raising=False)


def test_flag_off_get_list_returns_404(client, client_id, monkeypatch):
    _flag_off(monkeypatch)
    r = client.get(f"/pantry/{client_id}")
    assert r.status_code == 404


def test_flag_off_post_returns_404(client, client_id, monkeypatch):
    _flag_off(monkeypatch)
    r = client.post(
        f"/pantry/{client_id}/items",
        json={"canonical_id": "olive_oil", "display_qty": 1, "display_unit": "g"},
    )
    assert r.status_code == 404


def test_flag_off_put_returns_404(client, client_id, monkeypatch):
    _flag_off(monkeypatch)
    r = client.put(
        f"/pantry/{client_id}/items/olive_oil",
        json={"quantity_grams": 10},
    )
    assert r.status_code == 404


def test_flag_off_delete_returns_404(client, client_id, monkeypatch):
    _flag_off(monkeypatch)
    r = client.delete(f"/pantry/{client_id}/items/olive_oil")
    assert r.status_code == 404


def test_flag_off_expiring_returns_404(client, client_id, monkeypatch):
    _flag_off(monkeypatch)
    r = client.get(f"/pantry/{client_id}/expiring")
    assert r.status_code == 404


# --- POST happy paths --------------------------------------------------


def test_post_with_canonical_id_creates_item(client, client_id, pantry_on):
    r = client.post(
        f"/pantry/{client_id}/items",
        json={
            "canonical_id": "olive_oil",
            "display_qty": 500,
            "display_unit": "g",
            "notes": "kitchen cabinet",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["canonical_id"] == "olive_oil"
    assert body["quantity_grams"] == 500.0
    assert body["display_qty"] == 500.0
    assert body["display_unit"] == "g"
    assert body["notes"] == "kitchen cabinet"


def test_post_with_raw_name_resolves_and_creates(client, client_id, pantry_on):
    r = client.post(
        f"/pantry/{client_id}/items",
        json={"raw_name": "olive oil", "display_qty": 100, "display_unit": "g"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["canonical_id"] == "olive_oil"
    assert body["quantity_grams"] == 100.0


def test_post_existing_canonical_id_increments(client, client_id, pantry_on):
    first = client.post(
        f"/pantry/{client_id}/items",
        json={"canonical_id": "olive_oil", "display_qty": 100, "display_unit": "g"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["quantity_grams"] == 100.0

    second = client.post(
        f"/pantry/{client_id}/items",
        json={"canonical_id": "olive_oil", "display_qty": 250, "display_unit": "g"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["quantity_grams"] == 350.0

    listed = client.get(f"/pantry/{client_id}")
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    assert items[0]["canonical_id"] == "olive_oil"
    assert items[0]["quantity_grams"] == 350.0


# --- POST error paths --------------------------------------------------


def test_post_raw_name_unresolved_returns_422(client, client_id, pantry_on):
    r = client.post(
        f"/pantry/{client_id}/items",
        json={"raw_name": "xyzzynotafood", "display_qty": 1, "display_unit": "g"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "unresolved_ingredient"


def test_post_unknown_unit_returns_422(client, client_id, pantry_on):
    r = client.post(
        f"/pantry/{client_id}/items",
        json={"canonical_id": "olive_oil", "display_qty": 1, "display_unit": "bogusunit"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "unknown_unit"


def test_post_unconvertible_quantity_returns_422(client, client_id, pantry_on):
    # ``butter`` has ``count_to_mass`` but no ``volume_to_mass`` density,
    # so a volume unit like ``cup`` cannot be converted to grams.
    r = client.post(
        f"/pantry/{client_id}/items",
        json={"canonical_id": "butter", "display_qty": 1, "display_unit": "cup"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "unconvertible_quantity"


def test_post_xor_both_set_returns_422(client, client_id, pantry_on):
    r = client.post(
        f"/pantry/{client_id}/items",
        json={
            "canonical_id": "olive_oil",
            "raw_name": "olive oil",
            "display_qty": 1,
            "display_unit": "g",
        },
    )
    assert r.status_code == 422


def test_post_xor_neither_set_returns_422(client, client_id, pantry_on):
    r = client.post(
        f"/pantry/{client_id}/items",
        json={"display_qty": 1, "display_unit": "g"},
    )
    assert r.status_code == 422


def test_post_invalid_quantity_returns_400(client, client_id, pantry_on):
    # display_qty=0 → grams=0 → store rejects with InvalidQuantity → 400.
    r = client.post(
        f"/pantry/{client_id}/items",
        json={"canonical_id": "olive_oil", "display_qty": 0, "display_unit": "g"},
    )
    assert r.status_code == 400


def test_post_whitespace_canonical_id_falls_through_to_raw_name(client, client_id, pantry_on):
    # Whitespace-only ``canonical_id`` must not bypass ``raw_name`` resolution;
    # the validator normalizes blank strings to ``None``.
    r = client.post(
        f"/pantry/{client_id}/items",
        json={
            "canonical_id": "   ",
            "raw_name": "olive oil",
            "display_qty": 100,
            "display_unit": "g",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["canonical_id"] == "olive_oil"


def test_post_unknown_client_returns_404(client, pantry_on):
    # No profile seeded ⇒ FK would fail. Route pre-checks and 404s cleanly.
    r = client.post(
        "/pantry/ghost-client/items",
        json={"canonical_id": "olive_oil", "display_qty": 100, "display_unit": "g"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["reason"] == "client_profile_not_found"


# --- PUT ----------------------------------------------------------------


def test_put_partial_update_preserves_other_fields(client, client_id, pantry_on):
    client.post(
        f"/pantry/{client_id}/items",
        json={
            "canonical_id": "olive_oil",
            "display_qty": 500,
            "display_unit": "g",
            "notes": "original notes",
        },
    ).raise_for_status()

    r = client.put(
        f"/pantry/{client_id}/items/olive_oil",
        json={"quantity_grams": 999.0},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["quantity_grams"] == 999.0
    # Untouched fields preserved (omitted from body ⇒ store's _UNSET sentinel).
    assert body["display_qty"] == 500.0
    assert body["display_unit"] == "g"
    assert body["notes"] == "original notes"


def test_put_explicit_null_clears_nullable_field(client, client_id, pantry_on):
    today = date.today()
    client.post(
        f"/pantry/{client_id}/items",
        json={
            "canonical_id": "olive_oil",
            "display_qty": 500,
            "display_unit": "g",
            "expires_on": today.isoformat(),
        },
    ).raise_for_status()

    r = client.put(
        f"/pantry/{client_id}/items/olive_oil",
        json={"expires_on": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["expires_on"] is None


def test_put_missing_item_returns_404(client, client_id, pantry_on):
    r = client.put(
        f"/pantry/{client_id}/items/does_not_exist",
        json={"quantity_grams": 10},
    )
    assert r.status_code == 404


def test_put_invalid_quantity_returns_400(client, client_id, pantry_on):
    client.post(
        f"/pantry/{client_id}/items",
        json={"canonical_id": "olive_oil", "display_qty": 500, "display_unit": "g"},
    ).raise_for_status()

    r = client.put(
        f"/pantry/{client_id}/items/olive_oil",
        json={"quantity_grams": -1.0},
    )
    assert r.status_code == 400


# --- DELETE -------------------------------------------------------------


def test_delete_removes_item(client, client_id, pantry_on):
    client.post(
        f"/pantry/{client_id}/items",
        json={"canonical_id": "olive_oil", "display_qty": 500, "display_unit": "g"},
    ).raise_for_status()

    r = client.delete(f"/pantry/{client_id}/items/olive_oil")
    assert r.status_code == 200
    assert r.json() == {"deleted": True, "client_id": client_id, "canonical_id": "olive_oil"}

    listed = client.get(f"/pantry/{client_id}")
    assert listed.status_code == 200
    assert listed.json() == []


def test_delete_absent_returns_404(client, client_id, pantry_on):
    r = client.delete(f"/pantry/{client_id}/items/olive_oil")
    assert r.status_code == 404


# --- GET list + sort ---------------------------------------------------


def _post_item(client, client_id, canonical_id: str, expires_on=None) -> None:
    body = {"canonical_id": canonical_id, "display_qty": 100, "display_unit": "g"}
    if expires_on is not None:
        body["expires_on"] = expires_on.isoformat()
    client.post(f"/pantry/{client_id}/items", json=body).raise_for_status()


def test_get_list_default_sort_expiring_nulls_last(client, client_id, pantry_on):
    today = date.today()
    _post_item(client, client_id, "olive_oil", expires_on=today + timedelta(days=7))
    _post_item(client, client_id, "butter", expires_on=today + timedelta(days=2))
    _post_item(client, client_id, "rice_white_raw", expires_on=None)

    r = client.get(f"/pantry/{client_id}")
    assert r.status_code == 200
    ids = [row["canonical_id"] for row in r.json()]
    assert ids == ["butter", "olive_oil", "rice_white_raw"]


def test_get_list_sort_name(client, client_id, pantry_on):
    for cid in ("olive_oil", "butter", "rice_white_raw"):
        _post_item(client, client_id, cid)
    r = client.get(f"/pantry/{client_id}", params={"sort": "name"})
    assert r.status_code == 200
    ids = [row["canonical_id"] for row in r.json()]
    assert ids == sorted(ids)


def test_get_list_sort_added_desc(client, client_id, pantry_on):
    import time

    _post_item(client, client_id, "olive_oil")
    time.sleep(0.01)
    _post_item(client, client_id, "butter")
    time.sleep(0.01)
    _post_item(client, client_id, "rice_white_raw")

    r = client.get(f"/pantry/{client_id}", params={"sort": "added_desc"})
    assert r.status_code == 200
    ids = [row["canonical_id"] for row in r.json()]
    assert ids[0] == "rice_white_raw"
    assert ids[-1] == "olive_oil"


def test_get_list_invalid_sort_returns_422(client, client_id, pantry_on):
    r = client.get(f"/pantry/{client_id}", params={"sort": "bogus"})
    assert r.status_code == 422


# --- GET expiring ------------------------------------------------------


def test_expiring_default_days_filters_to_near_term(client, client_id, pantry_on):
    today = date.today()
    _post_item(client, client_id, "olive_oil", expires_on=today + timedelta(days=2))
    _post_item(client, client_id, "butter", expires_on=today + timedelta(days=10))

    r = client.get(f"/pantry/{client_id}/expiring")
    assert r.status_code == 200
    ids = [row["canonical_id"] for row in r.json()]
    assert ids == ["olive_oil"]


def test_expiring_custom_days_broadens_window(client, client_id, pantry_on):
    today = date.today()
    _post_item(client, client_id, "olive_oil", expires_on=today + timedelta(days=2))
    _post_item(client, client_id, "butter", expires_on=today + timedelta(days=10))

    r = client.get(f"/pantry/{client_id}/expiring", params={"days": 14})
    assert r.status_code == 200
    ids = sorted(row["canonical_id"] for row in r.json())
    assert ids == ["butter", "olive_oil"]


def test_expiring_negative_days_returns_400(client, client_id, pantry_on):
    r = client.get(f"/pantry/{client_id}/expiring", params={"days": -1})
    assert r.status_code == 400


def test_expiring_empty_pantry_returns_empty_list(client, client_id, pantry_on):
    r = client.get(f"/pantry/{client_id}/expiring")
    assert r.status_code == 200
    assert r.json() == []
