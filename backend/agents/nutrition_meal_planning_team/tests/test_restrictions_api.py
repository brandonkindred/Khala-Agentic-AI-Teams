"""SPEC-006 integration tests: profile restriction endpoints.

Uses Postgres via the same fixture pattern as ``test_api.py`` and
``test_profile_biometrics.py``. The intake agent's LLM call falls
through to the structural merge when no LLM is reachable (per
``conftest.py``'s ``LLM_MAX_RETRIES=0``), so these tests do not need
to mock strands — the resolver hook fires on the structural-fallback
path just as it does on the LLM path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shared_postgres import is_postgres_enabled

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_agents_dir))

from nutrition_meal_planning_team.api.main import app  # noqa: E402

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="SPEC-006 restriction endpoints require Postgres.",
)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("NUTRITION_RESTRICTION_RESOLVER", "1")
    yield


# --- GET /restrictions ---------------------------------------------------


def test_intake_resolves_on_write(client, flag_on):
    client.put(
        "/profile/sp1",
        json={
            "allergies_and_intolerances": ["cashew", "gluten-free"],
            "dietary_needs": ["vegan"],
        },
    )
    r = client.get("/profile/sp1/restrictions")
    assert r.status_code == 200
    data = r.json()
    resolved_raws = {e["raw"] for e in data["resolved"]}
    assert resolved_raws == {"cashew", "gluten-free", "vegan"}
    assert data["ambiguous"] == []
    assert data["unresolved"] == []
    # KB version is stamped.
    assert data["kb_version"]


def test_ambiguous_nuts_surfaces_and_resolves(client, flag_on):
    client.put(
        "/profile/sp2",
        json={"allergies_and_intolerances": ["nuts"], "dietary_needs": []},
    )
    r = client.get("/profile/sp2/restrictions")
    assert r.status_code == 200
    data = r.json()
    assert len(data["ambiguous"]) == 1
    assert data["ambiguous"][0]["raw"] == "nuts"

    # User picks the "both" candidate.
    chosen = data["ambiguous"][0]["candidates"][-1]
    r2 = client.post(
        "/profile/sp2/restrictions/resolve-ambiguous",
        json={"raw": "nuts", "chosen_candidate": chosen},
    )
    assert r2.status_code == 200
    after = r2.json()
    assert after["ambiguous"] == []
    resolved_raws = [e["raw"] for e in after["resolved"]]
    assert "nuts" in resolved_raws

    # Subsequent GET does not re-ask.
    r3 = client.get("/profile/sp2/restrictions")
    assert r3.json()["ambiguous"] == []


def test_resolve_ambiguous_returns_400_for_unknown_raw(client, flag_on):
    client.put(
        "/profile/sp3",
        json={"allergies_and_intolerances": ["nuts"], "dietary_needs": []},
    )
    r = client.post(
        "/profile/sp3/restrictions/resolve-ambiguous",
        json={
            "raw": "not-a-real-raw",
            "chosen_candidate": {
                "raw": "not-a-real-raw",
                "allergen_tags": ["peanut"],
                "confidence": 1.0,
                "source": "user",
                "rule": "category",
            },
        },
    )
    assert r.status_code == 400


def test_get_restrictions_404_when_profile_missing(client, flag_on):
    r = client.get("/profile/never-created/restrictions")
    assert r.status_code == 404


def test_reresolve_preserves_confirmed_choices(client, flag_on):
    client.put(
        "/profile/sp4",
        json={"allergies_and_intolerances": ["nuts"], "dietary_needs": []},
    )
    data = client.get("/profile/sp4/restrictions").json()
    chosen = data["ambiguous"][0]["candidates"][-1]
    client.post(
        "/profile/sp4/restrictions/resolve-ambiguous",
        json={"raw": "nuts", "chosen_candidate": chosen},
    )
    # Confirmed. Re-resolve should keep the confirmation.
    r = client.post("/profile/sp4/restrictions/reresolve")
    assert r.status_code == 200
    after = r.json()
    # The user previously confirmed "nuts" so it should NOT reappear as
    # ambiguous after re-resolve.
    assert all(a["raw"] != "nuts" for a in after["ambiguous"])
    assert any(e["raw"] == "nuts" for e in after["resolved"])


def test_flag_off_leaves_resolution_empty(client, monkeypatch):
    # Explicitly ensure the flag is off.
    monkeypatch.delenv("NUTRITION_RESTRICTION_RESOLVER", raising=False)
    client.put(
        "/profile/sp5",
        json={
            "allergies_and_intolerances": ["vegan", "cashew"],
            "dietary_needs": [],
        },
    )
    r = client.get("/profile/sp5/restrictions")
    assert r.status_code == 200
    data = r.json()
    assert data["resolved"] == []
    assert data["ambiguous"] == []
    assert data["unresolved"] == []
    assert data["kb_version"] == ""
