"""Shared Google browser login credentials (Postgres-only; no SQLite fallback)."""

from __future__ import annotations

from unittest import mock

import pytest

from unified_api import google_browser_login_credentials as mod


def test_without_postgres_get_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    assert mod.get_google_browser_login_credentials() == ("", "")
    assert not mod.google_browser_login_credentials_configured()
    assert not mod.google_browser_login_storage_available()


def test_without_postgres_set_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    with pytest.raises(RuntimeError, match="POSTGRES_HOST"):
        mod.set_google_browser_login_credentials("user@example.com", "secret-pass")


def test_without_postgres_clear_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    mod.clear_google_browser_login_credentials()  # does not raise


def test_save_rejects_invalid_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    with mock.patch.object(mod, "pg_set_credential"), pytest.raises(ValueError, match="email"):
        mod.set_google_browser_login_credentials("not-an-email", "x")


def test_save_clear_roundtrip_postgres_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    store: dict[tuple[str, str], str] = {}

    def fake_get(service: str, key: str) -> str:
        return store.get((service, key), "")

    def fake_set(service: str, key: str, value: str) -> None:
        store[(service, key)] = value

    def fake_del(service: str, key: str) -> None:
        store.pop((service, key), None)

    with (
        mock.patch.object(mod, "pg_get_credential", side_effect=fake_get),
        mock.patch.object(mod, "pg_set_credential", side_effect=fake_set),
        mock.patch.object(mod, "pg_delete_credential", side_effect=fake_del),
    ):
        mod.clear_google_browser_login_credentials()
        mod.set_google_browser_login_credentials("user@example.com", "secret-pass")
        assert mod.google_browser_login_credentials_configured()
        em, pw = mod.get_google_browser_login_credentials()
        assert em == "user@example.com"
        assert pw == "secret-pass"
        mod.clear_google_browser_login_credentials()
        assert not mod.google_browser_login_credentials_configured()


def test_legacy_medium_keys_migrate_postgres_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Old medium_google_browser rows are read once and moved to platform_google_browser."""
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    store = {
        ("medium_google_browser", "google_login_email"): "legacy@example.com",
        ("medium_google_browser", "google_login_password"): "legacy-secret",
    }

    def fake_get(service: str, key: str) -> str:
        return store.get((service, key), "")

    def fake_set(service: str, key: str, value: str) -> None:
        store[(service, key)] = value

    def fake_del(service: str, key: str) -> None:
        store.pop((service, key), None)

    with (
        mock.patch.object(mod, "pg_get_credential", side_effect=fake_get),
        mock.patch.object(mod, "pg_set_credential", side_effect=fake_set),
        mock.patch.object(mod, "pg_delete_credential", side_effect=fake_del),
    ):
        em, pw = mod.get_google_browser_login_credentials()
        assert em == "legacy@example.com"
        assert pw == "legacy-secret"
        em2, pw2 = mod.get_google_browser_login_credentials()
        assert em2 == "legacy@example.com"
        assert pw2 == "legacy-secret"
