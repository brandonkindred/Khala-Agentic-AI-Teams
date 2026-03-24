"""
Slack notifier: fire-and-forget posting to Slack.

Supports two modes:
- webhook: Incoming Webhook URL
- bot: Slack Bot token + chat.postMessage default channel

Used to send open questions (software engineering) and PA responses to the configured channel.
Reads config from integrations_store (and SLACK_WEBHOOK_URL env fallback for webhook mode).
All calls are non-blocking; errors are logged only.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)


def _get_slack_config() -> dict[str, Any]:
    """Get Slack config from store (with env fallback). Safe to call from any thread."""
    try:
        from unified_api.integrations_store import get_slack_config

        return get_slack_config()
    except ImportError:
        return {
            "enabled": bool(os.getenv("SLACK_WEBHOOK_URL")),
            "mode": "webhook",
            "webhook_url": os.getenv("SLACK_WEBHOOK_URL", "").strip(),
            "bot_token": "",
            "default_channel": "",
            "channel_display_name": "",
            "notify_open_questions": True,
            "notify_pa_responses": True,
        }


def _get_status_base_url() -> str:
    return os.getenv("UI_BASE_URL", "http://localhost:4200").rstrip("/")


def _post_webhook_sync(url: str, payload: dict[str, Any]) -> None:
    try:
        import urllib.request

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 400:
                logger.warning("Slack webhook returned %s", resp.status)
    except Exception as e:
        logger.warning("Slack webhook post failed: %s", e)


def _post_bot_sync(token: str, channel: str, payload: dict[str, Any]) -> None:
    try:
        from slack_sdk import WebClient

        client = WebClient(token=token)
        response = client.chat_postMessage(
            channel=channel,
            text=str(payload.get("text") or ""),
            blocks=payload.get("blocks"),
        )
        if not bool(response.get("ok", False)):
            logger.warning("Slack bot post failed: %s", response)
    except Exception as e:
        logger.warning("Slack bot post failed: %s", e)


def _run_in_background(target: Any, *args: Any, **kwargs: Any) -> None:
    t = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
    t.start()


def _send_payload(cfg: dict[str, Any], payload: dict[str, Any]) -> None:
    mode = cfg.get("mode", "webhook")
    if mode == "bot":
        token = str(cfg.get("bot_token") or "").strip()
        channel = str(cfg.get("default_channel") or "").strip()
        if not token or not channel:
            return
        _post_bot_sync(token, channel, payload)
        return

    webhook_url = str(cfg.get("webhook_url") or "").strip()
    if not webhook_url:
        return
    _post_webhook_sync(webhook_url, payload)


def _build_open_questions_blocks(
    job_id: str,
    questions: list[dict[str, Any]],
    source: str,
    status_url: str,
) -> list[dict[str, Any]]:
    source_label = {
        "run-team": "Run team",
        "planning-v2": "Planning v2",
        "product-analysis": "Product analysis",
    }.get(source, source)
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Open questions ({source_label})", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Job ID:* `{job_id}`\n*Answer in UI:* <{status_url}|Open questions>",
            },
        },
        {"type": "divider"},
    ]
    for i, q in enumerate(questions[:20], 1):
        q_text = q.get("question_text") or q.get("text") or str(q.get("id", ""))
        context = q.get("context") or ""
        block_text = f"*{i}. {q_text}*"
        if context:
            block_text += f"\n_{context}_"
        options = q.get("options") or []
        if options:
            opt_lines = ["• " + (o.get("id") or o.get("text") or str(o)) for o in options[:10]]
            block_text += "\n" + "\n".join(opt_lines)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": block_text[:2900]},
            }
        )
    if len(questions) > 20:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"_... and {len(questions) - 20} more._"},
            }
        )
    return blocks


def notify_open_questions(
    job_id: str,
    questions: list[dict[str, Any]],
    source: str,
    status_url: str | None = None,
) -> None:
    cfg = _get_slack_config()
    if not cfg.get("enabled") or not bool(cfg.get("notify_open_questions", True)):
        return

    base = _get_status_base_url()
    link = status_url or f"{base}/software-engineering?job={job_id}"
    blocks = _build_open_questions_blocks(job_id, questions, source, link)
    payload = {"text": f"Open questions ({source}): job {job_id}", "blocks": blocks}

    def _send() -> None:
        _send_payload(cfg, payload)

    _run_in_background(_send)


def notify_pa_response(
    user_id: str,
    user_message: str,
    response_message: str,
    actions_taken: list[str] | None = None,
    follow_ups: list[str] | None = None,
) -> None:
    cfg = _get_slack_config()
    if not cfg.get("enabled") or not bool(cfg.get("notify_pa_responses", True)):
        return

    text = f"*User ({user_id}):* {user_message[:500]}\n*Assistant:* {response_message[:1500]}"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Personal Assistant", "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]
    if actions_taken:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Actions: " + ", ".join(actions_taken[:10])},
            }
        )
    if follow_ups:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Follow-ups: " + ", ".join(follow_ups[:5])},
            }
        )
    payload = {"text": "Personal Assistant reply", "blocks": blocks}

    def _send() -> None:
        _send_payload(cfg, payload)

    _run_in_background(_send)
