"""Tests for the unified-API lifespan hook that registers team-assistant mount specs.

UNIFIED_API_TEAM_ASSISTANTS_ENABLED lets operators skip registering all ~19
team-assistant conversational sub-apps at startup (a kill-switch). Startup
itself only builds a lightweight registry of mount specs — no FastAPI
sub-app is constructed or mounted until a future first-request hook does so
on demand. Regression coverage for both the flag and the registration-only
contract.
"""

from __future__ import annotations

import pytest

import unified_api.main as main


@pytest.fixture(autouse=True)
def _restore_assistant_registry():
    """Snapshot/restore _ASSISTANT_REGISTRY so these tests (which clear and
    repopulate it directly) don't leak state into each other or into other
    test modules that import unified_api.main."""
    original = dict(main._ASSISTANT_REGISTRY)
    yield
    main._ASSISTANT_REGISTRY.clear()
    main._ASSISTANT_REGISTRY.update(original)


def test_maybe_register_team_assistants_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """UNIFIED_API_TEAM_ASSISTANTS_ENABLED=false must skip registering any
    assistant mount specs entirely."""
    monkeypatch.setattr(main, "UNIFIED_API_TEAM_ASSISTANTS_ENABLED", False)
    calls: list[bool] = []

    def fake_build():
        calls.append(True)
        return {"blogging": object()}

    monkeypatch.setattr(main, "_build_assistant_registry", fake_build)

    result = main._maybe_register_team_assistants()

    assert calls == []
    assert result == 0
    assert main._ASSISTANT_REGISTRY == {}


def test_maybe_register_team_assistants_registers_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (flag true, or unset) must populate the registry from
    _build_assistant_registry's return value."""
    monkeypatch.setattr(main, "UNIFIED_API_TEAM_ASSISTANTS_ENABLED", True)
    fake_registry = {"blogging": object(), "software_engineering": object()}
    calls: list[bool] = []

    def fake_build():
        calls.append(True)
        return fake_registry

    monkeypatch.setattr(main, "_build_assistant_registry", fake_build)

    result = main._maybe_register_team_assistants()

    assert calls == [True]
    assert result == 2
    assert fake_registry == main._ASSISTANT_REGISTRY


def test_maybe_register_team_assistants_clears_stale_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repeated lifespan run (e.g. uvicorn --reload, test fixtures) must not
    leave stale entries from a previous run mixed into the new registry."""
    monkeypatch.setattr(main, "UNIFIED_API_TEAM_ASSISTANTS_ENABLED", True)
    main._ASSISTANT_REGISTRY["stale_team"] = object()
    monkeypatch.setattr(main, "_build_assistant_registry", lambda: {"blogging": object()})

    main._maybe_register_team_assistants()

    assert "stale_team" not in main._ASSISTANT_REGISTRY
    assert set(main._ASSISTANT_REGISTRY) == {"blogging"}


def test_maybe_register_team_assistants_swallows_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure inside _build_assistant_registry must be logged and swallowed,
    not propagated — startup of the rest of the app must not be aborted."""
    monkeypatch.setattr(main, "UNIFIED_API_TEAM_ASSISTANTS_ENABLED", True)

    def fake_build():
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "_build_assistant_registry", fake_build)

    result = main._maybe_register_team_assistants()

    assert result == 0
    assert main._ASSISTANT_REGISTRY == {}


def test_build_assistant_registry_never_constructs_sub_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registration must not pay the cost it's meant to defer: no assistant
    sub-app should be constructed while building the registry."""
    from team_assistant import api as team_assistant_api

    def fail_if_called(config):
        raise AssertionError("create_assistant_app must not be called during registration")

    monkeypatch.setattr(team_assistant_api, "create_assistant_app", fail_if_called)

    registry = main._build_assistant_registry()

    assert registry  # sanity: teams were actually registered
    assert all(isinstance(spec, main.AssistantMountSpec) for spec in registry.values())


def test_build_assistant_registry_covers_known_team_configs() -> None:
    """Every TEAM_ASSISTANT_CONFIGS entry with a matching TEAM_CONFIGS team
    must produce a registry entry with the expected mount path."""
    from team_assistant.config import TEAM_ASSISTANT_CONFIGS

    registry = main._build_assistant_registry()

    expected_keys = {team_key for team_key in TEAM_ASSISTANT_CONFIGS if team_key in main.TEAM_CONFIGS}
    assert set(registry) == expected_keys

    for team_key in ("software_engineering", "blogging"):
        assert team_key in registry
        spec = registry[team_key]
        team_cfg = main.TEAM_CONFIGS[team_key]
        assert spec.team_key == team_key
        assert spec.mount_path == f"{team_cfg.prefix}/assistant"
        assert spec.assistant_config is TEAM_ASSISTANT_CONFIGS[team_key]
