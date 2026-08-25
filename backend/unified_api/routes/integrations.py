"""
Integrations API: configure and list integrations (e.g. Slack).

Endpoints:
- GET    /api/integrations               -> list integrations (id, type, enabled, channel)
- GET    /api/integrations/slack         -> Slack config detail (sensitive values masked)
- PUT    /api/integrations/slack         -> save Slack config (credentials, webhook, bot settings)
- GET    /api/integrations/slack/oauth/connect   -> return Slack OAuth authorization URL
- GET    /api/integrations/slack/oauth/callback  -> handle Slack OAuth redirect, store token
- DELETE /api/integrations/slack/oauth   -> disconnect Slack OAuth (clear token)
- GET/PUT/DELETE /api/integrations/google-browser-login -> shared encrypted Gmail/Google credentials (Playwright; Postgres only)
- POST   /api/integrations/medium/session/browser-login -> Playwright Medium+Google (uses shared Google credentials)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import json
import logging
import os
import re
import subprocess
import urllib.parse
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from shared.concurrency import flock_lock
from shared.postgres import bounded_probe
from software_engineering_team.clone_workspace import (
    PER_ISSUE_DIR_TEMPLATE,
    agent_cache_dir,
    clone_lock_path,
)
from software_engineering_team.github_source.client import _pr_detail_from_payload
from unified_api.google_browser_login_credentials import (
    clear_google_browser_login_credentials,
    get_google_browser_login_credentials,
    google_browser_login_credentials_configured,
    google_browser_login_storage_available,
    set_google_browser_login_credentials,
)
from unified_api.integration_credentials import resolve_credential_with_env_fallback
from unified_api.integrations_store import (
    clear_github_config,
    clear_medium_google_oauth_identity,
    clear_medium_session_storage,
    clear_slack_oauth,
    clear_tradingview_config,
    generate_medium_google_oauth_state,
    generate_oauth_state,
    get_github_config,
    get_github_config_meta,
    get_integrations_list,
    get_medium_config,
    get_slack_config,
    get_tradingview_config,
    set_github_config,
    set_medium_config,
    set_medium_google_oauth_identity,
    set_medium_session_storage_state_json,
    set_slack_config,
    set_slack_oauth_token,
    set_tradingview_config,
    verify_and_clear_medium_google_oauth_state,
    verify_and_clear_oauth_state,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

# Slack OAuth v2 constants
_SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
_SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
# Minimum scopes: post messages (chat:write) + post to public channels without
# needing to join them first (chat:write.public) + list channels (channels:read)
_SLACK_SCOPES = (
    "chat:write,chat:write.public,channels:read,app_mentions:read,im:history,im:read,im:write,commands,users:read"
)

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_GOOGLE_SCOPES = "openid email profile"


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
    signing_secret: str = Field(
        "",
        description="Slack app Signing Secret (from Basic Information page — required for receiving events and commands).",
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
    client_id_configured: bool = Field(False, description="True if a Slack app Client ID is stored.")
    signing_secret_configured: bool = Field(
        False, description="True if signing secret is stored (required for events)."
    )
    webhook_url: str | None = None
    webhook_configured: bool = Field(description="True if webhook URL is stored.")
    bot_token_configured: bool = Field(False, description="True if a bot token is configured.")
    default_channel: str = ""
    channel_display_name: str = ""
    notify_open_questions: bool = True
    notify_pa_responses: bool = True
    # OAuth connection info
    oauth_connected: bool = Field(False, description="True when a bot token was obtained via OAuth.")
    team_name: str | None = Field(None, description="Slack workspace name (populated after OAuth).")
    team_id: str | None = Field(None, description="Slack team/workspace ID.")
    # Event subscription URLs (for display — paste these into Slack app config)
    events_url: str = Field("", description="URL for Slack Event Subscriptions Request URL.")
    commands_url: str = Field("", description="URL for Slack Slash Commands Request URL.")


class SlackOAuthConnectResponse(BaseModel):
    """Response for GET /api/integrations/slack/oauth/connect."""

    url: str = Field(description="Slack OAuth v2 authorization URL. Open this in a browser to start the flow.")
    client_id: str = Field(description="Slack app client ID embedded in the URL (for reference).")


class IntegrationListItem(BaseModel):
    """Single item in GET /api/integrations list."""

    id: str
    type: str
    enabled: bool
    channel: str | None = None


MediumOAuthProvider = Literal["google", "apple", "facebook", "twitter"]


class MediumConfigUpdate(BaseModel):
    """Request body for PUT /api/integrations/medium."""

    enabled: bool = Field(False, description="Enable Medium.com integration (blogging stats agent).")
    oauth_provider: MediumOAuthProvider = Field(
        "google",
        description="Identity provider you use on Medium (Google OAuth is supported for platform sign-in).",
    )
    google_client_id: str = Field("", description="Google OAuth client ID (Web application).")
    google_client_secret: str = Field("", description="Google OAuth client secret.")


class MediumConfigResponse(BaseModel):
    """Response for GET /api/integrations/medium."""

    enabled: bool
    oauth_provider: MediumOAuthProvider = "google"
    oauth_identity_connected: bool = Field(False, description="True after Google OAuth completes.")
    google_client_configured: bool = False
    session_configured: bool = Field(False, description="True when Playwright storage_state is stored.")
    linked_email: str | None = None
    linked_name: str | None = None


class MediumGoogleOAuthConnectResponse(BaseModel):
    """Authorization URL for Google (identity link for Medium workflow)."""

    url: str


class MediumSessionImportBody(BaseModel):
    """POST /api/integrations/medium/session — Playwright storage_state object."""

    storage_state: dict[str, Any] = Field(..., description="Full object from Playwright context.storage_state()")


class GoogleBrowserLoginCredentialsBody(BaseModel):
    """PUT /api/integrations/google-browser-login — shared encrypted Gmail/Google credentials."""

    email: str = Field(..., description="Google account email (e.g. Gmail) for browser-based sign-in flows.")
    password: str = Field(..., description="Account password (never returned by GET).")


class GoogleBrowserLoginStatusResponse(BaseModel):
    """GET/PUT/DELETE /api/integrations/google-browser-login — status (no secrets returned)."""

    configured: bool = Field(False, description="True when encrypted email+password are stored for Playwright.")
    storage_available: bool = Field(
        ...,
        description="False when POSTGRES_HOST is unset; browser-login credentials are not persisted (PUT returns 503).",
    )


class TradingViewConfigUpdate(BaseModel):
    """Request body for PUT /api/integrations/tradingview."""

    enabled: bool = Field(False, description="Enable the TradingView MCP data source for the Strategy Lab.")
    mcp_server_url: str = Field(
        "",
        description="Base URL of the TradingView MCP server (streamable-HTTP JSON-RPC endpoint).",
    )
    tool_name: str = Field(
        "",
        description="MCP tool the client calls to fetch OHLCV bars (blank uses the 'get_ohlcv' default).",
    )
    auth_token: str = Field(
        "",
        description="Bearer token / API key for the MCP server (stored encrypted; empty preserves existing).",
    )


class TradingViewConfigResponse(BaseModel):
    """Response for GET/PUT/DELETE /api/integrations/tradingview (secrets masked)."""

    enabled: bool
    mcp_server_url: str = ""
    tool_name: str = ""
    auth_token_configured: bool = Field(False, description="True when an encrypted auth token is stored.")


class TradingViewTestResponse(BaseModel):
    """Result of POST /api/integrations/tradingview/test (a live reachability probe)."""

    ok: bool = Field(description="True when the stored MCP server answered the OHLCV probe without error.")
    detail: str = Field(description="Human-readable outcome — the reason for a failure, or a success note.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_slack_config_response(cfg: dict) -> SlackConfigResponse:
    base = os.getenv("SLACK_PUBLIC_URL", "").strip() or os.getenv("UI_BASE_URL", "http://localhost:8080").rstrip("/")
    # If UI_BASE_URL points to frontend (4200), use the API port instead
    if ":4200" in base:
        base = base.replace(":4200", ":8080")
    return SlackConfigResponse(
        enabled=cfg["enabled"],
        mode=cfg.get("mode", "webhook"),
        client_id_configured=bool(cfg.get("client_id")),
        signing_secret_configured=bool(cfg.get("signing_secret")),
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
        events_url=f"{base}/api/integrations/slack/events",
        commands_url=f"{base}/api/integrations/slack/commands",
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


async def _exchange_code(code: str, redirect_uri: str, client_id: str, client_secret: str) -> dict:
    """Exchange an OAuth authorization code for a bot token via Slack's API.

    Runs on the event loop via ``httpx.AsyncClient`` so the outbound token
    exchange does not block concurrent request handling.

    Preconditions:
        - ``code``, ``redirect_uri``, ``client_id`` and ``client_secret`` are the
          non-empty values supplied by Slack's OAuth v2 redirect and the stored
          Slack app config.
    Postconditions:
        - Returns the parsed JSON body of Slack's ``oauth.v2.access`` response.
          Slack signals OAuth failure with HTTP 200 and ``{"ok": false, ...}``,
          so that payload is returned (not raised) for the caller to inspect.
        - Raises ``httpx.HTTPError`` on transport failure or non-2xx status, or
          ``ValueError`` if the body is not valid JSON; the caller handles both.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _SLACK_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
        )
    resp.raise_for_status()
    return resp.json()


def _build_medium_config_response(cfg: dict) -> MediumConfigResponse:
    prov = cfg.get("oauth_provider", "google")
    if prov not in ("google", "apple", "facebook", "twitter"):
        prov = "google"
    return MediumConfigResponse(
        enabled=cfg["enabled"],
        oauth_provider=prov,
        oauth_identity_connected=bool(cfg.get("oauth_identity_connected")),
        google_client_configured=bool(cfg.get("google_client_id")),
        session_configured=bool(cfg.get("session_configured")),
        linked_email=cfg.get("linked_email") or None,
        linked_name=cfg.get("linked_name") or None,
    )


def _get_medium_google_redirect_uri(request: Request) -> str:
    env_uri = os.getenv("MEDIUM_GOOGLE_REDIRECT_URI", "").strip()
    if env_uri:
        return env_uri
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/integrations/medium/oauth/google/callback"


async def _exchange_google_oauth_code(code: str, redirect_uri: str, client_id: str, client_secret: str) -> dict:
    """Exchange a Google OAuth authorization code for tokens (Medium linking).

    Runs on the event loop via ``httpx.AsyncClient`` so the outbound token
    exchange does not block concurrent request handling.

    Preconditions:
        - ``code``, ``redirect_uri``, ``client_id`` and ``client_secret`` are the
          non-empty values from Google's OAuth redirect and the stored Medium
          Google-client config.
    Postconditions:
        - Returns the parsed JSON token payload (typically ``access_token`` and,
          on first consent, ``refresh_token``).
        - Raises ``httpx.HTTPError`` on transport failure or non-2xx status, or
          ``ValueError`` if the body is not valid JSON; the caller handles both.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    resp.raise_for_status()
    return resp.json()


async def _google_userinfo(access_token: str) -> dict:
    """Fetch the Google userinfo profile for an OAuth access token.

    Runs on the event loop via ``httpx.AsyncClient`` so the outbound lookup does
    not block concurrent request handling.

    Preconditions:
        - ``access_token`` is a non-empty Google OAuth access token.
    Postconditions:
        - Returns the parsed JSON userinfo document (e.g. ``email``, ``name``).
        - Raises ``httpx.HTTPError`` on transport failure or non-2xx status, or
          ``ValueError`` if the body is not valid JSON; the caller handles both.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[IntegrationListItem])
async def list_integrations() -> list[IntegrationListItem]:
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
        signing_secret=(body.signing_secret or "").strip(),
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

    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "scope": _SLACK_SCOPES,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    url = f"{_SLACK_AUTHORIZE_URL}?{params}"

    return SlackOAuthConnectResponse(url=url, client_id=client_id)


@router.get("/slack/oauth/callback")
async def slack_oauth_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
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
        result = await _exchange_code(code, redirect_uri, client_id, client_secret)
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


# ---------------------------------------------------------------------------
# Slack Events API, Slash Commands, and Interactive Components
# ---------------------------------------------------------------------------

# HTTP status for refusing a Slack request that can't be authenticated because no signing
# secret is configured. Matches the in-file GitHub webhook sibling, which uses 403 for the
# identical "server refuses; no secret configured" case and reserves 401 for a real signature
# mismatch. Flip to 401 if a reviewer prefers that convention.
_SLACK_NO_SECRET_STATUS = 403


@router.post("/slack/events")
async def slack_events(request: Request) -> Any:
    """Receive Slack Events API payloads and route events to team assistants.

    Handles:
    - ``url_verification``: Slack's Request-URL setup probe → echo the ``challenge``
      token. It triggers no assistant work, so (like GitHub's ``ping``) it is the one
      event served without a configured signing secret.
    - ``event_callback`` (``app_mention`` / ``message.im``): handed to a background
      thread that runs the team assistant against payload-supplied identity.

    Slack expects a 2xx within 3 seconds, so processing is handed to a background
    thread by :func:`dispatch_event`.

    Preconditions: ``request`` is the raw Slack Events API delivery; the
        ``X-Slack-Request-Timestamp`` and ``X-Slack-Signature`` headers carry the
        replay timestamp and HMAC-SHA256 signature, and the JSON body's ``type`` field
        carries the event type.
    Postconditions:
        - When a signing secret is configured, the ``X-Slack-Signature`` HMAC is
          verified against the raw body (300s replay window) and a mismatch, stale
          timestamp, or malformed signature raises ``HTTPException(401)`` — for every
          event type, including ``url_verification``.
        - Fails closed when no signing secret is configured: ``url_verification`` still
          returns its ``challenge`` (so an operator can register the Request URL during
          app setup), but every other event — each of which can dispatch assistant work
          from payload-supplied identity — is refused with
          ``HTTPException(_SLACK_NO_SECRET_STATUS)`` and never dispatched. An unsigned
          request must never reach :func:`process_slack_message`. Because
          ``get_slack_config()`` reports an empty ``signing_secret`` both when it is
          genuinely unset AND when Postgres is unreachable, this refusal also covers a
          credential-store outage — the dangerous dispatch is never reached without a
          verified signature.
        - A non-JSON body, or a JSON body that is not an object, raises
          ``HTTPException(400)`` (guarding ``payload.get(...)`` against AttributeError →
          an unhandled 500).
        - ``url_verification`` returns ``{"challenge": ...}``; a verified
          ``event_callback`` is handed to :func:`dispatch_event` (which never raises) and
          ``{"ok": True}`` is returned before any assistant work runs; any other verified
          event type is a no-op ``{"ok": True}``.
    """
    from unified_api.slack_events_handler import (
        dispatch_event,
        handle_url_verification,
        verify_slack_request,
    )

    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    cfg = get_slack_config()
    signing_secret = str(cfg.get("signing_secret") or "").strip()

    # When a secret exists, verify FIRST — a bad/stale/malformed signature is a client
    # auth failure (401), independent of event type.
    if signing_secret and not verify_slack_request(signing_secret, body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # Parse the body BEFORE the no-secret refusal: the event type lives in the JSON body
    # (not a header), so url_verification must be identifiable while unsigned.
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

    # A real Slack event body is always a JSON object; a non-object (list/number/string/
    # null) is malformed. Reject it rather than letting payload.get(...) below raise
    # AttributeError → an unhandled 500.
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Slack event payload must be a JSON object.")

    event_type = str(payload.get("type", "")).strip()

    # url_verification is the setup analogue of GitHub's ping: it only echoes a challenge
    # and dispatches nothing, so it is served with or without a secret.
    if event_type == "url_verification":
        return handle_url_verification(payload)

    # Fail closed: with no signing secret we cannot authenticate the sender, and an
    # unsigned event_callback could forge an app_mention/IM to drive the assistant.
    # Refuse every non-setup event until a secret is configured. (An empty secret also
    # means "Postgres unreachable" — see docstring — so this covers an outage too.)
    if not signing_secret:
        logger.warning(
            "Slack signing secret not configured — only url_verification is served; "
            "event_callback and all other events are refused until a signing secret is set"
        )
        raise HTTPException(
            status_code=_SLACK_NO_SECRET_STATUS,
            detail=(
                "Slack signing secret not configured; refusing to process events. "
                "Configure the Slack signing secret and retry."
            ),
        )

    # Verified event_callback — respond immediately, process async.
    if event_type == "event_callback":
        dispatch_event(payload)
        return {"ok": True}

    return {"ok": True}


@router.post("/github/events")
async def github_events(request: Request) -> Any:
    """Receive GitHub webhook deliveries and trigger reviews from PR comments.

    Handles:
    - ``ping``: GitHub's webhook-setup probe → ``{"ok": true}``
    - ``issue_comment`` (``action == "created"``): a ``@khala review`` comment from a
      collaborator on a PR starts the existing code-review flow.

    GitHub expects a fast 2xx (deliveries time out), so event processing is handed to a
    background thread.

    Preconditions: ``request`` is the raw GitHub webhook delivery; the ``X-GitHub-Event``
        and ``X-Hub-Signature-256`` headers carry the event type and HMAC signature.
    Postconditions:
        - When a webhook secret is configured (stored credential or ``GITHUB_WEBHOOK_SECRET``),
          the ``X-Hub-Signature-256`` HMAC is verified against the raw body and a mismatch
          raises ``HTTPException(401)``.
        - Fails closed on a credential-store outage: if the secret is stored only in
          Postgres and that store is unreachable, this raises ``HTTPException(503)``
          rather than silently skipping verification — an unreachable store must never
          be treated the same as "no secret configured", or a forged payload could pass
          unverified for the duration of the outage.
        - Fails closed when no secret is configured: ``ping`` still returns ``{"ok": true}``
          (so an operator can verify delivery during setup), but every other event —
          each of which can start a paid review from payload-supplied identity — is
          refused with ``HTTPException(403)`` until a signing secret is set. An unsigned
          request must never be able to trigger a review.
        - ``ping`` deliveries return ``{"ok": true}`` after signature handling.
        - A non-JSON body raises ``HTTPException(400)``; otherwise the event (with the
          ``X-GitHub-Delivery`` header, used by :func:`dispatch_github_event` to skip a
          redelivery of the same delivery ID) is dispatched to
          :func:`dispatch_github_event` (which never raises) and ``{"ok": true}`` is
          returned. Returns before any review work runs.
    """
    from unified_api.github_events_handler import (
        dispatch_github_event,
        verify_github_signature,
    )
    from unified_api.integrations_store import get_github_webhook_secret_status

    body = await request.body()
    event_type = request.headers.get("X-GitHub-Event", "").strip()
    signature = request.headers.get("X-Hub-Signature-256", "")

    secret, secret_store_reachable = await asyncio.to_thread(get_github_webhook_secret_status)
    if secret:
        if not verify_github_signature(secret, body, signature):
            raise HTTPException(status_code=401, detail="Invalid GitHub signature")
    elif not secret_store_reachable:
        # Fail closed: we cannot tell whether a secret is actually configured, so
        # refuse rather than silently skip verification (see docstring above).
        raise HTTPException(
            status_code=503,
            detail=(
                "Cannot reach the GitHub credential store (Postgres); webhook signature "
                "verification is temporarily unavailable. Restore the database connection "
                "and retry."
            ),
        )
    else:
        logger.warning(
            "GitHub webhook secret not configured — only ping is served; review-triggering "
            "events are refused until a signing secret is set"
        )

    # Respond to the setup probe before attempting to parse an event payload.
    if event_type == "ping":
        return {"ok": True}

    # Beyond ``ping``, every event this endpoint dispatches can start a paid PR review,
    # and ``dispatch_github_event`` trusts payload fields (``author_association``,
    # ``repository``) to authorize and scope it. Without a configured signing secret we
    # cannot authenticate the sender, so an unsigned request could forge a collaborator
    # ``issue_comment`` for any repo the PAT can reach and spend review budget. Refuse review-
    # triggering events when no secret is configured — ``ping`` is allowed above so an
    # operator can still verify webhook delivery before setting the secret.
    if not secret:
        raise HTTPException(
            status_code=403,
            detail=(
                "GitHub webhook signing secret not configured; refusing to process "
                "review-triggering events. Configure a secret (stored credential or "
                "GITHUB_WEBHOOK_SECRET) and retry."
            ),
        )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

    # A real GitHub webhook body is always a JSON object; a non-object (list/number/
    # string/null) is malformed. Reject it here rather than letting dispatch (which does
    # ``payload.get(...)``) raise AttributeError → an unhandled 500.
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload must be a JSON object.")

    delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()
    dispatch_github_event(event_type, payload, delivery_id)
    return {"ok": True}


@router.post("/slack/commands")
async def slack_commands(request: Request) -> Any:
    """Receive Slack slash-command (/khala) submissions.

    Returns an immediate acknowledgment, then :func:`process_slash_command` processes the
    command in a background thread and posts the result to the Slack-supplied
    ``response_url``.

    Preconditions: ``request`` is the raw Slack slash-command delivery
        (``application/x-www-form-urlencoded``); the ``X-Slack-Request-Timestamp`` and
        ``X-Slack-Signature`` headers carry the replay timestamp and HMAC-SHA256 signature.
    Postconditions:
        - When a signing secret is configured, the ``X-Slack-Signature`` HMAC is verified
          against the raw body (300s replay window) and a mismatch, stale timestamp, or
          malformed signature raises ``HTTPException(401)``.
        - Fails closed when no signing secret is configured: the request is refused with
          ``HTTPException(_SLACK_NO_SECRET_STATUS)`` and no command is processed. Slack
          sends slash commands only after the app is fully configured, so — unlike
          ``/slack/events`` — there is no unsigned setup probe to exempt. An unsigned
          request must never be able to switch teams, invoke an assistant, or post to an
          attacker-supplied ``response_url``. (An empty secret also means "Postgres
          unreachable"; this refusal covers that outage too.)
        - Otherwise the form body is parsed and handed to :func:`process_slash_command`,
          whose immediate acknowledgment dict is returned.
    """
    from unified_api.slack_events_handler import (
        process_slash_command,
        verify_slack_request,
    )

    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    cfg = get_slack_config()
    signing_secret = str(cfg.get("signing_secret") or "").strip()

    if not signing_secret:
        logger.warning("Slack signing secret not configured — refusing slash command until a signing secret is set")
        raise HTTPException(
            status_code=_SLACK_NO_SECRET_STATUS,
            detail=(
                "Slack signing secret not configured; refusing to process slash commands. "
                "Configure the Slack signing secret and retry."
            ),
        )
    if not verify_slack_request(signing_secret, body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # Parse form-encoded body
    form_data: dict[str, str] = {}
    for pair in body.decode("utf-8").split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            form_data[urllib.parse.unquote_plus(key)] = urllib.parse.unquote_plus(value)

    return process_slash_command(form_data)


@router.post("/slack/interactive")
async def slack_interactive(request: Request) -> Any:
    """Receive Slack interactive-component payloads (button clicks, menus, etc.).

    Placeholder for future interactive handling; today it only authenticates the request
    and returns ``{"ok": True}``.

    Preconditions: ``request`` is the raw Slack interactivity delivery; the
        ``X-Slack-Request-Timestamp`` and ``X-Slack-Signature`` headers carry the replay
        timestamp and HMAC-SHA256 signature.
    Postconditions:
        - When a signing secret is configured, the ``X-Slack-Signature`` HMAC is verified
          against the raw body (300s replay window) and a mismatch, stale timestamp, or
          malformed signature raises ``HTTPException(401)``.
        - Fails closed when no signing secret is configured: the request is refused with
          ``HTTPException(_SLACK_NO_SECRET_STATUS)``. Slack sends interactive payloads only
          after the app is fully configured, so there is no unsigned setup probe to exempt.
          (An empty secret also means "Postgres unreachable"; this refusal covers that
          outage too.)
        - Otherwise returns ``{"ok": True}``.
    """
    from unified_api.slack_events_handler import verify_slack_request

    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    cfg = get_slack_config()
    signing_secret = str(cfg.get("signing_secret") or "").strip()

    if not signing_secret:
        logger.warning(
            "Slack signing secret not configured — refusing interactive payload until a signing secret is set"
        )
        raise HTTPException(
            status_code=_SLACK_NO_SECRET_STATUS,
            detail=(
                "Slack signing secret not configured; refusing to process interactive components. "
                "Configure the Slack signing secret and retry."
            ),
        )
    if not verify_slack_request(signing_secret, body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    return {"ok": True}


# ---------------------------------------------------------------------------
# Shared Google / Gmail credentials for Playwright (any integration using "Sign in with Google")
# ---------------------------------------------------------------------------


@router.get("/google-browser-login", response_model=GoogleBrowserLoginStatusResponse)
async def get_google_browser_login_status() -> GoogleBrowserLoginStatusResponse:
    """Return whether encrypted shared Google browser-login credentials are stored (no secrets)."""
    avail = google_browser_login_storage_available()
    return GoogleBrowserLoginStatusResponse(
        configured=google_browser_login_credentials_configured(),
        storage_available=avail,
    )


@router.put("/google-browser-login", response_model=GoogleBrowserLoginStatusResponse)
async def put_google_browser_login_credentials(
    body: GoogleBrowserLoginCredentialsBody,
) -> GoogleBrowserLoginStatusResponse:
    """Encrypt and store shared Gmail/Google email+password for browser automation across integrations."""
    try:
        set_google_browser_login_credentials(body.email, body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    logger.info("Shared Google browser-login credentials stored (encrypted).")
    return GoogleBrowserLoginStatusResponse(
        configured=True,
        storage_available=google_browser_login_storage_available(),
    )


@router.delete("/google-browser-login", response_model=GoogleBrowserLoginStatusResponse)
async def delete_google_browser_login_credentials() -> GoogleBrowserLoginStatusResponse:
    """Remove shared Google browser-login credentials."""
    clear_google_browser_login_credentials()
    return GoogleBrowserLoginStatusResponse(
        configured=False,
        storage_available=google_browser_login_storage_available(),
    )


# ---------------------------------------------------------------------------
# TradingView MCP integration (data source for the Strategy Lab)
# ---------------------------------------------------------------------------


def _validate_mcp_server_url(url: str) -> None:
    """Reject a non-empty TradingView MCP URL that is not an http(s) endpoint.

    Preconditions: ``url`` is the raw (already-stripped) server URL.
    Postconditions: returns ``None`` for an empty URL or one starting with
        ``http://`` / ``https://``; raises ``HTTPException(400)`` otherwise so a
        typo can't be persisted as a live endpoint.
    """
    if not url:
        return
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(
            status_code=400,
            detail="mcp_server_url must start with http:// or https://",
        )


def _build_tradingview_config_response(cfg: dict) -> TradingViewConfigResponse:
    """Map a stored TradingView config dict to the API response (token masked).

    Preconditions: ``cfg`` is a :func:`get_tradingview_config` dict.
    Postconditions: returns a :class:`TradingViewConfigResponse` carrying ``enabled``,
        ``mcp_server_url``, ``tool_name`` and ``auth_token_configured`` (a bool) — the raw
        ``auth_token`` is never placed on the response.
    """
    return TradingViewConfigResponse(
        enabled=bool(cfg.get("enabled", False)),
        mcp_server_url=str(cfg.get("mcp_server_url", "")).strip(),
        tool_name=str(cfg.get("tool_name", "")).strip(),
        auth_token_configured=bool(cfg.get("auth_token")),
    )


@router.get("/tradingview", response_model=TradingViewConfigResponse)
async def get_tradingview() -> TradingViewConfigResponse:
    """Return the TradingView MCP integration config (auth token masked)."""
    return _build_tradingview_config_response(get_tradingview_config())


@router.put("/tradingview", response_model=TradingViewConfigResponse)
async def update_tradingview(body: TradingViewConfigUpdate) -> TradingViewConfigResponse:
    """Save the TradingView MCP config (auth token stored encrypted).

    Requires a server URL when enabling so the Strategy Lab has an endpoint to call.
    """
    mcp_server_url = (body.mcp_server_url or "").strip()
    _validate_mcp_server_url(mcp_server_url)

    if body.enabled and not mcp_server_url and not get_tradingview_config().get("mcp_server_url"):
        raise HTTPException(
            status_code=400,
            detail="mcp_server_url is required when the TradingView integration is enabled.",
        )

    set_tradingview_config(
        enabled=body.enabled,
        mcp_server_url=mcp_server_url,
        tool_name=(body.tool_name or "").strip(),
        auth_token=(body.auth_token or "").strip(),
    )
    return _build_tradingview_config_response(get_tradingview_config())


@router.delete("/tradingview", response_model=TradingViewConfigResponse)
async def delete_tradingview() -> TradingViewConfigResponse:
    """Disconnect the TradingView integration (removes the token and resets config)."""
    clear_tradingview_config()
    return _build_tradingview_config_response(get_tradingview_config())


@router.post("/tradingview/test", response_model=TradingViewTestResponse)
async def test_tradingview() -> TradingViewTestResponse:
    """Probe the stored TradingView MCP server to verify the URL/token actually work.

    Runs a small OHLCV request against the configured endpoint so the user can confirm
    connectivity before (or independent of) enabling the data source. The stored config
    is used even when ``enabled`` is false — you test before you switch it on.

    Preconditions: a non-empty ``mcp_server_url`` is stored (else ``HTTPException(400)``).
    Postconditions: returns HTTP 200 with ``ok=True`` when the server answered the probe
        without a protocol/tool error, or ``ok=False`` with a friendly ``detail`` when the
        server was unreachable or returned an MCP error. A missing endpoint is the only
        input error surfaced as a non-200 (400); reachability failures are reported in-band
        so the UI can render a red result rather than trapping an exception.
    """
    from datetime import date, timedelta

    from fastapi.concurrency import run_in_threadpool

    from investment_team.tradingview_mcp.client import TradingViewMcpClient

    cfg = get_tradingview_config()
    server_url = str(cfg.get("mcp_server_url", "")).strip()
    if not server_url:
        raise HTTPException(
            status_code=400,
            detail="Configure and save a TradingView MCP server URL before testing the connection.",
        )

    client = TradingViewMcpClient(
        server_url,
        auth_token=str(cfg.get("auth_token", "")).strip(),
        tool_name=str(cfg.get("tool_name", "")).strip() or "get_ohlcv",
        timeout=10.0,
    )
    end = date.today()
    start = end - timedelta(days=7)

    try:
        rows = await run_in_threadpool(
            client.fetch_ohlcv,
            "AAPL",
            "stock",
            start.isoformat(),
            end.isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 - any reachability/parse failure is a failed test, not a 500
        # Mirrors MarketDataService._fetch_tradingview_mcp: a non-JSON 200 response raises
        # json.JSONDecodeError (not TradingViewMcpError), and the probe must still report
        # it in-band as an unreachable result rather than surfacing an HTTP 500.
        return TradingViewTestResponse(ok=False, detail=f"TradingView MCP server unreachable: {exc}")

    note = f"Connected — the MCP server returned {len(rows)} price bar(s) for the probe request."
    return TradingViewTestResponse(ok=True, detail=note)


# ---------------------------------------------------------------------------
# Medium.com integration (OAuth identity + Playwright session for stats agent)
# ---------------------------------------------------------------------------


@router.get("/medium", response_model=MediumConfigResponse)
async def get_medium() -> MediumConfigResponse:
    """Return Medium integration status (no secrets)."""
    return _build_medium_config_response(get_medium_config())


@router.put("/medium", response_model=MediumConfigResponse)
async def update_medium(body: MediumConfigUpdate) -> MediumConfigResponse:
    """
    Save Medium integration: enabled flag, OAuth provider, and optional Google OAuth app credentials.
    """
    # Google OAuth client ID/secret are optional: only needed for GET .../medium/oauth/google/connect.
    # Medium browser session: PUT .../google-browser-login + POST .../medium/session/browser-login, or import session.

    set_medium_config(
        enabled=body.enabled,
        oauth_provider=body.oauth_provider,
        google_client_id=(body.google_client_id or "").strip(),
        google_client_secret=(body.google_client_secret or "").strip(),
    )
    return _build_medium_config_response(get_medium_config())


@router.get("/medium/oauth/google/connect", response_model=MediumGoogleOAuthConnectResponse)
async def medium_google_oauth_connect(request: Request) -> MediumGoogleOAuthConnectResponse:
    """
    Start Google OAuth (OpenID) to link the Google account used for Medium.

    Configure redirect URI in Google Cloud Console to match MEDIUM_GOOGLE_REDIRECT_URI
    or {API}/api/integrations/medium/oauth/google/callback.
    """
    cfg = get_medium_config()
    if cfg.get("oauth_provider") != "google":
        raise HTTPException(
            status_code=400,
            detail="Set OAuth provider to Google in Medium integration settings to use this flow.",
        )
    client_id = cfg.get("google_client_id", "").strip()
    client_secret = cfg.get("google_client_secret", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=400,
            detail="Google OAuth Client ID and Client Secret must be saved first (PUT /api/integrations/medium).",
        )
    state = generate_medium_google_oauth_state()
    redirect_uri = _get_medium_google_redirect_uri(request)
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _GOOGLE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    url = f"{_GOOGLE_AUTH_URL}?{params}"
    return MediumGoogleOAuthConnectResponse(url=url)


@router.get("/medium/oauth/google/callback")
async def medium_google_oauth_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
) -> RedirectResponse:
    """Google redirects here after user consents; we store refresh token and profile email."""
    ui_base = _get_ui_base_url()
    integrations_ui = f"{ui_base}/integrations"

    if error:
        logger.warning("Medium Google OAuth error: %s", error)
        return RedirectResponse(url=f"{integrations_ui}?medium_error={urllib.parse.quote(error)}")

    if not code or not state:
        return RedirectResponse(url=f"{integrations_ui}?medium_error=missing_code_or_state")

    if not verify_and_clear_medium_google_oauth_state(state):
        logger.warning("Medium Google OAuth state mismatch or expired")
        return RedirectResponse(url=f"{integrations_ui}?medium_error=invalid_state")

    cfg = get_medium_config()
    client_id = cfg.get("google_client_id", "").strip()
    client_secret = cfg.get("google_client_secret", "").strip()
    if not client_id or not client_secret:
        return RedirectResponse(url=f"{integrations_ui}?medium_error=missing_credentials")

    redirect_uri = _get_medium_google_redirect_uri(request)
    try:
        token_payload = await _exchange_google_oauth_code(code, redirect_uri, client_id, client_secret)
    except Exception as exc:
        logger.error("Medium Google token exchange failed: %s", exc)
        return RedirectResponse(url=f"{integrations_ui}?medium_error=token_exchange_failed")

    refresh_token = str(token_payload.get("refresh_token") or "")
    access_token = str(token_payload.get("access_token") or "")
    email, name = "", ""
    if access_token:
        try:
            info = await _google_userinfo(access_token)
            email = str(info.get("email") or "")
            name = str(info.get("name") or "")
        except Exception as exc:
            logger.warning("Medium Google userinfo failed: %s", exc)

    set_medium_google_oauth_identity(refresh_token=refresh_token, linked_email=email, linked_name=name)
    logger.info("Medium Google OAuth linked: email=%s", email or "(unknown)")
    return RedirectResponse(url=f"{integrations_ui}?medium_google_connected=1")


@router.delete("/medium/oauth/google", response_model=MediumConfigResponse)
async def medium_google_oauth_disconnect() -> MediumConfigResponse:
    """Remove stored Google identity tokens and linked email (keeps Medium browser session if any)."""
    clear_medium_google_oauth_identity()
    return _build_medium_config_response(get_medium_config())


@router.post("/medium/session/browser-login", response_model=MediumConfigResponse)
async def medium_browser_login_session() -> MediumConfigResponse:
    """
    Run Playwright on the server: open medium.com sign-in, sign in with Google using
    shared encrypted credentials from PUT .../google-browser-login, then persist storage_state to disk.
    """
    from unified_api.medium_browser_login import perform_medium_google_browser_login

    cfg = get_medium_config()
    if not cfg.get("enabled"):
        raise HTTPException(
            status_code=400,
            detail="Enable the Medium integration first (PUT /api/integrations/medium).",
        )
    if str(cfg.get("oauth_provider", "google")).strip().lower() != "google":
        raise HTTPException(
            status_code=400,
            detail="Automated browser login supports Google as the Medium identity provider only.",
        )

    email, password = get_google_browser_login_credentials()
    if not email or not password:
        raise HTTPException(
            status_code=400,
            detail="Save shared Google sign-in credentials first (PUT /api/integrations/google-browser-login).",
        )

    loop = asyncio.get_running_loop()

    def _run() -> None:
        state = perform_medium_google_browser_login(email, password)
        raw = json.dumps(state, separators=(",", ":"))
        set_medium_session_storage_state_json(raw)

    try:
        await loop.run_in_executor(None, _run)
    except RuntimeError as e:
        msg = str(e)
        if "playwright is not installed" in msg.lower():
            raise HTTPException(status_code=400, detail=msg) from e
        raise HTTPException(status_code=500, detail=msg) from e

    logger.info("Medium browser-login session saved from automated Google sign-in.")
    return _build_medium_config_response(get_medium_config())


@router.post("/medium/session", response_model=MediumConfigResponse)
async def medium_import_session(body: MediumSessionImportBody) -> MediumConfigResponse:
    """
    Store Playwright storage_state for medium.com (from context.storage_state() after signing in).

    Required for the blogging Medium stats agent to run.
    """
    try:
        raw = json.dumps(body.storage_state, separators=(",", ":"))
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid storage_state: {e}") from e
    set_medium_session_storage_state_json(raw)
    return _build_medium_config_response(get_medium_config())


@router.delete("/medium/session", response_model=MediumConfigResponse)
async def medium_clear_session() -> MediumConfigResponse:
    """Remove stored Medium browser session."""
    clear_medium_session_storage()
    return _build_medium_config_response(get_medium_config())


# ---------------------------------------------------------------------------
# GitHub integration
# ---------------------------------------------------------------------------

_GITHUB_SERVICE = "github"
_GITHUB_API_BASE = "https://api.github.com"
# GitHub's issues endpoint returns at most 100 items per page; we request the max
# and follow the Link header so the panel shows every open issue, not just page one.
_GITHUB_ISSUES_PER_PAGE = 100
# Safety bound against a pathological repo or a redirect loop in the Link header.
# 100 issues/page * 50 pages = 5000 open issues, far beyond any realistic repo.
_GITHUB_MAX_ISSUE_PAGES = 50
# Open pull requests paginate the same way; bound the follow identically. GitHub's
# pulls endpoint shares the issues endpoint's 100-item page ceiling, but PRs get their
# own constant so the two page sizes can be tuned independently without one silently
# changing the other.
_GITHUB_PRS_PER_PAGE = 100
_GITHUB_MAX_PR_PAGES = 50
# The PAT's accessible-repository list (GET /user/repos) paginates the same way.
# 100 repos/page * 20 pages = 2000 repositories, far beyond any realistic PAT grant.
_GITHUB_REPOS_PER_PAGE = 100
_GITHUB_MAX_REPO_PAGES = 20
# Per-issue blocked_by dependencies also paginate; bound the follow so a pathological
# issue can't fan into an unbounded number of requests. 100/page * 10 pages = 1000
# dependencies on a single issue, far beyond any realistic case.
_GITHUB_DEPENDENCY_PER_PAGE = 100
_GITHUB_MAX_DEPENDENCY_PAGES = 10
# Per-request timeout (seconds) for direct GitHub REST calls (repos/issues/pulls
# listing). One constant so the latency budget is tuned in a single place; the
# coding-team calls forwarded via _forward_to_coding_team() use their own longer
# timeouts (those are a different upstream with different latency characteristics).
_GITHUB_HTTP_TIMEOUT = 15.0
# Allowlist for a single owner/repo path component: GitHub logins and repository
# names are ASCII alphanumerics plus ``.``, ``_``, ``-``. Validating against this
# (rather than blocklisting a few bad characters) is what keeps a caller-supplied
# value from rewriting the GitHub API request path or escaping the workspace root.
# ``\Z`` (not ``$``) so the anchor can't match before a trailing newline — a value like
# "name\n" must be rejected even if it ever reached here un-stripped.
_REPO_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+\Z")


def _parse_dependency_concurrency(raw: str | None) -> int:
    """Parse the ``GITHUB_DEPENDENCY_CONCURRENCY`` knob.

    Postconditions:
        - Returns a positive int; the default is 8. Empty, non-integer, or
          non-positive input falls back to 8.
    """
    try:
        value = int((raw or "").strip())
    except ValueError:
        return 8
    return value if value > 0 else 8


# Bounded fan-out for the per-issue blocked_by dependency enrichment in the issue
# list, resolved once at import like the other _GITHUB_* settings above.
_GITHUB_DEPENDENCY_CONCURRENCY = _parse_dependency_concurrency(os.environ.get("GITHUB_DEPENDENCY_CONCURRENCY"))


class GitHubConfigResponse(BaseModel):
    enabled: bool
    token_configured: bool
    # Legacy optional default repository. Repository access itself is defined by the
    # PAT's own authorization configuration — the pickers list every accessible repo
    # via GET /github/repos and pass an explicit owner/repo per request.
    owner: str
    repo: str
    default_label: str
    webhook_secret_configured: bool = Field(
        default=False,
        description=(
            "True when a webhook signing secret is configured (stored credential or "
            "GITHUB_WEBHOOK_SECRET env var), used to verify '@khala review' PR-comment "
            "webhooks delivered to POST /api/integrations/github/events."
        ),
    )
    credential_store_unreachable: bool = Field(
        default=False,
        description=(
            "True when Postgres (the PAT store) is configured but unreachable, so "
            "token_configured may read False only because the store is down — not "
            "because no token was saved. Lets the UI warn instead of showing "
            "'not connected'."
        ),
    )


class GitHubConfigUpdate(BaseModel):
    enabled: bool = True
    token: str = Field(default="", description="Personal Access Token; empty preserves existing")
    owner: str = Field(default="", description="Optional default repository owner; access comes from the PAT itself")
    repo: str = Field(default="", description="Optional default repository name; access comes from the PAT itself")
    default_label: str = ""
    repo_path: str = Field(default="", description="Operator override for local checkout path")
    webhook_secret: str = Field(default="", description="GitHub webhook signing secret; empty preserves existing")


class GitHubRepoItem(BaseModel):
    """One repository the configured PAT can access (GET /github/repos).

    The list mirrors GitHub's ``GET /user/repos`` for the stored token: for a
    fine-grained PAT that is exactly the repositories granted to the token; for a
    classic PAT it is every repository the token's owner can access.
    """

    owner: str
    name: str
    full_name: str
    private: bool = False
    archived: bool = False
    html_url: str = ""
    description: str = ""
    default_branch: str = ""
    # GitHub's open_issues_count includes open pull requests — an at-a-glance hint
    # for the pickers, not the exact open-issue total.
    open_issues_count: int = 0
    pushed_at: str = ""


class GitHubDependencyRef(BaseModel):
    """A single issue this issue is blocked by (a GitHub ``blocked_by`` dependency)."""

    number: int
    title: str
    state: str  # "open" | "closed"


class GitHubIssueItem(BaseModel):
    number: int
    title: str
    body_preview: str
    labels: list[str]
    html_url: str
    # Issue dependencies (GitHub "blocked by" relationships). An issue "depends on"
    # the issues in ``dependencies`` and is ``blocked`` while any of them are open.
    dependencies: list[GitHubDependencyRef] = []
    open_dependencies: list[int] = []
    blocked: bool = False


class RunGitHubIssueRequest(BaseModel):
    issue_number: int
    base_branch: str | None = None
    # Target repository. Blank falls back to the legacy configured default owner/repo;
    # the PAT's own authorization decides whether the repository is actually reachable.
    owner: str = ""
    repo: str = ""


class RunGitHubIssueResponse(BaseModel):
    job_id: str
    issue_number: int
    issue_url: str
    status: str = "pending"
    message: str = "Job started. Poll GET /api/coding-team/status/{job_id} for progress."


class GitHubPullRequestItem(BaseModel):
    number: int
    title: str
    body_preview: str
    author: str
    html_url: str
    head: str
    base: str
    draft: bool = False
    labels: list[str] = []
    updated_at: str = ""


class RunPrReviewRequest(BaseModel):
    pr_number: int
    base_branch: str | None = None
    # Target repository. Blank falls back to the legacy configured default owner/repo;
    # the PAT's own authorization decides whether the repository is actually reachable.
    owner: str = ""
    repo: str = ""


class RunPrReviewResponse(BaseModel):
    """Response for ``POST /github/review-pr``: identifies the started review job
    (id, PR number/url, initial status) and carries the server-clock start time so
    the UI can compute a live duration on server timestamps at both ends."""

    job_id: str
    pr_number: int
    pr_url: str
    status: str = "pending"
    message: str = "Review started. Poll GET /api/coding-team/status/{job_id} for progress."
    # ISO-8601 server-clock start time, forwarded from the coding team so the UI can
    # compute a live review duration on server timestamps at both ends. Optional.
    created_at: str | None = None


class CodeReviewRunItem(BaseModel):
    """One persisted code-review run for a PR (GET /github/reviews)."""

    job_id: str
    pr_number: int
    pr_url: str | None = None
    status: str
    status_text: str | None = None
    review_summary: dict[str, Any] | None = None
    error: str | None = None
    created_at: str
    completed_at: str | None = None


class CodeReviewTranscriptEntry(BaseModel):
    """One LLM call the review pipeline made (GET /github/reviews/{job_id}/transcript)."""

    stage: str
    target: str
    model: str
    prompt: str
    response: str
    started_at: str
    duration_ms: int


class CodeReviewTranscript(BaseModel):
    """A review's full durable transcript, in call order."""

    job_id: str
    entries: list[CodeReviewTranscriptEntry] = Field(default_factory=list)


class CreateReviewIssuesBody(BaseModel):
    """Request body for POST /github/reviews/{job_id}/issues.

    The GitHub token is injected server-side (never sent by the browser). The
    Code Review page sends the repository the review belongs to (``owner``/
    ``repo``) so the coding-team service can confirm the issues are filed into
    the reviewed repository — access itself comes from the PAT.
    """

    proposal_ids: list[str] = Field(
        default_factory=list,
        description="Ids of the review's pending issue proposals to file as GitHub issues.",
    )
    owner: str = Field(description="Owner of the repository the review belongs to.")
    repo: str = Field(description="Name of the repository the review belongs to.")


class CreatedReviewIssueItem(BaseModel):
    """One GitHub issue opened from a review's pending issue proposal."""

    proposal_id: str
    issue_number: int
    issue_url: str
    title: str


class CreateReviewIssuesResponse(BaseModel):
    """Result of POST /github/reviews/{job_id}/issues.

    ``proposals`` is the review's full, updated pending-proposal list (created
    ones now carry ``issue_number``/``issue_url``); ``created`` names only the
    issues opened by this request.
    """

    job_id: str
    created: list[CreatedReviewIssueItem] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Out-of-scope issue proposals — aggregated across reviews
# ---------------------------------------------------------------------------


class OutOfScopeProposalItem(BaseModel):
    """One unfiled out-of-scope issue proposal from code reviews."""

    id: str
    job_id: str
    pr_number: int
    pr_url: str | None = None
    severity: str
    category: str
    file_path: str
    line: int | None = None
    description: str
    suggestion: str = ""
    locations: list[dict[str, Any]] = Field(default_factory=list)
    issue_number: int | None = None
    issue_url: str | None = None


class OutOfScopeProposalsResponse(BaseModel):
    """All unfiled out-of-scope issue proposals for a repository."""

    owner: str
    repo: str
    proposals: list[OutOfScopeProposalItem] = Field(default_factory=list)
    total: int
    unfiled: int


class FileOutOfScopeIssuesBody(BaseModel):
    """Request body for POST /github/reviews/out-of-scope-issues/file.

    The GitHub token is injected server-side (never sent by the browser).
    """

    proposal_ids: list[str] = Field(description="Composite ids of the form 'job_id:proposal_id'.")
    owner: str = Field(description="Repository owner.")
    repo: str = Field(description="Repository name.")


class EnhancedCreatedIssueItem(BaseModel):
    """One GitHub issue created via the enhanced issue builder."""

    proposal_id: str
    issue_number: int
    issue_url: str
    title: str
    label: str
    complexity_score: int
    merged_into_existing: bool = False


class FileOutOfScopeIssuesResponse(BaseModel):
    """Result of POST /github/reviews/out-of-scope-issues/file."""

    created: list[EnhancedCreatedIssueItem] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def _build_github_config_response(
    cfg: dict[str, Any], *, credential_store_unreachable: bool = False
) -> GitHubConfigResponse:
    return GitHubConfigResponse(
        enabled=cfg["enabled"],
        token_configured=cfg["token_configured"],
        owner=cfg["owner"],
        repo=cfg["repo"],
        default_label=cfg["default_label"],
        webhook_secret_configured=bool(cfg.get("webhook_secret_configured", False)),
        credential_store_unreachable=credential_store_unreachable,
    )


def _degraded_github_config_response() -> GitHubConfigResponse:
    """Best-effort GitHub config when the credential store is unreachable.

    Preconditions: none.
    Postconditions: returns the JSON-only settings (owner/repo/enabled/default_label,
        read via ``get_github_config_meta`` — NO Postgres) with ``token_configured=False``
        and ``credential_store_unreachable=True``. ``webhook_secret_configured`` reflects
        the ``GITHUB_WEBHOOK_SECRET`` env var (readable without Postgres), so an
        env-configured secret is not falsely reported as absent during a store outage; the
        stored-credential secret is the only webhook field that is genuinely unknown here.
        Only the credential-derived fields are unknown during a store outage; the saved
        settings are not, so the panel shows the configured repo plus the unreachable
        warning instead of blanking everything (which would conflate "store down" with
        "nothing configured"). Never raises — a failure reading even the JSON falls back to
        empty settings.
    """
    try:
        meta = get_github_config_meta()
    except Exception:  # noqa: BLE001 - even the JSON read failed; degrade to empty settings
        logger.exception("GitHub settings read failed during degraded fallback")
        meta = {"enabled": False, "owner": "", "repo": "", "default_label": ""}
    return GitHubConfigResponse(
        enabled=bool(meta.get("enabled", False)),
        token_configured=False,
        owner=str(meta.get("owner", "")),
        repo=str(meta.get("repo", "")),
        default_label=str(meta.get("default_label", "")),
        webhook_secret_configured=bool(os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()),
        credential_store_unreachable=True,
    )


async def _github_config_response() -> GitHubConfigResponse:
    """Read the GitHub config off the event loop and report store reachability.

    Preconditions: none.
    Postconditions: returns the current config. ``credential_store_unreachable`` is
        derived from the SAME single credential read that backs ``token_configured``
        (``get_github_config`` -> ``get_credential_status``), so the panel and the
        run/review routes can never disagree about reachability — no separate probe.
        The read runs via the shared :func:`shared.postgres.bounded_probe`, whose budget
        (``connect_timeout + statement_timeout + 1s``) is large enough that the
        statement_timeout-bounded worker finishes — releasing the credential-store
        ``_LOCK`` — before the outer guard fires, and that a within-bounds slow read isn't
        falsely flagged. On timeout/error it logs and returns
        :func:`_degraded_github_config_response`, which PRESERVES the JSON-only settings
        (owner/repo) rather than blanking them. Never raises.
    """

    def _build() -> GitHubConfigResponse:
        cfg = get_github_config()
        # store_reachable is False only on a connection/query error; disabled Postgres
        # reports reachable=True ("absent", not an outage), matching the panel intent.
        return _build_github_config_response(
            cfg,
            credential_store_unreachable=not cfg.get("store_reachable", True),
        )

    return await bounded_probe(
        _build,
        on_failure=_degraded_github_config_response,
        label="GitHub config read",
    )


@router.get("/github", response_model=GitHubConfigResponse)
async def get_github() -> GitHubConfigResponse:
    """Return GitHub integration config status."""
    return await _github_config_response()


@router.put("/github", response_model=GitHubConfigResponse)
async def update_github(body: GitHubConfigUpdate) -> GitHubConfigResponse:
    """Save or update GitHub integration config (PAT stored encrypted)."""
    set_github_config(
        enabled=body.enabled,
        owner=body.owner,
        repo=body.repo,
        personal_access_token=body.token,
        default_label=body.default_label,
        repo_path=body.repo_path,
        webhook_secret=body.webhook_secret,
    )
    return await _github_config_response()


@router.delete("/github", response_model=GitHubConfigResponse)
async def delete_github() -> GitHubConfigResponse:
    """Disconnect GitHub integration (removes PAT and resets config)."""
    clear_github_config()
    return await _github_config_response()


async def _iter_github_pages(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None,
    max_pages: int,
    sem: asyncio.Semaphore | None = None,
) -> AsyncIterator[httpx.Response]:
    """Yield successive GitHub responses, following the ``Link`` header.

    Shared pagination primitive for the issue list and the per-issue dependency fetch.
    Query params ride only on the first request; GitHub's "next" Link URL already
    carries them forward, so they are dropped on subsequent pages.

    Preconditions:
        - ``client`` is an open ``AsyncClient`` and ``headers`` carry a valid PAT.
        - ``max_pages`` >= 1 bounds the number of pages followed.
    Postconditions:
        - Yields at most ``max_pages`` responses. Each request is made while holding
          ``sem`` (when provided), so the semaphore is released between pages rather
          than held for a whole issue's pagination. The caller inspects each response's
          status/body and may stop early (e.g. on a non-200).
    """
    next_url: str | None = url
    request_params = params
    pages = 0
    while next_url and pages < max_pages:
        # Hold the semaphore (when one is supplied) only for the request itself; a
        # nullcontext keeps the unbounded issue-list path on the same single code path.
        async with sem or contextlib.nullcontext():
            resp = await client.get(next_url, headers=headers, params=request_params)
        yield resp
        pages += 1
        next_url = resp.links.get("next", {}).get("url")
        request_params = None  # subsequent pages use the Link URL verbatim


async def _fetch_blocked_by(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    issue_number: int,
    headers: dict[str, str],
    sem: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """Fetch every ``blocked_by`` dependency for a single issue.

    Preconditions:
        - ``client`` is an open ``AsyncClient`` and ``headers`` carry a valid PAT.
        - ``sem`` bounds the number of concurrent dependency fetches.
    Postconditions:
        - Returns the dependency issue objects across all result pages (the endpoint
          paginates via the ``Link`` header like the issue list), bounded by
          ``_GITHUB_MAX_DEPENDENCY_PAGES`` pages of ``_GITHUB_DEPENDENCY_PER_PAGE``.
        - Best-effort: returns whatever was gathered before any non-200 status
          (404/410/422 = feature disabled or no dependencies, 5xx, throttle) or transport
          error — a single issue's lookup must never fail the whole list. ``blocked`` is
          derived only from the dependencies actually observed; we deliberately do not
          fail safe toward "blocked" on an incomplete fetch, because the picker's
          ``blocked`` flag is paired with the open-dependency list (a forced block with no
          known open dependency would render an empty, confusing warning), and a total
          dependency-API outage would otherwise flag every issue. The only residual gap is
          the rare case of an open blocker on an unfetched later page of a many-page list.
    """
    # The blocked_by endpoint returns standard issue objects, so the default
    # ``application/vnd.github+json`` Accept header (already in ``headers``) is correct.
    url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by"
    params: dict[str, Any] | None = {"per_page": _GITHUB_DEPENDENCY_PER_PAGE}
    out: list[dict[str, Any]] = []
    try:
        pages = _iter_github_pages(client, url, headers, params, _GITHUB_MAX_DEPENDENCY_PAGES, sem=sem)
        async with contextlib.aclosing(pages) as page_iter:
            async for resp in page_iter:
                if resp.status_code != 200:
                    logger.debug(
                        "blocked_by fetch for %s/%s#%d returned %d; treating as no further dependencies",
                        owner,
                        repo,
                        issue_number,
                        resp.status_code,
                    )
                    return out
                payload = resp.json()
                if not isinstance(payload, list):
                    return out
                out.extend(payload)
        return out
    except Exception as e:  # noqa: BLE001 - best-effort enrichment; see Postconditions
        logger.warning(
            "blocked_by fetch for %s/%s#%d failed: %s; treating as no dependencies",
            owner,
            repo,
            issue_number,
            e,
        )
        return out


def _build_issue_item(raw: dict[str, Any], raw_deps: list[dict[str, Any]]) -> GitHubIssueItem:
    """Build a ``GitHubIssueItem`` from a raw issue payload and its blocked_by deps.

    Preconditions:
        - ``raw`` is a GitHub issue payload (carries at least ``number``).
        - ``raw_deps`` is the issue's blocked_by dependency objects (possibly empty).
    Postconditions:
        - Returns a fully-populated item; ``blocked`` is true iff any dependency is open.
          A dependency with a missing ``state`` is treated as open so a malformed object
          fails safe toward "blocked" rather than silently marking the issue runnable.
    """
    refs: list[GitHubDependencyRef] = []
    for dep in raw_deps:
        if not isinstance(dep, dict) or "number" not in dep:
            continue
        refs.append(
            GitHubDependencyRef(
                number=dep["number"],
                title=dep.get("title") or "",
                state=dep.get("state") or "open",
            )
        )
    open_dependencies = [ref.number for ref in refs if ref.state == "open"]
    return GitHubIssueItem(
        number=raw["number"],
        title=raw.get("title") or "",
        body_preview=(raw.get("body") or "")[:200],
        labels=[lbl["name"] for lbl in (raw.get("labels") or []) if isinstance(lbl, dict) and lbl.get("name")],
        html_url=raw.get("html_url") or "",
        dependencies=refs,
        open_dependencies=open_dependencies,
        blocked=bool(open_dependencies),
    )


def _resolve_github_access(token_override: str | None = None) -> tuple[dict[str, Any], str]:
    """Validate the GitHub integration is usable and return (cfg, token).

    Repository access is defined by the PAT itself (the repositories its GitHub
    authorization configuration grants), so this deliberately does NOT require an
    owner/repo — it is the shared prerequisite check for every GitHub route,
    including the repo-discovery listing that has no single target repository.

    Preconditions:
        - When ``token_override`` is ``None``, a stored PAT must be available (read
          from the credential store here). When supplied, the caller has already
          resolved a PAT (e.g. the GitHub webhook path, which reads the credential
          once and reuses it for both the PR-comment reaction and the review start).
    Postconditions:
        - Returns the JSON-only config dict plus the resolved token. A disabled
          integration or missing PAT raises ``HTTPException(400)``.
        - When ``token_override`` is ``None`` and no token is found, an unreachable
          Postgres credential store raises ``HTTPException(503)`` instead of 400, so a
          transient DB outage is never reported as "PAT not configured". The 400-vs-503
          decision comes from a SINGLE credential read
          (:func:`unified_api.integration_credentials.resolve_credential_with_env_fallback`
          reports value + reachability together — the same shared helper the GitHub
          webhook signing secret uses, so the two credentials can't silently diverge on
          this fail-closed behavior), so there is no second probe and no TOCTOU window
          between the read and the probe.
        - When ``token_override`` is supplied, the credential-store read is skipped
          entirely (the store is not re-touched) and ``token_override`` is returned
          verbatim; only the JSON-only ``enabled`` setting is validated.
          Blocking I/O — async callers offload via ``asyncio.to_thread``.
    """
    # JSON-only settings (no credential read) are always checked first. .get()
    # defensively: a malformed/legacy config record missing the key must read as
    # disabled, never as a KeyError → opaque 500.
    cfg = get_github_config_meta()
    if not cfg.get("enabled"):
        raise HTTPException(status_code=400, detail="GitHub integration is not enabled.")
    # A pre-resolved PAT is always a non-empty string; both ``None`` and ``""`` mean "no
    # override, read from the store" — an empty string is never a valid token to forward,
    # so falling through to the store read is the safe (and intended) behavior here.
    if token_override:
        token = token_override
    else:
        # The only DB round-trip on this path is the single
        # resolve_credential_with_env_fallback call below — which yields BOTH the token
        # value and the 503-vs-400 reachability signal (the PAT has no env fallback, so
        # env_var is omitted; only the reachability distinction is shared with the
        # webhook secret's own fail-closed check).
        token, store_reachable = resolve_credential_with_env_fallback(_GITHUB_SERVICE, "personal_access_token")
        if not token:
            # An empty token can mean two very different things, and the same read
            # tells us which: a down credential store (503, transient) vs a genuinely
            # missing PAT (400, operator action required). No separate probe → no race.
            if not store_reachable:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Cannot reach the GitHub credential store (Postgres); the integration "
                        "is temporarily unavailable. Restore the database connection and retry."
                    ),
                )
            raise HTTPException(status_code=400, detail="GitHub PAT not configured.")
    return cfg, token


def _validate_repo_component(label: str, value: str | None) -> str:
    """Normalize and validate a caller-supplied ``owner``/``repo`` component.

    Preconditions: ``label`` is ``"owner"`` or ``"repo"`` (used in the error detail);
        ``value`` is the raw caller-supplied string or ``None``.
    Postconditions: returns the stripped value (``""`` when absent/blank). Raises
        ``HTTPException(400)`` for any non-blank value outside GitHub's owner/repo
        character set (ASCII alphanumerics plus ``.``, ``_``, ``-``), or equal to
        ``".."``/``"."``. This is an allowlist, not a blocklist: it rejects not only
        path separators, null bytes, and whitespace but also URL metacharacters
        (``?`` ``#`` ``%`` ``@`` ``:`` …) that would otherwise rewrite the GitHub API
        request target once concatenated into the request path, and traversal
        segments that would escape the workspace root — no real GitHub owner/repo
        contains any of these.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if value in ("..", ".") or not _REPO_COMPONENT_RE.match(value):
        raise HTTPException(status_code=400, detail=f"invalid GitHub {label}: {value!r}")
    return value


def _resolve_github_target(
    token_override: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
) -> tuple[dict[str, Any], str, str, str]:
    """Validate GitHub integration config and return (cfg, token, owner, repo).

    This is the ONE validation path shared by every repository-scoped GitHub route —
    manual UI triggers and the webhook trigger cannot silently drift from each other.

    Preconditions:
        - The GitHub integration is enabled; the PAT prerequisite is exactly
          :func:`_resolve_github_access`'s (``token_override`` is forwarded verbatim).
          ``token_override`` exists for the webhook caller in
          ``github_events_handler.py``, which has already resolved the PAT and passes
          it through; every in-module route handler calls with ``token_override=None``
          and lets this function resolve the token.
        - ``owner``/``repo`` are the caller-requested target repository, or
          ``None``/blank to fall back to the configured default owner/repo (the
          legacy single-repo settings, kept as an optional default).
    Postconditions:
        - Returns the config dict plus the resolved token/owner/repo. The target is
          the request-supplied owner/repo when both are present (validated via
          :func:`_validate_repo_component`); supplying only one of the two raises
          ``HTTPException(400)``, as does having neither a request target nor a
          configured default. Which repositories the token can actually reach is
          decided by GitHub when the target is used — the PAT's own authorization
          configuration is the sole access list, never a Khala-side allowlist.
          Blocking I/O — async callers offload via ``asyncio.to_thread``.
    """
    cfg, token = _resolve_github_access(token_override)
    req_owner = _validate_repo_component("owner", owner)
    req_repo = _validate_repo_component("repo", repo)
    if req_owner and req_repo:
        return cfg, token, req_owner, req_repo
    if req_owner or req_repo:
        raise HTTPException(status_code=400, detail="GitHub owner and repo must be provided together.")
    # The configured default is operator-set, but a corrupted/misconfigured value would
    # otherwise flow unchecked into URL segments and filesystem paths — run it through the
    # SAME validation as a request-supplied target so the fallback can't traverse or build
    # a malformed GitHub URL. .get() defensively: a malformed/legacy record missing a key
    # surfaces as this clean 400, never a KeyError → opaque 500 (matching _repo_path_override).
    cfg_owner = _validate_repo_component("owner", cfg.get("owner", ""))
    cfg_repo = _validate_repo_component("repo", cfg.get("repo", ""))
    if not cfg_owner or not cfg_repo:
        raise HTTPException(
            status_code=400,
            detail="GitHub owner/repo not specified — pass owner/repo or configure a default repository.",
        )
    return cfg, token, cfg_owner, cfg_repo


def _github_api_headers(token: str) -> dict[str, str]:
    """Standard GitHub REST headers for the configured PAT."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "khala-integrations",
    }


async def _assert_pat_can_reach_repo(owner: str, repo: str, token: str) -> None:
    """Raise unless the stored PAT can actually reach ``owner``/``repo`` on GitHub.

    The issue- and PR-listing routes are gated implicitly: their own GitHub list call
    404s for a repository the token can't see, so a caller can only read issues/pulls
    for repos the PAT reaches. The review-history route reads only Khala's local
    ``code_review_runs`` table, so without this probe any enabled caller could request an
    arbitrary repository name and read persisted review summaries/errors for repos the
    token can't access. This restores the invariant that the PAT's own authorization —
    not a Khala-side allowlist — is the sole source of repository access, for the
    history-only route too.

    Preconditions:
        - ``owner``/``repo`` are already validated (see :func:`_validate_repo_component`).
        - ``token`` is the resolved PAT.
    Postconditions:
        - Returns ``None`` when ``GET /repos/{owner}/{repo}`` returns 200. Raises
          ``HTTPException`` otherwise: 404 when the PAT can't see the repository (GitHub
          returns 404 for both missing and inaccessible repos — the correct fail-closed
          signal, and identical to the issue/PR routes' not-found response), 401 for an
          invalid/expired token, 502 for any other upstream status, and 504/502 for a
          timeout/transport error. No ``httpx`` error escapes unhandled.
    """
    url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}"
    headers = _github_api_headers(token)
    try:
        async with httpx.AsyncClient(timeout=_GITHUB_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
    except httpx.TimeoutException as e:
        raise HTTPException(status_code=504, detail="GitHub API timed out verifying repository access.") from e
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach GitHub to verify repository access: {e}") from e
    if resp.status_code == 200:
        return
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="GitHub token is invalid or expired.")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Repository {owner}/{repo} not found.")
    raise HTTPException(status_code=502, detail=f"GitHub API returned {resp.status_code} verifying repository access.")


async def _collect_github_pages(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
    max_pages: int,
    not_found_message: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch every page of a GitHub list endpoint, mapping HTTP errors to HTTPException.

    Postconditions:
        - Returns ``(raw_items, has_more)`` where ``has_more`` is True iff the page
          cap (rather than the end of the list) stopped pagination. 401/404/non-200
          responses raise ``HTTPException`` (401/404/502) — ``not_found_message`` is the
          caller-supplied 404 message (a repository-scoped listing names the repo; the
          account-scoped repo listing names the token). Shared by the repo-, issue- and
          PR-listing routes so their pagination and error mapping cannot drift.
    """
    raw: list[dict[str, Any]] = []
    has_more = False
    page_iter = _iter_github_pages(client, base_url, headers, params, max_pages)
    async with contextlib.aclosing(page_iter) as pages:
        async for resp in pages:
            if resp.status_code == 401:
                raise HTTPException(status_code=401, detail="GitHub token is invalid or expired.")
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=not_found_message)
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"GitHub API returned {resp.status_code}.")
            # A 200 whose body isn't JSON (an HTML error page from a proxy/outage) would
            # otherwise raise inside resp.json() and escape as an unhandled 500. Map it to a
            # 502 like any other upstream failure. json.JSONDecodeError is a ValueError
            # subclass, so catching ValueError covers httpx's decode error too.
            try:
                page = resp.json()
            except ValueError as e:
                raise HTTPException(status_code=502, detail="GitHub API returned a non-JSON response.") from e
            # A list endpoint always returns a JSON array on 200; a non-list body is
            # malformed, so treat it as an upstream error rather than iterating a dict's keys.
            # Log the actual shape (e.g. a ``{"message": ...}`` error object served with a 200)
            # so the 502 is diagnosable without reproducing the upstream response.
            if not isinstance(page, list):
                logger.warning(
                    "GitHub list endpoint %s returned a 200 with a non-list body (%s); mapping to 502",
                    base_url,
                    type(page).__name__,
                )
                raise HTTPException(status_code=502, detail="GitHub API returned an unexpected response shape.")
            raw.extend(page)
            has_more = bool(resp.links.get("next", {}).get("url"))
    return raw, has_more


def _build_github_repo_item(raw: dict[str, Any]) -> GitHubRepoItem:
    """Map a raw GitHub repository payload onto the picker's repo model.

    Preconditions: ``raw`` is a repository object from ``GET /user/repos`` (carries at
        least ``name``; a missing/malformed field degrades to its model default).
    Postconditions: returns a fully-populated item; ``owner`` comes from
        ``owner.login`` and ``full_name`` falls back to ``owner/name`` when absent.
    """
    owner = str((raw.get("owner") or {}).get("login") or "")
    name = str(raw.get("name") or "")
    issues_count = raw.get("open_issues_count")
    return GitHubRepoItem(
        owner=owner,
        name=name,
        full_name=str(raw.get("full_name") or f"{owner}/{name}"),
        private=bool(raw.get("private", False)),
        archived=bool(raw.get("archived", False)),
        html_url=str(raw.get("html_url") or ""),
        description=str(raw.get("description") or ""),
        default_branch=str(raw.get("default_branch") or ""),
        open_issues_count=issues_count if isinstance(issues_count, int) and not isinstance(issues_count, bool) else 0,
        pushed_at=str(raw.get("pushed_at") or ""),
    )


@router.get("/github/repos", response_model=list[GitHubRepoItem])
async def list_github_repos() -> list[GitHubRepoItem]:
    """List every repository the stored PAT can access.

    Backed by GitHub's ``GET /user/repos`` for the stored token, so the token's own
    authorization configuration (fine-grained repo grants, or a classic PAT's account
    access) is the single source of truth — no repository list is configured in Khala.
    Follows GitHub's ``Link``-header pagination so the pickers see the complete set.

    Preconditions:
        - The GitHub integration is enabled and a PAT is stored; each missing
          prerequisite raises ``HTTPException(400)`` (503 when the credential store is
          unreachable). No owner/repo configuration is required.
    Postconditions:
        - Returns the accessible repositories most-recently-pushed first (GitHub's
          ``sort=pushed`` default order), bounded by ``_GITHUB_MAX_REPO_PAGES`` pages of
          ``_GITHUB_REPOS_PER_PAGE`` items. Hitting that bound logs a warning and
          returns the repositories gathered so far rather than failing. Items without a
          resolvable name are dropped.
    """
    _cfg, token = await asyncio.to_thread(_resolve_github_access)

    params: dict[str, Any] = {"per_page": _GITHUB_REPOS_PER_PAGE, "sort": "pushed"}
    headers = _github_api_headers(token)
    base_url = f"{_GITHUB_API_BASE}/user/repos"

    async with httpx.AsyncClient(timeout=_GITHUB_HTTP_TIMEOUT) as client:
        raw_repos, has_more = await _collect_github_pages(
            client,
            base_url,
            headers,
            params,
            _GITHUB_MAX_REPO_PAGES,
            not_found_message="GitHub did not recognize the stored token's account.",
        )
    # Keep exactly the items with a usable string name (GitHub always sends one);
    # anything else can't be addressed as owner/name by the downstream routes.
    items = [
        _build_github_repo_item(raw)
        for raw in raw_repos
        if isinstance(raw, dict) and isinstance(raw.get("name"), str) and raw["name"]
    ]

    if has_more:
        logger.warning(
            "list_github_repos hit the %d-page cap; returning the first %d repositories only",
            _GITHUB_MAX_REPO_PAGES,
            len(items),
        )
    return items


@router.get("/github/issues", response_model=list[GitHubIssueItem])
async def list_github_issues(
    label: str | None = Query(default=None),
    owner: str | None = Query(default=None),
    repo: str | None = Query(default=None),
) -> list[GitHubIssueItem]:
    """List every open issue from a GitHub repository the PAT can access.

    Follows GitHub's ``Link``-header pagination so the panel shows the complete set
    of open issues rather than only the first page. Each returned issue is enriched
    with its ``blocked_by`` issue dependencies so the picker can flag blocked issues.

    Preconditions:
        - The GitHub integration is enabled and a PAT is stored; the target repository
          is the ``owner``/``repo`` query pair, falling back to the configured default
          — each missing prerequisite raises ``HTTPException(400)``.
    Postconditions:
        - Returns every open issue (pull requests excluded) across all result pages,
          in GitHub's response order, bounded by ``_GITHUB_MAX_ISSUE_PAGES`` pages of
          ``_GITHUB_ISSUES_PER_PAGE`` items. Hitting that bound logs a warning and
          returns the issues gathered so far rather than failing.
        - Each item carries ``dependencies`` (its ``blocked_by`` issues), the derived
          ``open_dependencies`` (numbers still open) and ``blocked`` flag. Dependency
          fetches fan out concurrently bounded by ``GITHUB_DEPENDENCY_CONCURRENCY``
          (default 8); a failed or absent lookup yields empty dependencies for that
          issue and never fails the list. This adds roughly one extra request wave per
          ``GITHUB_DEPENDENCY_CONCURRENCY`` issues to the response latency.
    """
    # Whether the caller named a specific repository (vs. falling back to the configured
    # default) — captured before _resolve_github_target overwrites owner/repo below.
    explicit_target = bool((owner or "").strip() and (repo or "").strip())
    cfg, token, owner, repo = await asyncio.to_thread(_resolve_github_target, None, owner, repo)

    params: dict[str, Any] = {"state": "open", "per_page": _GITHUB_ISSUES_PER_PAGE}
    # The configured ``default_label`` is scoped to the legacy default repo. Applying it to
    # an explicitly-targeted repo would silently hide every issue that repo never tagged, so
    # the config fallback label applies ONLY when no specific repo was requested. An explicit
    # ``?label=`` always wins. .get() defensively: a config saved before this field existed
    # must not KeyError → 500.
    use_label = label or (None if explicit_target else cfg.get("default_label"))
    if use_label:
        params["labels"] = use_label
    headers = _github_api_headers(token)
    base_url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/issues"

    async with httpx.AsyncClient(timeout=_GITHUB_HTTP_TIMEOUT) as client:
        raw_pages, has_more = await _collect_github_pages(
            client,
            base_url,
            headers,
            params,
            _GITHUB_MAX_ISSUE_PAGES,
            not_found_message=f"Repository {owner}/{repo} not found.",
        )
        # Exclude pull requests (the issues endpoint returns both), and drop any entry
        # without an integer ``number`` — a malformed payload must not KeyError → 500 when
        # ``number`` is read below (for the dependency fetch and the issue view-model).
        # ``bool`` is an ``int`` subclass, so exclude it: a ``number: true`` must not read as 1.
        raw_issues = [
            raw
            for raw in raw_pages
            if "pull_request" not in raw
            and isinstance(raw.get("number"), int)
            and not isinstance(raw.get("number"), bool)
        ]

        # Enrich each issue with its blocked_by dependencies. Fan out under a bounded
        # semaphore so a large page is not an N+1 storm of serial round-trips.
        sem = asyncio.Semaphore(_GITHUB_DEPENDENCY_CONCURRENCY)
        dep_results = await asyncio.gather(
            *(_fetch_blocked_by(client, owner, repo, raw["number"], headers, sem) for raw in raw_issues)
        )

    # Build each item once, with its dependencies resolved, so the model is complete
    # at construction rather than mutated afterwards.
    items = [_build_issue_item(raw, raw_deps) for raw, raw_deps in zip(raw_issues, dep_results, strict=True)]

    if has_more:
        logger.warning(
            "list_github_issues hit the %d-page cap for %s/%s; returning the first %d open issues only",
            _GITHUB_MAX_ISSUE_PAGES,
            owner,
            repo,
            len(items),
        )
    return items


def _build_pull_request_item(raw: dict[str, Any]) -> GitHubPullRequestItem:
    """Map a raw GitHub pull-request payload onto the panel's PR model.

    Field extraction is delegated to the coding team's ``_pr_detail_from_payload`` so
    GitHub's PR payload shape is parsed in exactly one place; this only adapts the
    parsed detail to the panel's response model (truncating the body for preview).
    """
    detail = _pr_detail_from_payload(raw)
    return GitHubPullRequestItem(
        number=detail.number,
        title=detail.title,
        body_preview=detail.body or "",
        author=detail.author,
        html_url=detail.html_url,
        head=detail.head,
        base=detail.base,
        draft=detail.draft,
        labels=list(detail.labels),
        updated_at=detail.updated_at,
    )


@router.get("/github/pulls", response_model=list[GitHubPullRequestItem])
async def list_github_pulls(
    owner: str | None = Query(default=None),
    repo: str | None = Query(default=None),
) -> list[GitHubPullRequestItem]:
    """List every open pull request from a GitHub repository the PAT can access.

    Follows GitHub's ``Link``-header pagination so the panel shows the complete set
    of open PRs rather than only the first page.

    Preconditions:
        - The GitHub integration is enabled and a PAT is stored; the target repository
          is the ``owner``/``repo`` query pair, falling back to the configured default
          — each missing prerequisite raises ``HTTPException(400)``.
    Postconditions:
        - Returns every open pull request in GitHub's response order, bounded by
          ``_GITHUB_MAX_PR_PAGES`` pages of ``_GITHUB_PRS_PER_PAGE`` items. Hitting that
          bound logs a warning and returns the PRs gathered so far rather than failing.
    """
    _cfg, token, owner, repo = await asyncio.to_thread(_resolve_github_target, None, owner, repo)

    params: dict[str, Any] = {"state": "open", "per_page": _GITHUB_PRS_PER_PAGE}
    headers = _github_api_headers(token)
    base_url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/pulls"

    async with httpx.AsyncClient(timeout=_GITHUB_HTTP_TIMEOUT) as client:
        raw_pulls, has_more = await _collect_github_pages(
            client,
            base_url,
            headers,
            params,
            _GITHUB_MAX_PR_PAGES,
            not_found_message=f"Repository {owner}/{repo} not found.",
        )
    items = [_build_pull_request_item(raw) for raw in raw_pulls]

    if has_more:
        logger.warning(
            "list_github_pulls hit the %d-page cap for %s/%s; returning the first %d open PRs only",
            _GITHUB_MAX_PR_PAGES,
            owner,
            repo,
            len(items),
        )
    return items


def _repo_path_override(cfg: dict[str, Any], owner: str, repo: str) -> str:
    """Return the operator's pinned checkout path when it applies to this target.

    Preconditions: ``cfg`` is a :func:`get_github_config_meta` dict; ``owner``/``repo``
        are the resolved target repository.
    Postconditions: returns the ``repo_path`` override only when one is set AND the
        target matches the configured default owner/repo (case-insensitively, as GitHub
        treats them); ``""`` otherwise. The override predates PAT-wide repository
        access and pins a single local checkout, so it must never be applied to a
        *different* repository the PAT can now reach — that checkout's remote wouldn't
        match and every run against it would fail.
    """
    override = cfg.get("repo_path", "").strip()
    if not override:
        return ""
    # Strip the stored values before comparing: the resolved target was stripped by
    # _validate_repo_component, so an un-stripped stored default (e.g. " acme ") would
    # otherwise never match and the pinned checkout would be silently ignored.
    cfg_owner = str(cfg.get("owner", "")).strip()
    cfg_repo = str(cfg.get("repo", "")).strip()
    if cfg_owner.casefold() == owner.casefold() and cfg_repo.casefold() == repo.casefold():
        return override
    return ""


def _resolve_repo_path(cfg: dict[str, Any], owner: str, repo: str, issue_number: int | None = None) -> str:
    """Resolve the local checkout path for the coding team.

    Priority: config override (default repo only) > SE_WORKSPACE_DIR env >
    WORKSPACE_ROOT env > AGENT_CACHE fallback.
    The ``AGENT_CACHE`` fallback defaults to the relative ``.agent_cache`` (a
    repo-wide convention), so when neither ``AGENT_CACHE`` nor a workspace-root
    env var is set the path is resolved against the process working directory;
    deployments that need a stable absolute location set ``AGENT_CACHE`` (Docker
    sets it to ``/data/agents``).

    When ``issue_number`` is given and the path is auto-derived (no operator
    override), the checkout is namespaced per-issue with an ``issue-{N}`` segment
    so multiple coding-team jobs can run on different issues of the same
    repository in true filesystem isolation, concurrently. An operator override
    is returned verbatim — the operator manages that checkout themselves, so it
    is neither per-issue-namespaced nor auto-cleaned.

    Preconditions:
        - ``owner`` and ``repo`` are the non-empty target repository (the run
          routes resolve this via ``_resolve_github_target`` before calling).
        - ``issue_number`` is a positive issue number or ``None`` (the PR-review
          path passes ``None`` and gets the repo-level path; it never clones).
    Postconditions:
        - Returns the override verbatim when set and applicable to this target
          (see :func:`_repo_path_override`). The override is trusted operator
          configuration and is intentionally NOT traversal-sanitized (unlike the
          auto-derived ``owner``/``repo`` below) and not auto-cleaned; if that
          config source ever accepts untrusted input it must be sanitized by the
          caller.
        - Otherwise returns an absolute derived path; with ``issue_number`` set
          the path ends in ``issue-{issue_number}`` and two distinct issue
          numbers map to two distinct paths.
        - Raises ``HTTPException(400)`` when ``owner``/``repo`` are missing or
          carry a path separator, ``..`` segment, or null byte, or when
          ``issue_number`` is non-positive — defense-in-depth so this path
          builder can't be coerced into escaping the workspace root or building
          a degenerate ``issue-0`` segment even if a caller skipped validation.

    Note:
        The auto-derived layout differs by source: a workspace-root env var gives
        ``{root}/{owner}_{repo}[/issue-N]`` while the ``AGENT_CACHE`` fallback
        gives ``{cache}/github_workspaces/{owner}/{repo}[/issue-N]``. This is
        intentional (``AGENT_CACHE`` is a shared multi-team cache namespaced under
        ``github_workspaces``; a dedicated workspace root is not), and
        ``ephemeral_workspace_roots`` mirrors both shapes for the cleanup guard.
    """
    override = _repo_path_override(cfg, owner, repo)
    if override:
        return override

    # Enforce the documented precondition explicitly: owner/repo must be present
    # and non-empty before they become path components. A caller that bypassed
    # upstream validation would otherwise build a degenerate path; surface a
    # clean 400 instead.
    for label, value in (("owner", owner), ("repo", repo)):
        if not value:
            raise HTTPException(status_code=400, detail=f"missing GitHub {label}")

    # Defense-in-depth: owner/repo become path components below, so reject any value
    # that could traverse out of the workspace or was otherwise never a legal component.
    # Delegate the character-class rules to the ONE validation predicate (same 400 detail)
    # so this filesystem-path layer can never disagree with the request boundary on what is
    # acceptable. The routes resolve owner/repo through _validate_repo_component (which
    # strips) before reaching here, so a value that isn't already its own stripped form
    # means a caller bypassed that boundary — reject it rather than silently normalize.
    for label, value in (("owner", owner), ("repo", repo)):
        if value != value.strip():
            raise HTTPException(status_code=400, detail=f"invalid GitHub {label}: {value!r}")
        _validate_repo_component(label, value)

    # Enforce the documented precondition: a non-positive issue_number would yield
    # a degenerate ``issue-0`` / ``issue--1`` segment (and never names a real
    # GitHub issue), so reject it here rather than build a bad path.
    if issue_number is not None and issue_number < 1:
        raise HTTPException(status_code=400, detail=f"issue_number must be positive: {issue_number!r}")

    issue_segment = PER_ISSUE_DIR_TEMPLATE.format(issue_number=issue_number) if issue_number is not None else None

    # Auto-derived paths are resolved to absolute so they are stable regardless
    # of the process working directory at clone vs. cleanup time, and so the
    # ephemeral-root safety check (which also resolves) compares like with like.
    for env_var in ("SE_WORKSPACE_DIR", "WORKSPACE_ROOT"):
        val = os.environ.get(env_var, "").strip()
        if val:
            base = Path(val) / f"{owner}_{repo}"
            target = base / issue_segment if issue_segment else base
            return str(target.resolve())

    # Shared AGENT_CACHE resolver (single source of truth) so the derived path
    # and the cleanup safety root in ephemeral_workspace_roots never diverge.
    base = Path(agent_cache_dir()) / "github_workspaces" / owner / repo
    target = base / issue_segment if issue_segment else base
    return str(target.resolve())


def _git_auth_env(token: str) -> dict[str, str]:
    """Build env dict that injects Basic credentials via GIT_CONFIG_* env vars.

    GitHub's git smart-HTTP endpoint only accepts ``Basic`` credentials
    (username ``x-access-token``, password = the token). A ``Bearer`` header —
    which the REST API accepts — is rejected by the git endpoint with 401
    ``invalid credentials`` even when the token is valid, after which git
    tries to prompt for a username and fails headless ("terminal prompts
    disabled"). Unlike ``-c http.extraHeader``, environment-based config is
    transient and never written to ``.git/config`` — safe for clone and
    fetch alike.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _scrub_git_secret(text: str, token: str) -> str:
    """Redact every representation of the credential from git output.

    Preconditions:
        - ``token`` is non-empty.
    Postconditions:
        - Neither the raw token nor its Basic-encoded form (the header value
          built by ``_git_auth_env``) appears in the returned text.
    """
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return text.replace(token, "***").replace(encoded, "***")


def _redact_url_userinfo(url: str) -> str:
    """Strip any ``user:pass@`` userinfo from a URL so it is safe to surface in errors.

    Preconditions: ``url`` is a string (need not be a valid URL).
    Postconditions: returns the URL with its userinfo (``user[:pass]@``) removed. An
        operator-pinned checkout's remote may embed credentials this service does not
        control (and ``_scrub_git_secret`` only knows the *PAT*, not those), so echoing
        the raw remote in a mismatch error could leak them. On a parse failure returns
        ``"<redacted>"`` rather than risk leaking an unparseable-but-credentialed URL.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        # ``.hostname`` never raises, but ``.port`` validates lazily and raises ValueError on
        # a malformed/out-of-range port — read both inside the guard so a bad remote returns
        # "<redacted>" rather than letting this safety helper itself raise.
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<redacted>"
    if not hostname:
        # No recognizable authority (e.g. an ssh ``git@host:owner/repo`` scp-like remote):
        # drop anything before an ``@`` defensively rather than echo possible credentials.
        return url.split("@", 1)[-1] if "@" in url else url
    netloc = hostname + (f":{port}" if port else "")
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc))


def _remote_matches(remote_url: str, owner: str, repo: str) -> bool:
    """True iff ``remote_url``'s final ``owner/repo`` segments match exactly.

    A substring check (``f"{owner}/{repo}" in url``) gives false positives — e.g.
    ``acme/widget`` matches ``acme/widget-extra``, and ``acme/widget`` is also a
    suffix of ``notacme/widget``. This compares the last two path segments
    exactly (case-insensitively, since GitHub treats owner/repo that way) after
    stripping a trailing ``.git``/slash, and normalizes the ``git@host:owner/repo``
    scp form to ``/``-separated so both URL styles work.

    Postconditions:
        - Returns True iff the remote's last two segments equal ``owner``/``repo``
          case-insensitively; False otherwise (including malformed/short URLs).
    """
    cleaned = remote_url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    parts = [seg for seg in cleaned.replace(":", "/").split("/") if seg]
    if len(parts) < 2:
        return False
    got_owner, got_repo = parts[-2], parts[-1]
    return got_owner.casefold() == owner.casefold() and got_repo.casefold() == repo.casefold()


def _ensure_repo_clone(repo_path: str, owner: str, repo: str, token: str, *, platform_owned: bool = True) -> str | None:
    """Clone or fetch the repository.

    Auth is passed via ``GIT_CONFIG_*`` environment variables so the token
    is transient and never persisted in ``.git/config``.

    Preconditions:
        - ``owner`` and ``repo`` are non-empty; ``token`` authorizes read access.
        - ``platform_owned`` is True for an auto-derived per-issue checkout this
          service owns (and may later auto-clean), False for an operator-pinned
          ``repo_path`` the operator manages.
    Postconditions:
        - On success the repository is present at ``repo_path`` and ``None`` is
          returned.
        - Every failure is returned as a human-readable, token-scrubbed string:
          a missing ``git`` binary, a clone/fetch timeout, a non-zero git exit,
          or an existing checkout that points at a different remote. This function
          never lets a ``subprocess`` exception escape, so the caller can surface
          a clean error rather than an unhandled 500.
        - For a platform-owned checkout, clone-or-fetch is serialized per checkout
          via an exclusive ``flock`` on a sibling lock file, so two concurrent
          requests for the same issue (even across worker processes on the shared
          host volume) cannot interleave a ``git clone`` into a half-populated
          directory. An operator-pinned checkout is fetched **without** the sibling
          lock: it is never auto-cleaned (so the lock's survive-the-rmtree role
          doesn't apply) and may live under a parent the service cannot write,
          where creating a sibling lock would wrongly fail an otherwise-valid
          fetch.
    """
    env = _git_auth_env(token)
    path = Path(repo_path)

    # mkdir(exist_ok=True) on an existing parent is a no-op and needs no write
    # permission, so this stays safe for operator paths under read-only parents.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"could not prepare workspace dir for {owner}/{repo}: {e}"

    def _clone_or_fetch() -> str | None:
        try:
            if path.is_dir() and (path / ".git").is_dir():
                url_check = subprocess.run(
                    ["git", "-C", repo_path, "remote", "get-url", "origin"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                url_out = url_check.stdout.strip()
                if url_check.returncode != 0 or not _remote_matches(url_out, owner, repo):
                    # Redact any embedded credentials before surfacing the remote in the error.
                    return (
                        f"existing checkout at {repo_path} does not match {owner}/{repo} "
                        f"(remote origin: {_redact_url_userinfo(url_out)})"
                    )

                result = subprocess.run(
                    ["git", "-C", repo_path, "fetch", "--all"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=env,
                )
                if result.returncode != 0:
                    return f"git fetch failed: {_scrub_git_secret(result.stderr, token)}"
                return None

            clone_url = f"https://github.com/{owner}/{repo}.git"
            result = subprocess.run(
                ["git", "clone", clone_url, repo_path],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
            if result.returncode != 0:
                safe_err = _scrub_git_secret(result.stderr, token)
                return f"git clone failed: {safe_err}"
            return None
        except FileNotFoundError:
            # `git` is not on PATH in this image — surfaces as a clear message
            # instead of a FileNotFoundError bubbling up to an opaque 500.
            return "git executable not found on the server; install git in the API image."
        except subprocess.TimeoutExpired as e:
            return f"git operation timed out after {e.timeout:.0f}s while preparing {owner}/{repo}."

    # Operator-pinned checkout: fetch directly, no sibling lock (see Postconditions).
    if not platform_owned:
        return _clone_or_fetch()

    # Platform-owned per-issue checkout: serialize via an exclusive flock on a
    # sibling lock file. open()/flock() failures are workspace problems (and a
    # FileNotFoundError from open must not be mistaken for a missing git binary),
    # so they are handled here rather than by the git-specific handler above. The
    # lock lives beside the checkout so it survives the coding team's post-success
    # rmtree of repo_path.
    lock_path = clone_lock_path(path)
    with contextlib.ExitStack() as stack:
        try:
            # Isolates a lock-acquisition failure (open()/flock() inside
            # flock_lock's __enter__) from anything _clone_or_fetch() itself
            # might raise: only the former should produce the "could not
            # acquire clone lock" message below.
            stack.enter_context(flock_lock(lock_path))
        except OSError as e:
            return f"could not acquire clone lock for {owner}/{repo}: {e}"
        return _clone_or_fetch()


def _require_coding_team_url() -> str:
    """Return the configured coding-team service URL or raise a 503.

    Preconditions: none.
    Postconditions: returns the stripped ``CODING_TEAM_SERVICE_URL`` when set to a
        non-blank value; raises ``HTTPException(503)`` otherwise. Single source for
        the "downstream configured?" check shared by the three coding-team routes so
        their failure mode cannot drift.
    """
    coding_team_url = os.environ.get("CODING_TEAM_SERVICE_URL", "").strip()
    if not coding_team_url:
        raise HTTPException(status_code=503, detail="Coding team service not configured (CODING_TEAM_SERVICE_URL).")
    return coding_team_url


async def _forward_to_coding_team(
    coding_team_url: str,
    path: str,
    *,
    method: str = "POST",
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: float = 60.0,
    log_prefix: str,
    timeout_detail: str,
    generic_failure_detail: str,
) -> Any:
    """POST/GET ``path`` on the coding-team service and return its parsed JSON body.

    Preconditions:
        - ``coding_team_url`` is the configured coding-team service base URL (see
          :func:`_require_coding_team_url`).
        - ``method`` is ``"POST"`` or ``"GET"``; the caller supplies ``json_body``
          for a POST or ``params`` for a GET, matching that method's contract.
    Postconditions:
        - Returns the upstream response's parsed JSON body on a 200. Every
          failure path raises ``HTTPException`` instead of letting an ``httpx``
          error (or a malformed response body) escape unhandled: a timeout
          raises 504 with ``timeout_detail``, an unreachable service raises 502,
          and a non-200 response re-raises the upstream status code with a
          bounded copy of its detail for a 4xx or ``generic_failure_detail`` for
          a 5xx (never echoing a possible stack trace to the client).
    """
    target = f"{coding_team_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=10.0)) as client:
            resp = (
                await client.get(target, params=params)
                if method == "GET"
                else await client.post(target, json=json_body)
            )
    except httpx.TimeoutException as e:
        logger.warning("%s: coding team service timed out: %s", log_prefix, e)
        raise HTTPException(status_code=504, detail=timeout_detail) from e
    except httpx.HTTPError as e:
        logger.warning("%s: cannot reach coding team service at %s: %s", log_prefix, coding_team_url, e)
        raise HTTPException(status_code=502, detail=f"Could not reach coding team service: {e}") from e

    if resp.status_code != 200:
        # The upstream body can carry internal detail (a stack trace on an unhandled
        # 5xx, an HTML error page). Always log it server-side. For 4xx the detail is
        # client-actionable, so return a bounded copy; for 5xx return a generic
        # message so internal traces aren't exposed.
        try:
            upstream_detail = resp.json().get("detail", resp.text)
        except Exception:
            upstream_detail = resp.text
        logger.warning("%s: coding team service returned %s: %s", log_prefix, resp.status_code, upstream_detail)
        client_detail = str(upstream_detail) if resp.status_code < 500 else generic_failure_detail
        raise HTTPException(status_code=resp.status_code, detail=client_detail)

    try:
        return resp.json()
    except ValueError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Coding team service returned an unexpected response: {e}",
        ) from e


@router.post("/github/run-issue", response_model=RunGitHubIssueResponse)
async def run_github_issue(body: RunGitHubIssueRequest) -> RunGitHubIssueResponse:
    """Start the coding team on a specific GitHub issue.

    Preconditions:
        - GitHub integration is enabled with a stored PAT. The target repository comes
          from the request body (``owner``/``repo``); if omitted it falls back to the
          optional configured default, and a target reachable by the PAT is required.
        - ``CODING_TEAM_SERVICE_URL`` points at a reachable coding-team service.
    Postconditions:
        - On success returns a ``RunGitHubIssueResponse`` describing the started job.
        - Every failure path raises ``HTTPException`` with an explanatory ``detail``;
          no ``subprocess`` or ``httpx`` error is allowed to escape as an unhandled
          exception. An unhandled exception becomes a 500 generated *outside* the CORS
          middleware, so the browser receives it without an ``Access-Control-Allow-Origin``
          header, drops the response, and the UI can only report an opaque
          "0 Unknown Error" — useless for diagnosis.
    """
    # Centralized validation (enabled + PAT + target repo), which also maps an
    # unreachable credential store to a 503 rather than a misleading "not configured".
    cfg, token, owner, repo = await asyncio.to_thread(_resolve_github_target, None, body.owner, body.repo)

    # Validate the downstream is configured before the (slow) clone, so a
    # misconfiguration fails fast instead of after a multi-second checkout.
    coding_team_url = _require_coding_team_url()

    # Namespace the checkout per-issue (unless the operator pins an explicit
    # repo_path for this target repo) so two issues of the same repo get isolated
    # clones and can run concurrently. The per-issue clone is platform-owned and
    # ephemeral, so ask the coding team to delete it once the work is safely
    # published to a PR; an operator-managed override is never auto-cleaned.
    repo_path = _resolve_repo_path(cfg, owner, repo, issue_number=body.issue_number)
    cleanup_checkout_on_success = not _repo_path_override(cfg, owner, repo)

    loop = asyncio.get_running_loop()
    clone_err = await loop.run_in_executor(
        None,
        functools.partial(
            _ensure_repo_clone, repo_path, owner, repo, token, platform_owned=cleanup_checkout_on_success
        ),
    )
    if clone_err:
        logger.warning("github run-issue: repository preparation failed: %s", clone_err)
        raise HTTPException(status_code=502, detail=clone_err)

    payload: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "repo_path": repo_path,
        "issue_number": body.issue_number,
        "github_token": token,
        "cleanup_checkout_on_success": cleanup_checkout_on_success,
    }
    if body.base_branch:
        payload["base_branch"] = body.base_branch

    # connect fast-fails an unreachable service; the longer read budget covers the
    # coding team's synchronous GitHub API round-trips inside /run-from-github.
    data = await _forward_to_coding_team(
        coding_team_url,
        "run-from-github",
        json_body=payload,
        log_prefix="github run-issue",
        timeout_detail="Coding team service timed out while starting the job.",
        # e.g. "issue blocked by sub-issues", "no ready issues" for 4xx.
        generic_failure_detail="Failed to start the coding job.",
    )
    try:
        return RunGitHubIssueResponse(
            job_id=data["job_id"],
            issue_number=data["issue_number"],
            issue_url=data["issue_url"],
            status=data.get("status", "pending"),
            message=data.get("message", ""),
        )
    except (KeyError, TypeError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Coding team service returned an unexpected response: {e}",
        ) from e


async def _start_pr_review(
    pr_number: int,
    base_branch: str | None,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
) -> RunPrReviewResponse:
    """Resolve the GitHub target and start a PR review on the coding-team service.

    Shared by ``POST /github/review-pr`` (manual UI trigger) and the GitHub webhook
    handler (``@khala review`` PR comment) so the two paths cannot drift.

    Unlike ``run_github_issue`` this does **not** clone the repository: the review
    reads the PR diff and file content purely through the GitHub REST API, so the
    multi-second checkout is skipped. ``repo_path`` is still resolved and forwarded
    for request-shape parity with ``/run-from-github``; the coding-team hook never
    touches the checkout.

    Preconditions:
        - GitHub integration is enabled, and either a stored PAT is available or
          ``token`` was supplied by the caller.
        - ``CODING_TEAM_SERVICE_URL`` points at a reachable coding-team service.
        - ``token``, when supplied, is a GitHub PAT the caller already resolved (the
          webhook path passes this so a single credential-store read serves the whole
          request); when ``None`` the PAT is read via ``_resolve_github_target``.
        - ``owner``/``repo``, when supplied, are the target repository (the UI route
          forwards the request body's target; the webhook path forwards the commented
          PR's repository). Blank/``None`` falls back to the configured default
          owner/repo. Whether the PAT can actually reach the target is GitHub's
          decision when the review runs — the token's authorization configuration is
          the sole access list.
    Postconditions:
        - On success returns a ``RunPrReviewResponse`` describing the started review
          job. Every failure path raises ``HTTPException`` with an explanatory detail;
          no ``httpx`` error escapes as an unhandled exception.
    """
    # Single validation path (shared with every other GitHub route via
    # _resolve_github_target) — token, when pre-resolved by the caller (webhook path),
    # is forwarded as an override so the credential store is never re-touched; otherwise
    # it's read here, with the same 503-vs-400 reachability handling as the UI route.
    cfg, token, owner, repo = await asyncio.to_thread(_resolve_github_target, token, owner, repo)

    coding_team_url = _require_coding_team_url()

    payload: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "repo_path": _resolve_repo_path(cfg, owner, repo),
        "pr_number": pr_number,
        "github_token": token,
    }
    if base_branch:
        payload["base_branch"] = base_branch

    data = await _forward_to_coding_team(
        coding_team_url,
        "review-pr",
        json_body=payload,
        log_prefix="github review-pr",
        timeout_detail="Coding team service timed out while starting the review.",
        # e.g. "PR not found" for 4xx.
        generic_failure_detail="Failed to start the review.",
    )
    try:
        return RunPrReviewResponse(
            job_id=data["job_id"],
            pr_number=data["pr_number"],
            pr_url=data["pr_url"],
            status=data.get("status", "pending"),
            message=data.get("message", ""),
            created_at=data.get("created_at"),
        )
    except (KeyError, TypeError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Coding team service returned an unexpected response: {e}",
        ) from e


@router.post("/github/review-pr", response_model=RunPrReviewResponse)
async def run_github_review_pr(body: RunPrReviewRequest) -> RunPrReviewResponse:
    """Start the code-reviewer agents on a specific open pull request.

    Thin wrapper over :func:`_start_pr_review` (the shared review-start path also used by
    the ``@khala review`` PR-comment webhook).

    Preconditions: ``body`` carries a ``pr_number``, optional ``base_branch``, and an
        optional target ``owner``/``repo``; see :func:`_start_pr_review` for the full
        GitHub-target precondition.
    Postconditions: returns exactly what :func:`_start_pr_review` returns/raises for
        ``(body.pr_number, body.base_branch, body.owner, body.repo)`` with no
        pre-resolved token (the PAT is read fresh here) — this route delegates its
        whole contract to that function.
    """
    return await _start_pr_review(body.pr_number, body.base_branch, owner=body.owner, repo=body.repo)


@router.get("/github/reviews", response_model=list[CodeReviewRunItem])
async def list_github_reviews(
    pr_number: int | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    owner: str | None = Query(default=None),
    repo: str | None = Query(default=None),
) -> list[CodeReviewRunItem]:
    """List persisted code-review runs for a repository the PAT can access.

    Powers the Code Review page's per-PR review history: row status badges and
    the expanded reviews table. The target repository is the ``owner``/``repo``
    query pair (falling back to the configured default), then the request is
    forwarded to the coding-team service which owns the ``code_review_runs`` table.

    Preconditions:
        - GitHub integration is enabled with a stored PAT; the target repository is
          the ``owner``/``repo`` query pair or the configured default, and the PAT can
          actually reach it (verified against GitHub — see below).
        - ``CODING_TEAM_SERVICE_URL`` points at a reachable coding-team service.
        - ``limit`` is in ``[1, 2000]`` (validated by FastAPI).
    Postconditions:
        - Returns up to ``limit`` review runs (optionally filtered to
          ``pr_number``), newest-first. Every failure path raises
          ``HTTPException``; no ``httpx`` error escapes unhandled.
        - Unlike the issue/PR routes (implicitly gated by their own GitHub list call),
          this reads Khala's local ``code_review_runs`` table, so it first verifies the
          PAT can reach the target repository (``_assert_pat_can_reach_repo``): a repo
          the token can't access yields the same 404 as the issue/PR routes and NO
          history is returned, so stored review summaries can't leak across the PAT's
          access boundary.
    """
    _cfg, token, owner, repo = await asyncio.to_thread(_resolve_github_target, None, owner, repo)

    coding_team_url = _require_coding_team_url()

    # History lives in Khala's own store, not GitHub, so gate it on the PAT actually
    # reaching the repo — the issue/PR routes get this gate for free from their GitHub
    # list call; this history-only route must ask explicitly.
    await _assert_pat_can_reach_repo(owner, repo, token)

    params: dict[str, Any] = {"owner": owner, "repo": repo, "limit": limit}
    if pr_number is not None:
        params["pr_number"] = pr_number

    data = await _forward_to_coding_team(
        coding_team_url,
        "reviews",
        method="GET",
        params=params,
        timeout_s=30.0,
        log_prefix="github reviews",
        timeout_detail="Coding team service timed out while listing reviews.",
        generic_failure_detail="Failed to retrieve review history.",
    )
    try:
        return [CodeReviewRunItem.model_validate(item) for item in data]
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Coding team service returned an unexpected response: {e}",
        ) from e


@router.get("/github/reviews/{job_id}/transcript", response_model=CodeReviewTranscript)
async def get_github_review_transcript(
    job_id: str,
    owner: str,
    repo: str,
) -> CodeReviewTranscript:
    """Return one review's full durable transcript (every LLM call it made).

    The Code Review page's "View Transcript" action, available once a review
    row shows a completed status. ``owner``/``repo`` are the repository the
    review row belongs to (the page already has them from the review list) —
    required (not looked up) so the PAT-reachability gate below runs before
    any transcript content is fetched.

    Preconditions:
        - GitHub integration is enabled with a stored PAT that can reach
          ``owner``/``repo`` (verified against GitHub, same gate as
          ``GET /github/reviews``).
        - ``CODING_TEAM_SERVICE_URL`` points at a reachable coding-team service.
    Postconditions:
        - Returns the transcript's entries in call order. Every failure path
          raises ``HTTPException``; no ``httpx`` error escapes unhandled. A
          ``job_id`` whose stored review belongs to a different repository than
          ``owner``/``repo`` is refused by the coding-team service (409),
          forwarded unchanged.
    """
    _cfg, token, owner, repo = await asyncio.to_thread(_resolve_github_target, None, owner, repo)

    coding_team_url = _require_coding_team_url()

    # Same rationale as GET /github/reviews: history lives in Khala's own store,
    # not GitHub, so gate it on the PAT actually reaching the target repo.
    await _assert_pat_can_reach_repo(owner, repo, token)

    data = await _forward_to_coding_team(
        coding_team_url,
        f"reviews/{job_id}/transcript",
        method="GET",
        params={"owner": owner, "repo": repo},
        timeout_s=30.0,
        log_prefix="github review transcript",
        timeout_detail="Coding team service timed out while retrieving the transcript.",
        generic_failure_detail="Failed to retrieve the review transcript.",
    )
    try:
        return CodeReviewTranscript.model_validate(data)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Coding team service returned an unexpected response: {e}",
        ) from e


@router.post("/github/reviews/{job_id}/issues", response_model=CreateReviewIssuesResponse)
async def create_github_review_issues(job_id: str, body: CreateReviewIssuesBody) -> CreateReviewIssuesResponse:
    """File GitHub issues for a review's selected pre-existing findings.

    A PR review does not comment on bugs it finds in pre-existing, unchanged code;
    it collects them as proposals on the review summary. The Code Review page
    calls this with the proposal ids the user chose to file, plus the repository
    the review belongs to (``owner``/``repo``). The GitHub token is injected
    server-side (never sent by the browser); access comes from the PAT. The
    request is forwarded to the coding-team service, which validates owner/repo
    against the stored review before opening any issue.

    Preconditions:
        - GitHub integration is enabled and a PAT is configured.
        - ``CODING_TEAM_SERVICE_URL`` points at a reachable coding-team service.
    Postconditions:
        - Returns the issues opened by this request plus the review's updated
          proposal list. Every failure path raises ``HTTPException``; no ``httpx``
          error escapes unhandled. The upstream status code is preserved (404 for
          an unknown review, 409 when owner/repo do not match the review, 502 for
          a GitHub API failure, etc.). A 400 is raised for a missing or malformed
          owner/repo.
        - Like ``list_github_reviews``, this route acts on Khala's own store
          (mutating a review's stored proposals) rather than being implicitly
          gated by a GitHub call, so it first verifies the PAT can reach
          ``owner``/``repo`` (``_assert_pat_can_reach_repo``) — a repo the token
          can't access yields the same 404 as the issue/PR routes, so a caller
          cannot use a job_id to file issues into (or read proposal detail from)
          a repository outside the PAT's own access boundary.
    """
    # Access is defined by the PAT, so resolve only the token — the target repo is
    # the caller's own (validated below and re-checked against the stored review by
    # the coding-team service), not a single configured default.
    _cfg, token = await asyncio.to_thread(_resolve_github_access)
    owner = _validate_repo_component("owner", body.owner)
    repo = _validate_repo_component("repo", body.repo)
    if not owner or not repo:
        raise HTTPException(status_code=400, detail="GitHub owner and repo are required.")

    # Cheap, local checks first (mirrors list_github_reviews): a misconfigured
    # downstream fails fast with 503 before any network round-trip.
    coding_team_url = _require_coding_team_url()

    await _assert_pat_can_reach_repo(owner, repo, token)

    payload: dict[str, Any] = {
        "proposal_ids": body.proposal_ids,
        "owner": owner,
        "repo": repo,
        "github_token": token,
    }
    data = await _forward_to_coding_team(
        coding_team_url,
        f"reviews/{job_id}/issues",
        json_body=payload,
        log_prefix="github create-issues",
        timeout_detail="Coding team service timed out while creating issues.",
        generic_failure_detail="Failed to create issues.",
    )
    try:
        return CreateReviewIssuesResponse.model_validate(data)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Coding team service returned an unexpected response: {e}",
        ) from e


# ---------------------------------------------------------------------------
# Out-of-scope issue proposals — aggregated across reviews for a repo
# ---------------------------------------------------------------------------


@router.get("/github/reviews/out-of-scope-issues", response_model=OutOfScopeProposalsResponse)
async def list_out_of_scope_issues(
    owner: str,
    repo: str,
    limit: int = Query(default=500, ge=1, le=2000),
) -> OutOfScopeProposalsResponse:
    """List all unfiled out-of-scope issue proposals across reviews for a repository.

    Powers the Coding Team Issues tab: shows pre-existing bugs found by code
    reviews that haven't been filed as GitHub issues yet.

    Preconditions:
        - GitHub integration is enabled with a stored PAT that can reach
          ``owner``/``repo``.
        - ``CODING_TEAM_SERVICE_URL`` points at a reachable coding-team service.
    Postconditions:
        - Returns unfiled proposals newest-review-first. Each carries the
          originating review's job_id and PR for provenance.
    """
    _cfg, token, owner, repo = await asyncio.to_thread(_resolve_github_target, None, owner, repo)

    coding_team_url = _require_coding_team_url()
    await _assert_pat_can_reach_repo(owner, repo, token)

    params: dict[str, Any] = {"owner": owner, "repo": repo, "limit": limit}
    data = await _forward_to_coding_team(
        coding_team_url,
        "reviews/out-of-scope-issues",
        method="GET",
        params=params,
        timeout_s=30.0,
        log_prefix="github out-of-scope-issues",
        timeout_detail="Coding team service timed out while listing out-of-scope issues.",
        generic_failure_detail="Failed to retrieve out-of-scope issues.",
    )
    try:
        return OutOfScopeProposalsResponse.model_validate(data)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Coding team service returned an unexpected response: {e}",
        ) from e


@router.post("/github/reviews/out-of-scope-issues/file", response_model=FileOutOfScopeIssuesResponse)
async def file_out_of_scope_issues(body: FileOutOfScopeIssuesBody) -> FileOutOfScopeIssuesResponse:
    """File selected out-of-scope proposals as enhanced GitHub issues.

    For each selected proposal, checks for similar existing issues in the repo.
    If found, merges into the existing issue. Otherwise creates a new enhanced
    GitHub issue with Fibonacci complexity scoring, acceptance criteria, etc.

    Preconditions:
        - GitHub integration is enabled and a PAT is configured.
        - ``CODING_TEAM_SERVICE_URL`` points at a reachable coding-team service.
    Postconditions:
        - Returns created/merged issues and any per-proposal errors.
    """
    _cfg, token = await asyncio.to_thread(_resolve_github_access)
    owner = _validate_repo_component("owner", body.owner)
    repo = _validate_repo_component("repo", body.repo)
    if not owner or not repo:
        raise HTTPException(status_code=400, detail="GitHub owner and repo are required.")

    coding_team_url = _require_coding_team_url()
    await _assert_pat_can_reach_repo(owner, repo, token)

    payload: dict[str, Any] = {
        "proposal_ids": body.proposal_ids,
        "owner": owner,
        "repo": repo,
        "github_token": token,
    }
    data = await _forward_to_coding_team(
        coding_team_url,
        "reviews/out-of-scope-issues/file",
        json_body=payload,
        log_prefix="github file-out-of-scope-issues",
        timeout_detail="Coding team service timed out while filing out-of-scope issues.",
        generic_failure_detail="Failed to file out-of-scope issues.",
    )
    try:
        return FileOutOfScopeIssuesResponse.model_validate(data)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Coding team service returned an unexpected response: {e}",
        ) from e
