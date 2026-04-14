"""Tests for per-team infrastructure scaffolding."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture(autouse=True)
def _isolate_agent_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    # Reset module-level cache so each test gets fresh state
    import agentic_team_provisioning.infrastructure as infra_mod

    infra_mod._AGENT_CACHE = str(tmp_path)
    infra_mod._infra_cache.clear()


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def test_provision_team_creates_directories(tmp_path: Path, fake_pg: dict) -> None:
    from agentic_team_provisioning.infrastructure import provision_team

    infra = provision_team("test-team-1")
    assert infra.assets_dir.is_dir()
    assert infra.runs_dir.is_dir()
    assert infra.base_dir == tmp_path / "provisioned_teams" / "test-team-1"


def test_provision_team_is_idempotent(tmp_path: Path, fake_pg: dict) -> None:
    from agentic_team_provisioning.infrastructure import provision_team

    infra1 = provision_team("test-team-3")
    infra2 = provision_team("test-team-3")
    assert infra1.base_dir == infra2.base_dir


def test_get_team_infrastructure_caching(tmp_path: Path, fake_pg: dict) -> None:
    from agentic_team_provisioning.infrastructure import get_team_infrastructure

    infra1 = get_team_infrastructure("test-team-4")
    infra2 = get_team_infrastructure("test-team-4")
    assert infra1 is infra2


def test_form_store_crud(tmp_path: Path, fake_pg: dict) -> None:
    from agentic_team_provisioning.infrastructure import provision_team

    infra = provision_team("test-team-5")
    store = infra.form_store

    # Create
    record = store.create_record("intake", {"name": "Alice", "role": "engineer"})
    assert record["form_key"] == "intake"
    assert record["data"]["name"] == "Alice"
    record_id = record["record_id"]

    # Read
    records = store.get_records("intake")
    assert len(records) == 1
    assert records[0]["record_id"] == record_id

    fetched = store.get_record(record_id)
    assert fetched is not None
    assert fetched["data"]["name"] == "Alice"

    # Update
    assert store.update_record(record_id, {"name": "Alice", "role": "lead"})
    updated = store.get_record(record_id)
    assert updated is not None
    assert updated["data"]["role"] == "lead"

    # List keys
    keys = store.list_form_keys()
    assert "intake" in keys

    # Delete
    assert store.delete_record(record_id)
    assert store.get_record(record_id) is None
    assert store.get_records("intake") == []


def test_form_store_nonexistent_record(tmp_path: Path, fake_pg: dict) -> None:
    from agentic_team_provisioning.infrastructure import provision_team

    infra = provision_team("test-team-6")
    assert infra.form_store.get_record("nonexistent") is None
    assert not infra.form_store.update_record("nonexistent", {"x": 1})
    assert not infra.form_store.delete_record("nonexistent")


def test_form_store_is_scoped_by_team_id(tmp_path: Path, fake_pg: dict) -> None:
    """A team's form store never sees another team's rows."""
    from agentic_team_provisioning.infrastructure import provision_team

    infra_a = provision_team("team-a")
    infra_b = provision_team("team-b")

    rec_a = infra_a.form_store.create_record("intake", {"who": "a"})
    rec_b = infra_b.form_store.create_record("intake", {"who": "b"})

    # Reads scoped by team
    assert [r["record_id"] for r in infra_a.form_store.get_records("intake")] == [
        rec_a["record_id"]
    ]
    assert [r["record_id"] for r in infra_b.form_store.get_records("intake")] == [
        rec_b["record_id"]
    ]
    # B's record is invisible to A
    assert infra_a.form_store.get_record(rec_b["record_id"]) is None
