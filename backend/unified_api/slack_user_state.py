"""
Per-Slack-user state: current team selection and conversation IDs.

Stored as JSON at {AGENT_CACHE}/slack_user_state.json.
Thread-safe (same locking pattern as integrations_store.py).

Structure:
{
  "U12345": {
    "current_team": "personal_assistant",
    "conversations": {
      "personal_assistant": "conv-uuid-1",
      "blogging": "conv-uuid-2"
    }
  }
}
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = ".agent_cache"
_DEFAULT_TEAM = "personal_assistant"
_LOCK = threading.Lock()


def _get_state_path() -> Path:
    cache_dir = os.getenv("AGENT_CACHE", _DEFAULT_CACHE_DIR)
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / "slack_user_state.json"


def _read_all() -> dict[str, Any]:
    path = _get_state_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return {}
        return json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read slack user state %s: %s", path, e)
        return {}


def _write_all(data: dict[str, Any]) -> None:
    path = _get_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.warning("Failed to write slack user state %s: %s", path, e)
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def _get_user(data: dict, slack_user_id: str) -> dict:
    """Return user entry, creating defaults if missing."""
    if slack_user_id not in data:
        data[slack_user_id] = {"current_team": _DEFAULT_TEAM, "conversations": {}}
    user = data[slack_user_id]
    if "current_team" not in user:
        user["current_team"] = _DEFAULT_TEAM
    if "conversations" not in user:
        user["conversations"] = {}
    return user


def get_user_team(slack_user_id: str) -> str:
    """Return the user's currently selected team key."""
    with _LOCK:
        data = _read_all()
    user = data.get(slack_user_id) or {}
    return str(user.get("current_team", _DEFAULT_TEAM)).strip() or _DEFAULT_TEAM


def set_user_team(slack_user_id: str, team_key: str) -> None:
    """Set the user's currently selected team."""
    with _LOCK:
        data = _read_all()
        user = _get_user(data, slack_user_id)
        user["current_team"] = team_key
        _write_all(data)


def get_conversation_id(slack_user_id: str, team_key: str) -> str | None:
    """Return the conversation ID for the (user, team) pair, or None if not started."""
    with _LOCK:
        data = _read_all()
    user = data.get(slack_user_id) or {}
    convos = user.get("conversations") or {}
    cid = convos.get(team_key)
    return str(cid) if cid else None


def set_conversation_id(slack_user_id: str, team_key: str, conversation_id: str) -> None:
    """Store a conversation ID for the (user, team) pair."""
    with _LOCK:
        data = _read_all()
        user = _get_user(data, slack_user_id)
        user["conversations"][team_key] = conversation_id
        _write_all(data)


def reset_conversation(slack_user_id: str, team_key: str) -> None:
    """Clear the conversation ID for the (user, team) pair so a fresh one is created next time."""
    with _LOCK:
        data = _read_all()
        user = _get_user(data, slack_user_id)
        user["conversations"].pop(team_key, None)
        _write_all(data)
