"""
Integrations API: configure and list integrations (e.g. Slack).

Endpoints:
- GET    /api/integrations               -> list integrations (id, type, enabled, channel)
- GET    /api/integrations/slack         -> Slack config detail (sensitive values masked)
- PUT    /api/integrations/slack         -> save Slack config (credentials, webhook, bot settings)
- GET    /api/integrations/slack/oauth/connect   -> return Slack OAuth authorization URL
- GET    /api/integrations/slack/oauth/callback  -> handle Slack OAuth redirect, store token
- DELETE /api/integrations/slack/oauth   -> disconnect Slack OAuth (clear token)
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from unified_api.integrations_store import (
    clear_slack_oauth,
    generate_oauth_state,
    get_integrations_list,
    get_slack_config,
    set_slack_config,
    set_slack_oauth_token,
    verify_and_clear_oauth_state,
)

import os

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

# Slack OAuth v2 constants
_SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
_SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
# Minimum scopes: post messages (chat:write) + post to public channels without
# needing to join them first (chat:write.public) + list channels (channels:read)
_SLACK_SCOPES = "chat:write,chat:write.public,channels:read"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SlackConfigUpdate(BaseModel):
    """Request body for PUT /api/integrations/slack."""

    enabled: bool = Field(False, description="Whether Slack integration is enabled.")
    mode: Literal["webhook", "bot"] = Field(
        "webhook",
        description="Slack delivery mode: webhook (incoming webhook URL) or bot (bot token + default channel).",
    )
    client_id: str = Field("", description="Slack app Client ID (required for OAuth).")
    client_secret: str = Field("", description="Slack app Client Secret (required for OAuth).")
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
    client_id_configured: bool = Field(False, description="True if a Slack app Client ID is stored.")
    webhook_url: Optional[str] = None
    webhook_configured: bool = Field(description="True if webhook URL is stored.")
    bot_token_configured: bool = Field(False, description="True if a bot token is configured.")
    default_channel: str = ""
    channel_display_name: str = ""
    notify_open_questions: bool = True
    notify_pa_responses: bool = True
    # OAuth connection info
    oauth_connected: bool = Field(False, description="True when a bot token was obtained via OAuth.")
    team_name: Optional[str] = Field(None, description="Slack workspace name (populated after OAuth).")
    team_id: Optional[str] = Field(None, description="Slack team/workspace ID.")


class SlackOAuthConnectResponse(BaseModel):
    """Response for GET /api/integrations/slack/oauth/connect."""

    url: str = Field(description="Slack OAuth v2 authorization URL. Open this in a browser to start the flow.")
    client_id: str = Field(description="Slack app client ID embedded in the URL (for reference).")


class IntegrationListItem(BaseModel):
    """Single item in GET /api/integrations list."""

    id: str
    type: str
    enabled: bool
    channel: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_slack_config_response(cfg: dict) -> SlackConfigResponse:
    return SlackConfigResponse(
        enabled=cfg["enabled"],
        mode=cfg.get("mode", "webhook"),
        client_id_configured=bool(cfg.get("client_id")),
        webhook_url=None,  # never expose raw URL
        webhook_configured=bool(cfg.get("webhook_url")),
        bot_token_configured=bool(cfg.get("bot_token")),
        default_channel=cfg.get("default_channel") or "",
        channel_display_name=cfg.get("channel_display_name") or "",
        notify_open_questions=bool(cfg.get("notify_open_questions", True)),
        notify_pa_responses=bool(cfg.get("notify_pa_responses", True)),
        oauth_connected=bool(cfg.get("team_id")),
        team_name=cfg.get("team_name") or None,
        team_id=cfg.get("team_id") or None,
    )


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


def _get_ui_base_url() -> str:
    return os.getenv("UI_BASE_URL", "http://localhost:4200").rstrip("/")


def _get_redirect_uri(request: Request) -> str:
    """
    Return the OAuth redirect URI.
    Prefer SLACK_REDIRECT_URI env var (required in production behind a proxy/load-balancer).
    Falls back to deriving from the incoming request's base URL.
    """
    env_uri = os.getenv("SLACK_REDIRECT_URI", "").strip()
    if env_uri:
        return env_uri
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/integrations/slack/oauth/callback"


def _exchange_code(code: str, redirect_uri: str, client_id: str, client_secret: str) -> dict:
    """Exchange an OAuth authorization code for a bot token via Slack's API."""
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(
        _SLACK_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=List[IntegrationListItem])
async def list_integrations() -> List[IntegrationListItem]:
    raw = get_integrations_list()
    return [IntegrationListItem(**item) for item in raw]


@router.get("/slack", response_model=SlackConfigResponse)
async def get_slack() -> SlackConfigResponse:
    return _build_slack_config_response(get_slack_config())


@router.put("/slack", response_model=SlackConfigResponse)
async def update_slack(body: SlackConfigUpdate) -> SlackConfigResponse:
    webhook_url = (body.webhook_url or "").strip()
    bot_token = (body.bot_token or "").strip()
    default_channel = (body.default_channel or "").strip()

    if body.enabled:
        if body.mode == "webhook":
            _validate_webhook_url(webhook_url)
            if not webhook_url:
                # Allow if already configured in store
                cfg = get_slack_config()
                if not cfg.get("webhook_url"):
                    raise HTTPException(
                        status_code=400,
                        detail="webhook_url is required when mode=webhook and Slack is enabled.",
                    )
        else:
            if bot_token:
                _validate_bot_token(bot_token)
            elif not get_slack_config().get("bot_token"):
                raise HTTPException(
                    status_code=400,
                    detail="bot_token is required when mode=bot and Slack is enabled.",
                )
            if not default_channel:
                raise HTTPException(
                    status_code=400,
                    detail="default_channel is required when mode=bot and Slack is enabled.",
                )

    set_slack_config(
        enabled=body.enabled,
        mode=body.mode,
        client_id=(body.client_id or "").strip(),
        client_secret=(body.client_secret or "").strip(),
        webhook_url=webhook_url,
        bot_token=bot_token,
        default_channel=default_channel,
        channel_display_name=(body.channel_display_name or "").strip(),
        notify_open_questions=body.notify_open_questions,
        notify_pa_responses=body.notify_pa_responses,
    )
    return _build_slack_config_response(get_slack_config())


# ---------------------------------------------------------------------------
# Slack OAuth v2
# ---------------------------------------------------------------------------


@router.get("/slack/oauth/connect", response_model=SlackOAuthConnectResponse)
async def slack_oauth_connect(request: Request) -> SlackOAuthConnectResponse:
    """
    Return the Slack OAuth v2 authorization URL.

    Requires Client ID and Client Secret to be saved via PUT /api/integrations/slack first.
    """
    cfg = get_slack_config()
    client_id = cfg.get("client_id", "").strip()
    client_secret = cfg.get("client_secret", "").strip()

    if not client_id:
        raise HTTPException(
            status_code=400,
            detail="Slack Client ID is not configured. Enter it in the integrations settings first.",
        )
    if not client_secret:
        raise HTTPException(
            status_code=400,
            detail="Slack Client Secret is not configured. Enter it in the integrations settings first.",
        )

    state = generate_oauth_state()
    redirect_uri = _get_redirect_uri(request)

    params = urllib.parse.urlencode({
        "client_id": client_id,
        "scope": _SLACK_SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
    })
    url = f"{_SLACK_AUTHORIZE_URL}?{params}"

    return SlackOAuthConnectResponse(url=url, client_id=client_id)


@router.get("/slack/oauth/callback")
async def slack_oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
) -> RedirectResponse:
    """
    Handle the OAuth redirect from Slack after the user authorizes the app.

    Slack calls this endpoint with ?code=...&state=... on success or
    ?error=access_denied on cancellation.

    On success: exchanges the code for a bot token and redirects to the UI.
    On failure: redirects to the UI with an error query parameter.
    """
    ui_base = _get_ui_base_url()
    integrations_ui = f"{ui_base}/integrations"

    # User cancelled or Slack returned an error
    if error:
        logger.warning("Slack OAuth error returned: %s", error)
        return RedirectResponse(url=f"{integrations_ui}?slack_error={urllib.parse.quote(error)}")

    if not code or not state:
        return RedirectResponse(url=f"{integrations_ui}?slack_error=missing_code_or_state")

    # Verify CSRF state
    if not verify_and_clear_oauth_state(state):
        logger.warning("Slack OAuth state mismatch or expired")
        return RedirectResponse(url=f"{integrations_ui}?slack_error=invalid_state")

    # Load credentials from store
    cfg = get_slack_config()
    client_id = cfg.get("client_id", "").strip()
    client_secret = cfg.get("client_secret", "").strip()
    if not client_id or not client_secret:
        logger.error("Slack OAuth callback: client credentials missing from store")
        return RedirectResponse(url=f"{integrations_ui}?slack_error=missing_credentials")

    # Exchange code for token
    try:
        redirect_uri = _get_redirect_uri(request)
        result = _exchange_code(code, redirect_uri, client_id, client_secret)
    except Exception as exc:
        logger.error("Slack OAuth token exchange failed: %s", exc)
        return RedirectResponse(url=f"{integrations_ui}?slack_error=token_exchange_failed")

    if not result.get("ok"):
        slack_err = result.get("error", "unknown_error")
        logger.error("Slack OAuth exchange returned error: %s", slack_err)
        return RedirectResponse(url=f"{integrations_ui}?slack_error={urllib.parse.quote(slack_err)}")

    bot_token = result.get("access_token", "")
    team = result.get("team") or {}
    team_id = team.get("id", "")
    team_name = team.get("name", "")
    bot_user_id = result.get("bot_user_id", "")

    set_slack_oauth_token(
        bot_token=bot_token,
        team_id=team_id,
        team_name=team_name,
        bot_user_id=bot_user_id,
    )

    logger.info("Slack OAuth complete: team=%s (%s), bot_user=%s", team_name, team_id, bot_user_id)
    team_param = urllib.parse.quote(team_name or team_id)
    return RedirectResponse(url=f"{integrations_ui}?slack_connected=1&team={team_param}")


@router.delete("/slack/oauth", response_model=SlackConfigResponse)
async def slack_oauth_disconnect() -> SlackConfigResponse:
    """
    Disconnect Slack OAuth — removes the stored bot token, team info, and disables the integration.
    Preserves app credentials (client_id, client_secret).
    Does not revoke the token at Slack's end (the user can do that via Slack's app management).
    """
    clear_slack_oauth()
    return _build_slack_config_response(get_slack_config())
