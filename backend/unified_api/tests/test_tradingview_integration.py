"""Tests for the TradingView MCP integration: store persistence + /api/integrations/tradingview routes."""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi.testclient import TestClient

from unified_api.main import app

client = TestClient(app, follow_redirects=False)

_STORE_MODULE = "unified_api.routes.integrations"

_DEFAULT_TV_CFG = {
    "enabled": False,
    "mcp_server_url": "",
    "tool_name": "get_ohlcv",
    "auth_token": "",
}


# ---------------------------------------------------------------------------
# Store tests (file-backed JSON + encrypted-at-rest token)
# ---------------------------------------------------------------------------


def _reload_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    import unified_api.integration_credentials as creds_mod
    import unified_api.integrations_store as store_mod

    importlib.reload(creds_mod)
    importlib.reload(store_mod)
    return store_mod


def _install_fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """In-memory Fernet-backed fake for the Postgres credential store."""
    monkeypatch.setenv("POSTGRES_HOST", "fake-postgres-for-tests")
    import unified_api.integration_credentials as creds_mod
    import unified_api.postgres_encrypted_credentials as pg_mod

    fernet = creds_mod.get_integration_fernet()
    store: dict[tuple[str, str], str] = {}

    def _get(service: str, key: str) -> str:
        ct = store.get((service, key))
        return fernet.decrypt(ct.encode()).decode() if ct else ""

    def _set(service: str, key: str, value: str) -> None:
        if not value:
            store.pop((service, key), None)
            return
        store[(service, key)] = fernet.encrypt(value.encode()).decode()

    def _delete(service: str, key: str) -> None:
        store.pop((service, key), None)

    monkeypatch.setattr(pg_mod, "postgres_credentials_enabled", lambda: True)
    monkeypatch.setattr(pg_mod, "pg_get_credential", _get)
    monkeypatch.setattr(pg_mod, "pg_set_credential", _set)
    monkeypatch.setattr(pg_mod, "pg_delete_credential", _delete)
    return store


def test_get_tradingview_config_defaults_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _reload_store(tmp_path, monkeypatch)
    cfg = store.get_tradingview_config()
    assert cfg["enabled"] is False
    assert cfg["mcp_server_url"] == ""
    assert cfg["tool_name"] == "get_ohlcv"  # defaulted
    assert cfg["auth_token"] == ""


def test_set_and_get_tradingview_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _reload_store(tmp_path, monkeypatch)
    creds = _install_fake_pg(monkeypatch)

    store.set_tradingview_config(
        enabled=True,
        mcp_server_url="https://tv-mcp.example.com/mcp",
        tool_name="fetch_bars",
        auth_token="secret-token",
    )
    cfg = store.get_tradingview_config()
    assert cfg["enabled"] is True
    assert cfg["mcp_server_url"] == "https://tv-mcp.example.com/mcp"
    assert cfg["tool_name"] == "fetch_bars"
    assert cfg["auth_token"] == "secret-token"
    # Token is encrypted at rest — plaintext never appears in the credential store values.
    assert all("secret-token" not in ct for ct in creds.values())


def test_set_tradingview_config_blank_token_preserves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _reload_store(tmp_path, monkeypatch)
    _install_fake_pg(monkeypatch)

    store.set_tradingview_config(enabled=True, mcp_server_url="https://a.example/mcp", auth_token="tok1")
    # Re-save without a token (e.g. user edits the URL only) — token must survive.
    store.set_tradingview_config(enabled=False, mcp_server_url="https://b.example/mcp", auth_token="")
    cfg = store.get_tradingview_config()
    assert cfg["auth_token"] == "tok1"
    assert cfg["mcp_server_url"] == "https://b.example/mcp"
    assert cfg["enabled"] is False


def test_clear_tradingview_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _reload_store(tmp_path, monkeypatch)
    _install_fake_pg(monkeypatch)

    store.set_tradingview_config(enabled=True, mcp_server_url="https://a.example/mcp", auth_token="tok")
    store.clear_tradingview_config()
    cfg = store.get_tradingview_config()
    assert cfg["enabled"] is False
    assert cfg["mcp_server_url"] == ""
    assert cfg["auth_token"] == ""


def test_tradingview_in_integrations_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _reload_store(tmp_path, monkeypatch)
    _install_fake_pg(monkeypatch)
    store.set_tradingview_config(enabled=True, mcp_server_url="https://tv.example/mcp", auth_token="")
    items = {item["id"]: item for item in store.get_integrations_list()}
    assert "tradingview" in items
    assert items["tradingview"]["enabled"] is True
    assert items["tradingview"]["channel"] == "https://tv.example/mcp"


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


def test_get_tradingview_route_masks_token() -> None:
    cfg = dict(_DEFAULT_TV_CFG, enabled=True, mcp_server_url="https://tv/mcp", auth_token="super-secret")
    with patch(f"{_STORE_MODULE}.get_tradingview_config", return_value=cfg):
        resp = client.get("/api/integrations/tradingview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["mcp_server_url"] == "https://tv/mcp"
    assert body["auth_token_configured"] is True
    # The raw token must never be serialized to the client.
    assert "super-secret" not in resp.text
    assert "auth_token" not in body


def test_put_tradingview_route_saves() -> None:
    saved = dict(_DEFAULT_TV_CFG, enabled=True, mcp_server_url="https://tv/mcp", auth_token="tok")
    with (
        patch(f"{_STORE_MODULE}.set_tradingview_config") as mock_set,
        patch(f"{_STORE_MODULE}.get_tradingview_config", return_value=saved),
    ):
        resp = client.put(
            "/api/integrations/tradingview",
            json={
                "enabled": True,
                "mcp_server_url": "https://tv/mcp",
                "tool_name": "",
                "auth_token": "tok",
            },
        )
    assert resp.status_code == 200
    mock_set.assert_called_once()
    assert resp.json()["auth_token_configured"] is True


def test_put_tradingview_rejects_bad_url() -> None:
    with patch(f"{_STORE_MODULE}.get_tradingview_config", return_value=dict(_DEFAULT_TV_CFG)):
        resp = client.put(
            "/api/integrations/tradingview",
            json={"enabled": True, "mcp_server_url": "ftp://bad", "tool_name": "", "auth_token": ""},
        )
    assert resp.status_code == 400
    assert "http" in resp.json()["detail"].lower()


def test_put_tradingview_enabled_requires_url() -> None:
    with patch(f"{_STORE_MODULE}.get_tradingview_config", return_value=dict(_DEFAULT_TV_CFG)):
        resp = client.put(
            "/api/integrations/tradingview",
            json={"enabled": True, "mcp_server_url": "", "tool_name": "", "auth_token": ""},
        )
    assert resp.status_code == 400
    assert "required" in resp.json()["detail"].lower()


def test_put_tradingview_enabled_ok_when_url_already_stored() -> None:
    existing = dict(_DEFAULT_TV_CFG, mcp_server_url="https://tv/mcp")
    with (
        patch(f"{_STORE_MODULE}.set_tradingview_config"),
        patch(f"{_STORE_MODULE}.get_tradingview_config", return_value=existing),
    ):
        # Enable with a blank URL body — allowed because one is already stored.
        resp = client.put(
            "/api/integrations/tradingview",
            json={"enabled": True, "mcp_server_url": "", "tool_name": "", "auth_token": ""},
        )
    assert resp.status_code == 200


def test_delete_tradingview_route() -> None:
    with (
        patch(f"{_STORE_MODULE}.clear_tradingview_config") as mock_clear,
        patch(f"{_STORE_MODULE}.get_tradingview_config", return_value=dict(_DEFAULT_TV_CFG)),
    ):
        resp = client.delete("/api/integrations/tradingview")
    assert resp.status_code == 200
    mock_clear.assert_called_once()
    assert resp.json()["enabled"] is False
