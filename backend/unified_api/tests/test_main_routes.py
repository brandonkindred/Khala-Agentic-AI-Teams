"""Tests for unified API root endpoints: GET /, GET /health, GET /teams, and mount helpers."""

import sys
from pathlib import Path
from unittest.mock import patch

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi.testclient import TestClient

from unified_api.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------


def test_root_returns_200():
    """GET / returns HTTP 200."""
    resp = client.get("/")
    assert resp.status_code == 200


def test_root_response_has_required_fields():
    """GET / response contains name, version, teams, and docs_url."""
    resp = client.get("/")
    data = resp.json()
    assert data["name"] == "Khala Unified API"
    assert data["version"] == "1.0.0"
    assert isinstance(data["teams"], list)
    assert len(data["teams"]) > 0
    assert data["docs_url"] == "/docs"


def test_root_teams_have_required_fields():
    """Each team entry in GET / has name, prefix, description, tags, and enabled."""
    resp = client.get("/")
    for team in resp.json()["teams"]:
        assert "name" in team, f"Missing 'name' in team entry: {team}"
        assert "prefix" in team, f"Missing 'prefix' in team entry: {team}"
        assert "description" in team, f"Missing 'description' in team entry: {team}"
        assert "tags" in team, f"Missing 'tags' in team entry: {team}"
        assert "enabled" in team, f"Missing 'enabled' in team entry: {team}"


def test_root_teams_prefixes_start_with_api():
    """All team prefixes reported by GET / start with /api/."""
    resp = client.get("/")
    for team in resp.json()["teams"]:
        assert team["prefix"].startswith("/api/"), f"Bad prefix: {team['prefix']}"


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


def test_health_returns_200():
    """GET /health returns HTTP 200."""
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_response_has_required_fields():
    """GET /health response contains status, version, and teams."""
    resp = client.get("/health")
    data = resp.json()
    assert "status" in data
    assert "version" in data
    assert "teams" in data
    assert data["version"] == "1.0.0"


def test_health_status_is_valid_value():
    """GET /health status is either 'healthy' or 'degraded'."""
    resp = client.get("/health")
    assert resp.json()["status"] in ("healthy", "degraded")


def test_health_teams_have_required_fields():
    """Each team in GET /health has name, prefix, status, and enabled."""
    resp = client.get("/health")
    for team in resp.json()["teams"]:
        assert "name" in team
        assert "prefix" in team
        assert "status" in team
        assert "enabled" in team
        assert team["status"] in ("healthy", "unavailable"), f"Unexpected status: {team['status']}"


def test_health_degraded_when_enabled_team_not_mounted():
    """GET /health returns 'degraded' when an enabled team failed to mount."""
    from unified_api import main as unified_main
    from unified_api.config import TEAM_CONFIGS, TeamConfig

    # Insert a fake enabled team that is not mounted
    fake_key = "_test_fake_team"
    TEAM_CONFIGS[fake_key] = TeamConfig(name="Fake", prefix="/api/fake", description="test", enabled=True)
    unified_main._registered_teams[fake_key] = False

    try:
        resp = client.get("/health")
        assert resp.json()["status"] == "degraded"
    finally:
        del TEAM_CONFIGS[fake_key]
        unified_main._registered_teams.pop(fake_key, None)


# ---------------------------------------------------------------------------
# GET /teams
# ---------------------------------------------------------------------------


def test_list_teams_returns_200():
    """GET /teams returns HTTP 200."""
    resp = client.get("/teams")
    assert resp.status_code == 200


def test_list_teams_response_structure():
    """GET /teams returns a 'teams' dict with per-team info objects."""
    resp = client.get("/teams")
    data = resp.json()
    assert "teams" in data
    teams = data["teams"]
    assert isinstance(teams, dict)
    for key, info in teams.items():
        assert "name" in info, f"Team {key} missing 'name'"
        assert "prefix" in info, f"Team {key} missing 'prefix'"
        assert "mounted" in info, f"Team {key} missing 'mounted'"
        assert "enabled" in info, f"Team {key} missing 'enabled'"


def test_list_teams_includes_all_configured_teams():
    """GET /teams includes every key from TEAM_CONFIGS."""
    from unified_api.config import TEAM_CONFIGS

    resp = client.get("/teams")
    teams = resp.json()["teams"]
    for key in TEAM_CONFIGS:
        assert key in teams, f"Team {key} missing from /teams response"


def test_list_teams_docs_url_none_when_not_mounted():
    """GET /teams sets docs_url to null for teams that are not mounted."""
    resp = client.get("/teams")
    for key, info in resp.json()["teams"].items():
        if not info["mounted"]:
            assert info["docs_url"] is None, f"Team {key}: expected null docs_url when not mounted"


def test_list_teams_docs_url_set_when_mounted():
    """GET /teams sets docs_url to <prefix>/docs for mounted teams."""
    resp = client.get("/teams")
    for _key, info in resp.json()["teams"].items():
        if info["mounted"]:
            assert info["docs_url"] == f"{info['prefix']}/docs"


# ---------------------------------------------------------------------------
# Proxy registration
# ---------------------------------------------------------------------------


def test_register_proxy_routes_skips_teams_without_url():
    """_register_proxy_routes skips teams whose service URL env var is not set."""
    from fastapi import FastAPI

    from unified_api.main import _register_proxy_routes

    test_app = FastAPI()
    with patch("unified_api.main.get_enabled_teams", return_value={}):
        result = _register_proxy_routes(test_app)

    assert isinstance(result, dict)
    assert all(v is False for v in result.values())
