"""End-to-end tests for the review-verdict-cache persist/restore wiring.

Unlike ``test_coding_team_graph_persist.py`` (the coordinator's own contract in isolation) and
``test_coding_team_orchestrator.py``'s review-cache unit tests (a single ``CodingTeamSwarm``
constructed directly, never persisted), these tests exercise the FULL round trip: a real
``CodingTeamSwarm`` runs through ``run_coding_team_orchestrator``, its review verdicts reach a
job record via a real ``GraphPersistCoordinator``, and a second swarm/orchestrator invocation is
seeded from that persisted record and demonstrably reuses the cached verdicts.

Golden rule: no test in this file monkeypatches ``CodingTeamSwarm`` itself — doing so would make
the very wiring under test vacuous. Only the Tech Lead (planning/review), the implementation
worker factory, git, and the worktree manager are stubbed.

``serialize_review_cache``/``deserialize_review_cache`` themselves are not tested here (see
``test_swarm_review.py``) — only their effect as part of the end-to-end wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from software_engineering_team import coding_team_orchestrator as orch_mod
from software_engineering_team.coding_team_orchestrator import (
    CodingTeamSwarm,
    run_coding_team_orchestrator,
)
from software_engineering_team.graph_persist import GraphPersistCoordinator
from software_engineering_team.models import CodingTeamPlanInput, StackSpec, Task, TaskStatus
from software_engineering_team.swarm_review import _review_verdict_cache_key
from software_engineering_team.task_graph import TaskGraphService

GIT_UTILS = "shared.git.git_utils"

# A fixed diff shared by the retry/shared-restore-path tests: the seeded cache entry's key and the
# key the retry run recomputes must be byte-identical for a cache hit, so both are built from this
# same constant rather than two independently hand-written diff strings.
_FIXED_DIFF = "diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new"


# --------------------------------------------------------------------------- stubs
#
# Self-contained duplicates of the stubs in test_coding_team_orchestrator.py (this suite's
# convention: each test file owns its own fixtures rather than importing another test module).


class _DefaultGroomTaskMixin:
    """Ungroomed-default ``run_groom_task`` — mirrors the real fallback shape."""

    def run_groom_task(
        self, task_id, task_title, task_description, task_dependencies, plan_context
    ):
        return {
            "acceptance_criteria": [],
            "out_of_scope": "",
            "description_enriched": task_description,
            "priority": "medium",
            "subtasks": [],
            "task_dependencies": task_dependencies,
        }


class StubTechLead(_DefaultGroomTaskMixin):
    """Duck-typed Tech Lead: plans a fixed task list, always returns a fixed review verdict, and
    records every ``run_code_review`` call so tests can assert cache hits (no call) vs misses."""

    def __init__(self, llm=None, *, approved=True, tasks=None):
        self.approved = approved
        self.review_calls: List[str] = []
        self._tasks = (
            tasks
            if tasks is not None
            else [{"id": "t1", "title": "T1"}, {"id": "t2", "title": "T2"}]
        )

    def run_plan_to_task_graph(self, plan_input):
        return {"tasks": self._tasks, "stacks": [{"name": "backend", "tools_services": []}]}

    def run_code_review(
        self,
        task_title,
        task_description,
        acceptance_criteria,
        changes_summary,
        user_decisions=None,
        progress_callback=None,
        spec_content="",
    ):
        self.review_calls.append(changes_summary)
        return {"approved": self.approved, "reason": "ok", "requested_changes": []}

    def run_assignments(self, agent_ids, ready_tasks, free_agents):
        assignments = [
            {"agent_id": a, "task_id": t["id"]} for t, a in zip(ready_tasks, free_agents)
        ]
        return {"assignments": assignments}

    def run_revision_adjudication(
        self, task_title, task_description, acceptance_criteria, changes_summary, revision_feedback
    ):
        return {"verdict": "fail", "reason": "stub verdict"}


class StubWorker:
    """Duck-typed implementation worker: deterministically "implements" every task it is given."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.stack_spec = StackSpec(name=agent_id, tools_services=[])
        self.implement_calls: List[Task] = []

    def run_implement(self, task: Task, path) -> Dict[str, Any]:
        self.implement_calls.append(task)
        return {
            "status": "in_review",
            "feature_branch": f"feature/{task.id}",
            "changes_summary": f"did {task.id}",
            "files_to_create_or_edit": [],
            "error": None,
        }


class _FakeWorktreeManager:
    """Test double for WorktreeManager: no real git worktree I/O, just distinct child dirs."""

    def __init__(self, repo_path: Path, agent_ids):
        self._paths = {aid: Path(repo_path) / f"_wt_{aid}" for aid in agent_ids}

    def prepare(self) -> None:
        for path in self._paths.values():
            path.mkdir(parents=True, exist_ok=True)

    def path_for(self, agent_id: str) -> Path:
        return self._paths[agent_id]

    def cleanup(self) -> None:
        pass


class _FakeJobStore:
    """Dict-backed job record with real merge semantics.

    Unlike the append-only ``writes: List[Dict]`` pattern used elsewhere in this suite (which only
    records write calls but never simulates the merged record), these tests need to read back a
    genuinely MERGED job record and feed it into a second orchestrator/swarm invocation — exactly
    what the real job service's persistence provides.
    """

    def __init__(self, initial: Dict[str, Any] | None = None) -> None:
        self.record: Dict[str, Any] = dict(initial or {})
        self.writes: List[Dict[str, Any]] = []

    def update(self, **kw: Any) -> None:
        self.writes.append(kw)
        self.record.update(kw)

    def get(self, _job_id: Any = None) -> Dict[str, Any]:
        return dict(self.record)


def _make_real_swarm(tmp_path, tech_lead, workers, *, spec_content="", restored_review_cache=None):
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=tech_lead,
        workers=workers,
        graph=graph,
        path=Path(tmp_path),
        agent_ids=[w.agent_id for w in workers],
        llm_getter=lambda key: None,
        spec_content=spec_content,
        restored_review_cache=restored_review_cache,
    )
    swarm._worktrees = _FakeWorktreeManager(swarm.path, [w.agent_id for w in workers])
    swarm._worktrees.prepare()
    swarm._run_quality_gates = lambda *a, **k: True  # type: ignore[method-assign]
    return swarm, graph


def _patch_git(monkeypatch, diff: str = "", merge=(True, "ok")):
    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: diff)
    monkeypatch.setattr(f"{GIT_UTILS}.merge_branch", lambda *a, **k: merge)


def _patch_full_swarm_run(monkeypatch, *, diff: str = "DIFF"):
    """Wire a full ``run_coding_team_orchestrator`` call to build a REAL ``CodingTeamSwarm``
    without real git, real worktrees, or real quality gates.

    Deliberately never patches ``CodingTeamSwarm`` itself (unlike almost every test in
    ``test_coding_team_orchestrator.py``) — these tests exist specifically to prove the real
    class's cache export/restore wiring reaches the job record and back.
    """
    _patch_git(monkeypatch, diff=diff)
    monkeypatch.setattr(orch_mod, "WorktreeManager", _FakeWorktreeManager)
    monkeypatch.setattr(orch_mod.CodingTeamSwarm, "_run_quality_gates", lambda self, *a, **k: True)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: StubWorker(agent_id),
    )


def _seed_snapshot_with_matching_cache(status: str = "failed") -> Dict[str, Any]:
    """A resumable task (default: FAILED, for the retry_failed test) whose seeded
    ``review_verdict_cache`` entry matches exactly what a resumed run (using
    ``StubWorker``/``_FIXED_DIFF``) will recompute — title/description/acceptance_criteria/
    changes_summary/diff/spec_content all line up, so this cache entry's key is derived from the
    real ``_review_verdict_cache_key`` helper against those exact values rather than a hand-hashed
    string (which would silently drift if the key's field set ever changes).

    ``status="to_do"`` is used by the thread/Temporal shared-restore-path test, which does not go
    through ``retry_failed`` (``run_orchestrator_wired`` does not expose that parameter) — a
    plain TO_DO task is picked up and reviewed on the very next round regardless.
    """
    task = {
        "id": "t1",
        "title": "T1",
        "description": "",
        "status": status,
        "dependencies": [],
        "acceptance_criteria": [],
        "changes_summary": "did t1",
        "feature_branch": "feature/t1",
        "revision_count": 3,
    }
    evidence = orch_mod._build_review_evidence("did t1", _FIXED_DIFF)
    cache_key = _review_verdict_cache_key(
        task_title="T1",
        task_description="",
        acceptance_criteria=[],
        evidence=evidence,
        user_decisions=[],
        spec_content="",
    )
    verdict = {"approved": True, "reason": "ok", "requested_changes": []}
    return {
        "task_graph_snapshot": [task],
        "agent_task_map": {},
        "review_verdict_cache": [{"task_id": "t1", "cache_key": cache_key, "verdict": verdict}],
    }


# --------------------------------------------------------------------------- criterion 1


def test_persist_then_restore_populates_cache(tmp_path, monkeypatch):
    """A real swarm's reviews reach the job record as a non-empty review_verdict_cache; a second
    swarm built from that persisted value starts with a non-empty in-memory cache."""
    _patch_full_swarm_run(monkeypatch, diff="diff --git a/x b/x")
    tech_lead = StubTechLead(
        approved=True, tasks=[{"id": "t1", "title": "T1"}, {"id": "t2", "title": "T2"}]
    )
    monkeypatch.setattr(orch_mod, "TechLeadAgent", lambda llm: tech_lead)

    store = _FakeJobStore()
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=store.update,
        get_job_fn=store.get,
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    assert store.record["status"] == "completed"
    cache = store.record.get("review_verdict_cache")
    assert cache, "persisted job record must carry a non-empty review_verdict_cache"
    assert {entry["task_id"] for entry in cache} == {"t1", "t2"}

    swarm2, _ = _make_real_swarm(
        tmp_path, StubTechLead(approved=True), [StubWorker("a1")], restored_review_cache=cache
    )
    assert swarm2._review_verdict_cache
    assert set(swarm2._review_verdict_cache) == {"t1", "t2"}


# --------------------------------------------------------------------------- criterion 2


def test_cache_hit_skips_run_code_review(tmp_path, monkeypatch):
    """A restored swarm reviewing a task whose inputs are byte-identical to the cached entry
    reuses the cached verdict and never calls run_code_review again."""
    _patch_git(monkeypatch, diff="diff --git a/x b/x")
    tech_lead1 = StubTechLead(approved=True)
    swarm1, graph1 = _make_real_swarm(tmp_path, tech_lead1, [StubWorker("a1")])
    graph1.add_task("t1", title="T1")
    graph1.assign_task_to_agent("t1", "a1")
    graph1.set_task_in_review("t1")
    swarm1._review_and_merge(lambda **kw: None)
    assert tech_lead1.review_calls  # sanity: the first pass is a genuine miss
    exported = swarm1.export_review_cache()
    assert exported

    tech_lead2 = StubTechLead(approved=True)
    swarm2, graph2 = _make_real_swarm(
        tmp_path, tech_lead2, [StubWorker("a1")], restored_review_cache=exported
    )
    graph2.add_task("t1", title="T1")
    graph2.assign_task_to_agent("t1", "a1")
    graph2.set_task_in_review("t1")
    swarm2._review_and_merge(lambda **kw: None)

    assert tech_lead2.review_calls == []  # cache hit: run_code_review never called
    assert graph2.get_task("t1").status == TaskStatus.MERGED


def test_cache_miss_on_changed_diff_calls_run_code_review(tmp_path, monkeypatch):
    """The hit/miss discrimination is real: a changed branch diff since the cached call misses
    the cache and calls run_code_review again, rather than the cache always short-circuiting."""
    _patch_git(monkeypatch, diff="diff --git a/x b/x")
    tech_lead1 = StubTechLead(approved=True)
    swarm1, graph1 = _make_real_swarm(tmp_path, tech_lead1, [StubWorker("a1")])
    graph1.add_task("t1", title="T1")
    graph1.assign_task_to_agent("t1", "a1")
    graph1.set_task_in_review("t1")
    swarm1._review_and_merge(lambda **kw: None)
    exported = swarm1.export_review_cache()

    _patch_git(monkeypatch, diff="a completely different diff")
    tech_lead2 = StubTechLead(approved=True)
    swarm2, graph2 = _make_real_swarm(
        tmp_path, tech_lead2, [StubWorker("a1")], restored_review_cache=exported
    )
    graph2.add_task("t1", title="T1")
    graph2.assign_task_to_agent("t1", "a1")
    graph2.set_task_in_review("t1")
    swarm2._review_and_merge(lambda **kw: None)

    assert tech_lead2.review_calls  # cache miss: run_code_review called for the changed diff


def test_cache_miss_on_changed_changes_summary_calls_run_code_review(tmp_path, monkeypatch):
    """The hit/miss discrimination also covers changes_summary independently of the diff: with the
    branch diff held fixed, a changed changes_summary (task.changes_summary, the field
    ``_compute_review`` folds into ``evidence`` alongside the diff — see swarm_review.py) since the
    cached call misses the cache and calls run_code_review again."""
    _patch_git(monkeypatch, diff="diff --git a/x b/x")
    tech_lead1 = StubTechLead(approved=True)
    swarm1, graph1 = _make_real_swarm(tmp_path, tech_lead1, [StubWorker("a1")])
    graph1.add_task("t1", title="T1")
    graph1.assign_task_to_agent("t1", "a1")
    graph1.update_task("t1", changes_summary="did t1 v1")
    graph1.set_task_in_review("t1")
    swarm1._review_and_merge(lambda **kw: None)
    exported = swarm1.export_review_cache()

    tech_lead2 = StubTechLead(approved=True)
    swarm2, graph2 = _make_real_swarm(
        tmp_path, tech_lead2, [StubWorker("a1")], restored_review_cache=exported
    )
    graph2.add_task("t1", title="T1")
    graph2.assign_task_to_agent("t1", "a1")
    graph2.update_task("t1", changes_summary="did t1 v2")
    graph2.set_task_in_review("t1")
    swarm2._review_and_merge(lambda **kw: None)

    assert tech_lead2.review_calls  # cache miss: run_code_review called for the changed summary


# --------------------------------------------------------------------------- criterion 3


def test_persist_sync_omits_cache_when_no_swarm_attached(tmp_path):
    """A coordinator with no swarm attached (review_cache_export unset — the coordinator's own
    default) writes no review_verdict_cache field and raises nothing."""
    store = _FakeJobStore()
    coord = GraphPersistCoordinator(
        "j1",
        store.update,
        progress_base=0,
        progress_span=100,
        phase="task_graph",
        status_text="Building task graph from plan",
    )
    try:
        coord.graph.add_task("t1", title="T1")
        coord.persist_sync()
    finally:
        coord.stop()
    assert store.writes
    assert "review_verdict_cache" not in store.writes[-1]


def test_orchestrator_pre_swarm_persist_never_carries_cache(tmp_path, monkeypatch):
    """The orchestrator's own pre-swarm persist_sync() call (before CodingTeamSwarm exists, and
    thus before coord.review_cache_export is set) never writes review_verdict_cache, even when the
    run ends early right after that point."""
    tech_lead = StubTechLead(approved=True)
    monkeypatch.setattr(orch_mod, "TechLeadAgent", lambda llm: tech_lead)

    def _boom(*a, **k):
        raise RuntimeError("cannot build workers")

    monkeypatch.setattr(orch_mod, "_build_implementation_worker", _boom)

    store = _FakeJobStore()
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=store.update,
        get_job_fn=store.get,
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    assert store.record["status"] == "failed"
    assert all("review_verdict_cache" not in w for w in store.writes)


# --------------------------------------------------------------------------- criterion 4


@pytest.mark.parametrize(
    "corrupt",
    [
        "not-a-list",
        [{"task_id": "t1"}],
        [{"task_id": "t1", "cache_key": 123, "verdict": "not-a-dict"}],
        [{"task_id": "t1", "cache_key": "k", "verdict": {"approved": "not-a-bool"}}],
    ],
)
def test_corrupt_review_cache_degrades_to_empty(tmp_path, corrupt):
    """A CodingTeamSwarm built from a corrupt review_verdict_cache value starts cleanly with an
    empty cache — never raises."""
    swarm, _ = _make_real_swarm(
        tmp_path, StubTechLead(approved=True), [StubWorker("a1")], restored_review_cache=corrupt
    )
    assert swarm._review_verdict_cache == {}
    assert swarm.export_review_cache() == []


def test_resume_with_corrupt_review_cache_completes_cleanly(tmp_path, monkeypatch):
    """A full resume with a corrupt review_verdict_cache field in the job record completes
    normally — the corrupt value degrades to an empty cache rather than aborting the job."""
    _patch_full_swarm_run(monkeypatch, diff="diff --git a/x b/x")
    tech_lead = StubTechLead(approved=True)
    monkeypatch.setattr(orch_mod, "TechLeadAgent", lambda llm: tech_lead)

    snapshot = {
        "task_graph_snapshot": [
            {"id": "t1", "title": "T1", "status": "to_do", "dependencies": []},
        ],
        "agent_task_map": {},
        "review_verdict_cache": "not-a-list",
    }
    store = _FakeJobStore(snapshot)
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=store.update,
        get_job_fn=store.get,
        cache_dir=tmp_path,
        get_llm=lambda key: None,
    )

    assert store.record["status"] == "completed"


# --------------------------------------------------------------------------- criterion 5


def test_retry_failed_restores_and_benefits_from_cache(tmp_path, monkeypatch):
    """retry_failed=True demotes a FAILED task to TO_DO and restores the review verdict cache;
    when the retry reproduces byte-identical review inputs, the restored cache is actually
    consulted (not just restored-and-ignored) — run_code_review is never called."""
    _patch_full_swarm_run(monkeypatch, diff=_FIXED_DIFF)
    tech_lead = StubTechLead(approved=True)
    monkeypatch.setattr(orch_mod, "TechLeadAgent", lambda llm: tech_lead)

    store = _FakeJobStore(_seed_snapshot_with_matching_cache())
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=store.update,
        get_job_fn=store.get,
        cache_dir=tmp_path,
        get_llm=lambda key: None,
        retry_failed=True,
    )

    assert store.record["status"] == "completed"
    assert tech_lead.review_calls == []  # cache hit on the retried task


# --------------------------------------------------------------------------- criterion 6


def test_restored_review_cache_caps_at_twenty_entries(tmp_path):
    """A job record whose review_verdict_cache field somehow carries more than 20 entries (e.g.
    hand-edited, migrated, or written by something other than serialize_review_cache — the normal
    persist path already caps at 20 before it ever reaches storage) still only seeds 20 entries
    into the live swarm cache: deserialize_review_cache enforces the same cap serialize_review_cache
    does, so restore can never grow the cache past the size the rest of the system assumes."""
    oversized = [
        {"task_id": f"task-{i}", "cache_key": f"key-{i}", "verdict": {"approved": True}}
        for i in range(25)
    ]
    swarm, _ = _make_real_swarm(
        tmp_path,
        StubTechLead(approved=True),
        [StubWorker("a1")],
        restored_review_cache=oversized,
    )
    assert len(swarm._review_verdict_cache) == 20
    assert set(swarm._review_verdict_cache) == {f"task-{i}" for i in range(5, 25)}


def _run_via_wired(tmp_path, monkeypatch, *, pause_strategy: str, job_record: Dict[str, Any]):
    """Call ``run_orchestrator_wired`` directly with each ``pause_strategy`` value it accepts,
    proving the shared restore/cache-consultation path inside it (and the
    ``run_coding_team_orchestrator`` call it wraps) is invariant to that flag — i.e. there is no
    separate restore code path per strategy value to diverge.

    This does NOT by itself prove a real caller passes the value it claims to: that delegation is
    verified separately (see ``test_run_pipeline_activity_delegates_with_restored_cache`` below,
    and ``test_coding_team_temporal_activity.py``'s own
    ``kwargs["pause_strategy"] == "return"`` assertions for the Temporal activity). Combined, the
    two establish what a single "both modes" test asserting only on this helper cannot: that the
    real Temporal entrypoint both requests ``pause_strategy="return"`` AND that value exercises
    the exact same cache-restore behavior as ``"block"``.
    """
    from software_engineering_team.api import coding_team_main as main_mod
    from software_engineering_team.api import orchestration as orch_wired_mod

    store = _FakeJobStore(job_record)
    # get_job_fn is fixed to _main.get_job inside run_orchestrator_wired (not caller-overridable),
    # so the job-record seam is patched at the module level; update_job_fn IS overridable and is
    # passed directly.
    monkeypatch.setattr(main_mod, "get_job", lambda jid: store.get(jid))
    plan = CodingTeamPlanInput(repo_path=str(tmp_path))
    orch_wired_mod.run_orchestrator_wired(
        "j1",
        str(tmp_path),
        plan,
        pause_strategy=pause_strategy,
        update_job_fn=store.update,
    )
    return store


@pytest.mark.parametrize("pause_strategy", ["block", "return"])
def test_run_orchestrator_wired_restore_path_is_pause_strategy_agnostic(
    tmp_path, monkeypatch, pause_strategy
):
    """run_orchestrator_wired's cache-restore/cache-consultation behavior is identical for
    pause_strategy='block' and 'return' — there is exactly one restore path inside it, not one
    per strategy value that could silently diverge."""
    _patch_full_swarm_run(monkeypatch, diff=_FIXED_DIFF)
    tech_lead = StubTechLead(approved=True)
    monkeypatch.setattr(orch_mod, "TechLeadAgent", lambda llm: tech_lead)

    store = _run_via_wired(
        tmp_path,
        monkeypatch,
        pause_strategy=pause_strategy,
        job_record=_seed_snapshot_with_matching_cache(status="to_do"),
    )

    assert store.record["status"] == "completed"
    assert tech_lead.review_calls == []  # cache hit, identical for both pause_strategy values


def test_run_pipeline_activity_delegates_with_restored_cache(tmp_path, monkeypatch):
    """The real Temporal entrypoint, ``run_pipeline_activity`` (invoked, not stubbed — unlike
    ``test_coding_team_temporal_activity.py``'s delegation-shape tests, which stub
    run_orchestrator_wired itself), runs a real CodingTeamSwarm end to end and consults a
    persisted review-verdict cache exactly as the plain orchestrator entrypoint does — closing the
    gap the helper-level test above cannot: proof that this real caller's
    pause_strategy="return" request reaches and benefits from the restore path.
    """
    import software_engineering_team.engine_provider as ep
    from software_engineering_team.api import coding_team_main as main_mod
    from software_engineering_team.temporal.coding_team_workflow import run_pipeline_activity

    _patch_full_swarm_run(monkeypatch, diff=_FIXED_DIFF)
    tech_lead = StubTechLead(approved=True)
    monkeypatch.setattr(orch_mod, "TechLeadAgent", lambda llm: tech_lead)
    monkeypatch.setattr(ep, "get_engine_provider", lambda: object())

    seed = _seed_snapshot_with_matching_cache(status="to_do")
    store = _FakeJobStore({"job_id": "j1", "repo_path": str(tmp_path), **seed})
    monkeypatch.setattr(main_mod, "create_job", lambda **kw: None)
    monkeypatch.setattr(main_mod, "get_job", lambda jid: store.get(jid))
    monkeypatch.setattr(main_mod, "update_job", lambda jid, **kw: store.update(**kw))

    out = run_pipeline_activity(
        {
            "job_id": "j1",
            "repo_path": str(tmp_path),
            "plan_input": {"objective": "resume with cache", "repo_path": str(tmp_path)},
        }
    )

    assert out["outcome"] == "completed"
    assert store.record["status"] == "completed"
    assert tech_lead.review_calls == []  # the activity's real delegation reached the cache hit
