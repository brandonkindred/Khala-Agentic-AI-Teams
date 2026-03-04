"""
Integrations API: configure and list integrations (e.g. Slack).

Endpoints:
- GET  /api/integrations       -> list integrations (id, type, enabled, channel)
- GET  /api/integrations/slack -> Slack config detail (optionally mask webhook)
- PUT  /api/integrations/slack -> save Slack config (validate URL; optional probe)
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from unified_api.integrations_store import (
    get_integrations_list,
    get_slack_config,
    set_slack_config,
)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

# --- Pydantic models ---


class SlackConfigUpdate(BaseModel):
    """Request body for PUT /api/integrations/slack."""

    enabled: bool = Field(False, description="Whether Slack integration is enabled.")
    webhook_url: str = Field("", description="Slack Incoming Webhook URL (https://hooks.slack.com/...).")
    channel_display_name: str = Field("", description="Optional channel label for display (e.g. #engineering).")


class SlackConfigResponse(BaseModel):
    """Response for GET /api/integrations/slack."""

    enabled: bool
    webhook_url: Optional[str] = None  # None or masked when not exposing full URL
    webhook_configured: bool = Field(description="True if a non-empty webhook URL is stored or set via env.")
    channel_display_name: str = ""


class IntegrationListItem(BaseModel):
    """Single item in GET /api/integrations list."""

    id: str
    type: str
    enabled: bool
    channel: Optional[str] = None


def _validate_webhook_url(url: str) -> None:
    """Raise HTTPException if URL is not a valid Slack webhook URL."""
    if not url or not url.strip():
        return
    u = url.strip()
    if not u.startswith("https://hooks.slack.com/"):
        raise HTTPException(
            status_code=400,
            detail="webhook_url must start with https://hooks.slack.com/",
        )
    if len(u) < 50:
        raise HTTPException(status_code=400, detail="webhook_url appears invalid or incomplete.")


def _optional_probe_webhook(url: str) -> bool:
    """Send a test message to the webhook; return True on success. Log and return False on failure."""
    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            url,
            data=_json.dumps({"text": "Slack integration test from Strands Agents."}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


@router.get("", response_model=List[IntegrationListItem])
async def list_integrations() -> List[IntegrationListItem]:
    """List configured integrations (id, type, enabled, channel). No sensitive data."""
    raw = get_integrations_list()
    return [IntegrationListItem(**item) for item in raw]


@router.get("/slack", response_model=SlackConfigResponse)
async def get_slack() -> SlackConfigResponse:
    """Get Slack integration config. webhook_url is masked (only webhook_configured is exposed)."""
    cfg = get_slack_config()
    return SlackConfigResponse(
        enabled=cfg["enabled"],
        webhook_url=None,
        webhook_configured=bool(cfg.get("webhook_url")),
        channel_display_name=cfg.get("channel_display_name") or "",
    )


@router.put("/slack", response_model=SlackConfigResponse)
async def update_slack(body: SlackConfigUpdate) -> SlackConfigResponse:
    """
    Save Slack integration config.
    Validates webhook URL format. Optionally send a probe message to verify the webhook.
    """
    webhook_url = (body.webhook_url or "").strip()
    if body.enabled and webhook_url:
        _validate_webhook_url(webhook_url)
        # Optional probe: we could add a query param ?probe=true later; for now just validate format.
    set_slack_config(
        enabled=body.enabled,
        webhook_url=webhook_url,
        channel_display_name=(body.channel_display_name or "").strip(),
    )
    cfg = get_slack_config()
    return SlackConfigResponse(
        enabled=cfg["enabled"],
        webhook_url=None,
        webhook_configured=bool(cfg.get("webhook_url")),
        channel_display_name=cfg.get("channel_display_name") or "",
    )
