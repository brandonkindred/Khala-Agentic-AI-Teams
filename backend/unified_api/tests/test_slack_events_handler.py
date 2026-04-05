"""Unit tests for Slack events handler."""

import hashlib
import hmac
import time
from unittest.mock import MagicMock, patch

from unified_api import slack_events_handler

# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _make_signature(secret: str, timestamp: str, body: str) -> str:
    """Build a valid Slack v0 signature for testing."""
    sig_basestring = f"v0:{timestamp}:{body}"
    h = hmac.new(secret.encode(), sig_basestring.encode(), hashlib.sha256)
    return f"v0={h.hexdigest()}"


def test_verify_slack_request_valid() -> None:
    secret = "test_signing_secret_abc"
    body = '{"type":"url_verification","challenge":"xyz"}'
    ts = str(int(time.time()))
    sig = _make_signature(secret, ts, body)
    assert slack_events_handler.verify_slack_request(secret, body.encode(), ts, sig) is True


def test_verify_slack_request_invalid_signature() -> None:
    secret = "test_signing_secret_abc"
    body = '{"type":"url_verification","challenge":"xyz"}'
    ts = str(int(time.time()))
    assert slack_events_handler.verify_slack_request(secret, body.encode(), ts, "v0=bad_signature") is False


def test_verify_slack_request_expired_timestamp() -> None:
    secret = "test_signing_secret_abc"
    body = '{"type":"url_verification","challenge":"xyz"}'
    ts = str(int(time.time()) - 600)  # 10 minutes ago
    sig = _make_signature(secret, ts, body)
    assert slack_events_handler.verify_slack_request(secret, body.encode(), ts, sig) is False


def test_verify_slack_request_bad_timestamp() -> None:
    assert slack_events_handler.verify_slack_request("secret", b"body", "not_a_number", "v0=abc") is False


# ---------------------------------------------------------------------------
# URL verification
# ---------------------------------------------------------------------------


def test_handle_url_verification() -> None:
    result = slack_events_handler.handle_url_verification({"challenge": "test_challenge_123"})
    assert result == {"challenge": "test_challenge_123"}


def test_handle_url_verification_empty() -> None:
    result = slack_events_handler.handle_url_verification({})
    assert result == {"challenge": ""}


# ---------------------------------------------------------------------------
# Team switching detection
# ---------------------------------------------------------------------------

_MOCK_REGISTRY = {
    "personal_assistant": {"name": "Personal Assistant", "prefix": "/api/personal-assistant", "description": "PA"},
    "blogging": {"name": "Blogging", "prefix": "/api/blogging", "description": "Blog"},
    "software_engineering": {
        "name": "Software Engineering",
        "prefix": "/api/software-engineering",
        "description": "SE",
    },
    "market_research": {"name": "Market Research", "prefix": "/api/market-research", "description": "MR"},
    "sales_team": {"name": "AI Sales Team", "prefix": "/api/sales", "description": "Sales"},
}


def test_detect_team_switch_exact_key() -> None:
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY):
        assert slack_events_handler.detect_team_switch("switch to blogging") == "blogging"


def test_detect_team_switch_display_name() -> None:
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY):
        assert slack_events_handler.detect_team_switch("switch to Market Research") == "market_research"


def test_detect_team_switch_partial_match() -> None:
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY):
        assert slack_events_handler.detect_team_switch("switch to software engineering team") == "software_engineering"


def test_detect_team_switch_not_a_switch() -> None:
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY):
        assert slack_events_handler.detect_team_switch("help me with my code") is None


def test_detect_team_switch_use_pattern() -> None:
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY):
        assert slack_events_handler.detect_team_switch("use the blogging team") == "blogging"


# ---------------------------------------------------------------------------
# Bot mention stripping
# ---------------------------------------------------------------------------


def test_strip_bot_mention() -> None:
    assert slack_events_handler._strip_bot_mention("<@U123BOT> hello world", "U123BOT") == "hello world"


def test_strip_bot_mention_no_mention() -> None:
    assert slack_events_handler._strip_bot_mention("hello world", "U123BOT") == "hello world"


def test_strip_bot_mention_empty_bot_id() -> None:
    assert slack_events_handler._strip_bot_mention("<@UABC> hello", "") == "<@UABC> hello"


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------


def test_dispatch_event_ignores_bot_messages() -> None:
    payload = {"event": {"type": "message", "bot_id": "B123", "text": "hi"}}
    with patch("unified_api.slack_events_handler.process_slack_message") as mock:
        slack_events_handler.dispatch_event(payload)
    mock.assert_not_called()


def test_dispatch_event_ignores_subtypes() -> None:
    payload = {"event": {"type": "message", "subtype": "message_changed", "text": "hi"}}
    with patch("unified_api.slack_events_handler.process_slack_message") as mock:
        slack_events_handler.dispatch_event(payload)
    mock.assert_not_called()


def test_dispatch_event_handles_app_mention() -> None:
    payload = {"event": {"type": "app_mention", "user": "U001", "text": "<@BOT> hi", "channel": "C001"}}
    with patch("threading.Thread") as mock_thread:
        mock_thread.return_value.start = MagicMock()
        slack_events_handler.dispatch_event(payload)
    mock_thread.assert_called_once()


def test_dispatch_event_handles_dm() -> None:
    payload = {"event": {"type": "message", "channel_type": "im", "user": "U001", "text": "hello", "channel": "D001"}}
    with patch("threading.Thread") as mock_thread:
        mock_thread.return_value.start = MagicMock()
        slack_events_handler.dispatch_event(payload)
    mock_thread.assert_called_once()


def test_dispatch_event_ignores_channel_messages() -> None:
    payload = {"event": {"type": "message", "channel_type": "channel", "user": "U001", "text": "hi"}}
    with patch("threading.Thread") as mock_thread:
        slack_events_handler.dispatch_event(payload)
    mock_thread.assert_not_called()


# ---------------------------------------------------------------------------
# Slash command processing
# ---------------------------------------------------------------------------


def test_slash_command_help() -> None:
    result = slack_events_handler.process_slash_command({"text": "help", "user_id": "U001"})
    assert result["response_type"] == "ephemeral"
    assert "Slash Commands" in result["text"]


def test_slash_command_empty_shows_help() -> None:
    result = slack_events_handler.process_slash_command({"text": "", "user_id": "U001"})
    assert "Slash Commands" in result["text"]


def test_slash_command_team_list() -> None:
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY):
        result = slack_events_handler.process_slash_command({"text": "team list", "user_id": "U001"})
    assert result["response_type"] == "ephemeral"
    assert "Available Teams" in result["text"]


def test_slash_command_team_switch() -> None:
    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY),
        patch("unified_api.slack_user_state.set_user_team") as mock_set,
    ):
        result = slack_events_handler.process_slash_command({"text": "team blogging", "user_id": "U001"})
    mock_set.assert_called_once_with("U001", "blogging")
    assert "Blogging" in result["text"]


def test_slash_command_team_unknown() -> None:
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY):
        result = slack_events_handler.process_slash_command({"text": "team nonexistent", "user_id": "U001"})
    assert "Unknown team" in result["text"]


def test_slash_command_reset() -> None:
    with (
        patch("unified_api.slack_user_state.get_user_team", return_value="blogging"),
        patch("unified_api.slack_user_state.reset_conversation") as mock_reset,
        patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY),
    ):
        result = slack_events_handler.process_slash_command({"text": "reset", "user_id": "U001"})
    mock_reset.assert_called_once_with("U001", "blogging")
    assert "reset" in result["text"].lower()


def test_slash_command_status() -> None:
    with (
        patch("unified_api.slack_user_state.get_user_team", return_value="blogging"),
        patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY),
    ):
        result = slack_events_handler.process_slash_command({"text": "status", "user_id": "U001"})
    assert "Blogging" in result["text"]


# ---------------------------------------------------------------------------
# Response block building
# ---------------------------------------------------------------------------


def test_build_response_blocks_basic() -> None:
    blocks = slack_events_handler._build_response_blocks("Test Team", "Hello there")
    assert len(blocks) == 2
    assert blocks[0]["type"] == "context"
    assert "Test Team" in blocks[0]["elements"][0]["text"]
    assert blocks[1]["type"] == "section"
    assert "Hello there" in blocks[1]["text"]["text"]


def test_build_response_blocks_with_suggestions() -> None:
    blocks = slack_events_handler._build_response_blocks("Team", "Reply", ["Q1?", "Q2?"])
    assert len(blocks) == 4  # context, section, divider, context
    assert blocks[2]["type"] == "divider"
    assert "Q1?" in blocks[3]["elements"][0]["text"]
