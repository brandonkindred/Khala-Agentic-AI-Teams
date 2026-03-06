"""
Integrations store: file-backed persistence for integration config (e.g. Slack).

JSON structure (integrations.json):
{
  "slack": {
    "enabled": false,
    "mode": "webhook",
    "webhook_url": "",
    "bot_token": "",
    "default_channel": "",
    "channel_display_name": "",
    "notify_open_questions": true,
    "notify_pa_responses": true
  }
}

File path: {AGENT_CACHE}/integrations.json (AGENT_CACHE env or .agent_cache).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = ".agent_cache"
_LOCK = threading.Lock()


def _get_integrations_path() -> Path:
    """Return path to integrations.json. Uses AGENT_CACHE env or .agent_cache."""
    cache_dir = os.getenv("AGENT_CACHE", _DEFAULT_CACHE_DIR)
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / "integrations.json"


def _read_raw() -> Dict[str, Any]:
    """Read raw JSON from file. Caller should hold _LOCK if needed."""
    path = _get_integrations_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return {}
        return json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read integrations file %s: %s", path, e)
        return {}


def _write_raw(data: Dict[str, Any]) -> None:
    """Write JSON to file with atomic write (temp + rename). Caller should hold _LOCK."""
    path = _get_integrations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.warning("Failed to write integrations file %s: %s", path, e)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def get_slack_config() -> Dict[str, Any]:
    """
    Return Slack config dict for webhook or bot posting modes.
    If SLACK_WEBHOOK_URL env is set and webhook_url is empty in store, it is used as the webhook_url
    (env override/fallback so deploy can set URL without UI).
    """
    with _LOCK:
        data = _read_raw()
    slack = data.get("slack") or {}
    webhook_url = str(slack.get("webhook_url", "")).strip()
    if not webhook_url:
        webhook_url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    return {
        "enabled": bool(slack.get("enabled", False)),
        "mode": str(slack.get("mode", "webhook")).strip() or "webhook",
        "webhook_url": webhook_url,
        "bot_token": str(slack.get("bot_token", "")).strip(),
        "default_channel": str(slack.get("default_channel", "")).strip(),
        "channel_display_name": str(slack.get("channel_display_name", "")).strip(),
        "notify_open_questions": bool(slack.get("notify_open_questions", True)),
        "notify_pa_responses": bool(slack.get("notify_pa_responses", True)),
    }


def set_slack_config(
    enabled: bool,
    webhook_url: str,
    mode: str = "webhook",
    bot_token: str = "",
    default_channel: str = "",
    channel_display_name: str = "",
    notify_open_questions: bool = True,
    notify_pa_responses: bool = True,
) -> None:
    """Persist Slack config with atomic write."""
    mode = (mode or "webhook").strip() or "webhook"
    webhook_url = (webhook_url or "").strip()
    bot_token = (bot_token or "").strip()
    default_channel = (default_channel or "").strip()
    channel_display_name = (channel_display_name or "").strip()
    with _LOCK:
        data = _read_raw()
        data["slack"] = {
            "enabled": enabled,
            "mode": mode,
            "webhook_url": webhook_url,
            "bot_token": bot_token,
            "default_channel": default_channel,
            "channel_display_name": channel_display_name,
            "notify_open_questions": notify_open_questions,
            "notify_pa_responses": notify_pa_responses,
        }
        _write_raw(data)


def get_integrations_list() -> List[Dict[str, Any]]:
    """
    Return list of integration entries for GET /api/integrations.
    Each entry: id, type, enabled, channel (no raw webhook_url).
    """
    with _LOCK:
        data = _read_raw()
    slack = data.get("slack") or {}
    return [
        {
            "id": "slack",
            "type": "slack",
            "enabled": bool(slack.get("enabled", False)),
            "channel": str(slack.get("channel_display_name", "")).strip() or None,
        }
    ]
