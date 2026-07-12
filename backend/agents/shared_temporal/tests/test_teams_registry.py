"""Tests for shared_temporal.teams_registry's generic worker-startup host.

``start_all_team_workers`` is the fallback path a single-process deployment
could use to boot every registered team's worker in one call, as an
alternative to each team's own dedicated boot hook. It must still honor a
team's own task-queue override (e.g. SOC2's ``resolve_task_queue()``,
following ``TEMPORAL_TASK_QUEUE_SOC2``) rather than always assuming the
``f"{team}-queue"`` convention — otherwise a worker started through this
generic host would poll a different queue than ``start_workflow_sync``
dispatches to, leaving jobs queued with no worker.
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


def test_start_all_team_workers_passes_resolved_queue_to_start_team_worker(monkeypatch):
    """The registry's start loop must route each team's resolved queue (not a
    hardcoded f"{team}-queue") into start_team_worker."""
    captured: dict = {}

    def _fake_start_team_worker(team, workflows, activities, *, task_queue):
        captured[team] = task_queue
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
    assert captured == {"soc2_compliance": "overridden-queue"}
