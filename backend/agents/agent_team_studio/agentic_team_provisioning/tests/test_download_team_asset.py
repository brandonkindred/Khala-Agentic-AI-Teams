"""Tests for GET /teams/{team_id}/assets/{name} (download_team_asset).

Previously the route built ``path = infra.assets_dir / safe_name`` and served
it via ``FileResponse`` without verifying the resolved path stayed inside
``assets_dir`` — a symlink placed in the assets directory would be followed,
serving arbitrary files from the host filesystem. It also passed the raw
sanitized name to ``FileResponse(filename=...)``, which can produce a
malformed ``Content-Disposition`` header for names containing quotes or
non-ASCII characters.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

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


def test_download_within_assets_dir_succeeds_with_encoded_header(client: TestClient):
    team_id = _seed_team()
    name = "café report.txt"
    upload = client.post(
        f"/teams/{team_id}/assets",
        files={"file": (name, b"hello", "text/plain")},
    )
    assert upload.status_code == 200

    resp = client.get(f"/teams/{team_id}/assets/{quote(name)}")
    assert resp.status_code == 200
    assert resp.content == b"hello"
    assert resp.headers["content-disposition"] == f"attachment; filename*=UTF-8''{quote(name)}"


def test_download_rejects_symlink_escaping_assets_dir(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_team_studio.agentic_team_provisioning.infrastructure import (
        get_team_infrastructure,
    )

    team_id = _seed_team()
    infra = get_team_infrastructure(team_id)

    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(b"top secret host data")

    escape_link = infra.assets_dir / "escape.txt"
    escape_link.symlink_to(secret)

    resp = client.get(f"/teams/{team_id}/assets/escape.txt")
    assert resp.status_code == 404


def test_download_unknown_asset_404(client: TestClient):
    team_id = _seed_team()
    resp = client.get(f"/teams/{team_id}/assets/does-not-exist.txt")
    assert resp.status_code == 404
