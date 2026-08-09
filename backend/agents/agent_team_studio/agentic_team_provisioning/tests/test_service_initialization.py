"""Unit tests for ``initialize_service()``.

Covers the fix moving the retroactive team-provisioning/registry-registration
loop and the orphaned pipeline-run reap out of module import time (they used to
run unconditionally as soon as ``api/main.py`` was imported) and into
``initialize_service()``, called from the ``_startup`` lifespan hook instead. See
``test_service_initialization_import_gating.py`` for the import-time-safety
regression; this file covers ``initialize_service()``'s own per-team isolation
behavior in isolation from the ASGI lifespan.
"""

from __future__ import annotations

import pytest

from agent_team_studio.agentic_team_provisioning.api import main as main_mod


class _FakeTeam:
    def __init__(self, team_id: str, agents: list) -> None:
        self.team_id = team_id
        self.agents = agents


def test_initialize_service_isolates_team_failures_and_still_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single team's infra/registry failure must not stop the other teams or
    skip the orphan reap.

    Preconditions: ``_store.list_teams()`` returns three teams; team ``t1``'s
        ``get_team_infrastructure`` call raises; team ``t2``'s
        ``register_team_manifests`` call raises; team ``t3`` succeeds cleanly.
    Postconditions: ``get_team_infrastructure``/``register_team_manifests`` were
        attempted for every team regardless of another team's (or the same
        team's own infra step's) failure, and ``reap_orphaned_runs`` still ran
        exactly once despite the failures.
    """
    teams = {
        "t1": _FakeTeam("t1", [object()]),
        "t2": _FakeTeam("t2", [object()]),
        "t3": _FakeTeam("t3", [object()]),
    }

    infra_calls: list[str] = []

    def _get_infra(team_id: str):
        infra_calls.append(team_id)
        if team_id == "t1":
            raise RuntimeError("infra down")

    manifest_calls: list[str] = []

    def _register_manifests(team_id: str, agents):
        manifest_calls.append(team_id)
        if team_id == "t2":
            raise RuntimeError("registry down")

    reap_calls: list[int] = []

    monkeypatch.setattr(main_mod._store, "list_teams", lambda: [{"team_id": tid} for tid in teams])
    monkeypatch.setattr(main_mod._store, "get_team", lambda tid: teams[tid])
    monkeypatch.setattr(main_mod, "get_team_infrastructure", _get_infra)
    monkeypatch.setattr(main_mod, "register_team_manifests", _register_manifests)
    monkeypatch.setattr(
        main_mod._pipeline_runner,
        "reap_orphaned_runs",
        lambda: (reap_calls.append(1), 0)[1],
    )

    main_mod.initialize_service()

    assert infra_calls == ["t1", "t2", "t3"]
    assert manifest_calls == ["t1", "t2", "t3"]
    assert reap_calls == [1]


def test_initialize_service_survives_list_teams_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure listing teams must not prevent the orphan reap from running.

    Preconditions: ``_store.list_teams()`` raises.
    Postconditions: ``initialize_service()`` does not raise, no per-team work is
        attempted, and ``reap_orphaned_runs`` still runs.
    """

    def _boom():
        raise RuntimeError("db down")

    infra_calls: list[str] = []
    reap_calls: list[int] = []

    monkeypatch.setattr(main_mod._store, "list_teams", _boom)
    monkeypatch.setattr(main_mod, "get_team_infrastructure", lambda tid: infra_calls.append(tid))
    monkeypatch.setattr(
        main_mod._pipeline_runner,
        "reap_orphaned_runs",
        lambda: (reap_calls.append(1), 0)[1],
    )

    main_mod.initialize_service()

    assert infra_calls == []
    assert reap_calls == [1]


def test_initialize_service_survives_reap_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reap failure must not propagate out of ``initialize_service()``.

    Preconditions: ``_store.list_teams()`` returns no teams; ``reap_orphaned_runs``
        raises.
    Postconditions: ``initialize_service()`` returns normally instead of raising.
    """
    monkeypatch.setattr(main_mod._store, "list_teams", lambda: [])

    def _boom():
        raise RuntimeError("reap failed")

    monkeypatch.setattr(main_mod._pipeline_runner, "reap_orphaned_runs", _boom)

    main_mod.initialize_service()  # must not raise
