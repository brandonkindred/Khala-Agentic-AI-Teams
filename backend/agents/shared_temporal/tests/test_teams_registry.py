"""Tests for shared_temporal.teams_registry's generic worker-startup host.

``start_all_team_workers`` is the fallback path a single-process deployment
could use to boot every registered team's worker in one call, as an
alternative to each team's own dedicated boot hook. It must still honor a
team's own task-queue override (e.g. SOC2's ``resolve_task_queue()``,
following ``TEMPORAL_TASK_QUEUE_SOC2``) rather than always assuming the
``f"{team}-queue"`` convention — otherwise a worker started through this
generic host would poll a different queue than ``start_workflow_sync``
dispatches to, leaving jobs queued with no worker. It must likewise honor a
team's own ``MAX_CONCURRENT_ACTIVITIES`` (e.g. SOC2's, sized for its 5-way
criterion fan-out) rather than always falling back to ``start_team_worker``'s
default of 4 — otherwise a worker started through this generic host would be
under-provisioned relative to the team's own dedicated boot hook.
"""

from __future__ import annotations

from types import SimpleNamespace

from shared_temporal import teams_registry


def test_resolve_task_queue_defaults_to_team_queue_convention():
    mod = SimpleNamespace()
    assert teams_registry._resolve_task_queue("market_research", mod) == "market_research-queue"


def test_resolve_task_queue_prefers_module_override():
    mod = SimpleNamespace(resolve_task_queue=lambda: "custom-soc2-queue")
    assert teams_registry._resolve_task_queue("soc2_compliance", mod) == "custom-soc2-queue"


def test_resolve_task_queue_ignores_non_callable_attribute():
    """A module could export a plain string under this name; only a callable
    override is honored, matching soc2_compliance_team.temporal.resolve_task_queue's
    contract (a function, not a constant)."""
    mod = SimpleNamespace(resolve_task_queue="not-callable")
    assert teams_registry._resolve_task_queue("soc2_compliance", mod) == "soc2_compliance-queue"


def test_resolve_max_concurrent_activities_defaults_to_none():
    mod = SimpleNamespace()
    assert teams_registry._resolve_max_concurrent_activities(mod) is None


def test_resolve_max_concurrent_activities_prefers_module_override():
    mod = SimpleNamespace(MAX_CONCURRENT_ACTIVITIES=8)
    assert teams_registry._resolve_max_concurrent_activities(mod) == 8


def test_resolve_max_concurrent_activities_ignores_non_int_attribute():
    mod = SimpleNamespace(MAX_CONCURRENT_ACTIVITIES="not-an-int")
    assert teams_registry._resolve_max_concurrent_activities(mod) is None


def test_start_all_team_workers_passes_resolved_queue_to_start_team_worker(monkeypatch):
    """The registry's start loop must route each team's resolved queue (not a
    hardcoded f"{team}-queue") into start_team_worker."""
    captured: dict = {}

    def _fake_start_team_worker(team, workflows, activities, *, task_queue, **kwargs):
        captured[team] = {"task_queue": task_queue, **kwargs}
        return True

    fake_module = SimpleNamespace(
        WORKFLOWS=["wf"],
        ACTIVITIES=["act"],
        resolve_task_queue=lambda: "overridden-queue",
    )

    monkeypatch.setattr(teams_registry, "start_team_worker", _fake_start_team_worker)
    monkeypatch.setattr(
        teams_registry,
        "TEAM_TEMPORAL_MODULES",
        {"soc2_compliance": "soc2_compliance_team.temporal"},
    )
    monkeypatch.setattr(teams_registry.importlib, "import_module", lambda path: fake_module)

    results = teams_registry.start_all_team_workers()

    assert results == {"soc2_compliance": True}
    assert captured == {"soc2_compliance": {"task_queue": "overridden-queue"}}


def test_start_all_team_workers_passes_resolved_concurrency_to_start_team_worker(monkeypatch):
    """A team exporting MAX_CONCURRENT_ACTIVITIES must have that value routed
    into start_team_worker, not silently dropped to the shared default."""
    captured: dict = {}

    def _fake_start_team_worker(team, workflows, activities, *, task_queue, **kwargs):
        captured[team] = {"task_queue": task_queue, **kwargs}
        return True

    fake_module = SimpleNamespace(
        WORKFLOWS=["wf"],
        ACTIVITIES=["act"],
        resolve_task_queue=lambda: "soc2-queue",
        MAX_CONCURRENT_ACTIVITIES=8,
    )

    monkeypatch.setattr(teams_registry, "start_team_worker", _fake_start_team_worker)
    monkeypatch.setattr(
        teams_registry,
        "TEAM_TEMPORAL_MODULES",
        {"soc2_compliance": "soc2_compliance_team.temporal"},
    )
    monkeypatch.setattr(teams_registry.importlib, "import_module", lambda path: fake_module)

    results = teams_registry.start_all_team_workers()

    assert results == {"soc2_compliance": True}
    assert captured["soc2_compliance"]["max_concurrent_activities"] == 8
