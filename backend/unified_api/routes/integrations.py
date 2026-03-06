"""
Integrations API: configure and list integrations (e.g. Slack).

Endpoints:
- GET  /api/integrations       -> list integrations (id, type, enabled, channel)
- GET  /api/integrations/slack -> Slack config detail (sensitive values masked)
- PUT  /api/integrations/slack -> save Slack config for webhook or bot mode
"""

from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from unified_api.integrations_store import (
    get_integrations_list,
    get_slack_config,
    set_slack_config,
)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


class SlackConfigUpdate(BaseModel):
    """Request body for PUT /api/integrations/slack."""

    enabled: bool = Field(False, description="Whether Slack integration is enabled.")
    mode: Literal["webhook", "bot"] = Field(
        "webhook",
        description="Slack delivery mode: webhook (incoming webhook URL) or bot (bot token + default channel).",
    )
    webhook_url: str = Field("", description="Slack Incoming Webhook URL (https://hooks.slack.com/...).")
    bot_token: str = Field("", description="Slack bot token (xoxb-...) used with chat.postMessage.")
    default_channel: str = Field("", description="Default target channel for bot mode (e.g. #eng or C123...).")
    channel_display_name: str = Field("", description="Optional channel label for display (e.g. #engineering).")
    notify_open_questions: bool = Field(True, description="Post open questions to Slack.")
    notify_pa_responses: bool = Field(True, description="Post Personal Assistant responses to Slack.")


class SlackConfigResponse(BaseModel):
    """Response for GET /api/integrations/slack."""

    enabled: bool
    mode: Literal["webhook", "bot"] = "webhook"
    webhook_url: Optional[str] = None
    webhook_configured: bool = Field(description="True if webhook URL is available (stored or env fallback).")
    bot_token_configured: bool = Field(False, description="True if a bot token is configured.")
    default_channel: str = ""
    channel_display_name: str = ""
    notify_open_questions: bool = True
    notify_pa_responses: bool = True


class IntegrationListItem(BaseModel):
    """Single item in GET /api/integrations list."""

    id: str
    type: str
    enabled: bool
    channel: Optional[str] = None


def _validate_webhook_url(url: str) -> None:
    if not url or not url.strip():
        return
    u = url.strip()
    if not u.startswith("https://hooks.slack.com/"):
        raise HTTPException(status_code=400, detail="webhook_url must start with https://hooks.slack.com/")
    if len(u) < 50:
        raise HTTPException(status_code=400, detail="webhook_url appears invalid or incomplete.")


def _validate_bot_token(token: str) -> None:
    if not token:
        raise HTTPException(status_code=400, detail="bot_token is required when mode=bot and Slack is enabled.")
    if not token.startswith("xoxb-"):
        raise HTTPException(status_code=400, detail="bot_token must start with xoxb-")


@router.get("", response_model=List[IntegrationListItem])
async def list_integrations() -> List[IntegrationListItem]:
    raw = get_integrations_list()
    return [IntegrationListItem(**item) for item in raw]


@router.get("/slack", response_model=SlackConfigResponse)
async def get_slack() -> SlackConfigResponse:
    cfg = get_slack_config()
    return SlackConfigResponse(
        enabled=cfg["enabled"],
        mode=cfg.get("mode", "webhook"),
        webhook_url=None,
        webhook_configured=bool(cfg.get("webhook_url")),
        bot_token_configured=bool(cfg.get("bot_token")),
        default_channel=cfg.get("default_channel") or "",
        channel_display_name=cfg.get("channel_display_name") or "",
        notify_open_questions=bool(cfg.get("notify_open_questions", True)),
        notify_pa_responses=bool(cfg.get("notify_pa_responses", True)),
    )


@router.put("/slack", response_model=SlackConfigResponse)
async def update_slack(body: SlackConfigUpdate) -> SlackConfigResponse:
    webhook_url = (body.webhook_url or "").strip()
    bot_token = (body.bot_token or "").strip()
    default_channel = (body.default_channel or "").strip()

    if body.enabled:
        if body.mode == "webhook":
            _validate_webhook_url(webhook_url)
            if not webhook_url:
                raise HTTPException(status_code=400, detail="webhook_url is required when mode=webhook and Slack is enabled.")
        else:
            _validate_bot_token(bot_token)
            if not default_channel:
                raise HTTPException(status_code=400, detail="default_channel is required when mode=bot and Slack is enabled.")

    set_slack_config(
        enabled=body.enabled,
        mode=body.mode,
        webhook_url=webhook_url,
        bot_token=bot_token,
        default_channel=default_channel,
        channel_display_name=(body.channel_display_name or "").strip(),
        notify_open_questions=body.notify_open_questions,
        notify_pa_responses=body.notify_pa_responses,
    )
    cfg = get_slack_config()
    return SlackConfigResponse(
        enabled=cfg["enabled"],
        mode=cfg.get("mode", "webhook"),
        webhook_url=None,
        webhook_configured=bool(cfg.get("webhook_url")),
        bot_token_configured=bool(cfg.get("bot_token")),
        default_channel=cfg.get("default_channel") or "",
        channel_display_name=cfg.get("channel_display_name") or "",
        notify_open_questions=bool(cfg.get("notify_open_questions", True)),
        notify_pa_responses=bool(cfg.get("notify_pa_responses", True)),
    )
