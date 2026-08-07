"""Tests for POST /teams/{team_id}/assets (upload_team_asset).

Previously the route read the whole upload into memory with no size limit,
wrote it with dest.write_bytes(content) unconditionally (silently overwriting
any same-named asset), and ran the blocking write/stat synchronously on the
event loop. The fix enforces a configurable size limit read in bounded
chunks (413 on overflow, without ever buffering the full oversized payload),
rejects a same-name collision with 409, and offloads the filesystem write/stat
to a thread.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture(autouse=True)
def _isolate_agent_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    import agent_team_studio.agentic_team_provisioning.infrastructure as infra_mod

    infra_mod._set_agent_cache_for_testing(str(tmp_path))
    infra_mod._clear_infra_cache_for_testing()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api import main

    return TestClient(main.app)


def _seed_team() -> str:
    store = AgenticTeamStore()
    team = store.create_team(name="Ops", description="")
    return team.team_id


def test_upload_within_limit_succeeds(client: TestClient):
    team_id = _seed_team()
    resp = client.post(
        f"/teams/{team_id}/assets",
        files={"file": ("report.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "report.txt"
    assert body["size_bytes"] == len(b"hello world")

    listed = client.get(f"/teams/{team_id}/assets").json()
    assert [a["name"] for a in listed] == ["report.txt"]


def test_upload_exceeding_limit_returns_413_and_writes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENTIC_TEAM_MAX_ASSET_BYTES", "10")
    team_id = _seed_team()

    resp = client.post(
        f"/teams/{team_id}/assets",
        files={"file": ("big.bin", b"x" * 1000, "application/octet-stream")},
    )
    assert resp.status_code == 413

    assert client.get(f"/teams/{team_id}/assets").json() == []


def test_upload_duplicate_name_returns_409_and_does_not_overwrite(client: TestClient):
    team_id = _seed_team()
    first = client.post(
        f"/teams/{team_id}/assets",
        files={"file": ("report.txt", b"original", "text/plain")},
    )
    assert first.status_code == 200

    second = client.post(
        f"/teams/{team_id}/assets",
        files={"file": ("report.txt", b"replacement", "text/plain")},
    )
    assert second.status_code == 409

    download = client.get(f"/teams/{team_id}/assets/report.txt")
    assert download.status_code == 200
    assert download.content == b"original"


def test_upload_offloads_filesystem_write_to_a_thread(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """The blocking write/stat run via asyncio.to_thread, not on the event loop."""
    from agent_team_studio.agentic_team_provisioning.api import main

    calls: list[str] = []
    real_to_thread = main.asyncio.to_thread

    async def _tracking_to_thread(func, *args, **kwargs):
        calls.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(main.asyncio, "to_thread", _tracking_to_thread)

    team_id = _seed_team()
    resp = client.post(
        f"/teams/{team_id}/assets",
        files={"file": ("report.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 200
    assert calls == ["write_bytes", "stat"]
