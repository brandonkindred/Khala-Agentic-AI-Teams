"""Tests for the shared Postgres access helper (``branding_team._db``).

These tests mock ``shared.postgres.get_conn`` with the same dict-backed
fake used by the store tests — see ``_fake_postgres.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from psycopg.errors import UniqueViolation
from psycopg.types.json import Json

from branding_team._db import PostgresHelperMixin
from branding_team.tests._fake_postgres import install_fake_postgres


class _Probe(PostgresHelperMixin):
    """Minimal concrete mixin subclass for exercising shared helper methods."""


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Install the branding fake Postgres harness and return its backing db."""
    return install_fake_postgres(monkeypatch)


def test_execute_then_fetch_one_round_trips(fake_pg: dict) -> None:
    """_execute inserts a row that _fetch_one can read back as a dict."""
    probe = _Probe()

    affected = probe._execute(
        "INSERT INTO branding_clients (id, data) VALUES (%s, %s)",
        ("client_1", Json({"id": "client_1", "name": "Acme"})),
    )
    assert affected == 1

    row = probe._fetch_one("SELECT data FROM branding_clients WHERE id = %s", ("client_1",))
    assert row == {"data": {"id": "client_1", "name": "Acme"}}


def test_fetch_one_returns_none_for_missing_row(fake_pg: dict) -> None:
    """_fetch_one returns None when no row matches."""
    probe = _Probe()
    row = probe._fetch_one("SELECT data FROM branding_clients WHERE id = %s", ("nope",))
    assert row is None


def test_fetch_all_returns_all_rows(fake_pg: dict) -> None:
    """_fetch_all returns every matching row as dicts."""
    probe = _Probe()
    probe._execute(
        "INSERT INTO branding_clients (id, data) VALUES (%s, %s)",
        ("client_1", Json({"id": "client_1", "name": "Acme"})),
    )
    probe._execute(
        "INSERT INTO branding_clients (id, data) VALUES (%s, %s)",
        ("client_2", Json({"id": "client_2", "name": "Globex"})),
    )

    rows = probe._fetch_all("SELECT data FROM branding_clients", ())
    assert sorted(r["data"]["id"] for r in rows) == ["client_1", "client_2"]


def test_transaction_shares_one_cursor_across_statements(fake_pg: dict) -> None:
    """_transaction yields one cursor shared across multiple statements."""
    probe = _Probe()

    with probe._transaction() as cur:
        cur.execute(
            "INSERT INTO branding_clients (id, data) VALUES (%s, %s)",
            ("client_1", Json({"id": "client_1", "name": "Acme"})),
        )
        cur.execute("SELECT data FROM branding_clients WHERE id = %s", ("client_1",))
        row = cur.fetchone()

    assert row == {"data": {"id": "client_1", "name": "Acme"}}


def test_execute_rowcount_reflects_matched_rows(fake_pg: dict) -> None:
    """_execute returns the number of rows matched by an UPDATE statement."""
    probe = _Probe()
    now = datetime.now(tz=timezone.utc)
    probe._execute(
        "INSERT INTO branding_conversations "
        "(conversation_id, brand_id, mission_json, latest_output_json, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        ("conv_1", None, Json({}), None, now, now),
    )

    # Use a live store UPDATE shape (set brand_id) — the bare ``SET updated_at``
    # form is no longer emitted by BrandingConversationStore and was dropped
    # from the fake dispatch table.
    affected = probe._execute(
        "UPDATE branding_conversations SET brand_id = %s, updated_at = %s "
        "WHERE conversation_id = %s",
        ("brand_1", now, "conv_1"),
    )
    assert affected == 1

    affected = probe._execute(
        "UPDATE branding_conversations SET brand_id = %s, updated_at = %s "
        "WHERE conversation_id = %s",
        ("brand_1", now, "does_not_exist"),
    )
    assert affected == 0


def test_duplicate_client_insert_raises_unique_violation(fake_pg: dict) -> None:
    """Duplicate branding_clients insert raises UniqueViolation and keeps the row."""
    probe = _Probe()
    original = {"id": "client_1", "name": "Acme"}
    probe._execute(
        "INSERT INTO branding_clients (id, data) VALUES (%s, %s)",
        ("client_1", Json(original)),
    )

    with pytest.raises(UniqueViolation, match="branding_clients_pkey"):
        probe._execute(
            "INSERT INTO branding_clients (id, data) VALUES (%s, %s)",
            ("client_1", Json({"id": "client_1", "name": "Overwrite"})),
        )

    assert fake_pg["clients"]["client_1"]["data"] == original


def test_duplicate_session_insert_raises_unique_violation(fake_pg: dict) -> None:
    """Duplicate branding_sessions insert raises UniqueViolation and keeps the row."""
    probe = _Probe()
    now = datetime.now(tz=timezone.utc)
    original = {"mission": {"company_name": "Acme"}, "questions": []}
    probe._execute(
        "INSERT INTO branding_sessions (session_id, session_json, updated_at) "
        "VALUES (%s, %s, %s)",
        ("sess_1", Json(original), now),
    )

    with pytest.raises(UniqueViolation, match="branding_sessions_pkey"):
        probe._execute(
            "INSERT INTO branding_sessions (session_id, session_json, updated_at) "
            "VALUES (%s, %s, %s)",
            (
                "sess_1",
                Json({"mission": {"company_name": "Overwrite"}, "questions": []}),
                now,
            ),
        )

    assert fake_pg["sessions"]["sess_1"]["session_json"] == original
    assert fake_pg["sessions"]["sess_1"]["updated_at"] is now


def test_duplicate_conversation_insert_raises_unique_violation(fake_pg: dict) -> None:
    """Duplicate branding_conversations insert raises UniqueViolation and keeps the row."""
    probe = _Probe()
    now = datetime.now(tz=timezone.utc)
    original_mission = {"company_name": "Acme"}
    probe._execute(
        "INSERT INTO branding_conversations "
        "(conversation_id, brand_id, mission_json, latest_output_json, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        ("conv_1", None, Json(original_mission), None, now, now),
    )

    with pytest.raises(UniqueViolation, match="branding_conversations_pkey"):
        probe._execute(
            "INSERT INTO branding_conversations "
            "(conversation_id, brand_id, mission_json, latest_output_json, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                "conv_1",
                "brand_overwrite",
                Json({"company_name": "Overwrite"}),
                Json({"status": "done"}),
                now,
                now,
            ),
        )

    row = fake_pg["conversations"]["conv_1"]
    assert row["mission_json"] == original_mission
    assert row["brand_id"] is None
    assert row["latest_output_json"] is None
    assert row["created_at"] is now
    assert row["updated_at"] is now
