"""Tests for the unified-API lifespan hook that mounts team-assistant sub-apps.

UNIFIED_API_TEAM_ASSISTANTS_ENABLED lets operators skip mounting all ~19
team-assistant conversational sub-apps at startup (a kill-switch, ahead of a
future lazy mount-on-first-request). Regression coverage for that flag.
"""

from __future__ import annotations

import pytest

import unified_api.main as main


def test_maybe_mount_team_assistants_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """UNIFIED_API_TEAM_ASSISTANTS_ENABLED=false must skip mounting any
    assistant sub-apps entirely."""
    monkeypatch.setattr(main, "UNIFIED_API_TEAM_ASSISTANTS_ENABLED", False)
    calls: list[bool] = []

    def fake_mount(app) -> int:
        calls.append(True)
        return 19

    monkeypatch.setattr(main, "_mount_team_assistants", fake_mount)

    result = main._maybe_mount_team_assistants(main.app)

    assert calls == []
    assert result == 0


def test_maybe_mount_team_assistants_mounts_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (flag true, or unset) must preserve today's mount-all-teams behavior."""
    monkeypatch.setattr(main, "UNIFIED_API_TEAM_ASSISTANTS_ENABLED", True)
    calls: list[bool] = []

    def fake_mount(app) -> int:
        calls.append(True)
        return 19

    monkeypatch.setattr(main, "_mount_team_assistants", fake_mount)

    result = main._maybe_mount_team_assistants(main.app)

    assert calls == [True]
    assert result == 19


def test_maybe_mount_team_assistants_swallows_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure inside _mount_team_assistants must be logged and swallowed,
    not propagated — startup of the rest of the app must not be aborted."""
    monkeypatch.setattr(main, "UNIFIED_API_TEAM_ASSISTANTS_ENABLED", True)

    def fake_mount(app) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "_mount_team_assistants", fake_mount)

    result = main._maybe_mount_team_assistants(main.app)

    assert result == 0
