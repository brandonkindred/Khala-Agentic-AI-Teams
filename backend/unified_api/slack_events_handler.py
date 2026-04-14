"""
Slack Events API handler: receive messages from Slack users and route them
to the appropriate team assistant, then post the response back to Slack.

Supports:
- URL verification challenge (required for Slack app setup)
- app_mention events (bot @mentioned in a channel)
- message.im events (DMs to the bot)
- /khala slash command (team switching, help, reset, messages)
- Signature verification via HMAC-SHA256

All message processing runs in background threads to meet Slack's 3-second
response deadline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Team registry: maps team_key -> (display_name, prefix, description)
# Built from unified_api.config at import time.
# ---------------------------------------------------------------------------

_TEAM_REGISTRY: dict[str, dict[str, str]] = {}


def _build_team_registry() -> dict[str, dict[str, str]]:
    """Populate team registry from config + team_assistant configs."""
    global _TEAM_REGISTRY
    if _TEAM_REGISTRY:
        return _TEAM_REGISTRY

    try:
        from unified_api.config import TEAM_CONFIGS

        registry: dict[str, dict[str, str]] = {}
        # Only include teams that have a team_assistant config
        try:
            from team_assistant.config import TEAM_ASSISTANT_CONFIGS

            assistant_keys = set(TEAM_ASSISTANT_CONFIGS.keys())
        except ImportError:
            assistant_keys = set(TEAM_CONFIGS.keys())

        for key, cfg in TEAM_CONFIGS.items():
            if key in assistant_keys and cfg.enabled:
                registry[key] = {
                    "name": cfg.name,
                    "prefix": cfg.prefix,
                    "description": cfg.description,
                }

        _TEAM_REGISTRY = registry
    except ImportError:
        logger.warning("Could not import team configs for Slack registry")
    return _TEAM_REGISTRY


def get_available_teams() -> dict[str, dict[str, str]]:
    """Return {team_key: {name, prefix, description}} for all teams with assistants."""
    return _build_team_registry()


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def verify_slack_request(
    signing_secret: str,
    body: bytes,
    timestamp: str,
    signature: str,
) -> bool:
    """
    Verify a Slack request using HMAC-SHA256 signature.

    Args:
        signing_secret: Slack app signing secret.
        body: Raw request body bytes.
        timestamp: Value of X-Slack-Request-Timestamp header.
        signature: Value of X-Slack-Signature header.

    Returns:
        True if signature is valid and request is fresh (< 5 minutes old).
    """
    # Reject stale requests (replay protection)
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    if abs(time.time() - ts) > 300:
        return False

    try:
        from slack_sdk.signature import SignatureVerifier

        verifier = SignatureVerifier(signing_secret)
        return verifier.is_valid(
            body=body.decode("utf-8"),
            timestamp=timestamp,
            signature=signature,
        )
    except Exception:
        logger.exception("Slack signature verification error")
        return False


# ---------------------------------------------------------------------------
# URL verification
# ---------------------------------------------------------------------------


def handle_url_verification(payload: dict[str, Any]) -> dict[str, str]:
    """Handle Slack's url_verification challenge. Returns {"challenge": ...}."""
    return {"challenge": str(payload.get("challenge", ""))}


# ---------------------------------------------------------------------------
# Team switching detection
# ---------------------------------------------------------------------------

# Pattern: "switch to <team>" or "use <team> team"
_SWITCH_PATTERNS = [
    re.compile(r"(?:switch|change|go)\s+to\s+(?:the\s+)?(.+?)(?:\s+team)?$", re.IGNORECASE),
    re.compile(r"use\s+(?:the\s+)?(.+?)\s+team$", re.IGNORECASE),
]


def _normalize_team_key(raw: str) -> str | None:
    """Try to match a user-provided team name to a valid team_key."""
    registry = get_available_teams()
    raw_lower = raw.strip().lower().replace("-", "_").replace(" ", "_")

    # Exact match on key
    if raw_lower in registry:
        return raw_lower

    # Match on display name (case-insensitive)
    for key, info in registry.items():
        if info["name"].lower().replace(" ", "_") == raw_lower:
            return key
        if info["name"].lower() == raw.strip().lower():
            return key

    # Partial/fuzzy: check if raw is a substring of key or name
    for key, info in registry.items():
        if raw_lower in key or raw_lower in info["name"].lower().replace(" ", "_"):
            return key

    return None


def detect_team_switch(text: str) -> str | None:
    """
    If the text is a team-switch command, return the target team_key.
    Returns None if it's not a switch command.
    """
    for pat in _SWITCH_PATTERNS:
        m = pat.search(text.strip())
        if m:
            return _normalize_team_key(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Message processing (runs in background thread)
# ---------------------------------------------------------------------------


def _strip_bot_mention(text: str, bot_user_id: str) -> str:
    """Remove <@BOT_ID> mention prefix from message text."""
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}>\s*", "", text, count=1)
    return text.strip()


def _get_bot_token() -> str:
    """Get bot token from integrations store."""
    try:
        from unified_api.integrations_store import get_slack_config

        cfg = get_slack_config()
        return str(cfg.get("bot_token") or "").strip()
    except ImportError:
        return ""


def _get_bot_user_id() -> str:
    """Get the bot's own Slack user ID from stored config."""
    try:
        from unified_api.integrations_store import get_slack_config

        cfg = get_slack_config()
        return str(cfg.get("bot_user_id") or "").strip()
    except ImportError:
        return ""


def _call_team_assistant(team_prefix: str, conversation_id: str | None, message: str) -> dict[str, Any] | None:
    """
    Call a team assistant's conversation/messages endpoint.

    Uses httpx with ASGI transport to call in-process (no network round-trip).
    Falls back to localhost HTTP if ASGI transport isn't available.
    """
    import httpx

    path = f"{team_prefix}/assistant/conversation/messages"
    params = {}
    if conversation_id:
        params["conversation_id"] = conversation_id

    # Try ASGI in-process transport first
    try:
        from httpx import ASGITransport

        from unified_api.main import app

        with httpx.Client(transport=ASGITransport(app=app), base_url="http://internal") as client:
            resp = client.post(path, params=params, json={"message": message}, timeout=120)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("Team assistant %s returned %s: %s", path, resp.status_code, resp.text[:200])
            return None
    except Exception:
        logger.debug("ASGI transport failed, trying localhost HTTP", exc_info=True)

    # Fallback: HTTP to localhost
    port = os.getenv("UNIFIED_API_PORT", "8080")
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            resp = client.post(path, params=params, json={"message": message}, timeout=120)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("Team assistant HTTP %s returned %s: %s", path, resp.status_code, resp.text[:200])
    except Exception:
        logger.exception("Team assistant HTTP call failed for %s", path)
    return None


def _create_conversation(team_prefix: str) -> str | None:
    """Create a new conversation for a team assistant. Returns conversation_id or None."""
    import httpx

    path = f"{team_prefix}/assistant/conversations"

    try:
        from httpx import ASGITransport

        from unified_api.main import app

        with httpx.Client(transport=ASGITransport(app=app), base_url="http://internal") as client:
            resp = client.post(path, timeout=30)
            if resp.status_code == 200:
                return resp.json().get("conversation_id")
    except Exception:
        logger.debug("ASGI create conversation failed, trying HTTP", exc_info=True)

    port = os.getenv("UNIFIED_API_PORT", "8080")
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            resp = client.post(path, timeout=30)
            if resp.status_code == 200:
                return resp.json().get("conversation_id")
    except Exception:
        logger.exception("Create conversation HTTP call failed for %s", path)
    return None


def _post_slack_message(
    token: str,
    channel: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    thread_ts: str | None = None,
) -> None:
    """Post a message to Slack using the bot token."""
    try:
        from slack_sdk import WebClient

        client = WebClient(token=token)
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        response = client.chat_postMessage(**kwargs)
        if not bool(response.get("ok", False)):
            logger.warning("Slack post failed: %s", response)
    except Exception:
        logger.exception("Failed to post Slack message to %s", channel)


def _build_response_blocks(
    team_name: str,
    reply_text: str,
    suggested_questions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build Block Kit blocks for the assistant response."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"*{team_name}*"}],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": reply_text[:2900]},
        },
    ]
    if suggested_questions:
        hints = "\n".join(f"• {q}" for q in suggested_questions[:5])
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"_Suggestions:_\n{hints}"}],
            }
        )
    return blocks


def process_slack_message(event: dict[str, Any]) -> None:
    """
    Process an incoming Slack message event (app_mention or DM).
    Runs in a background thread.
    """
    from unified_api.slack_user_state import (
        get_conversation_id,
        get_user_team,
        set_conversation_id,
        set_user_team,
    )

    slack_user_id = str(event.get("user", "")).strip()
    if not slack_user_id:
        return

    raw_text = str(event.get("text", "")).strip()
    channel = str(event.get("channel", "")).strip()
    thread_ts = str(event.get("thread_ts") or event.get("ts") or "").strip()

    bot_user_id = _get_bot_user_id()
    bot_token = _get_bot_token()
    if not bot_token:
        logger.warning("No Slack bot token configured; cannot respond to message")
        return

    text = _strip_bot_mention(raw_text, bot_user_id)
    if not text:
        return

    registry = get_available_teams()

    # Check for team-switch command
    target_team = detect_team_switch(text)
    if target_team and target_team in registry:
        set_user_team(slack_user_id, target_team)
        team_info = registry[target_team]
        _post_slack_message(
            bot_token,
            channel,
            f"Switched to *{team_info['name']}* team. {team_info['description']}",
            thread_ts=thread_ts,
        )
        return

    # Get current team
    current_team = get_user_team(slack_user_id)
    if current_team not in registry:
        current_team = "personal_assistant"
        set_user_team(slack_user_id, current_team)

    team_info = registry.get(current_team)
    if not team_info:
        _post_slack_message(bot_token, channel, "No team assistant available.", thread_ts=thread_ts)
        return

    team_prefix = team_info["prefix"]
    team_name = team_info["name"]

    # Get or create conversation
    conv_id = get_conversation_id(slack_user_id, current_team)
    if not conv_id:
        conv_id = _create_conversation(team_prefix)
        if conv_id:
            set_conversation_id(slack_user_id, current_team, conv_id)

    # Call team assistant
    result = _call_team_assistant(team_prefix, conv_id, text)
    if not result:
        _post_slack_message(
            bot_token,
            channel,
            f"Sorry, the {team_name} assistant didn't respond. Please try again.",
            thread_ts=thread_ts,
        )
        return

    # If we got a new conversation_id from the response, store it
    resp_conv_id = result.get("conversation_id")
    if resp_conv_id and resp_conv_id != conv_id:
        set_conversation_id(slack_user_id, current_team, resp_conv_id)

    # Extract the assistant's reply (last assistant message)
    messages = result.get("messages") or []
    reply_text = ""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            reply_text = msg.get("content", "")
            break

    if not reply_text:
        reply_text = "I processed your request but have no text response."

    suggested_questions = result.get("suggested_questions") or []
    blocks = _build_response_blocks(team_name, reply_text, suggested_questions)

    _post_slack_message(
        bot_token,
        channel,
        reply_text[:3000],
        blocks=blocks,
        thread_ts=thread_ts,
    )


# ---------------------------------------------------------------------------
# Slash command processing
# ---------------------------------------------------------------------------


def _build_team_list_text() -> str:
    """Build formatted text listing all available teams."""
    registry = get_available_teams()
    lines = ["*Available Teams:*\n"]
    for key, info in sorted(registry.items(), key=lambda x: x[1]["name"]):
        lines.append(f"• `{key}` — *{info['name']}*: {info['description']}")
    lines.append("\n_Switch with: `/khala team <name>`_")
    return "\n".join(lines)


def _build_help_text() -> str:
    """Build help text for the /khala command."""
    return (
        "*Khala Assistant — Slash Commands*\n\n"
        "• `/khala <message>` — Send a message to your current team's assistant\n"
        "• `/khala team list` — List all available teams\n"
        "• `/khala team <name>` — Switch to a different team\n"
        "• `/khala reset` — Start a fresh conversation with current team\n"
        "• `/khala status` — Show your current team\n"
        "• `/khala help` — Show this help message\n"
    )


def process_slash_command(form_data: dict[str, str]) -> dict[str, Any]:
    """
    Process a /khala slash command.

    Returns an immediate response dict for Slack. For messages that require
    assistant processing, spawns a background thread and posts the result
    to response_url.
    """
    from unified_api.slack_user_state import (
        get_conversation_id,
        get_user_team,
        reset_conversation,
        set_conversation_id,
        set_user_team,
    )

    text = str(form_data.get("text", "")).strip()
    slack_user_id = str(form_data.get("user_id", "")).strip()
    response_url = str(form_data.get("response_url", "")).strip()

    # /khala help
    if not text or text.lower() == "help":
        return {"response_type": "ephemeral", "text": _build_help_text()}

    # /khala team list
    if text.lower() in ("team list", "teams", "team"):
        return {"response_type": "ephemeral", "text": _build_team_list_text()}

    # /khala team <name>
    if text.lower().startswith("team "):
        team_raw = text[5:].strip()
        if team_raw.lower() == "list":
            return {"response_type": "ephemeral", "text": _build_team_list_text()}
        target = _normalize_team_key(team_raw)
        registry = get_available_teams()
        if target and target in registry:
            set_user_team(slack_user_id, target)
            info = registry[target]
            return {
                "response_type": "ephemeral",
                "text": f"Switched to *{info['name']}* team.\n_{info['description']}_",
            }
        return {
            "response_type": "ephemeral",
            "text": f"Unknown team: `{team_raw}`. Use `/khala team list` to see available teams.",
        }

    # /khala reset
    if text.lower() == "reset":
        current_team = get_user_team(slack_user_id)
        reset_conversation(slack_user_id, current_team)
        registry = get_available_teams()
        name = registry.get(current_team, {}).get("name", current_team)
        return {
            "response_type": "ephemeral",
            "text": f"Conversation reset for *{name}*. Your next message starts fresh.",
        }

    # /khala status
    if text.lower() == "status":
        current_team = get_user_team(slack_user_id)
        registry = get_available_teams()
        info = registry.get(current_team, {})
        return {
            "response_type": "ephemeral",
            "text": f"Current team: *{info.get('name', current_team)}*\n_{info.get('description', '')}_",
        }

    # /khala <message> — send to current team assistant in background
    def _process_in_background() -> None:
        try:
            registry = get_available_teams()
            current_team = get_user_team(slack_user_id)
            if current_team not in registry:
                current_team = "personal_assistant"
                set_user_team(slack_user_id, current_team)

            team_info = registry.get(current_team)
            if not team_info:
                _post_to_response_url(response_url, "No team assistant available.")
                return

            team_prefix = team_info["prefix"]
            team_name = team_info["name"]

            conv_id = get_conversation_id(slack_user_id, current_team)
            if not conv_id:
                conv_id = _create_conversation(team_prefix)
                if conv_id:
                    set_conversation_id(slack_user_id, current_team, conv_id)

            result = _call_team_assistant(team_prefix, conv_id, text)
            if not result:
                _post_to_response_url(response_url, f"The {team_name} assistant didn't respond. Please try again.")
                return

            resp_conv_id = result.get("conversation_id")
            if resp_conv_id and resp_conv_id != conv_id:
                set_conversation_id(slack_user_id, current_team, resp_conv_id)

            messages = result.get("messages") or []
            reply_text = ""
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    reply_text = msg.get("content", "")
                    break

            if not reply_text:
                reply_text = "Request processed."

            suggested = result.get("suggested_questions") or []
            blocks = _build_response_blocks(team_name, reply_text, suggested)
            _post_to_response_url(response_url, reply_text[:3000], blocks=blocks)
        except Exception:
            logger.exception("Slash command background processing failed")
            _post_to_response_url(response_url, "An error occurred processing your request.")

    if response_url:
        threading.Thread(target=_process_in_background, daemon=True).start()

    current_team = get_user_team(slack_user_id)
    registry = get_available_teams()
    team_name = registry.get(current_team, {}).get("name", current_team)
    return {"response_type": "ephemeral", "text": f"Processing with *{team_name}*..."}


def _post_to_response_url(
    response_url: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
) -> None:
    """Post a follow-up response to Slack's response_url."""
    if not response_url:
        return
    payload: dict[str, Any] = {"response_type": "ephemeral", "text": text, "replace_original": False}
    if blocks:
        payload["blocks"] = blocks
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            response_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 400:
                logger.warning("Slack response_url returned %s", resp.status)
    except Exception:
        logger.exception("Failed to post to Slack response_url")


# ---------------------------------------------------------------------------
# Event dispatch (called from the route handler)
# ---------------------------------------------------------------------------


def dispatch_event(payload: dict[str, Any]) -> None:
    """
    Dispatch a Slack event_callback payload. Runs in a background thread.

    Handles:
    - app_mention: bot was @mentioned in a channel
    - message (channel_type=im): DM to the bot
    """
    event = payload.get("event") or {}
    event_type = str(event.get("type", "")).strip()

    # Ignore bot's own messages
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    # Ignore message_changed, message_deleted, etc.
    subtype = event.get("subtype")
    if subtype and subtype != "file_share":
        return

    if event_type == "app_mention":
        threading.Thread(target=process_slack_message, args=(event,), daemon=True).start()
    elif event_type == "message":
        channel_type = str(event.get("channel_type", "")).strip()
        if channel_type == "im":
            threading.Thread(target=process_slack_message, args=(event,), daemon=True).start()
