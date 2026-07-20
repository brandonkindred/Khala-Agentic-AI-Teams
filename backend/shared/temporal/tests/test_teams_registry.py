"""Tests for shared_temporal.teams_registry's generic worker-startup host.

``start_all_team_workers`` is the fallback path a single-process deployment
could use to boot every registered team's worker in one call, as an
alternative to each team's own dedicated boot hook. It must still honor a
team's own task-queue override (e.g. SOC2's ``resolve_task_queue()``,
following ``TEMPORAL_TASK_QUEUE_SOC2``) rather than always assuming the
``f"{team}-queue"`` convention — otherwise a worker started through this
generic host would poll a different queue than ``start_workflow_sync``
dispatches to, leaving jobs queued with no worker. It must likewise honor a
team's own plain ``TASK_QUEUE`` constant when it exports no
``resolve_task_queue()`` (e.g. PA's fixed ``personal-assistant`` legacy-drain
queue, which also doesn't follow the ``f"{team}-queue"`` convention), and a
team's own ``MAX_CONCURRENT_ACTIVITIES`` (e.g. SOC2's, sized for its 5-way
criterion fan-out) rather than always falling back to ``start_team_worker``'s
default of 4 — otherwise a worker started through this generic host would be
under-provisioned relative to the team's own dedicated boot hook.
"""

from __future__ import annotations

import types

from shared_temporal import teams_registry


def _fake_module(
    *, workflows=None, activities=None, task_queue=None, max_concurrent_activities=None
):
    mod = types.ModuleType("fake_team.temporal")
    mod.WORKFLOWS = workflows if workflows is not None else [object()]
    mod.ACTIVITIES = activities if activities is not None else [object()]
    if task_queue is not None:
        mod.TASK_QUEUE = task_queue
    if max_concurrent_activities is not None:
        mod.MAX_CONCURRENT_ACTIVITIES = max_concurrent_activities
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


def test_start_all_team_workers_uses_module_max_concurrent_activities_when_present(monkeypatch):
    # A team that pins a non-default concurrency cap in its own dedicated
    # boot hook must export the SAME value here, since start_team_worker is
    # idempotent per team name and whichever caller starts the worker first
    # wins for the whole process.
    fake_mod = _fake_module(max_concurrent_activities=2)
    monkeypatch.setattr(teams_registry.importlib, "import_module", lambda path: fake_mod)
    monkeypatch.setattr(
        teams_registry, "TEAM_TEMPORAL_MODULES", {"fake_team": "fake_team.temporal"}
    )

    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue, max_concurrent_activities=4):
        captured.update(team=team, max_concurrent_activities=max_concurrent_activities)
        return True

    monkeypatch.setattr(teams_registry, "start_team_worker", _fake_start)

    teams_registry.start_all_team_workers()

    assert captured == {"team": "fake_team", "max_concurrent_activities": 2}


def test_start_all_team_workers_omits_max_concurrent_activities_when_module_has_none(monkeypatch):
    # No export -> the kwarg is left off entirely, so start_team_worker's own
    # default (not a duplicated literal here) is what actually applies.
    fake_mod = _fake_module()
    monkeypatch.setattr(teams_registry.importlib, "import_module", lambda path: fake_mod)
    monkeypatch.setattr(
        teams_registry, "TEAM_TEMPORAL_MODULES", {"fake_team": "fake_team.temporal"}
    )

    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(team=team)
        return True

    monkeypatch.setattr(teams_registry, "start_team_worker", _fake_start)

    teams_registry.start_all_team_workers()

    assert captured == {"team": "fake_team"}


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


def test_resolve_task_queue_defaults_to_team_queue_convention():
    mod = types.SimpleNamespace()
    assert teams_registry._resolve_task_queue("market_research", mod) == "market_research-queue"


def test_resolve_task_queue_prefers_module_override():
    mod = types.SimpleNamespace(resolve_task_queue=lambda: "custom-soc2-queue")
    assert teams_registry._resolve_task_queue("soc2_compliance", mod) == "custom-soc2-queue"


def test_resolve_task_queue_ignores_non_callable_attribute():
    """A module could export a plain string under this name; only a callable
    override is honored, matching soc2_compliance_team.temporal.resolve_task_queue's
    contract (a function, not a constant)."""
    mod = types.SimpleNamespace(resolve_task_queue="not-callable")
    assert teams_registry._resolve_task_queue("soc2_compliance", mod) == "soc2_compliance-queue"


def test_resolve_task_queue_falls_back_to_module_task_queue_when_no_resolver():
    """A team with a plain TASK_QUEUE constant and no resolve_task_queue()
    (e.g. PA's fixed, non-f"{team}-queue"-convention ``personal-assistant``
    queue) must have that constant honored, not silently overridden by the
    generic f"{team}-queue" convention — regression guard for the merge that
    introduced resolve_task_queue()/the SimpleNamespace-based tests above,
    which originally skipped this fallback."""
    mod = types.SimpleNamespace(TASK_QUEUE="personal-assistant")
    assert teams_registry._resolve_task_queue("personal_assistant", mod) == "personal-assistant"


def test_resolve_max_concurrent_activities_defaults_to_none():
    mod = types.SimpleNamespace()
    assert teams_registry._resolve_max_concurrent_activities(mod) is None


def test_resolve_max_concurrent_activities_prefers_module_override():
    mod = types.SimpleNamespace(MAX_CONCURRENT_ACTIVITIES=8)
    assert teams_registry._resolve_max_concurrent_activities(mod) == 8


def test_resolve_max_concurrent_activities_ignores_non_int_attribute():
    mod = types.SimpleNamespace(MAX_CONCURRENT_ACTIVITIES="not-an-int")
    assert teams_registry._resolve_max_concurrent_activities(mod) is None


def test_start_all_team_workers_passes_resolved_queue_to_start_team_worker(monkeypatch):
    """The registry's start loop must route each team's resolved queue (not a
    hardcoded f"{team}-queue") into start_team_worker."""
    captured: dict = {}

    def _fake_start_team_worker(team, workflows, activities, *, task_queue, **kwargs):
        captured[team] = {"task_queue": task_queue, **kwargs}
        return True

    fake_module = types.SimpleNamespace(
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

    fake_module = types.SimpleNamespace(
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
