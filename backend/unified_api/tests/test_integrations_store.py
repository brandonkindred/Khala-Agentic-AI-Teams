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


def _install_fake_pg_credentials(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """Patch the Postgres credential module with an in-memory dict-backed fake.

    After PR 1 the ``integrations_store.set_slack_config`` flow routes
    client_id / client_secret through ``pg_set_credential`` against the
    real ``encrypted_integration_credentials`` Postgres table. Tests
    that want to exercise the encrypted-at-rest behaviour without a
    live Postgres mock the ``pg_*`` surface and record what would have
    been written. The returned ``store`` dict maps
    ``(service, credential_key)`` → ciphertext (Fernet-encrypted) so
    individual tests can assert that plaintext never leaked into it.
    """
    monkeypatch.setenv("POSTGRES_HOST", "fake-postgres-for-tests")

    import unified_api.integration_credentials as creds_mod
    import unified_api.postgres_encrypted_credentials as pg_mod

    # A single shared fernet keeps encrypted ciphertexts stable across
    # get/set round-trips in each test.
    fernet = creds_mod.get_integration_fernet()
    store: dict[tuple[str, str], str] = {}

    def _fake_enabled() -> bool:
        return True

    def _fake_get(service: str, key: str) -> str:
        ciphertext = store.get((service, key))
        if not ciphertext:
            return ""
        return fernet.decrypt(ciphertext.encode()).decode()

    def _fake_set(service: str, key: str, value: str) -> None:
        if not value:
            store.pop((service, key), None)
            return
        store[(service, key)] = fernet.encrypt(value.encode()).decode()

    def _fake_delete(service: str, key: str) -> None:
        store.pop((service, key), None)

    def _fake_delete_service(service: str) -> None:
        for k in [k for k in store if k[0] == service]:
            store.pop(k, None)

    monkeypatch.setattr(pg_mod, "postgres_credentials_enabled", _fake_enabled)
    monkeypatch.setattr(pg_mod, "pg_get_credential", _fake_get)
    monkeypatch.setattr(pg_mod, "pg_set_credential", _fake_set)
    monkeypatch.setattr(pg_mod, "pg_delete_credential", _fake_delete)
    monkeypatch.setattr(pg_mod, "pg_delete_service_credentials", _fake_delete_service)

    return store


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
        bot_token="xoxb-FAKE-opaque-bot",
        default_channel="#eng",
        channel_display_name="#eng",
        notify_open_questions=True,
        notify_pa_responses=False,
    )
    cfg = store.get_slack_config()
    assert cfg["enabled"] is True
    assert cfg["mode"] == "bot"
    assert cfg["webhook_url"] == ""
    assert cfg["bot_token"] == "xoxb-FAKE-opaque-bot"
    assert cfg["default_channel"] == "#eng"
    assert cfg["channel_display_name"] == "#eng"
    assert cfg["notify_open_questions"] is True
    assert cfg["notify_pa_responses"] is False


def test_set_and_get_slack_credentials_encrypted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """client_id and client_secret are stored encrypted and returned decrypted."""
    store, _ = _reload_modules(tmp_path, monkeypatch)
    _install_fake_pg_credentials(monkeypatch)
    store.set_slack_config(
        enabled=False,
        client_id="opaque-client-id",
        client_secret="opaque-client-secret",
    )
    cfg = store.get_slack_config()
    assert cfg["client_id"] == "opaque-client-id"
    assert cfg["client_secret"] == "opaque-client-secret"

    # Verify the credentials are NOT stored in plain text in the JSON file
    json_path = tmp_path / "integrations.json"
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    slack_json = raw.get("slack", {})
    assert "client_id" not in slack_json
    assert "client_secret" not in slack_json
    assert "opaque-client-id" not in json.dumps(raw)
    assert "opaque-client-secret" not in json.dumps(raw)


def test_credentials_encrypted_at_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential ciphertexts handed to the Postgres store never contain plaintext.

    Inspects the in-memory fake backing ``pg_set_credential`` (which
    preserves the Fernet ciphertext verbatim) and asserts the raw value
    never contains the plaintext.
    """
    store, _ = _reload_modules(tmp_path, monkeypatch)
    cred_store = _install_fake_pg_credentials(monkeypatch)
    store.set_slack_config(
        enabled=False,
        client_id="opaque-cid-at-rest",
        client_secret="opaque-csec-at-rest",
    )

    raw_values = list(cred_store.values())
    assert raw_values, "no rows written to the credential store"
    assert all("opaque-cid-at-rest" not in v for v in raw_values), "client_id stored in plain text"
    assert all("opaque-csec-at-rest" not in v for v in raw_values), "client_secret stored in plain text"


def test_clear_slack_oauth_preserves_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """clear_slack_oauth removes bot token and team info but keeps app credentials."""
    store, _ = _reload_modules(tmp_path, monkeypatch)
    _install_fake_pg_credentials(monkeypatch)
    store.set_slack_config(
        enabled=True,
        mode="bot",
        client_id="opaque-cid",
        client_secret="opaque-csec",
    )
    store.set_slack_oauth_token(
        bot_token="xoxb-FAKE-opaque-bot",
        team_id="T123",
        team_name="Acme",
        bot_user_id="U1",
    )

    store.clear_slack_oauth()
    cfg = store.get_slack_config()
    assert cfg["client_id"] == "opaque-cid"
    assert cfg["client_secret"] == "opaque-csec"
    assert cfg["bot_token"] == ""
    assert cfg["team_id"] == ""
    assert cfg["team_name"] == ""
    assert cfg["enabled"] is False


def test_get_integrations_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_integrations_list returns Slack and Medium entries without sensitive credentials."""
    store, _ = _reload_modules(tmp_path, monkeypatch)
    store.set_slack_config(True, "https://hooks.slack.com/services/T/B/X", channel_display_name="#eng")
    items = store.get_integrations_list()
    assert len(items) == 2
    slack = next(i for i in items if i["id"] == "slack")
    assert slack["type"] == "slack"
    assert slack["enabled"] is True
    assert slack["channel"] == "#eng"
    assert "webhook_url" not in slack
    assert "client_id" not in slack
    assert "client_secret" not in slack
    medium = next(i for i in items if i["id"] == "medium")
    assert medium["type"] == "medium"
    assert medium["enabled"] is False


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


def test_medium_session_storage_uses_default_agent_cache_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Medium storage_state is written to AGENT_CACHE-backed browser session path by default."""
    store, _ = _reload_modules(tmp_path, monkeypatch)
    store.set_medium_session_storage_state_json('{"cookies":[],"origins":[]}')

    session_path = tmp_path / "integrations" / "browser_sessions" / "medium" / "storage_state.json"
    assert session_path.exists()
    assert session_path.read_text(encoding="utf-8") == '{"cookies":[],"origins":[]}'
    cfg = store.get_medium_config()
    assert cfg["session_configured"] is True


def test_medium_session_storage_honors_env_root_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """INTEGRATIONS_BROWSER_SESSION_ROOT overrides default disk location."""
    custom_root = tmp_path / "custom_browser_sessions"
    monkeypatch.setenv("INTEGRATIONS_BROWSER_SESSION_ROOT", str(custom_root))
    store, _ = _reload_modules(tmp_path, monkeypatch)
    store.set_medium_session_storage_state_json('{"cookies":[],"origins":[]}')

    session_path = custom_root / "medium" / "storage_state.json"
    assert session_path.exists()
    assert session_path.read_text(encoding="utf-8") == '{"cookies":[],"origins":[]}'


def test_clear_medium_session_storage_removes_disk_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """clear_medium_session_storage removes on-disk storage_state file."""
    store, _ = _reload_modules(tmp_path, monkeypatch)
    store.set_medium_session_storage_state_json('{"cookies":[],"origins":[]}')
    session_path = tmp_path / "integrations" / "browser_sessions" / "medium" / "storage_state.json"
    assert session_path.exists()

    store.clear_medium_session_storage()
    assert not session_path.exists()
    cfg = store.get_medium_config()
    assert cfg["session_configured"] is False
