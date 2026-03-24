"""
Integrations store: file-backed persistence for integration config (e.g. Slack).

Non-sensitive config (enabled, mode, channels, notification toggles, OAuth team info)
is stored in JSON at {AGENT_CACHE}/integrations.json.

Sensitive OAuth credentials (client_id, client_secret) are stored in an encrypted
SQLite database via integration_credentials.py — never in the JSON file.

JSON structure (integrations.json):
{
  "medium": {
    "enabled": false,
    "oauth_provider": "google",
    "oauth_identity_connected": false,
    "linked_email": "",
    "linked_name": ""
  },
  "slack": {
    "enabled": false,
    "mode": "webhook",        // "webhook" | "bot"
    "webhook_url": "",
    "bot_token": "",          // populated by OAuth or manual entry
    "default_channel": "",
    "channel_display_name": "",
    "notify_open_questions": true,
    "notify_pa_responses": true,
    // OAuth fields (set by /oauth/callback, cleared by /oauth DELETE)
    "team_id": "",
    "team_name": "",
    "bot_user_id": ""
  },
  // Transient OAuth CSRF state (cleared after use or expiry)
  "slack_oauth_state": {
    "value": "...",
    "created_at": "2024-01-01T00:00:00+00:00"
  }
}

File path: {AGENT_CACHE}/integrations.json (AGENT_CACHE env or .agent_cache).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from unified_api.integration_credentials import (
    delete_credential,
    get_credential,
    set_credential,
)

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = ".agent_cache"
_BROWSER_SESSION_ROOT_ENV = "INTEGRATIONS_BROWSER_SESSION_ROOT"
_DEFAULT_BROWSER_SESSIONS_SUBDIR = "integrations/browser_sessions"
_LOCK = threading.Lock()
_OAUTH_STATE_TTL_SECONDS = 600  # 10 minutes
_BROWSER_SESSION_ROOT_LOGGED = False

_SLACK_SERVICE = "slack"
_MEDIUM_SERVICE = "medium"


def _get_integrations_path() -> Path:
    """Return path to integrations.json. Uses AGENT_CACHE env or .agent_cache."""
    cache_dir = os.getenv("AGENT_CACHE", _DEFAULT_CACHE_DIR)
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / "integrations.json"


def _resolve_browser_session_root() -> Path:
    """Return root directory for browser session files (env override supported)."""
    global _BROWSER_SESSION_ROOT_LOGGED
    override = os.getenv(_BROWSER_SESSION_ROOT_ENV, "").strip()
    if override:
        root = Path(override).expanduser().resolve()
    else:
        cache_dir = Path(os.getenv("AGENT_CACHE", _DEFAULT_CACHE_DIR))
        root = (cache_dir / _DEFAULT_BROWSER_SESSIONS_SUBDIR).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        root.chmod(0o700)
    if not _BROWSER_SESSION_ROOT_LOGGED:
        logger.info(
            "Integration browser session root resolved to: %s (env_override=%s)",
            root,
            bool(override),
        )
        _BROWSER_SESSION_ROOT_LOGGED = True
    return root


def _medium_storage_state_path() -> Path:
    """Return Medium storage_state.json path under the browser session root."""
    medium_dir = _resolve_browser_session_root() / "medium"
    medium_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        medium_dir.chmod(0o700)
    return medium_dir / "storage_state.json"


def _write_text_atomic(path: Path, content: str) -> None:
    """Write text to disk atomically (tmp + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to read file %s: %s", path, e)
        return ""


def _read_raw() -> dict[str, Any]:
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


def _write_raw(data: dict[str, Any]) -> None:
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
            with contextlib.suppress(OSError):
                tmp.unlink()


def get_slack_config() -> dict[str, Any]:
    """
    Return Slack config dict. Non-sensitive fields come from the JSON store.
    Sensitive credentials (client_id, client_secret) come from the encrypted DB.
    """
    with _LOCK:
        data = _read_raw()
    slack = data.get("slack") or {}
    return {
        "enabled": bool(slack.get("enabled", False)),
        "mode": str(slack.get("mode", "webhook")).strip() or "webhook",
        # Sensitive: from encrypted DB
        "client_id": get_credential(_SLACK_SERVICE, "client_id"),
        "client_secret": get_credential(_SLACK_SERVICE, "client_secret"),
        # Non-sensitive: from JSON
        "webhook_url": str(slack.get("webhook_url", "")).strip(),
        "bot_token": str(slack.get("bot_token", "")).strip(),
        "default_channel": str(slack.get("default_channel", "")).strip(),
        "channel_display_name": str(slack.get("channel_display_name", "")).strip(),
        "notify_open_questions": bool(slack.get("notify_open_questions", True)),
        "notify_pa_responses": bool(slack.get("notify_pa_responses", True)),
        # OAuth-populated fields
        "team_id": str(slack.get("team_id", "")).strip(),
        "team_name": str(slack.get("team_name", "")).strip(),
        "bot_user_id": str(slack.get("bot_user_id", "")).strip(),
    }


def set_slack_config(
    enabled: bool,
    webhook_url: str = "",
    mode: str = "webhook",
    client_id: str = "",
    client_secret: str = "",
    bot_token: str = "",
    default_channel: str = "",
    channel_display_name: str = "",
    notify_open_questions: bool = True,
    notify_pa_responses: bool = True,
    team_id: str = "",
    team_name: str = "",
    bot_user_id: str = "",
) -> None:
    """
    Persist Slack config. Sensitive credentials go to the encrypted DB;
    all other fields go to the JSON store.
    Preserves existing values when empty strings are passed.
    """
    mode = (mode or "webhook").strip() or "webhook"

    # Write credentials to encrypted DB (only if non-empty, to preserve existing)
    if client_id.strip():
        set_credential(_SLACK_SERVICE, "client_id", client_id.strip())
    if client_secret.strip():
        set_credential(_SLACK_SERVICE, "client_secret", client_secret.strip())

    with _LOCK:
        data = _read_raw()
        existing = data.get("slack") or {}
        data["slack"] = {
            "enabled": enabled,
            "mode": mode,
            "webhook_url": webhook_url.strip() or existing.get("webhook_url", ""),
            "bot_token": bot_token.strip() or existing.get("bot_token", ""),
            "default_channel": default_channel.strip(),
            "channel_display_name": channel_display_name.strip(),
            "notify_open_questions": notify_open_questions,
            "notify_pa_responses": notify_pa_responses,
            # Preserve OAuth fields unless explicitly provided
            "team_id": team_id or existing.get("team_id", ""),
            "team_name": team_name or existing.get("team_name", ""),
            "bot_user_id": bot_user_id or existing.get("bot_user_id", ""),
        }
        _write_raw(data)


def set_slack_oauth_token(
    bot_token: str,
    team_id: str,
    team_name: str,
    bot_user_id: str,
    default_channel: str = "",
) -> None:
    """
    Store the result of a successful Slack OAuth exchange.
    Sets mode='bot', enabled=True, and saves team/user info.
    Preserves existing channel, notification preferences, and app credentials.
    """
    with _LOCK:
        data = _read_raw()
        existing = data.get("slack") or {}
        data["slack"] = {
            "enabled": True,
            "mode": "bot",
            "webhook_url": existing.get("webhook_url", ""),
            "bot_token": bot_token.strip(),
            "default_channel": default_channel.strip() or existing.get("default_channel", ""),
            "channel_display_name": existing.get("channel_display_name", ""),
            "notify_open_questions": bool(existing.get("notify_open_questions", True)),
            "notify_pa_responses": bool(existing.get("notify_pa_responses", True)),
            "team_id": team_id.strip(),
            "team_name": team_name.strip(),
            "bot_user_id": bot_user_id.strip(),
        }
        # Clear used OAuth state
        data.pop("slack_oauth_state", None)
        _write_raw(data)


def clear_slack_oauth() -> None:
    """
    Disconnect Slack OAuth — removes bot token and team info, disables integration.
    Preserves app credentials (client_id, client_secret) in the encrypted DB.
    """
    with _LOCK:
        data = _read_raw()
        existing = data.get("slack") or {}
        data["slack"] = {
            "enabled": False,
            "mode": existing.get("mode", "webhook"),
            "webhook_url": existing.get("webhook_url", ""),
            "bot_token": "",
            "default_channel": existing.get("default_channel", ""),
            "channel_display_name": existing.get("channel_display_name", ""),
            "notify_open_questions": bool(existing.get("notify_open_questions", True)),
            "notify_pa_responses": bool(existing.get("notify_pa_responses", True)),
            "team_id": "",
            "team_name": "",
            "bot_user_id": "",
        }
        data.pop("slack_oauth_state", None)
        _write_raw(data)
    # Note: client_id and client_secret are intentionally preserved in the encrypted DB


def generate_oauth_state() -> str:
    """
    Generate a cryptographically random OAuth state token and persist it.
    Returns the token to embed in the Slack authorize URL.
    Old state (if any) is overwritten.
    """
    state = secrets.token_urlsafe(32)
    now = datetime.now(tz=timezone.utc).isoformat()
    with _LOCK:
        data = _read_raw()
        data["slack_oauth_state"] = {"value": state, "created_at": now}
        _write_raw(data)
    return state


def verify_and_clear_oauth_state(state: str) -> bool:
    """
    Verify the OAuth state token matches what was stored and has not expired.
    Clears the stored state regardless of outcome.
    Returns True if valid, False otherwise.
    """
    with _LOCK:
        data = _read_raw()
        stored = data.pop("slack_oauth_state", None)
        _write_raw(data)

    if not stored or not state:
        return False
    if stored.get("value") != state:
        return False
    try:
        created = datetime.fromisoformat(stored["created_at"])
        age = datetime.now(tz=timezone.utc) - created
        if age > timedelta(seconds=_OAUTH_STATE_TTL_SECONDS):
            return False
    except (KeyError, ValueError):
        return False
    return True


def get_medium_config() -> dict[str, Any]:
    """
    Medium.com integration: blogging stats agent and OAuth identity (Google).
    Session cookies for medium.com are stored encrypted as session_storage_state.
    """
    with _LOCK:
        data = _read_raw()
    medium = data.get("medium") or {}
    session_raw = get_medium_session_storage_state_json()
    return {
        "enabled": bool(medium.get("enabled", False)),
        "oauth_provider": str(medium.get("oauth_provider", "google")).strip() or "google",
        "oauth_identity_connected": bool(medium.get("oauth_identity_connected", False)),
        "linked_email": str(medium.get("linked_email", "")).strip(),
        "linked_name": str(medium.get("linked_name", "")).strip(),
        "google_client_id": get_credential(_MEDIUM_SERVICE, "google_client_id"),
        "google_client_secret": get_credential(_MEDIUM_SERVICE, "google_client_secret"),
        "session_configured": bool(session_raw.strip()) if session_raw else False,
    }


def set_medium_config(
    *,
    enabled: bool,
    oauth_provider: str = "google",
    google_client_id: str = "",
    google_client_secret: str = "",
) -> None:
    """Persist Medium integration flags and provider. OAuth app creds go to encrypted DB when non-empty."""
    oauth_provider = (oauth_provider or "google").strip().lower()
    if oauth_provider not in ("google", "apple", "facebook", "twitter"):
        oauth_provider = "google"

    if google_client_id.strip():
        set_credential(_MEDIUM_SERVICE, "google_client_id", google_client_id.strip())
    if google_client_secret.strip():
        set_credential(_MEDIUM_SERVICE, "google_client_secret", google_client_secret.strip())

    with _LOCK:
        data = _read_raw()
        existing = data.get("medium") or {}
        data["medium"] = {
            "enabled": enabled,
            "oauth_provider": oauth_provider,
            "oauth_identity_connected": bool(existing.get("oauth_identity_connected", False)),
            "linked_email": str(existing.get("linked_email", "")).strip(),
            "linked_name": str(existing.get("linked_name", "")).strip(),
        }
        _write_raw(data)


def set_medium_google_oauth_identity(
    *,
    refresh_token: str,
    linked_email: str,
    linked_name: str = "",
) -> None:
    """Store Google OAuth refresh token and linked profile (after successful OAuth)."""
    if refresh_token.strip():
        set_credential(_MEDIUM_SERVICE, "google_refresh_token", refresh_token.strip())
    with _LOCK:
        data = _read_raw()
        existing = data.get("medium") or {}
        data["medium"] = {
            "enabled": bool(existing.get("enabled", True)),
            "oauth_provider": str(existing.get("oauth_provider", "google")).strip() or "google",
            "oauth_identity_connected": True,
            "linked_email": linked_email.strip(),
            "linked_name": (linked_name or "").strip(),
        }
        data.pop("medium_google_oauth_state", None)
        _write_raw(data)


def set_medium_session_storage_state_json(session_json: str) -> None:
    """Store Playwright storage_state JSON on disk for medium.com (required for the stats agent)."""
    session_json = (session_json or "").strip()
    path = _medium_storage_state_path()
    if not session_json:
        try:
            if path.exists():
                path.unlink()
        except OSError as e:
            logger.warning("Failed to remove Medium session file %s: %s", path, e)
        return
    _write_text_atomic(path, session_json)


def get_medium_session_storage_state_json() -> str:
    """Return Medium storage_state JSON from disk."""
    path = _medium_storage_state_path()
    return _read_text_if_exists(path).strip()


def clear_medium_google_oauth_identity() -> None:
    """Remove Google tokens and linked identity; keeps session and OAuth app credentials."""
    delete_credential(_MEDIUM_SERVICE, "google_refresh_token")
    with _LOCK:
        data = _read_raw()
        existing = data.get("medium") or {}
        data["medium"] = {
            "enabled": bool(existing.get("enabled", False)),
            "oauth_provider": str(existing.get("oauth_provider", "google")).strip() or "google",
            "oauth_identity_connected": False,
            "linked_email": "",
            "linked_name": "",
        }
        data.pop("medium_google_oauth_state", None)
        _write_raw(data)


def clear_medium_session_storage() -> None:
    """Remove stored Playwright session for Medium."""
    path = _medium_storage_state_path()
    try:
        if path.exists():
            path.unlink()
    except OSError as e:
        logger.warning("Failed to remove Medium session file %s: %s", path, e)


def generate_medium_google_oauth_state() -> str:
    """CSRF state for Medium's Google identity OAuth."""
    state = secrets.token_urlsafe(32)
    now = datetime.now(tz=timezone.utc).isoformat()
    with _LOCK:
        data = _read_raw()
        data["medium_google_oauth_state"] = {"value": state, "created_at": now}
        _write_raw(data)
    return state


def verify_and_clear_medium_google_oauth_state(state: str) -> bool:
    """Validate Medium Google OAuth state token and expiry; clear stored state."""
    with _LOCK:
        data = _read_raw()
        stored = data.pop("medium_google_oauth_state", None)
        _write_raw(data)

    if not stored or not state:
        return False
    if stored.get("value") != state:
        return False
    try:
        created = datetime.fromisoformat(stored["created_at"])
        age = datetime.now(tz=timezone.utc) - created
        if age > timedelta(seconds=_OAUTH_STATE_TTL_SECONDS):
            return False
    except (KeyError, ValueError):
        return False
    return True


def get_integrations_list() -> list[dict[str, Any]]:
    """
    Return list of integration entries for GET /api/integrations.
    Each entry: id, type, enabled, channel (no raw credentials).
    """
    with _LOCK:
        data = _read_raw()
    slack = data.get("slack") or {}
    medium = data.get("medium") or {}
    return [
        {
            "id": "slack",
            "type": "slack",
            "enabled": bool(slack.get("enabled", False)),
            "channel": str(slack.get("channel_display_name", "")).strip() or None,
        },
        {
            "id": "medium",
            "type": "medium",
            "enabled": bool(medium.get("enabled", False)),
            "channel": str(medium.get("linked_email", "")).strip()
            or str(medium.get("oauth_provider", "")).strip()
            or None,
        },
    ]
