"""
Read Medium.com integration config for the blogging stats agent.

Uses the same AGENT_CACHE layout as the unified API (integrations.json + encrypted DB).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _backend_root() -> Path:
    # shared/ -> blogging/ -> agents/ -> backend/
    return Path(__file__).resolve().parent.parent.parent.parent


def _ensure_backend_on_path() -> None:
    root = str(_backend_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def resolve_medium_stats_storage_state() -> Tuple[Optional[Dict[str, Any]], str, str]:
    """
    Load Playwright storage_state for medium.com from the platform integration.

    Returns:
        (storage_state_dict, account_hint, error_message)
        On success error_message is "".
    """
    _ensure_backend_on_path()
    try:
        from unified_api.integrations_store import (
            get_medium_config,
            get_medium_session_storage_state_json,
            set_medium_session_storage_state_json,
        )
    except ImportError as e:
        logger.warning("Medium integration modules unavailable: %s", e)
        return None, "", (
            "Medium integration is not available (run the Unified API from the backend repo "
            "so unified_api is on PYTHONPATH)."
        )

    cfg = get_medium_config()
    if not cfg.get("enabled"):
        return None, "", "Medium.com integration is disabled. Enable it under Integrations."

    raw = get_medium_session_storage_state_json()
    if not (raw and raw.strip()):
        from unified_api.google_browser_login_credentials import (
            get_google_browser_login_credentials,
        )
        from unified_api.medium_browser_login import perform_medium_google_browser_login

        em, pw = get_google_browser_login_credentials()
        if em and pw:
            try:
                state = perform_medium_google_browser_login(em, pw)
                set_medium_session_storage_state_json(json.dumps(state, separators=(",", ":")))
                raw = get_medium_session_storage_state_json()
            except Exception as e:
                logger.warning("Medium auto browser login failed: %s", e)
                return (
                    None,
                    "",
                    f"No Medium browser session and automated login failed: {e}. "
                    "Save credentials under Integrations or POST /api/integrations/medium/session/browser-login.",
                )
        else:
            return (
                None,
                "",
                "No Medium browser session. Save shared Google sign-in credentials in Postgres "
                "(PUT /api/integrations/google-browser-login with POSTGRES_HOST set), "
                "then run session capture or Medium stats.",
            )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, "", f"Stored Medium session is invalid JSON: {e}"

    if not isinstance(data, dict):
        return None, "", "Stored Medium session must be a JSON object (Playwright storage_state)."

    hint = ""
    email = str(cfg.get("linked_email") or "").strip()
    if "@" in email:
        hint = email.split("@", 1)[1]

    return data, hint, ""


def medium_stats_integration_eligible() -> Tuple[bool, str]:
    """True if the stats agent is allowed to run (integration enabled + auth requirements met)."""
    state, _, err = resolve_medium_stats_storage_state()
    if err:
        return False, err
    return bool(state), ""
