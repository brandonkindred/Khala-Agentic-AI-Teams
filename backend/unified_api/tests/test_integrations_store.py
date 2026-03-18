"""Unit tests for integrations store (get/set Slack config, list, missing file, invalid JSON)."""

import importlib
import json
from pathlib import Path

import pytest


def _reload_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Set AGENT_CACHE and reload both credential and store modules to pick up the new path."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    import unified_api.integration_credentials as creds_mod
    import unified_api.integrations_store as store_mod

    importlib.reload(creds_mod)
    importlib.reload(store_mod)
    return store_mod, creds_mod


def test_get_slack_config_defaults_when_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_slack_config returns defaults when integrations file does not exist."""
    store, _ = _reload_modules(tmp_path, monkeypatch)
    cfg = store.get_slack_config()
    assert cfg["enabled"] is False
    assert cfg["mode"] == "webhook"
    assert cfg["webhook_url"] == ""
    assert cfg["bot_token"] == ""
    assert cfg["default_channel"] == ""
    assert cfg["channel_display_name"] == ""
    assert cfg["notify_open_questions"] is True
    assert cfg["notify_pa_responses"] is True
    assert cfg["client_id"] == ""
    assert cfg["client_secret"] == ""


def test_set_and_get_slack_config_non_sensitive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """set_slack_config persists non-sensitive fields to JSON and get_slack_config returns them."""
    store, _ = _reload_modules(tmp_path, monkeypatch)
    store.set_slack_config(
        enabled=True,
        mode="bot",
        webhook_url="",
        bot_token="xoxb-token",
        default_channel="#eng",
        channel_display_name="#eng",
        notify_open_questions=True,
        notify_pa_responses=False,
    )
    cfg = store.get_slack_config()
    assert cfg["enabled"] is True
    assert cfg["mode"] == "bot"
    assert cfg["webhook_url"] == ""
    assert cfg["bot_token"] == "xoxb-token"
    assert cfg["default_channel"] == "#eng"
    assert cfg["channel_display_name"] == "#eng"
    assert cfg["notify_open_questions"] is True
    assert cfg["notify_pa_responses"] is False


def test_set_and_get_slack_credentials_encrypted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """client_id and client_secret are stored encrypted and returned decrypted."""
    store, _ = _reload_modules(tmp_path, monkeypatch)
    store.set_slack_config(
        enabled=False,
        client_id="my-client-id",
        client_secret="my-super-secret",
    )
    cfg = store.get_slack_config()
    assert cfg["client_id"] == "my-client-id"
    assert cfg["client_secret"] == "my-super-secret"

    # Verify the credentials are NOT stored in plain text in the JSON file
    json_path = tmp_path / "integrations.json"
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    slack_json = raw.get("slack", {})
    assert "client_id" not in slack_json
    assert "client_secret" not in slack_json
    assert "my-client-id" not in json.dumps(raw)
    assert "my-super-secret" not in json.dumps(raw)


def test_credentials_encrypted_in_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential values in the SQLite DB are not stored in plain text."""
    import sqlite3

    store, _ = _reload_modules(tmp_path, monkeypatch)
    store.set_slack_config(enabled=False, client_id="abc123", client_secret="topsecret")

    db_path = tmp_path / "integration_credentials.db"
    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT value FROM service_integrations").fetchall()
    conn.close()

    raw_values = [row[0] for row in rows]
    assert all("abc123" not in v for v in raw_values), "client_id stored in plain text"
    assert all("topsecret" not in v for v in raw_values), "client_secret stored in plain text"


def test_clear_slack_oauth_preserves_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """clear_slack_oauth removes bot token and team info but keeps app credentials."""
    store, _ = _reload_modules(tmp_path, monkeypatch)
    store.set_slack_config(enabled=True, mode="bot", client_id="cid", client_secret="csec")
    store.set_slack_oauth_token(bot_token="xoxb-tok", team_id="T123", team_name="Acme", bot_user_id="U1")

    store.clear_slack_oauth()
    cfg = store.get_slack_config()
    assert cfg["client_id"] == "cid"
    assert cfg["client_secret"] == "csec"
    assert cfg["bot_token"] == ""
    assert cfg["team_id"] == ""
    assert cfg["team_name"] == ""
    assert cfg["enabled"] is False


def test_get_integrations_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_integrations_list returns Slack entry without sensitive credentials."""
    store, _ = _reload_modules(tmp_path, monkeypatch)
    store.set_slack_config(True, "https://hooks.slack.com/services/T/B/X", channel_display_name="#eng")
    items = store.get_integrations_list()
    assert len(items) == 1
    assert items[0]["id"] == "slack"
    assert items[0]["type"] == "slack"
    assert items[0]["enabled"] is True
    assert items[0]["channel"] == "#eng"
    assert "webhook_url" not in items[0]
    assert "client_id" not in items[0]
    assert "client_secret" not in items[0]


def test_get_slack_config_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_slack_config returns defaults when file contains invalid JSON."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    path = tmp_path / "integrations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json {", encoding="utf-8")
    store, _ = _reload_modules(tmp_path, monkeypatch)
    cfg = store.get_slack_config()
    assert cfg["enabled"] is False
    assert cfg["webhook_url"] == ""
    assert cfg["channel_display_name"] == ""
