"""Tests for shared.http.base_url.resolve_base_url."""

from __future__ import annotations

import pytest

from shared.http.base_url import resolve_base_url


def test_default_env_wins_when_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "http://unified")
    monkeypatch.setenv("TEAM_OVERRIDE_URL", "http://team")
    assert resolve_base_url("UNIFIED_API_BASE_URL", "TEAM_OVERRIDE_URL") == "http://unified"


def test_falls_back_to_override_when_default_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNIFIED_API_BASE_URL", raising=False)
    monkeypatch.setenv("TEAM_OVERRIDE_URL", "http://team")
    assert resolve_base_url("UNIFIED_API_BASE_URL", "TEAM_OVERRIDE_URL") == "http://team"


def test_returns_none_when_neither_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNIFIED_API_BASE_URL", raising=False)
    monkeypatch.delenv("TEAM_OVERRIDE_URL", raising=False)
    assert resolve_base_url("UNIFIED_API_BASE_URL", "TEAM_OVERRIDE_URL") is None


def test_empty_string_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFIED_API_BASE_URL", "")
    monkeypatch.setenv("TEAM_OVERRIDE_URL", "http://team")
    assert resolve_base_url("UNIFIED_API_BASE_URL", "TEAM_OVERRIDE_URL") == "http://team"


def test_asserts_on_empty_env_names() -> None:
    with pytest.raises(AssertionError):
        resolve_base_url("", "TEAM_OVERRIDE_URL")
    with pytest.raises(AssertionError):
        resolve_base_url("UNIFIED_API_BASE_URL", "")
