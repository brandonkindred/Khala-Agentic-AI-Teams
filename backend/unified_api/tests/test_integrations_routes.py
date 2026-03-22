"""Tests for /api/integrations/* endpoints and route helper functions."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi.testclient import TestClient

from unified_api.main import app

client = TestClient(app, follow_redirects=False)


# ---------------------------------------------------------------------------
# Helper: default store stubs
# ---------------------------------------------------------------------------

_DEFAULT_SLACK_CFG = {
    "enabled": False,
    "mode": "webhook",
    "webhook_url": "",
    "bot_token": "",
    "default_channel": "",
    "channel_display_name": "",
    "notify_open_questions": True,
    "notify_pa_responses": True,
    "client_id": "",
    "client_secret": "",
    "team_id": "",
    "team_name": "",
    "bot_user_id": "",
}

_DEFAULT_MEDIUM_CFG = {
    "enabled": False,
    "oauth_provider": "google",
    "oauth_identity_connected": False,
    "google_client_id": "",
    "google_client_secret": "",
    "session_configured": False,
    "linked_email": "",
    "linked_name": "",
}

_STORE_MODULE = "unified_api.routes.integrations"


# ---------------------------------------------------------------------------
# Helper validators (pure unit tests, no HTTP)
# ---------------------------------------------------------------------------


def test_validate_webhook_url_accepts_valid_url():
    """_validate_webhook_url does not raise for a valid hooks.slack.com URL."""
    from unified_api.routes.integrations import _validate_webhook_url

    _validate_webhook_url("https://hooks.slack.com/services/T/B/" + "x" * 40)


def test_validate_webhook_url_raises_for_wrong_domain():
    """_validate_webhook_url raises 400 for a URL not starting with hooks.slack.com."""
    from fastapi import HTTPException

    from unified_api.routes.integrations import _validate_webhook_url

    with pytest.raises(HTTPException) as exc_info:
        _validate_webhook_url("https://evil.com/hooks/slack")
    assert exc_info.value.status_code == 400


def test_validate_webhook_url_raises_for_too_short_url():
    """_validate_webhook_url raises 400 when the URL is shorter than 50 chars."""
    from fastapi import HTTPException

    from unified_api.routes.integrations import _validate_webhook_url

    with pytest.raises(HTTPException) as exc_info:
        _validate_webhook_url("https://hooks.slack.com/short")
    assert exc_info.value.status_code == 400


def test_validate_webhook_url_is_noop_for_empty_string():
    """_validate_webhook_url does not raise for an empty/blank string."""
    from unified_api.routes.integrations import _validate_webhook_url

    _validate_webhook_url("")
    _validate_webhook_url("   ")


def test_validate_bot_token_accepts_valid_token():
    """_validate_bot_token does not raise for an xoxb- prefixed token."""
    from unified_api.routes.integrations import _validate_bot_token

    _validate_bot_token("xoxb-valid-token")


def test_validate_bot_token_raises_for_missing_token():
    """_validate_bot_token raises 400 when token is empty."""
    from fastapi import HTTPException

    from unified_api.routes.integrations import _validate_bot_token

    with pytest.raises(HTTPException) as exc_info:
        _validate_bot_token("")
    assert exc_info.value.status_code == 400


def test_validate_bot_token_raises_for_wrong_prefix():
    """_validate_bot_token raises 400 when token does not start with xoxb-."""
    from fastapi import HTTPException

    from unified_api.routes.integrations import _validate_bot_token

    with pytest.raises(HTTPException) as exc_info:
        _validate_bot_token("xoxa-some-token")
    assert exc_info.value.status_code == 400


def test_build_slack_config_response_masks_webhook_url():
    """_build_slack_config_response always returns webhook_url=None (never exposes raw URL)."""
    from unified_api.routes.integrations import _build_slack_config_response

    cfg = dict(_DEFAULT_SLACK_CFG, webhook_url="https://hooks.slack.com/services/T/B/X", enabled=True)
    resp = _build_slack_config_response(cfg)
    assert resp.webhook_url is None
    assert resp.webhook_configured is True


def test_build_slack_config_response_oauth_connected_set_from_team_id():
    """_build_slack_config_response sets oauth_connected=True when team_id is present."""
    from unified_api.routes.integrations import _build_slack_config_response

    cfg = dict(_DEFAULT_SLACK_CFG, team_id="T12345", team_name="Acme")
    resp = _build_slack_config_response(cfg)
    assert resp.oauth_connected is True
    assert resp.team_name == "Acme"
    assert resp.team_id == "T12345"


def test_build_medium_config_response_defaults_to_google_provider():
    """_build_medium_config_response coerces unknown oauth_provider to 'google'."""
    from unified_api.routes.integrations import _build_medium_config_response

    cfg = dict(_DEFAULT_MEDIUM_CFG, oauth_provider="unknown_provider")
    resp = _build_medium_config_response(cfg)
    assert resp.oauth_provider == "google"


# ---------------------------------------------------------------------------
# GET /api/integrations
# ---------------------------------------------------------------------------


def test_list_integrations_returns_200():
    """GET /api/integrations returns HTTP 200."""
    with patch(f"{_STORE_MODULE}.get_integrations_list", return_value=[
        {"id": "slack", "type": "slack", "enabled": False, "channel": None},
        {"id": "medium", "type": "medium", "enabled": False, "channel": None},
    ]):
        resp = client.get("/api/integrations")
    assert resp.status_code == 200


def test_list_integrations_returns_list():
    """GET /api/integrations returns a JSON list."""
    with patch(f"{_STORE_MODULE}.get_integrations_list", return_value=[
        {"id": "slack", "type": "slack", "enabled": False, "channel": None},
    ]):
        resp = client.get("/api/integrations")
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# GET /api/integrations/slack
# ---------------------------------------------------------------------------


def test_get_slack_returns_200():
    """GET /api/integrations/slack returns HTTP 200."""
    with patch(f"{_STORE_MODULE}.get_slack_config", return_value=dict(_DEFAULT_SLACK_CFG)):
        resp = client.get("/api/integrations/slack")
    assert resp.status_code == 200


def test_get_slack_response_shape():
    """GET /api/integrations/slack returns expected fields."""
    with patch(f"{_STORE_MODULE}.get_slack_config", return_value=dict(_DEFAULT_SLACK_CFG)):
        resp = client.get("/api/integrations/slack")
    data = resp.json()
    assert "enabled" in data
    assert "webhook_configured" in data
    assert "bot_token_configured" in data
    # Sensitive field must NOT be in response
    assert "webhook_url" in data  # field exists but is None
    assert data.get("webhook_url") is None


# ---------------------------------------------------------------------------
# PUT /api/integrations/slack validation
# ---------------------------------------------------------------------------


def test_put_slack_disabled_requires_no_validation():
    """PUT /api/integrations/slack with enabled=False succeeds without url/token."""
    with patch(f"{_STORE_MODULE}.get_slack_config", return_value=dict(_DEFAULT_SLACK_CFG)), \
         patch(f"{_STORE_MODULE}.set_slack_config"):
        resp = client.put("/api/integrations/slack", json={"enabled": False, "mode": "webhook"})
    assert resp.status_code == 200


def test_put_slack_webhook_mode_requires_webhook_url():
    """PUT /api/integrations/slack with enabled=True, mode=webhook, no URL returns 400."""
    with patch(f"{_STORE_MODULE}.get_slack_config", return_value=dict(_DEFAULT_SLACK_CFG)):
        resp = client.put("/api/integrations/slack", json={"enabled": True, "mode": "webhook", "webhook_url": ""})
    assert resp.status_code == 400


def test_put_slack_webhook_mode_rejects_non_slack_url():
    """PUT /api/integrations/slack rejects webhook_url not starting with hooks.slack.com."""
    with patch(f"{_STORE_MODULE}.get_slack_config", return_value=dict(_DEFAULT_SLACK_CFG)):
        resp = client.put("/api/integrations/slack", json={
            "enabled": True,
            "mode": "webhook",
            "webhook_url": "https://evil.com/hook",
        })
    assert resp.status_code == 400


def test_put_slack_bot_mode_requires_default_channel():
    """PUT /api/integrations/slack with mode=bot, missing default_channel returns 400."""
    with patch(f"{_STORE_MODULE}.get_slack_config", return_value=dict(_DEFAULT_SLACK_CFG)):
        resp = client.put("/api/integrations/slack", json={
            "enabled": True,
            "mode": "bot",
            "bot_token": "xoxb-some-token",
            "default_channel": "",
        })
    assert resp.status_code == 400


def test_put_slack_bot_mode_accepts_valid_token_and_channel():
    """PUT /api/integrations/slack with mode=bot, valid token and channel returns 200."""
    with patch(f"{_STORE_MODULE}.get_slack_config", return_value=dict(_DEFAULT_SLACK_CFG)), \
         patch(f"{_STORE_MODULE}.set_slack_config"):
        resp = client.put("/api/integrations/slack", json={
            "enabled": True,
            "mode": "bot",
            "bot_token": "xoxb-valid",
            "default_channel": "#eng",
        })
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/integrations/slack/oauth/connect
# ---------------------------------------------------------------------------


def test_slack_oauth_connect_returns_400_when_client_id_missing():
    """GET /slack/oauth/connect returns 400 when Client ID is not configured."""
    with patch(f"{_STORE_MODULE}.get_slack_config", return_value=dict(_DEFAULT_SLACK_CFG)):
        resp = client.get("/api/integrations/slack/oauth/connect")
    assert resp.status_code == 400
    assert "Client ID" in resp.json()["detail"]


def test_slack_oauth_connect_returns_400_when_client_secret_missing():
    """GET /slack/oauth/connect returns 400 when Client Secret is not configured."""
    with patch(f"{_STORE_MODULE}.get_slack_config",
               return_value=dict(_DEFAULT_SLACK_CFG, client_id="my-id")):
        resp = client.get("/api/integrations/slack/oauth/connect")
    assert resp.status_code == 400
    assert "Client Secret" in resp.json()["detail"]


def test_slack_oauth_connect_returns_url_and_client_id():
    """GET /slack/oauth/connect returns the Slack authorization URL and client_id."""
    with patch(f"{_STORE_MODULE}.get_slack_config",
               return_value=dict(_DEFAULT_SLACK_CFG, client_id="cid-123", client_secret="csec")), \
         patch(f"{_STORE_MODULE}.generate_oauth_state", return_value="test-state-xyz"):
        resp = client.get("/api/integrations/slack/oauth/connect")
    assert resp.status_code == 200
    data = resp.json()
    assert "slack.com/oauth/v2/authorize" in data["url"]
    assert data["client_id"] == "cid-123"
    assert "test-state-xyz" in data["url"]


# ---------------------------------------------------------------------------
# GET /api/integrations/slack/oauth/callback
# ---------------------------------------------------------------------------


def test_slack_oauth_callback_redirects_on_error_param():
    """GET /slack/oauth/callback with error= param redirects with slack_error."""
    resp = client.get("/api/integrations/slack/oauth/callback?error=access_denied")
    assert resp.status_code in (302, 307)
    assert "slack_error=access_denied" in resp.headers["location"]


def test_slack_oauth_callback_redirects_on_missing_code():
    """GET /slack/oauth/callback without code or state redirects with error."""
    resp = client.get("/api/integrations/slack/oauth/callback")
    assert resp.status_code in (302, 307)
    assert "slack_error" in resp.headers["location"]


def test_slack_oauth_callback_redirects_on_invalid_state():
    """GET /slack/oauth/callback with invalid state redirects with invalid_state error."""
    with patch(f"{_STORE_MODULE}.verify_and_clear_oauth_state", return_value=False):
        resp = client.get("/api/integrations/slack/oauth/callback?code=abc&state=bad-state")
    assert resp.status_code in (302, 307)
    assert "invalid_state" in resp.headers["location"]


def test_slack_oauth_callback_redirects_on_missing_credentials():
    """GET /slack/oauth/callback redirects with missing_credentials when store has no client_id."""
    with patch(f"{_STORE_MODULE}.verify_and_clear_oauth_state", return_value=True), \
         patch(f"{_STORE_MODULE}.get_slack_config", return_value=dict(_DEFAULT_SLACK_CFG)):
        resp = client.get("/api/integrations/slack/oauth/callback?code=abc&state=valid-state")
    assert resp.status_code in (302, 307)
    assert "missing_credentials" in resp.headers["location"]


def test_slack_oauth_callback_success_stores_token_and_redirects():
    """GET /slack/oauth/callback on success calls set_slack_oauth_token and redirects."""
    mock_exchange = {
        "ok": True,
        "access_token": "xoxb-new",
        "team": {"id": "T1", "name": "Acme"},
        "bot_user_id": "U1",
    }
    with patch(f"{_STORE_MODULE}.verify_and_clear_oauth_state", return_value=True), \
         patch(f"{_STORE_MODULE}.get_slack_config",
               return_value=dict(_DEFAULT_SLACK_CFG, client_id="cid", client_secret="csec")), \
         patch(f"{_STORE_MODULE}._exchange_code", return_value=mock_exchange), \
         patch(f"{_STORE_MODULE}.set_slack_oauth_token") as mock_set_token:
        resp = client.get("/api/integrations/slack/oauth/callback?code=authcode&state=valid")
    assert resp.status_code in (302, 307)
    assert "slack_connected=1" in resp.headers["location"]
    mock_set_token.assert_called_once_with(
        bot_token="xoxb-new",
        team_id="T1",
        team_name="Acme",
        bot_user_id="U1",
    )


# ---------------------------------------------------------------------------
# DELETE /api/integrations/slack/oauth
# ---------------------------------------------------------------------------


def test_slack_oauth_disconnect_calls_clear_and_returns_200():
    """DELETE /slack/oauth clears OAuth state and returns updated config."""
    with patch(f"{_STORE_MODULE}.clear_slack_oauth"), \
         patch(f"{_STORE_MODULE}.get_slack_config", return_value=dict(_DEFAULT_SLACK_CFG)):
        resp = client.delete("/api/integrations/slack/oauth")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# ---------------------------------------------------------------------------
# Google browser-login credentials
# ---------------------------------------------------------------------------


def test_get_google_browser_login_status_returns_200():
    """GET /google-browser-login returns HTTP 200 with configured and storage_available."""
    with patch(f"{_STORE_MODULE}.google_browser_login_credentials_configured", return_value=False), \
         patch(f"{_STORE_MODULE}.google_browser_login_storage_available", return_value=False):
        resp = client.get("/api/integrations/google-browser-login")
    assert resp.status_code == 200
    data = resp.json()
    assert "configured" in data
    assert "storage_available" in data


def test_put_google_browser_login_stores_credentials():
    """PUT /google-browser-login stores credentials and returns configured=True."""
    with patch(f"{_STORE_MODULE}.set_google_browser_login_credentials"), \
         patch(f"{_STORE_MODULE}.google_browser_login_storage_available", return_value=True):
        resp = client.put(
            "/api/integrations/google-browser-login",
            json={"email": "user@example.com", "password": "secret"},
        )
    assert resp.status_code == 200
    assert resp.json()["configured"] is True


def test_put_google_browser_login_returns_400_on_validation_error():
    """PUT /google-browser-login propagates ValueError as HTTP 400."""
    with patch(f"{_STORE_MODULE}.set_google_browser_login_credentials",
               side_effect=ValueError("Invalid email")):
        resp = client.put(
            "/api/integrations/google-browser-login",
            json={"email": "", "password": ""},
        )
    assert resp.status_code == 400


def test_put_google_browser_login_returns_503_when_storage_unavailable():
    """PUT /google-browser-login propagates RuntimeError (no Postgres) as HTTP 503."""
    with patch(f"{_STORE_MODULE}.set_google_browser_login_credentials",
               side_effect=RuntimeError("POSTGRES_HOST is not set")):
        resp = client.put(
            "/api/integrations/google-browser-login",
            json={"email": "user@example.com", "password": "pw"},
        )
    assert resp.status_code == 503


def test_delete_google_browser_login_returns_200():
    """DELETE /google-browser-login clears credentials and returns configured=False."""
    with patch(f"{_STORE_MODULE}.clear_google_browser_login_credentials"), \
         patch(f"{_STORE_MODULE}.google_browser_login_storage_available", return_value=True):
        resp = client.delete("/api/integrations/google-browser-login")
    assert resp.status_code == 200
    assert resp.json()["configured"] is False


# ---------------------------------------------------------------------------
# Medium integration config
# ---------------------------------------------------------------------------


def test_get_medium_returns_200():
    """GET /medium returns HTTP 200."""
    with patch(f"{_STORE_MODULE}.get_medium_config", return_value=dict(_DEFAULT_MEDIUM_CFG)):
        resp = client.get("/api/integrations/medium")
    assert resp.status_code == 200


def test_get_medium_response_fields():
    """GET /medium response includes enabled, oauth_provider, and status flags."""
    with patch(f"{_STORE_MODULE}.get_medium_config", return_value=dict(_DEFAULT_MEDIUM_CFG)):
        resp = client.get("/api/integrations/medium")
    data = resp.json()
    assert "enabled" in data
    assert "oauth_provider" in data
    assert "oauth_identity_connected" in data
    assert "session_configured" in data


def test_put_medium_saves_config():
    """PUT /medium saves the integration config and returns updated state."""
    with patch(f"{_STORE_MODULE}.set_medium_config") as mock_set, \
         patch(f"{_STORE_MODULE}.get_medium_config",
               return_value=dict(_DEFAULT_MEDIUM_CFG, enabled=True)):
        resp = client.put("/api/integrations/medium", json={"enabled": True, "oauth_provider": "google"})
    assert resp.status_code == 200
    mock_set.assert_called_once()


# ---------------------------------------------------------------------------
# Medium Google OAuth connect
# ---------------------------------------------------------------------------


def test_medium_google_oauth_connect_returns_400_when_provider_not_google():
    """GET /medium/oauth/google/connect returns 400 when oauth_provider != google."""
    with patch(f"{_STORE_MODULE}.get_medium_config",
               return_value=dict(_DEFAULT_MEDIUM_CFG, oauth_provider="apple")):
        resp = client.get("/api/integrations/medium/oauth/google/connect")
    assert resp.status_code == 400


def test_medium_google_oauth_connect_returns_400_when_client_credentials_missing():
    """GET /medium/oauth/google/connect returns 400 when Google client credentials are absent."""
    with patch(f"{_STORE_MODULE}.get_medium_config",
               return_value=dict(_DEFAULT_MEDIUM_CFG, oauth_provider="google")):
        resp = client.get("/api/integrations/medium/oauth/google/connect")
    assert resp.status_code == 400


def test_medium_google_oauth_connect_returns_auth_url():
    """GET /medium/oauth/google/connect returns a Google authorization URL."""
    with patch(f"{_STORE_MODULE}.get_medium_config", return_value=dict(
        _DEFAULT_MEDIUM_CFG,
        oauth_provider="google",
        google_client_id="gid",
        google_client_secret="gsec",
    )), patch(f"{_STORE_MODULE}.generate_medium_google_oauth_state", return_value="mg-state"):
        resp = client.get("/api/integrations/medium/oauth/google/connect")
    assert resp.status_code == 200
    assert "accounts.google.com" in resp.json()["url"]
    assert "mg-state" in resp.json()["url"]


# ---------------------------------------------------------------------------
# Medium Google OAuth callback
# ---------------------------------------------------------------------------


def test_medium_google_oauth_callback_redirects_on_error():
    """GET /medium/oauth/google/callback with error= redirects with medium_error."""
    resp = client.get("/api/integrations/medium/oauth/google/callback?error=access_denied")
    assert resp.status_code in (302, 307)
    assert "medium_error=access_denied" in resp.headers["location"]


def test_medium_google_oauth_callback_redirects_on_missing_params():
    """GET /medium/oauth/google/callback without code or state redirects with error."""
    resp = client.get("/api/integrations/medium/oauth/google/callback")
    assert resp.status_code in (302, 307)
    assert "medium_error" in resp.headers["location"]


def test_medium_google_oauth_callback_redirects_on_invalid_state():
    """GET /medium/oauth/google/callback with stale state redirects with invalid_state."""
    with patch(f"{_STORE_MODULE}.verify_and_clear_medium_google_oauth_state", return_value=False):
        resp = client.get("/api/integrations/medium/oauth/google/callback?code=c&state=bad")
    assert resp.status_code in (302, 307)
    assert "invalid_state" in resp.headers["location"]


def test_medium_google_oauth_callback_success_stores_identity():
    """GET /medium/oauth/google/callback on success stores identity and redirects."""
    with patch(f"{_STORE_MODULE}.verify_and_clear_medium_google_oauth_state", return_value=True), \
         patch(f"{_STORE_MODULE}.get_medium_config", return_value=dict(
             _DEFAULT_MEDIUM_CFG,
             google_client_id="gid",
             google_client_secret="gsec",
         )), \
         patch(f"{_STORE_MODULE}._exchange_google_oauth_code",
               return_value={"access_token": "at", "refresh_token": "rt"}), \
         patch(f"{_STORE_MODULE}._google_userinfo",
               return_value={"email": "user@example.com", "name": "User"}), \
         patch(f"{_STORE_MODULE}.set_medium_google_oauth_identity") as mock_set_id:
        resp = client.get("/api/integrations/medium/oauth/google/callback?code=c&state=valid")
    assert resp.status_code in (302, 307)
    assert "medium_google_connected=1" in resp.headers["location"]
    mock_set_id.assert_called_once_with(
        refresh_token="rt",
        linked_email="user@example.com",
        linked_name="User",
    )


# ---------------------------------------------------------------------------
# DELETE /api/integrations/medium/oauth/google
# ---------------------------------------------------------------------------


def test_medium_google_oauth_disconnect_returns_200():
    """DELETE /medium/oauth/google clears identity and returns updated config."""
    with patch(f"{_STORE_MODULE}.clear_medium_google_oauth_identity"), \
         patch(f"{_STORE_MODULE}.get_medium_config", return_value=dict(_DEFAULT_MEDIUM_CFG)):
        resp = client.delete("/api/integrations/medium/oauth/google")
    assert resp.status_code == 200
    assert resp.json()["oauth_identity_connected"] is False


# ---------------------------------------------------------------------------
# Medium session import / clear
# ---------------------------------------------------------------------------


def test_medium_import_session_stores_state():
    """POST /medium/session stores storage_state and returns updated config."""
    with patch(f"{_STORE_MODULE}.set_medium_session_storage_state_json") as mock_set, \
         patch(f"{_STORE_MODULE}.get_medium_config",
               return_value=dict(_DEFAULT_MEDIUM_CFG, session_configured=True)):
        resp = client.post(
            "/api/integrations/medium/session",
            json={"storage_state": {"cookies": [], "origins": []}},
        )
    assert resp.status_code == 200
    mock_set.assert_called_once()
    assert resp.json()["session_configured"] is True


def test_medium_import_session_returns_400_on_invalid_state():
    """POST /medium/session returns 400 when storage_state is missing."""
    resp = client.post("/api/integrations/medium/session", json={})
    assert resp.status_code == 422  # Pydantic validation error


def test_medium_clear_session_returns_200():
    """DELETE /medium/session clears storage and returns session_configured=False."""
    with patch(f"{_STORE_MODULE}.clear_medium_session_storage"), \
         patch(f"{_STORE_MODULE}.get_medium_config",
               return_value=dict(_DEFAULT_MEDIUM_CFG, session_configured=False)):
        resp = client.delete("/api/integrations/medium/session")
    assert resp.status_code == 200
    assert resp.json()["session_configured"] is False
