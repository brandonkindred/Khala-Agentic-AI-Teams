"""Unit tests for Slack notifier (mode routing, skips, and payload generation)."""

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
    cfg = {"mode": "bot", "bot_token": "xoxb-test", "default_channel": "#alerts"}
    payload = {"text": "hello", "blocks": []}
    with patch("unified_api.slack_notifier._post_bot_sync") as mock_bot:
        slack_notifier._send_payload(cfg, payload)
    mock_bot.assert_called_once_with("xoxb-test", "#alerts", payload)


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
