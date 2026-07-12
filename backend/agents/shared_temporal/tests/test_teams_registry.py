"""Tests for the shared Temporal worker registry."""

from __future__ import annotations

import types

from shared_temporal import teams_registry


def _fake_module(*, workflows=None, activities=None, task_queue=None):
    mod = types.ModuleType("fake_team.temporal")
    mod.WORKFLOWS = workflows if workflows is not None else [object()]
    mod.ACTIVITIES = activities if activities is not None else [object()]
    if task_queue is not None:
        mod.TASK_QUEUE = task_queue
    return mod


def test_start_all_team_workers_uses_module_task_queue_when_present(monkeypatch):
    """A team that exports its own TASK_QUEUE (e.g. a fixed/legacy queue name
    that doesn't follow the f"{team}-queue" convention) must have that exact
    queue passed to start_team_worker, not a derived name."""
    fake_mod = _fake_module(task_queue="custom-fixed-queue")
    monkeypatch.setattr(teams_registry.importlib, "import_module", lambda path: fake_mod)
    monkeypatch.setattr(
        teams_registry, "TEAM_TEMPORAL_MODULES", {"fake_team": "fake_team.temporal"}
    )

    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(team=team, task_queue=task_queue)
        return True

    monkeypatch.setattr(teams_registry, "start_team_worker", _fake_start)

    results = teams_registry.start_all_team_workers()

    assert captured == {"team": "fake_team", "task_queue": "custom-fixed-queue"}
    assert results == {"fake_team": True}


def test_start_all_team_workers_falls_back_to_derived_queue_when_module_has_none(monkeypatch):
    """A team module with no TASK_QUEUE export keeps the original f"{team}-queue"
    convention, unchanged."""
    fake_mod = _fake_module()
    monkeypatch.setattr(teams_registry.importlib, "import_module", lambda path: fake_mod)
    monkeypatch.setattr(
        teams_registry, "TEAM_TEMPORAL_MODULES", {"fake_team": "fake_team.temporal"}
    )

    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(team=team, task_queue=task_queue)
        return True

    monkeypatch.setattr(teams_registry, "start_team_worker", _fake_start)

    teams_registry.start_all_team_workers()

    assert captured == {"team": "fake_team", "task_queue": "fake_team-queue"}


def test_start_all_team_workers_only_filter(monkeypatch):
    """The ``only`` filter still restricts which registered teams are started."""
    fake_mod = _fake_module(task_queue="q")
    monkeypatch.setattr(teams_registry.importlib, "import_module", lambda path: fake_mod)
    monkeypatch.setattr(
        teams_registry,
        "TEAM_TEMPORAL_MODULES",
        {"team_a": "team_a.temporal", "team_b": "team_b.temporal"},
    )
    monkeypatch.setattr(teams_registry, "start_team_worker", lambda *a, **k: True)

    results = teams_registry.start_all_team_workers(only=["team_a"])

    assert results == {"team_a": True}


def test_start_all_team_workers_skips_module_missing_workflows_or_activities(monkeypatch):
    fake_mod = _fake_module(workflows=[], activities=[object()])
    monkeypatch.setattr(teams_registry.importlib, "import_module", lambda path: fake_mod)
    monkeypatch.setattr(
        teams_registry, "TEAM_TEMPORAL_MODULES", {"fake_team": "fake_team.temporal"}
    )

    results = teams_registry.start_all_team_workers()

    assert results == {"fake_team": False}


def test_start_all_team_workers_swallows_import_errors_per_team(monkeypatch):
    def _boom(path):
        raise ImportError("no such module")

    monkeypatch.setattr(teams_registry.importlib, "import_module", _boom)
    monkeypatch.setattr(
        teams_registry, "TEAM_TEMPORAL_MODULES", {"fake_team": "fake_team.temporal"}
    )

    results = teams_registry.start_all_team_workers()

    assert results == {"fake_team": False}
