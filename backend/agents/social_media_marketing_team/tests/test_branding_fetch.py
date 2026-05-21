"""Tests for the branding adapter's HTTP fetch layer.

Stubs ``httpx.Client`` so no real network calls are made.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from social_media_marketing_team.adapters import branding as bmod


class _Resp:
    def __init__(self, status_code: int, payload: Any = None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", "http://example.test")
            raise httpx.HTTPStatusError("error", request=req, response=httpx.Response(self.status_code, request=req))


class _Client:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        self._last_url = url
        return self._response


def _install_client(monkeypatch: pytest.MonkeyPatch, response: _Resp) -> None:
    monkeypatch.setattr(bmod.httpx, "Client", lambda timeout=30.0: _Client(response))


def test_base_url_returns_unified_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://api")
    monkeypatch.delenv("SOCIAL_MARKETING_BRANDING_URL", raising=False)
    assert bmod._base_url() == "http://api"


def test_base_url_falls_back_to_social_marketing_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UNIFIED_API_BASE_URL", raising=False)
    monkeypatch.setenv("SOCIAL_MARKETING_BRANDING_URL", "http://sm")
    assert bmod._base_url() == "http://sm"


def test_base_url_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNIFIED_API_BASE_URL", raising=False)
    monkeypatch.delenv("SOCIAL_MARKETING_BRANDING_URL", raising=False)
    assert bmod._base_url() is None


def test_fetch_brand_raises_runtime_when_no_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UNIFIED_API_BASE_URL", raising=False)
    monkeypatch.delenv("SOCIAL_MARKETING_BRANDING_URL", raising=False)
    with pytest.raises(RuntimeError, match="Branding API URL not configured"):
        bmod.fetch_brand("c", "b")


def test_fetch_brand_404_raises_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://api/")
    _install_client(monkeypatch, _Resp(404))
    with pytest.raises(bmod.BrandNotFoundError):
        bmod.fetch_brand("c-1", "b-1")


def test_fetch_brand_500_raises_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://api")
    _install_client(monkeypatch, _Resp(500))
    with pytest.raises(RuntimeError):
        bmod.fetch_brand("c", "b")


def test_fetch_brand_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://api")
    payload = {"id": "b", "name": "Brand"}
    _install_client(monkeypatch, _Resp(200, payload))
    assert bmod.fetch_brand("c", "b") == payload


def test_fetch_brand_json_decode_raises_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BadResp(_Resp):
        def json(self):
            raise ValueError("bad json")

    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://api")
    _install_client(monkeypatch, _BadResp(200))
    with pytest.raises(RuntimeError):
        bmod.fetch_brand("c", "b")


def test_fetch_brand_timeout_raises_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TimingOutClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://api")
    monkeypatch.setattr(bmod.httpx, "Client", lambda timeout=30.0: _TimingOutClient())
    with pytest.raises(RuntimeError):
        bmod.fetch_brand("c", "b")
