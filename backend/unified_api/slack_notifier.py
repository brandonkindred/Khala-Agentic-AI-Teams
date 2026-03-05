"""
Slack notifier: fire-and-forget posting to Slack Incoming Webhook.

Used to send open questions (software engineering) and PA responses to the configured channel.
Reads config from integrations_store (and SLACK_WEBHOOK_URL env fallback).
All calls are non-blocking; errors are logged only.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get_slack_config() -> Dict[str, Any]:
    """Get Slack config from store (with env fallback). Safe to call from any thread."""
    try:
        from unified_api.integrations_store import get_slack_config
        return get_slack_config()
    except ImportError:
        return {
            "enabled": bool(os.getenv("SLACK_WEBHOOK_URL")),
            "webhook_url": os.getenv("SLACK_WEBHOOK_URL", "").strip(),
            "channel_display_name": "",
        }


def _get_status_base_url() -> str:
    """Base URL for UI (e.g. for open-question links). From env or default."""
    return os.getenv("UI_BASE_URL", "http://localhost:4200").rstrip("/")


def _post_webhook_sync(url: str, payload: Dict[str, Any]) -> None:
    """POST JSON to webhook URL. Log errors; do not raise."""
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


def _run_in_background(target: Any, *args: Any, **kwargs: Any) -> None:
    """Run target in a daemon thread. Fire-and-forget."""
    t = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
    t.start()


def _build_open_questions_blocks(
    job_id: str,
    questions: List[Dict[str, Any]],
    source: str,
    status_url: str,
) -> List[Dict[str, Any]]:
    """Build Block Kit blocks for open questions message."""
    source_label = {
        "run-team": "Run team",
        "planning-v2": "Planning v2",
        "product-analysis": "Product analysis",
    }.get(source, source)
    blocks: List[Dict[str, Any]] = [
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
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": block_text[:2900]},
        })
    if len(questions) > 20:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"_... and {len(questions) - 20} more._"},
        })
    return blocks


def notify_open_questions(
    job_id: str,
    questions: List[Dict[str, Any]],
    source: str,
    status_url: Optional[str] = None,
) -> None:
    """
    Post open questions to Slack (fire-and-forget).
    source: one of run-team, planning-v2, product-analysis.
    status_url: full URL to job status / answer page; if None, built from UI_BASE_URL + path.
    """
    cfg = _get_slack_config()
    if not cfg.get("enabled") or not cfg.get("webhook_url"):
        return
    url = cfg["webhook_url"]
    base = _get_status_base_url()
    link = status_url or f"{base}/software-engineering?job={job_id}"
    blocks = _build_open_questions_blocks(job_id, questions, source, link)
    payload = {"text": f"Open questions ({source}): job {job_id}", "blocks": blocks}

    def _send() -> None:
        _post_webhook_sync(url, payload)

    _run_in_background(_send)


def notify_pa_response(
    user_id: str,
    user_message: str,
    response_message: str,
    actions_taken: Optional[List[str]] = None,
    follow_ups: Optional[List[str]] = None,
) -> None:
    """Post Personal Assistant request/response to Slack (fire-and-forget)."""
    cfg = _get_slack_config()
    if not cfg.get("enabled") or not cfg.get("webhook_url"):
        return
    url = cfg["webhook_url"]
    text = f"*User ({user_id}):* {user_message[:500]}\n*Assistant:* {response_message[:1500]}"
    blocks: List[Dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Personal Assistant", "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]
    if actions_taken:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "Actions: " + ", ".join(actions_taken[:10])},
        })
    if follow_ups:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "Follow-ups: " + ", ".join(follow_ups[:5])},
        })
    payload = {"text": "Personal Assistant reply", "blocks": blocks}

    def _send() -> None:
        _post_webhook_sync(url, payload)

    _run_in_background(_send)
