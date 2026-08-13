"""Unmocked tests for Agent Studio authoring Temporal dispatch gating.

``test_temporal_dispatch.py`` and ``test_direct_dispatch.py`` force
``_temporal_enabled`` on or off. This module pins the real gate: Temporal
authoring CRUD requires both a configured Temporal cluster *and* a live
in-process ``agent_studio`` worker, so CRUD still succeeds (via direct
in-process dispatch) when the Studio worker is disabled or absent.
"""

from __future__ import annotations

import pytest

import agent_platform.studio.temporal.dispatch as dispatch


def test_temporal_enabled_true_when_temporal_on_and_worker_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shared.temporal.client.is_temporal_enabled", lambda: True)
    monkeypatch.setattr(
        "shared.temporal.worker.is_team_worker_alive", lambda team: team == "agent_studio"
    )

    assert dispatch._temporal_enabled() is True


def test_temporal_enabled_false_when_temporal_on_and_worker_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shared.temporal.client.is_temporal_enabled", lambda: True)
    monkeypatch.setattr("shared.temporal.worker.is_team_worker_alive", lambda _team: False)

    assert dispatch._temporal_enabled() is False


def test_temporal_enabled_false_when_temporal_off_even_if_worker_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shared.temporal.client.is_temporal_enabled", lambda: False)
    monkeypatch.setattr(
        "shared.temporal.worker.is_team_worker_alive", lambda team: team == "agent_studio"
    )

    assert dispatch._temporal_enabled() is False
