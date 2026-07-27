"""Tests for the branding adapter's HTTP fetch layer.

Stubs ``get_json_with_status`` so no real network calls are made.
"""

from __future__ import annotations

import pytest

from social_media_marketing_team.adapters import branding as bmod


def _install_response(monkeypatch: pytest.MonkeyPatch, status_code, body) -> None:
    monkeypatch.setattr(bmod, "get_json_with_status", lambda *a, **k: (status_code, body))


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
    _install_response(monkeypatch, 404, None)
    with pytest.raises(bmod.BrandNotFoundError):
        bmod.fetch_brand("c-1", "b-1")


def test_fetch_brand_500_raises_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://api")
    _install_response(monkeypatch, 500, None)
    with pytest.raises(RuntimeError):
        bmod.fetch_brand("c", "b")


def test_fetch_brand_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://api")
    payload = {"id": "b", "name": "Brand"}
    _install_response(monkeypatch, 200, payload)
    assert bmod.fetch_brand("c", "b") == payload


def test_fetch_brand_json_decode_raises_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://api")
    _install_response(monkeypatch, 200, None)
    with pytest.raises(RuntimeError):
        bmod.fetch_brand("c", "b")


def test_fetch_brand_timeout_raises_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://api")
    _install_response(monkeypatch, None, None)
    with pytest.raises(RuntimeError):
        bmod.fetch_brand("c", "b")
