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

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from unified_api.google_browser_login_credentials import (
    clear_google_browser_login_credentials,
    get_google_browser_login_credentials,
    google_browser_login_credentials_configured,
    google_browser_login_storage_available,
    set_google_browser_login_credentials,
)
from unified_api.integrations_store import (
    clear_medium_google_oauth_identity,
    clear_medium_session_storage,
    clear_slack_oauth,
    generate_medium_google_oauth_state,
    generate_oauth_state,
    get_integrations_list,
    get_medium_config,
    get_slack_config,
    set_medium_config,
    set_medium_google_oauth_identity,
    set_medium_session_storage_state_json,
    set_slack_config,
    set_slack_oauth_token,
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
_SLACK_SCOPES = "chat:write,chat:write.public,channels:read"

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


def _exchange_google_oauth_code(code: str, redirect_uri: str, client_id: str, client_secret: str) -> dict:
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        _GOOGLE_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _google_userinfo(access_token: str) -> dict:
    req = urllib.request.Request(
        _GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


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


# ---------------------------------------------------------------------------
# Shared Google / Gmail credentials for Playwright (any integration using “Sign in with Google”)
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
async def put_google_browser_login_credentials(body: GoogleBrowserLoginCredentialsBody) -> GoogleBrowserLoginStatusResponse:
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
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
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
        token_payload = _exchange_google_oauth_code(code, redirect_uri, client_id, client_secret)
    except Exception as exc:
        logger.error("Medium Google token exchange failed: %s", exc)
        return RedirectResponse(url=f"{integrations_ui}?medium_error=token_exchange_failed")

    refresh_token = str(token_payload.get("refresh_token") or "")
    access_token = str(token_payload.get("access_token") or "")
    email, name = "", ""
    if access_token:
        try:
            info = _google_userinfo(access_token)
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
    import asyncio

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
