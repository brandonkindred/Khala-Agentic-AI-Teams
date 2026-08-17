"""Tests for POST /teams/{team_id}/assets (upload_team_asset).

Previously the route read the whole upload into memory with no size limit,
wrote it with dest.write_bytes(content) unconditionally (silently overwriting
any same-named asset), and ran the blocking write/stat synchronously on the
event loop. The fix enforces a configurable size limit read in bounded
chunks (413 on overflow), streams each chunk directly to disk instead of
buffering the full payload in memory, rejects a same-name collision with 409,
removes any partial file on failure, and offloads the filesystem
open/write/close/stat calls to a thread.
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
    """The blocking open/write/close/stat run via asyncio.to_thread, not on the event loop."""
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
    assert calls == ["open", "write", "close", "stat"]


def test_upload_streams_chunks_directly_to_disk_without_buffering_full_content(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Each chunk is written to disk as it's read, not buffered into one join+write.

    Forces a tiny chunk size so a small payload still spans several reads,
    then asserts more than one write call happened and that the writes
    concatenate back to the original payload exactly.
    """
    from agent_team_studio.agentic_team_provisioning.api import main

    monkeypatch.setattr(main, "_ASSET_UPLOAD_CHUNK_BYTES", 4)

    write_calls: list[bytes] = []
    real_to_thread = main.asyncio.to_thread

    async def _tracking_to_thread(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "write":
            write_calls.append(args[0])
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(main.asyncio, "to_thread", _tracking_to_thread)

    team_id = _seed_team()
    payload = b"0123456789abcdef"
    resp = client.post(
        f"/teams/{team_id}/assets",
        files={"file": ("data.bin", payload, "application/octet-stream")},
    )
    assert resp.status_code == 200
    assert len(write_calls) > 1
    assert b"".join(write_calls) == payload

    download = client.get(f"/teams/{team_id}/assets/data.bin")
    assert download.status_code == 200
    assert download.content == payload


def test_upload_exceeding_limit_removes_partial_file_from_disk(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """A rejected oversized upload leaves no partial file on disk, not just off the listing."""
    from agent_team_studio.agentic_team_provisioning.api import main
    from agent_team_studio.agentic_team_provisioning.infrastructure import (
        get_team_infrastructure,
    )

    monkeypatch.setenv("AGENTIC_TEAM_MAX_ASSET_BYTES", "10")
    team_id = _seed_team()

    resp = client.post(
        f"/teams/{team_id}/assets",
        files={"file": ("big.bin", b"x" * 1000, "application/octet-stream")},
    )
    assert resp.status_code == 413

    infra = get_team_infrastructure(team_id)
    assert not (infra.assets_dir / main._safe_asset_name("big.bin")).exists()
