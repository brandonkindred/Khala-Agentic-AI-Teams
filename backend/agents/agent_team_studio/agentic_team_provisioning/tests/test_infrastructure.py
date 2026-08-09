"""Tests for per-team infrastructure scaffolding."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture(autouse=True)
def _isolate_agent_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    # Reset process-local cache root + handle map so each test gets fresh state
    import agent_team_studio.agentic_team_provisioning.infrastructure as infra_mod

    infra_mod._set_agent_cache_for_testing(str(tmp_path))
    infra_mod._clear_infra_cache_for_testing()


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def test_provision_team_creates_directories(tmp_path: Path, fake_pg: dict) -> None:
    from agent_team_studio.agentic_team_provisioning.infrastructure import provision_team

    infra = provision_team("test-team-1")
    assert infra.assets_dir.is_dir()
    assert infra.runs_dir.is_dir()
    assert infra.base_dir == tmp_path / "provisioned_teams" / "test-team-1"


def test_provision_team_is_idempotent(tmp_path: Path, fake_pg: dict) -> None:
    from agent_team_studio.agentic_team_provisioning.infrastructure import provision_team

    infra1 = provision_team("test-team-3")
    infra2 = provision_team("test-team-3")
    assert infra1.base_dir == infra2.base_dir


def test_get_team_infrastructure_caching(tmp_path: Path, fake_pg: dict) -> None:
    from agent_team_studio.agentic_team_provisioning.infrastructure import get_team_infrastructure

    infra1 = get_team_infrastructure("test-team-4")
    infra2 = get_team_infrastructure("test-team-4")
    assert infra1 is infra2


def test_provision_team_rejects_empty_team_id(fake_pg: dict) -> None:
    from agent_team_studio.agentic_team_provisioning.infrastructure import provision_team

    with pytest.raises(ValueError, match="team_id must be a non-empty string"):
        provision_team("")


def test_get_team_infrastructure_rejects_empty_team_id(fake_pg: dict) -> None:
    from agent_team_studio.agentic_team_provisioning.infrastructure import get_team_infrastructure

    with pytest.raises(ValueError, match="team_id must be a non-empty string"):
        get_team_infrastructure("")


@pytest.mark.parametrize(
    "bad_id",
    [
        "../escape",
        "../../etc/cron.d",
        "a/b",
        "a\\b",
        "..",
        ".",
        "te am",
        "team;id",
        "team?id",
        "/abs",
    ],
)
def test_provision_team_rejects_unsafe_team_id(tmp_path: Path, fake_pg: dict, bad_id: str) -> None:
    """team_id is interpolated into a filesystem path under provisioned_teams/.

    Characters outside [A-Za-z0-9_-] (path separators, ``..``, whitespace, etc.)
    must be rejected before mkdir so a crafted id cannot escape the cache root.
    """
    from agent_team_studio.agentic_team_provisioning.infrastructure import provision_team

    with pytest.raises(ValueError, match="unsafe characters"):
        provision_team(bad_id)

    provisioned = tmp_path / "provisioned_teams"
    # No directory materialization for a rejected id — neither under the
    # intended subtree nor at a traversal escape outside it.
    assert not provisioned.exists() or bad_id not in {p.name for p in provisioned.iterdir()}
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "etc").exists()


@pytest.mark.parametrize(
    "bad_id",
    ["../escape", "a/b", ".."],
)
def test_get_team_infrastructure_rejects_unsafe_team_id(fake_pg: dict, bad_id: str) -> None:
    from agent_team_studio.agentic_team_provisioning.infrastructure import get_team_infrastructure

    with pytest.raises(ValueError, match="unsafe characters"):
        get_team_infrastructure(bad_id)


def test_build_team_infrastructure_rejects_resolved_escape(tmp_path: Path, fake_pg: dict) -> None:
    """Defense-in-depth: even if charset checks are bypassed, mkdir must not escape."""
    from agent_team_studio.agentic_team_provisioning.infrastructure import (
        _build_team_infrastructure,
    )

    with pytest.raises(ValueError, match="escapes provisioned_teams"):
        _build_team_infrastructure("../escape")

    assert not (tmp_path / "escape").exists()


def test_provision_team_replaces_cache_entry(tmp_path: Path, fake_pg: dict) -> None:
    from agent_team_studio.agentic_team_provisioning.infrastructure import (
        get_team_infrastructure,
        provision_team,
    )

    infra1 = get_team_infrastructure("test-team-7")
    infra2 = provision_team("test-team-7")
    infra3 = get_team_infrastructure("test-team-7")
    assert infra1 is not infra2
    assert infra2 is infra3


def test_form_store_crud(tmp_path: Path, fake_pg: dict) -> None:
    from agent_team_studio.agentic_team_provisioning.infrastructure import provision_team

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
    assert store.update_record("intake", record_id, {"name": "Alice", "role": "lead"})
    updated = store.get_record(record_id)
    assert updated is not None
    assert updated["data"]["role"] == "lead"

    # List keys
    keys = store.list_form_keys()
    assert "intake" in keys

    # Delete
    assert store.delete_record("intake", record_id)
    assert store.get_record(record_id) is None
    assert store.get_records("intake") == []


def test_form_store_nonexistent_record(tmp_path: Path, fake_pg: dict) -> None:
    from agent_team_studio.agentic_team_provisioning.infrastructure import provision_team

    infra = provision_team("test-team-6")
    assert infra.form_store.get_record("nonexistent") is None
    assert not infra.form_store.update_record("intake", "nonexistent", {"x": 1})
    assert not infra.form_store.delete_record("intake", "nonexistent")


def test_form_store_update_delete_scoped_by_form_key(tmp_path: Path, fake_pg: dict) -> None:
    """A record cannot be mutated through a mismatched form_key in the path,
    even when the team_id and record_id are otherwise correct."""
    from agent_team_studio.agentic_team_provisioning.infrastructure import provision_team

    infra = provision_team("test-team-8")
    store = infra.form_store

    record = store.create_record("intake", {"name": "Alice"})
    record_id = record["record_id"]
    store.create_record("survey", {"question": "unrelated"})

    # Wrong form_key: no-op, record untouched.
    assert not store.update_record("survey", record_id, {"name": "Mallory"})
    assert store.get_record(record_id)["data"]["name"] == "Alice"
    assert not store.delete_record("survey", record_id)
    assert store.get_record(record_id) is not None

    # Correct form_key: succeeds.
    assert store.update_record("intake", record_id, {"name": "Bob"})
    assert store.get_record(record_id)["data"]["name"] == "Bob"
    assert store.delete_record("intake", record_id)
    assert store.get_record(record_id) is None


def test_form_store_is_scoped_by_team_id(tmp_path: Path, fake_pg: dict) -> None:
    """A team's form store never sees another team's rows."""
    from agent_team_studio.agentic_team_provisioning.infrastructure import provision_team

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
