"""Tests for the shared Postgres access helper (``branding_team._db``).

These tests mock ``shared.postgres.get_conn`` with the same dict-backed
fake used by the store tests — see ``_fake_postgres.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from psycopg.types.json import Json

from branding_team._db import PostgresHelperMixin
from branding_team.tests._fake_postgres import install_fake_postgres


class _Probe(PostgresHelperMixin):
    pass


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def test_execute_then_fetch_one_round_trips(fake_pg: dict) -> None:
    probe = _Probe()

    affected = probe._execute(
        "INSERT INTO branding_clients (id, data) VALUES (%s, %s)",
        ("client_1", Json({"id": "client_1", "name": "Acme"})),
    )
    assert affected == 1

    row = probe._fetch_one("SELECT data FROM branding_clients WHERE id = %s", ("client_1",))
    assert row == {"data": {"id": "client_1", "name": "Acme"}}


def test_fetch_one_returns_none_for_missing_row(fake_pg: dict) -> None:
    probe = _Probe()
    row = probe._fetch_one("SELECT data FROM branding_clients WHERE id = %s", ("nope",))
    assert row is None


def test_fetch_all_returns_all_rows(fake_pg: dict) -> None:
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
