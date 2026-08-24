"""Unit tests for ``coding_team.agent_status``: the pure per-agent roster derivation.

These call ``build_agent_statuses``/``derive_stack_roster`` directly (no TestClient) so each
branch — worker working/in_review/idle, the Tech Lead's planning/reviewing/idle, and the
single current_activity overlay onto the right card — is exercised in isolation. The fixture
shapes (``stack_specs`` dicts and ``agent_task_map`` keyed by stack name) mirror what the
orchestrator persists (see ``test_orchestrator.py``).
"""

from __future__ import annotations

from typing import Any, Dict, List

from software_engineering_team.agent_status import (
    TECH_LEAD_AGENT_ID,
    _coerce_fraction,
    build_agent_statuses,
    derive_stack_roster,
)


def _by_id(entries) -> Dict[str, Any]:
    return {e.agent_id: e for e in entries}


# --------------------------------------------------------------------------- derive_stack_roster


def test_derive_roster_uses_name_and_tools():
    roster = derive_stack_roster([{"name": "frontend", "tools_services": ["Angular", "SCSS"]}])
    assert roster == [("frontend", "frontend", ["Angular", "SCSS"])]


def test_derive_roster_name_falls_back_to_stack_index():
    # A nameless stack and a non-dict entry both fall back to the positional id, and the
    # agent_id always equals the display name (the agent_task_map key the orchestrator writes).
    roster = derive_stack_roster([{"tools_services": ["Java"]}, "garbage"])
    assert roster == [("stack_0", "stack_0", ["Java"]), ("stack_1", "stack_1", [])]


def test_derive_roster_tolerates_malformed_tools():
    roster = derive_stack_roster([{"name": "x", "tools_services": None}, {"name": "y"}])
    assert roster == [("x", "x", []), ("y", "y", [])]


def test_derive_roster_empty():
    assert derive_stack_roster([]) == []


def test_derive_roster_non_list_returns_empty():
    # Defensive: a precondition violation (None / non-list) degrades to an empty roster, never raises.
    assert derive_stack_roster(None) == []  # type: ignore[arg-type]
    assert derive_stack_roster("garbage") == []  # type: ignore[arg-type]


def test_derive_roster_disambiguates_duplicate_names():
    # Two stacks sharing a name get unique agent_ids (so they never collide on one agent_task_map
    # key) while keeping the original display name.
    roster = derive_stack_roster([{"name": "backend"}, {"name": "backend"}, {"name": "backend"}])
    assert [e.agent_id for e in roster] == ["backend", "backend_2", "backend_3"]
    assert [e.display_name for e in roster] == ["backend", "backend", "backend"]


def test_derive_roster_avoids_cross_name_collisions():
    # A literal stack name ("backend_2") that matches a suffix generated for an earlier duplicate of
    # "backend" must still get a unique id — global uniqueness, not merely per-name counting. The
    # third stack keeps suffixing from its own name, so it becomes "backend_2_2", not a reused id.
    roster = derive_stack_roster([{"name": "backend"}, {"name": "backend"}, {"name": "backend_2"}])
    ids = [e.agent_id for e in roster]
    assert ids == ["backend", "backend_2", "backend_2_2"]
    assert len(set(ids)) == len(ids)  # all unique — the key guarantee


def test_duplicate_stack_names_yield_distinct_worker_cards():
    # End-to-end: two same-named stacks each mapped to their own task render as two distinct cards.
    stacks = [{"name": "backend"}, {"name": "backend"}]
    amap = {"backend": "t1", "backend_2": "t2"}
    snap = [
        {"id": "t1", "title": "API", "status": "in_progress"},
        {"id": "t2", "title": "Worker", "status": "in_progress"},
    ]
    entries = _by_id(build_agent_statuses(stacks, amap, snap, None, "coding"))
    assert entries["backend"].current_task_title == "API"
    assert entries["backend_2"].current_task_title == "Worker"


def test_derive_roster_copies_tools_list():
    src = ["Angular"]
    ((_aid, _name, tools),) = derive_stack_roster([{"name": "f", "tools_services": src}])
    tools.append("mutated")
    assert src == ["Angular"]  # the roster holds a copy, not the caller's list


def test_derive_roster_default_is_one_worker_per_stack():
    # CODING_TEAM_WORKERS_PER_STACK unset -> identical to the pre-widening single-id-per-stack shape.
    roster = derive_stack_roster([{"name": "backend_v2", "tools_services": ["FastAPI"]}])
    assert roster == [("backend_v2", "backend_v2", ["FastAPI"])]


def test_derive_roster_workers_per_stack_env_var(monkeypatch):
    monkeypatch.setenv("CODING_TEAM_WORKERS_PER_STACK", "3")
    roster = derive_stack_roster([{"name": "backend_v2", "tools_services": ["FastAPI"]}])
    assert [e.agent_id for e in roster] == ["backend_v2-1", "backend_v2-2", "backend_v2-3"]
    assert [e.display_name for e in roster] == ["backend_v2"] * 3
    assert [e.tools_services for e in roster] == [["FastAPI"]] * 3
    # Each worker holds its own copy of tools_services, not a shared reference.
    roster[0].tools_services.append("mutated")
    assert roster[1].tools_services == ["FastAPI"]


def test_derive_roster_workers_per_stack_multiple_stacks(monkeypatch):
    monkeypatch.setenv("CODING_TEAM_WORKERS_PER_STACK", "2")
    roster = derive_stack_roster([{"name": "backend_v2"}, {"name": "frontend_v2"}])
    ids = [e.agent_id for e in roster]
    assert ids == ["backend_v2-1", "backend_v2-2", "frontend_v2-1", "frontend_v2-2"]
    assert len(set(ids)) == len(ids)  # all globally unique


def test_derive_roster_workers_per_stack_garbage_or_zero_falls_back_to_one(monkeypatch):
    for bad_value in ("0", "-1", "garbage"):
        monkeypatch.setenv("CODING_TEAM_WORKERS_PER_STACK", bad_value)
        roster = derive_stack_roster([{"name": "backend_v2"}])
        assert [e.agent_id for e in roster] == ["backend_v2"]


def test_derive_roster_workers_per_stack_avoids_collision_with_literal_stack_name(monkeypatch):
    # A literal stack name that matches a worker-suffixed id generated for an earlier stack must
    # still resolve to a unique agent_id via the same global dedup used for base ids.
    monkeypatch.setenv("CODING_TEAM_WORKERS_PER_STACK", "2")
    roster = derive_stack_roster([{"name": "backend"}, {"name": "backend-1"}])
    ids = [e.agent_id for e in roster]
    # First stack ("backend") claims "backend-1"/"backend-2" as its two worker ids. The second
    # stack's literal name ("backend-1") then collides with the first worker id, so its own base
    # id is bumped to "backend-1_2" before it gets its two worker-suffixed ids.
    assert ids == ["backend-1", "backend-2", "backend-1_2-1", "backend-1_2-2"]
    assert len(set(ids)) == len(ids)


# --------------------------------------------------------------------------- _coerce_fraction


def test_coerce_fraction_clamps_and_rejects():
    assert _coerce_fraction(0.4) == 0.4
    assert _coerce_fraction(1.5) == 1.0
    assert _coerce_fraction(-0.2) == 0.0
    assert _coerce_fraction(1) == 1.0
    assert _coerce_fraction(True) is None  # bool is not a fraction
    assert _coerce_fraction("0.5") is None
    assert _coerce_fraction(None) is None


def test_coerce_fraction_rejects_non_finite():
    # NaN/±inf must become None — a NaN would otherwise fail the model's [0, 1] constraint and
    # crash the status endpoint when build_agent_statuses constructs the AgentStatusEntry.
    assert _coerce_fraction(float("nan")) is None
    assert _coerce_fraction(float("inf")) is None
    assert _coerce_fraction(float("-inf")) is None


def test_build_agent_statuses_tolerates_non_list_inputs():
    # A corrupt record (non-list/non-dict fields) must degrade to a Tech-Lead-only roster, never
    # raise — the function's "never raises on malformed input" contract.
    entries = build_agent_statuses(None, None, None, None, None)  # type: ignore[arg-type]
    assert [e.agent_id for e in entries] == [TECH_LEAD_AGENT_ID]


def test_build_agent_statuses_tolerates_non_hashable_task_id():
    # A corrupt task whose id is non-hashable (a list) must be skipped, not raise a TypeError while
    # building the id->task index.
    snap = [
        {"id": ["weird"], "title": "Bad", "status": "in_progress"},
        {"id": "t1", "title": "OK", "status": "in_progress"},
    ]
    entries = _by_id(
        build_agent_statuses([{"name": "frontend"}], {"frontend": "t1"}, snap, None, "coding")
    )
    assert entries["frontend"].current_task_title == "OK"


# --------------------------------------------------------------------------- roster shape


def test_tech_lead_always_first_and_planning_during_task_graph():
    entries = build_agent_statuses([], {}, [], None, "task_graph")
    assert len(entries) == 1
    tl = entries[0]
    assert tl.agent_id == TECH_LEAD_AGENT_ID
    assert tl.role == "tech_lead"
    assert tl.display_name == "Tech Lead"
    assert tl.stack is None
    assert tl.status == "planning"


def test_empty_stacks_tech_lead_idle_when_coding_and_no_review():
    entries = build_agent_statuses([], {}, [], None, "coding")
    assert [e.agent_id for e in entries] == [TECH_LEAD_AGENT_ID]
    assert entries[0].status == "idle"


def test_one_worker_per_stack_in_order():
    entries = build_agent_statuses(
        [{"name": "frontend"}, {"name": "backend"}], {}, [], None, "coding"
    )
    assert [e.agent_id for e in entries] == [TECH_LEAD_AGENT_ID, "frontend", "backend"]
    eng = entries[1]
    assert eng.role == "implementation_worker"
    assert eng.display_name == "Implementation Worker - frontend"
    assert eng.stack == "frontend"


# --------------------------------------------------------------------------- engineer status


def test_engineer_idle_when_not_in_map():
    entries = _by_id(build_agent_statuses([{"name": "frontend"}], {}, [], None, "coding"))
    eng = entries["frontend"]
    assert eng.status == "idle"
    assert eng.current_task_id is None
    assert eng.current_task_title is None


def test_engineer_working_when_task_in_progress():
    snap = [{"id": "t1", "title": "Build UI", "status": "in_progress"}]
    entries = _by_id(
        build_agent_statuses([{"name": "frontend"}], {"frontend": "t1"}, snap, None, "coding")
    )
    eng = entries["frontend"]
    assert eng.status == "working"
    assert eng.current_task_id == "t1"
    assert eng.current_task_title == "Build UI"


def test_engineer_in_review_when_task_in_review():
    snap = [{"id": "t2", "title": "API", "status": "in_review"}]
    entries = _by_id(
        build_agent_statuses([{"name": "backend"}], {"backend": "t2"}, snap, None, "coding")
    )
    assert entries["backend"].status == "in_review"


def test_engineer_working_when_mapped_task_missing_from_snapshot():
    # A defensive case: the map points at a task id absent from the snapshot -> treated as idle
    # (no task dict to read), never a crash.
    entries = _by_id(
        build_agent_statuses([{"name": "frontend"}], {"frontend": "ghost"}, [], None, "coding")
    )
    assert entries["frontend"].status == "idle"
    assert entries["frontend"].current_task_id is None


def test_multiple_concurrent_engineers():
    stacks = [{"name": "frontend"}, {"name": "backend"}]
    amap = {"frontend": "t1", "backend": "t2"}
    snap = [
        {"id": "t1", "title": "UI", "status": "in_progress"},
        {"id": "t2", "title": "API", "status": "in_progress"},
    ]
    entries = _by_id(build_agent_statuses(stacks, amap, snap, None, "coding"))
    assert entries["frontend"].status == "working"
    assert entries["backend"].status == "working"


# --------------------------------------------------------------------------- Tech Lead status


def test_tech_lead_reviewing_when_any_task_in_review():
    snap = [{"id": "t2", "title": "API", "status": "in_review"}]
    entries = _by_id(
        build_agent_statuses([{"name": "backend"}], {"backend": "t2"}, snap, None, "coding")
    )
    assert entries[TECH_LEAD_AGENT_ID].status == "reviewing"


def test_tech_lead_reviewing_when_tech_lead_review_activity_without_in_review_task():
    # The activity flags a tech_lead_review even if the snapshot has no in_review task yet.
    activity = {"agent": "tech_lead_review", "step": "reviewing", "fraction": 0.2}
    snap = [{"id": "t1", "title": "UI", "status": "in_progress"}]
    entries = _by_id(
        build_agent_statuses([{"name": "frontend"}], {"frontend": "t1"}, snap, activity, "coding")
    )
    assert entries[TECH_LEAD_AGENT_ID].status == "reviewing"


# --------------------------------------------------------------------------- activity overlay


def test_tech_lead_review_overlay_lands_on_tech_lead_not_engineer():
    """tech_lead_review carries the engineer's task_id and that task is still mapped to the
    engineer (in_review); branching on agent first keeps the overlay on the Tech Lead's card."""
    snap = [{"id": "t2", "title": "API", "status": "in_review"}]
    activity = {
        "agent": "tech_lead_review",
        "step": "parsing",
        "detail": "chunk 1/2",
        "fraction": 0.5,
        "task_id": "t2",
        "task_title": "API",
    }
    entries = _by_id(
        build_agent_statuses([{"name": "backend"}], {"backend": "t2"}, snap, activity, "coding")
    )
    tl = entries[TECH_LEAD_AGENT_ID]
    assert tl.current_step == "parsing"
    assert tl.activity_detail == "chunk 1/2"
    assert tl.activity_fraction == 0.5
    # The engineer is in_review but carries NO overlay (the review belongs to the Tech Lead).
    eng = entries["backend"]
    assert eng.status == "in_review"
    assert eng.current_step is None
    assert eng.activity_fraction is None


def test_activity_without_agent_key_overlays_engineer_and_leaves_tech_lead_idle():
    # A current_activity dict with no 'agent' key: activity_agent becomes None, so the overlay is
    # treated as engineer activity (lands on the task's owner) and the Tech Lead stays idle when no
    # task is in review.
    snap = [{"id": "t1", "title": "UI", "status": "in_progress"}]
    activity = {"step": "reviewing", "detail": "src/app.py", "fraction": 0.3, "task_id": "t1"}
    entries = _by_id(
        build_agent_statuses([{"name": "frontend"}], {"frontend": "t1"}, snap, activity, "coding")
    )
    eng = entries["frontend"]
    assert eng.current_step == "reviewing"
    assert eng.activity_detail == "src/app.py"
    assert eng.activity_fraction == 0.3
    assert entries[TECH_LEAD_AGENT_ID].status == "idle"
    assert entries[TECH_LEAD_AGENT_ID].current_step is None


def test_code_review_overlay_lands_on_owning_engineer_and_clamps_fraction():
    snap = [{"id": "t1", "title": "UI", "status": "in_progress"}]
    activity = {
        "agent": "code_review",
        "step": "reviewing",
        "detail": "src/app.py",
        "fraction": 1.5,
        "task_id": "t1",
    }
    entries = _by_id(
        build_agent_statuses([{"name": "frontend"}], {"frontend": "t1"}, snap, activity, "coding")
    )
    eng = entries["frontend"]
    assert eng.current_step == "reviewing"
    assert eng.activity_detail == "src/app.py"
    assert eng.activity_fraction == 1.0  # clamped
    assert entries[TECH_LEAD_AGENT_ID].current_step is None


def test_code_review_overlay_no_target_when_task_id_unmatched():
    # A code_review whose task_id matches no engineer's current task leaves every card un-overlaid.
    snap = [{"id": "t1", "title": "UI", "status": "in_progress"}]
    activity = {"agent": "code_review", "step": "reviewing", "task_id": "other"}
    entries = build_agent_statuses(
        [{"name": "frontend"}], {"frontend": "t1"}, snap, activity, "coding"
    )
    assert all(e.current_step is None for e in entries)


def test_no_overlay_when_activity_absent_or_malformed():
    snap = [{"id": "t1", "title": "UI", "status": "in_progress"}]
    for bad in (None, "garbage", 42, ["not", "a", "dict"]):
        entries = build_agent_statuses(
            [{"name": "frontend"}],
            {"frontend": "t1"},
            snap,
            bad,
            "coding",  # type: ignore[arg-type]
        )
        assert all(e.current_step is None for e in entries)
        # A non-dict activity must not flip the Tech Lead into "reviewing" via a bogus agent.
        tl = next(e for e in entries if e.role == "tech_lead")
        assert tl.status == "idle"


def test_snapshot_with_non_dict_task_entries_is_tolerated():
    snap: List[Any] = ["garbage", {"id": "t1", "title": "UI", "status": "in_progress"}]
    entries = _by_id(
        build_agent_statuses([{"name": "frontend"}], {"frontend": "t1"}, snap, None, "coding")
    )
    assert entries["frontend"].status == "working"
