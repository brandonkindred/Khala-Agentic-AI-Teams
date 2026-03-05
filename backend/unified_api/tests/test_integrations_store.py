"""Unit tests for integrations store (get/set Slack config, list, missing file, invalid JSON)."""

import json
from pathlib import Path

import pytest


def test_get_slack_config_defaults_when_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_slack_config returns defaults when integrations file does not exist."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    from unified_api import integrations_store

    cfg = integrations_store.get_slack_config()
    assert cfg["enabled"] is False
    assert cfg["webhook_url"] == ""
    assert cfg["channel_display_name"] == ""


def test_set_and_get_slack_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """set_slack_config persists and get_slack_config returns saved values."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    from unified_api import integrations_store

    integrations_store.set_slack_config(
        enabled=True,
        webhook_url="https://hooks.slack.com/services/T/B/X",
        channel_display_name="#eng",
    )
    cfg = integrations_store.get_slack_config()
    assert cfg["enabled"] is True
    assert cfg["webhook_url"] == "https://hooks.slack.com/services/T/B/X"
    assert cfg["channel_display_name"] == "#eng"


def test_get_integrations_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_integrations_list returns Slack entry without raw webhook_url."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    from unified_api import integrations_store

    integrations_store.set_slack_config(True, "https://hooks.slack.com/services/T/B/X", "#eng")
    items = integrations_store.get_integrations_list()
    assert len(items) == 1
    assert items[0]["id"] == "slack"
    assert items[0]["type"] == "slack"
    assert items[0]["enabled"] is True
    assert items[0]["channel"] == "#eng"
    assert "webhook_url" not in items[0]


def test_get_slack_config_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_slack_config returns defaults when file contains invalid JSON."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    path = tmp_path / "integrations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json {", encoding="utf-8")
    from unified_api import integrations_store

    cfg = integrations_store.get_slack_config()
    assert cfg["enabled"] is False
    assert cfg["webhook_url"] == ""
    assert cfg["channel_display_name"] == ""


def test_get_slack_config_env_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When webhook_url not in store, SLACK_WEBHOOK_URL env is used as fallback."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/env-fallback")
    path = tmp_path / "integrations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"slack": {"enabled": True, "webhook_url": "", "channel_display_name": ""}}), encoding="utf-8")
    from unified_api import integrations_store

    cfg = integrations_store.get_slack_config()
    assert cfg["webhook_url"] == "https://hooks.slack.com/env-fallback"
