"""Unit tests for Slack events handler."""

import hashlib
import hmac
import sys
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


# ---------------------------------------------------------------------------
# _build_team_registry
# ---------------------------------------------------------------------------


def test_build_team_registry_from_configs() -> None:
    fake_team_configs = {
        "blogging": MagicMock(enabled=True, name="Blogging", prefix="/api/blogging", description="Blog"),
        "disabled_team": MagicMock(enabled=False, name="Disabled", prefix="/api/disabled", description="No"),
    }
    fake_assistant_configs = {"blogging": object(), "disabled_team": object()}
    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", {}),
        patch.dict(
            sys.modules,
            {
                "unified_api.config": MagicMock(TEAM_CONFIGS=fake_team_configs),
                "team_assistant.config": MagicMock(TEAM_ASSISTANT_CONFIGS=fake_assistant_configs),
            },
        ),
    ):
        registry = slack_events_handler._build_team_registry()
    assert "blogging" in registry
    assert "disabled_team" not in registry  # enabled=False is excluded


def test_build_team_registry_returns_cached_value_without_rebuilding() -> None:
    cached = {"cached_team": {"name": "Cached", "prefix": "/x", "description": "d"}}
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", cached):
        assert slack_events_handler._build_team_registry() == cached


def test_build_team_registry_handles_missing_team_assistant_config() -> None:
    """When team_assistant.config isn't importable, every enabled team is included."""
    fake_team_configs = {
        "blogging": MagicMock(enabled=True, name="Blogging", prefix="/api/blogging", description="Blog"),
    }
    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", {}),
        patch.dict(sys.modules, {"unified_api.config": MagicMock(TEAM_CONFIGS=fake_team_configs)}),
        patch.dict(sys.modules, {"team_assistant.config": None}),
    ):
        registry = slack_events_handler._build_team_registry()
    assert "blogging" in registry


def test_build_team_registry_returns_empty_on_config_import_error() -> None:
    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", {}),
        patch.dict(sys.modules, {"unified_api.config": None}),
    ):
        registry = slack_events_handler._build_team_registry()
    assert registry == {}


# ---------------------------------------------------------------------------
# verify_slack_request: exception path
# ---------------------------------------------------------------------------


def test_verify_slack_request_swallows_verifier_exceptions() -> None:
    ts = str(int(time.time()))
    with patch("slack_sdk.signature.SignatureVerifier", side_effect=RuntimeError("boom")):
        assert slack_events_handler.verify_slack_request("secret", b"body", ts, "v0=abc") is False


# ---------------------------------------------------------------------------
# _normalize_team_key: display-name and partial-match branches
# ---------------------------------------------------------------------------


def test_normalize_team_key_display_name_with_internal_space() -> None:
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY):
        # "Market Research" (raw, case-insensitive, spaces intact) matches info["name"].lower()
        assert slack_events_handler._normalize_team_key("market research") == "market_research"


def test_normalize_team_key_partial_substring_match() -> None:
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY):
        assert slack_events_handler._normalize_team_key("blog") == "blogging"


def test_normalize_team_key_no_match_returns_none() -> None:
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY):
        assert slack_events_handler._normalize_team_key("totally-unknown-xyz") is None


# ---------------------------------------------------------------------------
# _get_bot_token / _get_bot_user_id
# ---------------------------------------------------------------------------


def test_get_bot_token_reads_from_store() -> None:
    with patch("unified_api.integrations_store.get_slack_config", return_value={"bot_token": " xoxb-abc "}):
        assert slack_events_handler._get_bot_token() == "xoxb-abc"


def test_get_bot_token_returns_empty_on_import_error() -> None:
    with patch.dict(sys.modules, {"unified_api.integrations_store": None}):
        assert slack_events_handler._get_bot_token() == ""


def test_get_bot_user_id_reads_from_store() -> None:
    with patch("unified_api.integrations_store.get_slack_config", return_value={"bot_user_id": " U123 "}):
        assert slack_events_handler._get_bot_user_id() == "U123"


def test_get_bot_user_id_returns_empty_on_import_error() -> None:
    with patch.dict(sys.modules, {"unified_api.integrations_store": None}):
        assert slack_events_handler._get_bot_user_id() == ""


# ---------------------------------------------------------------------------
# _call_team_assistant: ASGI success, ASGI failure -> HTTP fallback, both fail
# ---------------------------------------------------------------------------


def test_call_team_assistant_asgi_success() -> None:
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"messages": [{"role": "assistant", "content": "hi"}]}
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.post.return_value = mock_resp
    with patch("httpx.Client", return_value=mock_client):
        result = slack_events_handler._call_team_assistant("/api/blogging", "conv-1", "hello")
    assert result == {"messages": [{"role": "assistant", "content": "hi"}]}


def test_call_team_assistant_asgi_non_200_returns_none_without_http_fallback() -> None:
    """A non-200 ASGI response is a definitive answer (not a transport failure) —
    the function returns None directly rather than retrying over HTTP."""
    asgi_resp = MagicMock(status_code=500, text="err")
    asgi_client = MagicMock()
    asgi_client.__enter__.return_value = asgi_client
    asgi_client.__exit__.return_value = False
    asgi_client.post.return_value = asgi_resp

    with patch("httpx.Client", return_value=asgi_client) as mock_ctor:
        result = slack_events_handler._call_team_assistant("/api/blogging", None, "hello")
    assert result is None
    mock_ctor.assert_called_once()  # HTTP fallback never attempted


def test_call_team_assistant_asgi_raises_falls_back_to_http_success() -> None:
    http_resp = MagicMock(status_code=200)
    http_resp.json.return_value = {"ok": True}
    http_client = MagicMock()
    http_client.__enter__.return_value = http_client
    http_client.__exit__.return_value = False
    http_client.post.return_value = http_resp

    with patch("httpx.Client", side_effect=[RuntimeError("no asgi"), http_client]):
        result = slack_events_handler._call_team_assistant("/api/blogging", None, "hello")
    assert result == {"ok": True}


def test_call_team_assistant_both_transports_fail_returns_none() -> None:
    with patch("httpx.Client", side_effect=RuntimeError("no transport at all")):
        result = slack_events_handler._call_team_assistant("/api/blogging", None, "hello")
    assert result is None


def test_call_team_assistant_http_fallback_non_200_returns_none() -> None:
    http_resp = MagicMock(status_code=503, text="down")
    http_client = MagicMock()
    http_client.__enter__.return_value = http_client
    http_client.__exit__.return_value = False
    http_client.post.return_value = http_resp
    with patch("httpx.Client", side_effect=[RuntimeError("no asgi"), http_client]):
        result = slack_events_handler._call_team_assistant("/api/blogging", None, "hello")
    assert result is None


# ---------------------------------------------------------------------------
# _create_conversation: same ASGI/HTTP fallback shape
# ---------------------------------------------------------------------------


def test_create_conversation_asgi_success() -> None:
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"conversation_id": "conv-1"}
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.post.return_value = mock_resp
    with patch("httpx.Client", return_value=mock_client):
        assert slack_events_handler._create_conversation("/api/blogging") == "conv-1"


def test_create_conversation_falls_back_to_http() -> None:
    http_resp = MagicMock(status_code=200)
    http_resp.json.return_value = {"conversation_id": "conv-http"}
    http_client = MagicMock()
    http_client.__enter__.return_value = http_client
    http_client.__exit__.return_value = False
    http_client.post.return_value = http_resp
    with patch("httpx.Client", side_effect=[RuntimeError("no asgi"), http_client]):
        assert slack_events_handler._create_conversation("/api/blogging") == "conv-http"


def test_create_conversation_both_fail_returns_none() -> None:
    with patch("httpx.Client", side_effect=RuntimeError("no transport")):
        assert slack_events_handler._create_conversation("/api/blogging") is None


# ---------------------------------------------------------------------------
# _post_slack_message
# ---------------------------------------------------------------------------


def test_post_slack_message_success() -> None:
    mock_client = MagicMock()
    mock_client.chat_postMessage.return_value = {"ok": True}
    with patch("slack_sdk.WebClient", return_value=mock_client):
        slack_events_handler._post_slack_message("tok", "C001", "hi", blocks=[{"type": "section"}], thread_ts="1.1")
    mock_client.chat_postMessage.assert_called_once_with(
        channel="C001", text="hi", blocks=[{"type": "section"}], thread_ts="1.1"
    )


def test_post_slack_message_logs_when_not_ok() -> None:
    mock_client = MagicMock()
    mock_client.chat_postMessage.return_value = {"ok": False}
    with (
        patch("slack_sdk.WebClient", return_value=mock_client),
        patch.object(slack_events_handler.logger, "warning") as mock_warn,
    ):
        slack_events_handler._post_slack_message("tok", "C001", "hi")
    mock_warn.assert_called_once()


def test_post_slack_message_swallows_exceptions() -> None:
    with patch("slack_sdk.WebClient", side_effect=RuntimeError("boom")):
        slack_events_handler._post_slack_message("tok", "C001", "hi")  # must not raise


# ---------------------------------------------------------------------------
# process_slack_message: full flow branches
# ---------------------------------------------------------------------------


def test_process_slack_message_no_user_id_returns_early() -> None:
    with patch.object(slack_events_handler, "_get_bot_token") as mock_token:
        slack_events_handler.process_slack_message({"text": "hi", "channel": "C1"})
    mock_token.assert_not_called()


def test_process_slack_message_no_bot_token_returns_early() -> None:
    with (
        patch.object(slack_events_handler, "_get_bot_token", return_value=""),
        patch.object(slack_events_handler, "_get_bot_user_id", return_value=""),
        patch("unified_api.slack_events_handler._post_slack_message") as mock_post,
    ):
        slack_events_handler.process_slack_message({"user": "U1", "text": "hi", "channel": "C1"})
    mock_post.assert_not_called()


def test_process_slack_message_empty_text_after_strip_returns_early() -> None:
    with (
        patch.object(slack_events_handler, "_get_bot_token", return_value="tok"),
        patch.object(slack_events_handler, "_get_bot_user_id", return_value="BOT"),
        patch("unified_api.slack_events_handler._post_slack_message") as mock_post,
    ):
        slack_events_handler.process_slack_message({"user": "U1", "text": "<@BOT>", "channel": "C1"})
    mock_post.assert_not_called()


def test_process_slack_message_team_switch_posts_confirmation() -> None:
    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY),
        patch.object(slack_events_handler, "_get_bot_token", return_value="tok"),
        patch.object(slack_events_handler, "_get_bot_user_id", return_value=""),
        patch("unified_api.slack_user_state.set_user_team") as mock_set,
        patch("unified_api.slack_events_handler._post_slack_message") as mock_post,
    ):
        slack_events_handler.process_slack_message(
            {"user": "U1", "text": "switch to blogging", "channel": "C1", "ts": "1.0"}
        )
    mock_set.assert_called_once_with("U1", "blogging")
    assert "Blogging" in mock_post.call_args.args[2]


def test_process_slack_message_unknown_current_team_defaults_to_personal_assistant() -> None:
    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY),
        patch.object(slack_events_handler, "_get_bot_token", return_value="tok"),
        patch.object(slack_events_handler, "_get_bot_user_id", return_value=""),
        patch("unified_api.slack_user_state.get_user_team", return_value="not_a_real_team"),
        patch("unified_api.slack_user_state.set_user_team") as mock_set,
        patch("unified_api.slack_user_state.get_conversation_id", return_value=None),
        patch("unified_api.slack_user_state.set_conversation_id"),
        patch.object(slack_events_handler, "_create_conversation", return_value="conv-1"),
        patch.object(slack_events_handler, "_call_team_assistant", return_value=None),
        patch("unified_api.slack_events_handler._post_slack_message") as mock_post,
    ):
        slack_events_handler.process_slack_message({"user": "U1", "text": "hello there", "channel": "C1"})
    mock_set.assert_called_once_with("U1", "personal_assistant")
    assert "didn't respond" in mock_post.call_args.args[2]


def test_process_slack_message_no_team_info_posts_fallback() -> None:
    with (
        patch.object(slack_events_handler, "get_available_teams", return_value={}),
        patch.object(slack_events_handler, "_get_bot_token", return_value="tok"),
        patch.object(slack_events_handler, "_get_bot_user_id", return_value=""),
        patch("unified_api.slack_user_state.get_user_team", return_value="personal_assistant"),
        patch("unified_api.slack_user_state.set_user_team"),
        patch("unified_api.slack_events_handler._post_slack_message") as mock_post,
    ):
        slack_events_handler.process_slack_message({"user": "U1", "text": "hello", "channel": "C1"})
    assert "No team assistant available" in mock_post.call_args.args[2]


def test_process_slack_message_full_success_flow_with_reply_and_suggestions() -> None:
    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY),
        patch.object(slack_events_handler, "_get_bot_token", return_value="tok"),
        patch.object(slack_events_handler, "_get_bot_user_id", return_value=""),
        patch("unified_api.slack_user_state.get_user_team", return_value="blogging"),
        patch("unified_api.slack_user_state.get_conversation_id", return_value="existing-conv"),
        patch("unified_api.slack_user_state.set_conversation_id") as mock_set_conv,
        patch.object(
            slack_events_handler,
            "_call_team_assistant",
            return_value={
                "conversation_id": "new-conv",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "final reply"},
                ],
                "suggested_questions": ["Q1?"],
            },
        ),
        patch("unified_api.slack_events_handler._post_slack_message") as mock_post,
    ):
        slack_events_handler.process_slack_message({"user": "U1", "text": "hi there", "channel": "C1"})
    # A different conversation_id came back -> stored.
    mock_set_conv.assert_called_once_with("U1", "blogging", "new-conv")
    assert mock_post.call_args.args[2] == "final reply"


def test_process_slack_message_no_assistant_reply_uses_placeholder() -> None:
    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY),
        patch.object(slack_events_handler, "_get_bot_token", return_value="tok"),
        patch.object(slack_events_handler, "_get_bot_user_id", return_value=""),
        patch("unified_api.slack_user_state.get_user_team", return_value="blogging"),
        patch("unified_api.slack_user_state.get_conversation_id", return_value="conv-1"),
        patch.object(slack_events_handler, "_call_team_assistant", return_value={"messages": []}),
        patch("unified_api.slack_events_handler._post_slack_message") as mock_post,
    ):
        slack_events_handler.process_slack_message({"user": "U1", "text": "hi", "channel": "C1"})
    assert mock_post.call_args.args[2] == "I processed your request but have no text response."


def test_process_slack_message_creates_conversation_when_missing() -> None:
    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY),
        patch.object(slack_events_handler, "_get_bot_token", return_value="tok"),
        patch.object(slack_events_handler, "_get_bot_user_id", return_value=""),
        patch("unified_api.slack_user_state.get_user_team", return_value="blogging"),
        patch("unified_api.slack_user_state.get_conversation_id", return_value=None),
        patch.object(slack_events_handler, "_create_conversation", return_value="brand-new-conv") as mock_create,
        patch("unified_api.slack_user_state.set_conversation_id") as mock_set_conv,
        patch.object(slack_events_handler, "_call_team_assistant", return_value={"messages": []}),
        patch("unified_api.slack_events_handler._post_slack_message"),
    ):
        slack_events_handler.process_slack_message({"user": "U1", "text": "hi", "channel": "C1"})
    mock_create.assert_called_once_with("/api/blogging")
    mock_set_conv.assert_called_once_with("U1", "blogging", "brand-new-conv")


# ---------------------------------------------------------------------------
# process_slash_command: "team" bare word treated as team-list
# ---------------------------------------------------------------------------


def test_slash_command_team_bareword_shows_list() -> None:
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY):
        result = slack_events_handler.process_slash_command({"text": "team", "user_id": "U001"})
    assert "Available Teams" in result["text"]


def test_slash_command_team_list_explicit_suffix() -> None:
    with patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY):
        result = slack_events_handler.process_slash_command({"text": "team list", "user_id": "U001"})
    assert "Available Teams" in result["text"]


# ---------------------------------------------------------------------------
# process_slash_command: background message path (thread run synchronously)
# ---------------------------------------------------------------------------


def _run_thread_synchronously(*, target, daemon=True):
    class _SyncThread:
        def start(self):
            target()

    return _SyncThread()


def test_slash_command_message_success_posts_to_response_url() -> None:
    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY),
        patch("unified_api.slack_user_state.get_user_team", return_value="blogging"),
        patch("unified_api.slack_user_state.get_conversation_id", return_value="conv-1"),
        patch.object(
            slack_events_handler,
            "_call_team_assistant",
            return_value={
                "messages": [{"role": "assistant", "content": "reply text"}],
                "suggested_questions": [],
            },
        ),
        patch("threading.Thread", side_effect=_run_thread_synchronously),
        patch.object(slack_events_handler, "_post_to_response_url") as mock_post_url,
    ):
        result = slack_events_handler.process_slash_command(
            {"text": "hello world", "user_id": "U001", "response_url": "https://hooks.slack.com/resp"}
        )
    assert "Processing with" in result["text"]
    mock_post_url.assert_called_once()
    assert mock_post_url.call_args.args[1] == "reply text"


def test_slash_command_message_no_result_posts_error() -> None:
    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY),
        patch("unified_api.slack_user_state.get_user_team", return_value="blogging"),
        patch("unified_api.slack_user_state.get_conversation_id", return_value="conv-1"),
        patch.object(slack_events_handler, "_call_team_assistant", return_value=None),
        patch("threading.Thread", side_effect=_run_thread_synchronously),
        patch.object(slack_events_handler, "_post_to_response_url") as mock_post_url,
    ):
        slack_events_handler.process_slash_command(
            {"text": "hello world", "user_id": "U001", "response_url": "https://hooks.slack.com/resp"}
        )
    assert "didn't respond" in mock_post_url.call_args.args[1]


def test_slash_command_message_unknown_team_registry_falls_back_and_no_team_info() -> None:
    with (
        patch.object(slack_events_handler, "get_available_teams", return_value={}),
        patch("unified_api.slack_user_state.get_user_team", return_value="personal_assistant"),
        patch("unified_api.slack_user_state.set_user_team"),
        patch("threading.Thread", side_effect=_run_thread_synchronously),
        patch.object(slack_events_handler, "_post_to_response_url") as mock_post_url,
    ):
        slack_events_handler.process_slash_command(
            {"text": "hello world", "user_id": "U001", "response_url": "https://hooks.slack.com/resp"}
        )
    assert "No team assistant available" in mock_post_url.call_args.args[1]


def test_slash_command_message_creates_conversation_when_missing() -> None:
    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY),
        patch("unified_api.slack_user_state.get_user_team", return_value="blogging"),
        patch("unified_api.slack_user_state.get_conversation_id", return_value=None),
        patch.object(slack_events_handler, "_create_conversation", return_value="new-conv") as mock_create,
        patch("unified_api.slack_user_state.set_conversation_id") as mock_set_conv,
        patch.object(slack_events_handler, "_call_team_assistant", return_value={"messages": []}),
        patch("threading.Thread", side_effect=_run_thread_synchronously),
        patch.object(slack_events_handler, "_post_to_response_url"),
    ):
        slack_events_handler.process_slash_command(
            {"text": "hello world", "user_id": "U001", "response_url": "https://hooks.slack.com/resp"}
        )
    mock_create.assert_called_once_with("/api/blogging")
    mock_set_conv.assert_called_once_with("U001", "blogging", "new-conv")


def test_slash_command_message_background_exception_posts_error() -> None:
    # Only the FIRST get_user_team() call (inside the background closure) fails;
    # the synchronous call after thread.start() (building the immediate ack
    # response) must still succeed, matching real threading where the
    # background closure's exception can't propagate out of process_slash_command.
    calls = {"n": 0}

    def _flaky_get_user_team(_slack_user_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return "blogging"

    with (
        patch.object(slack_events_handler, "_TEAM_REGISTRY", _MOCK_REGISTRY),
        patch("unified_api.slack_user_state.get_user_team", side_effect=_flaky_get_user_team),
        patch("threading.Thread", side_effect=_run_thread_synchronously),
        patch.object(slack_events_handler, "_post_to_response_url") as mock_post_url,
    ):
        result = slack_events_handler.process_slash_command(
            {"text": "hello world", "user_id": "U001", "response_url": "https://hooks.slack.com/resp"}
        )
    assert "error occurred" in mock_post_url.call_args.args[1]
    assert "Processing with" in result["text"]


def test_slash_command_no_response_url_skips_background_thread() -> None:
    with patch("threading.Thread") as mock_thread:
        result = slack_events_handler.process_slash_command({"text": "hello", "user_id": "U001", "response_url": ""})
    mock_thread.assert_not_called()
    assert "Processing with" in result["text"]


# ---------------------------------------------------------------------------
# _post_to_response_url
# ---------------------------------------------------------------------------


def test_post_to_response_url_noop_when_empty() -> None:
    with patch("urllib.request.urlopen") as mock_urlopen:
        slack_events_handler._post_to_response_url("", "text")
    mock_urlopen.assert_not_called()


def test_post_to_response_url_success() -> None:
    resp = MagicMock()
    resp.status = 200
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=resp) as mock_urlopen:
        slack_events_handler._post_to_response_url(
            "https://hooks.slack.com/resp", "hello", blocks=[{"type": "section"}]
        )
    mock_urlopen.assert_called_once()


def test_post_to_response_url_logs_non_2xx() -> None:
    resp = MagicMock()
    resp.status = 500
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    with (
        patch("urllib.request.urlopen", return_value=resp),
        patch.object(slack_events_handler.logger, "warning") as mock_warn,
    ):
        slack_events_handler._post_to_response_url("https://hooks.slack.com/resp", "hello")
    mock_warn.assert_called_once()


def test_post_to_response_url_swallows_exceptions() -> None:
    with patch("urllib.request.urlopen", side_effect=OSError("network down")):
        slack_events_handler._post_to_response_url("https://hooks.slack.com/resp", "hello")  # must not raise
