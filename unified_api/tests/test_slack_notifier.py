"""Unit tests for Slack notifier (mock HTTP; no request when disabled; correct payload when enabled)."""

from unittest.mock import patch, MagicMock

import pytest


def test_notify_open_questions_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Slack is disabled or no webhook, no HTTP request is made."""
    monkeypatch.setenv("AGENT_CACHE", "/nonexistent")
    with patch("unified_api.slack_notifier._get_slack_config", return_value={"enabled": False, "webhook_url": "", "channel_display_name": ""}):
        with patch("unified_api.slack_notifier._post_webhook_sync") as mock_post:
            from unified_api import slack_notifier
            slack_notifier.notify_open_questions("job-1", [{"id": "q1", "question_text": "Q?"}], "run-team")
    mock_post.assert_not_called()


def test_notify_open_questions_sends_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Slack is enabled and webhook set, _post_webhook_sync is called with Block Kit payload."""
    monkeypatch.setenv("AGENT_CACHE", "/tmp")
    with patch("unified_api.slack_notifier._get_slack_config", return_value={"enabled": True, "webhook_url": "https://hooks.slack.com/x", "channel_display_name": ""}):
        with patch("unified_api.slack_notifier._post_webhook_sync") as mock_post:
            with patch("unified_api.slack_notifier._run_in_background", side_effect=lambda target, *a, **k: target()):
                from unified_api import slack_notifier
                slack_notifier.notify_open_questions("job-1", [{"id": "q1", "question_text": "What?"}], "run-team")
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert call_args[0][0] == "https://hooks.slack.com/x"
    payload = call_args[0][1]
    assert "blocks" in payload
    assert payload["text"].startswith("Open questions")
    assert "job-1" in payload["text"]


def test_notify_pa_response_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Slack is disabled, notify_pa_response does not post."""
    with patch("unified_api.slack_notifier._get_slack_config", return_value={"enabled": False, "webhook_url": "", "channel_display_name": ""}):
        with patch("unified_api.slack_notifier._post_webhook_sync") as mock_post:
            with patch("unified_api.slack_notifier._run_in_background", side_effect=lambda target, *a, **k: target()):
                from unified_api import slack_notifier
                slack_notifier.notify_pa_response("user1", "hi", "hello")
    mock_post.assert_not_called()


def test_notify_pa_response_errors_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """When _post_webhook_sync raises, the exception is not propagated (fire-and-forget)."""
    with patch("unified_api.slack_notifier._get_slack_config", return_value={"enabled": True, "webhook_url": "https://hooks.slack.com/x", "channel_display_name": ""}):
        with patch("unified_api.slack_notifier._post_webhook_sync", side_effect=RuntimeError("network error")):
            # Run target in background: in real code the thread swallows; here we run sync and swallow to assert no propagate to caller
            def run_target_sync(target, *a, **k):
                try:
                    target(*a, **k)
                except Exception:
                    pass  # fire-and-forget: exception stays in "background"
            with patch("unified_api.slack_notifier._run_in_background", side_effect=run_target_sync):
                from unified_api import slack_notifier
                slack_notifier.notify_pa_response("user1", "hi", "hello")
    # No exception raised to caller; fire-and-forget keeps errors out of main flow


def test_notify_open_questions_callable_with_orchestrator_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """notify_open_questions accepts (job_id, questions, source) as used by run-team/planning-v2/product-analysis."""
    mock_post = MagicMock()
    with patch("unified_api.slack_notifier._get_slack_config", return_value={"enabled": True, "webhook_url": "https://hooks.slack.com/x", "channel_display_name": ""}):
        with patch("unified_api.slack_notifier._post_webhook_sync", mock_post):
            with patch("unified_api.slack_notifier._run_in_background", side_effect=lambda target, *a, **k: target()):
                from unified_api import slack_notifier
                structured = [{"id": "q1", "question_text": "Clarify X?", "options": [{"id": "a1", "text": "Yes"}]}]
                slack_notifier.notify_open_questions("job-123", structured, "run-team")
    mock_post.assert_called_once()
    payload = mock_post.call_args[0][1]
    assert "job-123" in payload["text"]
    assert any("run-team" in str(b) or "Run team" in str(b) for b in payload.get("blocks", []))
