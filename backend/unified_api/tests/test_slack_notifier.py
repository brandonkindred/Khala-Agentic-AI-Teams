"""Unit tests for Slack notifier (mode routing, skips, and payload generation)."""

import sys
import threading
from unittest.mock import MagicMock, patch

from unified_api import slack_notifier


def test_notify_open_questions_skipped_when_disabled() -> None:
    with (
        patch(
            "unified_api.slack_notifier._get_slack_config",
            return_value={"enabled": False, "webhook_url": "", "channel_display_name": ""},
        ),
        patch("unified_api.slack_notifier._send_payload") as mock_send,
    ):
        slack_notifier.notify_open_questions("job-1", [{"id": "q1", "question_text": "Q?"}], "run-team")
    mock_send.assert_not_called()


def test_notify_open_questions_sends_when_enabled() -> None:
    with (
        patch(
            "unified_api.slack_notifier._get_slack_config",
            return_value={
                "enabled": True,
                "mode": "webhook",
                "webhook_url": "https://hooks.slack.com/x",
                "notify_open_questions": True,
            },
        ),
        patch("unified_api.slack_notifier._send_payload") as mock_send,
        patch("unified_api.slack_notifier._run_in_background", side_effect=lambda target, *a, **k: target()),
    ):
        slack_notifier.notify_open_questions("job-1", [{"id": "q1", "question_text": "What?"}], "run-team")
    mock_send.assert_called_once()


def test_notify_pa_response_skipped_when_toggle_off() -> None:
    with (
        patch(
            "unified_api.slack_notifier._get_slack_config", return_value={"enabled": True, "notify_pa_responses": False}
        ),
        patch("unified_api.slack_notifier._send_payload") as mock_send,
        patch("unified_api.slack_notifier._run_in_background", side_effect=lambda target, *a, **k: target()),
    ):
        slack_notifier.notify_pa_response("user1", "hi", "hello")
    mock_send.assert_not_called()


def test_send_payload_uses_bot_mode() -> None:
    cfg = {"mode": "bot", "bot_token": "xoxb-FAKE-opaque-bot", "default_channel": "#alerts"}
    payload = {"text": "hello", "blocks": []}
    with patch("unified_api.slack_notifier._post_bot_sync") as mock_bot:
        slack_notifier._send_payload(cfg, payload)
    mock_bot.assert_called_once_with("xoxb-FAKE-opaque-bot", "#alerts", payload)


def test_notify_open_questions_callable_with_orchestrator_signature() -> None:
    mock_send = MagicMock()
    with (
        patch(
            "unified_api.slack_notifier._get_slack_config",
            return_value={
                "enabled": True,
                "mode": "webhook",
                "webhook_url": "https://hooks.slack.com/x",
                "notify_open_questions": True,
            },
        ),
        patch("unified_api.slack_notifier._send_payload", mock_send),
        patch("unified_api.slack_notifier._run_in_background", side_effect=lambda target, *a, **k: target()),
    ):
        structured = [{"id": "q1", "question_text": "Clarify X?", "options": [{"id": "a1", "text": "Yes"}]}]
        slack_notifier.notify_open_questions("job-123", structured, "run-team")
    mock_send.assert_called_once()


# ---------------------------------------------------------------------------
# _get_slack_config: ImportError fallback to env-based webhook config
# ---------------------------------------------------------------------------


def test_get_slack_config_falls_back_to_env_when_store_unimportable(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/env-fallback")
    with patch.dict(sys.modules, {"unified_api.integrations_store": None}):
        cfg = slack_notifier._get_slack_config()
    assert cfg["enabled"] is True
    assert cfg["mode"] == "webhook"
    assert cfg["webhook_url"] == "https://hooks.slack.com/services/env-fallback"


def test_get_slack_config_env_fallback_disabled_without_webhook_url(monkeypatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    with patch.dict(sys.modules, {"unified_api.integrations_store": None}):
        cfg = slack_notifier._get_slack_config()
    assert cfg["enabled"] is False
    assert cfg["webhook_url"] == ""


# ---------------------------------------------------------------------------
# _post_webhook_sync
# ---------------------------------------------------------------------------


def test_post_webhook_sync_success() -> None:
    resp = MagicMock()
    resp.status = 200
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=resp) as mock_urlopen:
        slack_notifier._post_webhook_sync("https://hooks.slack.com/x", {"text": "hi"})
    mock_urlopen.assert_called_once()


def test_post_webhook_sync_logs_non_2xx_status() -> None:
    resp = MagicMock()
    resp.status = 500
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    with (
        patch("urllib.request.urlopen", return_value=resp),
        patch.object(slack_notifier.logger, "warning") as mock_warn,
    ):
        slack_notifier._post_webhook_sync("https://hooks.slack.com/x", {"text": "hi"})
    mock_warn.assert_called_once()


def test_post_webhook_sync_swallows_exceptions() -> None:
    with patch("urllib.request.urlopen", side_effect=OSError("network down")):
        slack_notifier._post_webhook_sync("https://hooks.slack.com/x", {"text": "hi"})  # must not raise


# ---------------------------------------------------------------------------
# _post_bot_sync
# ---------------------------------------------------------------------------


def test_post_bot_sync_success() -> None:
    mock_client = MagicMock()
    mock_client.chat_postMessage.return_value = {"ok": True}
    with patch("slack_sdk.WebClient", return_value=mock_client):
        slack_notifier._post_bot_sync("xoxb-fake", "#alerts", {"text": "hi", "blocks": [{"type": "section"}]})
    mock_client.chat_postMessage.assert_called_once_with(channel="#alerts", text="hi", blocks=[{"type": "section"}])


def test_post_bot_sync_logs_when_not_ok() -> None:
    mock_client = MagicMock()
    mock_client.chat_postMessage.return_value = {"ok": False, "error": "channel_not_found"}
    with (
        patch("slack_sdk.WebClient", return_value=mock_client),
        patch.object(slack_notifier.logger, "warning") as mock_warn,
    ):
        slack_notifier._post_bot_sync("xoxb-fake", "#alerts", {"text": "hi"})
    mock_warn.assert_called_once()


def test_post_bot_sync_swallows_exceptions() -> None:
    with patch("slack_sdk.WebClient", side_effect=RuntimeError("bad token")):
        slack_notifier._post_bot_sync("bad-token", "#alerts", {"text": "hi"})  # must not raise


# ---------------------------------------------------------------------------
# _run_in_background
# ---------------------------------------------------------------------------


def test_run_in_background_invokes_target_with_args_on_a_thread() -> None:
    called = {}
    done = threading.Event()

    def _target(a, b, kw=None):
        called["args"] = (a, b, kw)
        done.set()

    slack_notifier._run_in_background(_target, 1, 2, kw="three")
    assert done.wait(timeout=2)
    assert called["args"] == (1, 2, "three")


# ---------------------------------------------------------------------------
# _send_payload: mode routing edge cases
# ---------------------------------------------------------------------------


def test_send_payload_bot_mode_skips_when_token_missing() -> None:
    cfg = {"mode": "bot", "bot_token": "", "default_channel": "#alerts"}
    with patch("unified_api.slack_notifier._post_bot_sync") as mock_bot:
        slack_notifier._send_payload(cfg, {"text": "hi"})
    mock_bot.assert_not_called()


def test_send_payload_bot_mode_skips_when_channel_missing() -> None:
    cfg = {"mode": "bot", "bot_token": "xoxb-fake", "default_channel": ""}
    with patch("unified_api.slack_notifier._post_bot_sync") as mock_bot:
        slack_notifier._send_payload(cfg, {"text": "hi"})
    mock_bot.assert_not_called()


def test_send_payload_webhook_mode_skips_when_url_missing() -> None:
    cfg = {"mode": "webhook", "webhook_url": ""}
    with patch("unified_api.slack_notifier._post_webhook_sync") as mock_hook:
        slack_notifier._send_payload(cfg, {"text": "hi"})
    mock_hook.assert_not_called()


def test_send_payload_webhook_mode_posts_when_url_present() -> None:
    cfg = {"mode": "webhook", "webhook_url": "https://hooks.slack.com/x"}
    payload = {"text": "hi"}
    with patch("unified_api.slack_notifier._post_webhook_sync") as mock_hook:
        slack_notifier._send_payload(cfg, payload)
    mock_hook.assert_called_once_with("https://hooks.slack.com/x", payload)


# ---------------------------------------------------------------------------
# _build_open_questions_blocks: context text + truncation branches
# ---------------------------------------------------------------------------


def test_build_open_questions_blocks_includes_context_text() -> None:
    questions = [{"id": "q1", "question_text": "Pick one", "context": "Extra detail here"}]
    blocks = slack_notifier._build_open_questions_blocks("job-1", questions, "run-team", "http://x")
    section = next(b for b in blocks if b.get("type") == "section" and b["text"]["text"].startswith("*1."))
    assert "Extra detail here" in section["text"]["text"]


def test_build_open_questions_blocks_truncates_beyond_twenty() -> None:
    questions = [{"id": f"q{i}", "question_text": f"Q{i}"} for i in range(25)]
    blocks = slack_notifier._build_open_questions_blocks("job-1", questions, "run-team", "http://x")
    assert any("and 5 more" in b.get("text", {}).get("text", "") for b in blocks)


# ---------------------------------------------------------------------------
# post_threaded_message
# ---------------------------------------------------------------------------


def test_post_threaded_message_noop_without_token_or_channel() -> None:
    with patch("slack_sdk.WebClient") as mock_cls:
        slack_notifier.post_threaded_message("", "C001", None, "hi")
        slack_notifier.post_threaded_message("tok", "", None, "hi")
    mock_cls.assert_not_called()


def test_post_threaded_message_success_with_blocks_and_thread() -> None:
    mock_client = MagicMock()
    mock_client.chat_postMessage.return_value = {"ok": True}
    with patch("slack_sdk.WebClient", return_value=mock_client):
        slack_notifier.post_threaded_message("tok", "C001", "123.456", "hi", blocks=[{"type": "section"}])
    mock_client.chat_postMessage.assert_called_once_with(
        channel="C001", text="hi", blocks=[{"type": "section"}], thread_ts="123.456"
    )


def test_post_threaded_message_logs_when_not_ok() -> None:
    mock_client = MagicMock()
    mock_client.chat_postMessage.return_value = {"ok": False}
    with (
        patch("slack_sdk.WebClient", return_value=mock_client),
        patch.object(slack_notifier.logger, "warning") as mock_warn,
    ):
        slack_notifier.post_threaded_message("tok", "C001", None, "hi")
    mock_warn.assert_called_once()


def test_post_threaded_message_swallows_exceptions() -> None:
    with patch("slack_sdk.WebClient", side_effect=RuntimeError("boom")):
        slack_notifier.post_threaded_message("tok", "C001", None, "hi")  # must not raise


# ---------------------------------------------------------------------------
# notify_pa_response: actions_taken / follow_ups branches, real send path
# ---------------------------------------------------------------------------


def test_notify_pa_response_includes_actions_and_follow_ups() -> None:
    mock_send = MagicMock()
    with (
        patch(
            "unified_api.slack_notifier._get_slack_config",
            return_value={
                "enabled": True,
                "mode": "webhook",
                "webhook_url": "https://hooks.slack.com/x",
                "notify_pa_responses": True,
            },
        ),
        patch("unified_api.slack_notifier._send_payload", mock_send),
        patch("unified_api.slack_notifier._run_in_background", side_effect=lambda target, *a, **k: target()),
    ):
        slack_notifier.notify_pa_response(
            "user1",
            "hi",
            "hello there",
            actions_taken=["did a thing", "did another"],
            follow_ups=["follow up 1"],
        )
    mock_send.assert_called_once()
    _cfg, payload = mock_send.call_args.args
    blocks_text = [b["text"]["text"] for b in payload["blocks"] if "text" in b]
    assert any("did a thing" in t for t in blocks_text)
    assert any("follow up 1" in t for t in blocks_text)
