"""Tests for the coding-team human-in-the-loop decision gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from software_engineering_team import coding_team_orchestrator as orch_mod
from software_engineering_team.coding_team_orchestrator import (
    CodingTeamSwarm,
    _hydrate_resolved_from_record,
    _plan_with_hitl,
    _run_pause_cycle,
    run_coding_team_orchestrator,
)
from software_engineering_team.models import CodingTeamPlanInput, StackSpec, TaskStatus
from software_engineering_team.pause_cycle import (
    _ActivityPauseSignal,
    _format_decisions,
    _pause_context_for_source,
    mint_resume_token,
)
from software_engineering_team.task_graph import TaskGraphService

GIT_UTILS = "shared.git.git_utils"


# --------------------------------------------------------------------------- helpers / stubs


class _DefaultGroomTaskMixin:
    """Ungroomed-default ``run_groom_task`` for Tech-Lead stubs that don't care about grooming.

    Mirrors the real ``run_groom_task``'s own default/fallback shape, so mixing this in changes no
    existing assertion for a stub that doesn't otherwise override it.
    """

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


class StubTechLead:
    def __init__(self, approved: bool = True) -> None:
        self.approved = approved

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
        return {"approved": self.approved, "reason": "ok", "requested_changes": []}

    def run_assignments(self, agent_ids, ready_tasks, free_agents):
        return {
            "assignments": [
                {"agent_id": a, "task_id": t["id"]} for t, a in zip(ready_tasks, free_agents)
            ]
        }


def _patch_git(monkeypatch, diff: str = "", merge=(True, "ok")):
    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: diff)
    monkeypatch.setattr(f"{GIT_UTILS}.merge_branch", lambda *a, **k: merge)


def _answer_all(job: Dict[str, Any], option: str = "yes"):
    """Fake hitl.wait_for_answers: answer every surfaced question and clear the wait flag."""

    def fake_wait(job_id, get_job_fn, **kw):
        pend = job.get("pending_questions") or []
        job["submitted_answers"] = list(job.get("submitted_answers") or []) + [
            {"question_id": q["id"], "selected_option_id": option} for q in pend
        ]
        job["waiting_for_answers"] = False
        return True

    return fake_wait


# --------------------------------------------------------------------------- pure helpers


def test_format_decisions():
    out = _format_decisions(
        [
            {"question_text": "Strictness?", "answer": "strict"},
            {"question_text": "Which DB?", "answer": "Postgres"},
        ]
    )
    assert out.startswith("The user answered")  # preamble present for non-empty input
    assert "- Strictness? → strict" in out  # one bullet per decision
    assert "- Which DB? → Postgres" in out
    # empty input → empty string (safe for any caller; no preamble with nothing under it)
    assert _format_decisions([]) == ""


def test_render_decision_line_branches():
    from software_engineering_team.hitl import render_decision_line

    # question + answer → "q → a"
    assert render_decision_line({"question_text": "Which DB?", "answer": "Postgres"}) == (
        "Which DB? → Postgres"
    )
    # answer-only (no question text) → the bare answer, no "→" separator
    assert render_decision_line({"answer": "Use TLS"}) == "Use TLS"
    # question-only → "q → " (answer empty)
    assert render_decision_line({"question_text": "Which DB?"}) == "Which DB? → "
    # neither → ""
    assert render_decision_line({}) == ""


def test_hydrate_resolved_from_record():
    plan = CodingTeamPlanInput(repo_path="/tmp", open_questions=["a"])
    _hydrate_resolved_from_record(
        plan, {"submitted_answers": [{"question_id": "q1", "other_text": "custom"}]}
    )
    assert plan.resolved_questions[0]["question_id"] == "q1"
    assert plan.resolved_questions[0]["answer"] == "custom"
    # no submitted answers → no-op; existing ids not duplicated
    plan2 = CodingTeamPlanInput(repo_path="/tmp")
    _hydrate_resolved_from_record(plan2, {})
    assert not plan2.resolved_questions


def test_hydrate_skips_already_present_ids():
    plan = CodingTeamPlanInput(
        repo_path="/tmp", resolved_questions=[{"question_id": "q1", "answer": "kept"}]
    )
    _hydrate_resolved_from_record(
        plan, {"submitted_answers": [{"question_id": "q1", "other_text": "ignored"}]}
    )
    assert len(plan.resolved_questions) == 1
    assert plan.resolved_questions[0]["answer"] == "kept"


# --------------------------------------------------------------------------- _run_pause_cycle


def test_pause_cycle_nothing_to_ask():
    assert _run_pause_cycle("j", [], "s", get_job_fn=lambda j: {}, update_fn=lambda **k: None) == (
        [],
        True,
    )


def test_pause_cycle_rejects_invalid_pause_strategy():
    """_run_pause_cycle validates pause_strategy itself, not just its callers.

    run_coding_team_orchestrator and CodingTeamSwarm.run both validate before calling in,
    but a direct caller (a test, or future code) that bypasses both must still fail fast
    rather than silently falling through to block-mode's hitl.wait_for_answers call.
    """
    with pytest.raises(ValueError, match="pause_strategy must be 'block' or 'return'"):
        _run_pause_cycle(
            "j",
            ["Q?"],
            "s",
            get_job_fn=lambda j: {},
            update_fn=lambda **k: None,
            pause_strategy="invalid",
        )


# --------------------------------------------------------------------------- mint_resume_token / _pause_context_for_source


def test_mint_resume_token_rejects_empty_job_id():
    with pytest.raises(ValueError, match="job_id must be non-empty"):
        mint_resume_token("")


def test_pause_context_for_source_rejects_mismatched_pause_kind():
    with pytest.raises(ValueError, match="does not match"):
        _pause_context_for_source("plan_input", "worker_escalation")


def test_pause_context_for_source_rejects_engineer_source_with_no_task_id():
    with pytest.raises(ValueError, match="must carry a task id"):
        _pause_context_for_source("engineer:", "worker_escalation")


def test_pause_context_for_source_extracts_task_id():
    assert _pause_context_for_source("engineer:t1", "worker_escalation") == {"task_ids": ["t1"]}
    assert _pause_context_for_source("plan_input", "entry") is None


def test_pause_cycle_success_calls_on_pause(monkeypatch):
    job: Dict[str, Any] = {}
    updates: List[Dict[str, Any]] = []
    posted: List[Any] = []
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", _answer_all(job))
    resolved, ok = _run_pause_cycle(
        "j",
        ["Q?"],
        "src",
        get_job_fn=lambda j: job,
        update_fn=lambda **kw: (job.update(kw), updates.append(dict(kw))),
        on_pause=lambda qs: posted.append(qs),
    )
    assert ok is True
    assert resolved[0]["selected_option_id"] == "yes"
    assert posted  # on_pause invoked with structured questions
    assert any(u.get("status") == "waiting_for_user" for u in updates)
    assert job["waiting_for_answers"] is False


def test_pause_publish_writes_heartbeat_atomically_with_flag(monkeypatch):
    """The pause flag and the first heartbeat must be set in the SAME update, so a concurrent
    answer on another worker can never see waiting_for_answers without a live heartbeat."""
    updates: List[Dict[str, Any]] = []
    job: Dict[str, Any] = {}
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", _answer_all(job))
    _run_pause_cycle(
        "j",
        ["Q?"],
        "src",
        get_job_fn=lambda j: job,
        update_fn=lambda **kw: (job.update(kw), updates.append(dict(kw))),
        on_pause=lambda qs: None,
    )
    # The update that publishes the pause flag also carries the heartbeat timestamp.
    pause_update = next(u for u in updates if u.get("waiting_for_answers") is True)
    assert pause_update.get("answer_wait_heartbeat_at")
    assert "T" in pause_update["answer_wait_heartbeat_at"]  # ISO-8601
    assert "resume_claim_at" not in pause_update


def test_pause_renews_lease_during_slow_on_pause(monkeypatch):
    """A slow on_pause (e.g. a GitHub comment with retries exceeding the 30s staleness window) must
    not let the lease go stale: a background renewer heartbeats while the callback runs."""
    import threading as _threading

    # Shrink the renewal cadence so the test is fast and deterministic.
    monkeypatch.setattr(orch_mod.hitl, "ANSWER_WAIT_POLL_INTERVAL_S", 0.01)
    heartbeats: List[str] = []
    job: Dict[str, Any] = {}
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", _answer_all(job))

    first_beat = _threading.Event()

    def update_fn(**kw):
        job.update(kw)
        if "answer_wait_heartbeat_at" in kw and kw.get("waiting_for_answers") is None:
            heartbeats.append(kw["answer_wait_heartbeat_at"])
            first_beat.set()

    started = _threading.Event()

    def slow_on_pause(_qs):
        started.set()
        # Block on the actual renewal signal instead of a wall-clock sleep: the callback returns the
        # moment the background renewer heartbeats once (which is exactly what this test asserts), so
        # it is deterministic rather than racing real time. The generous timeout is only a safety cap
        # that trips on a genuinely broken renewer, not on a slow/loaded CI host.
        assert first_beat.wait(5.0), "renewer did not heartbeat during the slow on_pause callback"

    _run_pause_cycle(
        "j",
        ["Q?"],
        "src",
        get_job_fn=lambda j: job,
        update_fn=update_fn,
        on_pause=slow_on_pause,
    )
    assert started.is_set()
    # The lease was renewed at least once *during* the slow callback (separate from the atomic
    # initial heartbeat that rides the pause-publish update).
    assert len(heartbeats) >= 1


def test_pause_heartbeats_preserve_last_activity_at(monkeypatch):
    """Answer-wait heartbeats are liveness pings, not real activity: they must pin last_activity_at
    to its pre-pause value so a job waiting hours on the user does not look continuously active
    (the API contract excludes heartbeats from last_activity_at)."""
    import threading as _threading

    monkeypatch.setattr(orch_mod.hitl, "ANSWER_WAIT_POLL_INTERVAL_S", 0.01)
    pinned = "2020-01-01T00:00:00+00:00"
    job: Dict[str, Any] = {"last_activity_at": pinned}
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", _answer_all(job))

    hb_calls: List[Dict[str, Any]] = []
    first_beat = _threading.Event()

    def update_fn(**kw):
        job.update(kw)
        # Capture the renewal/wait-loop heartbeats (which carry answer_wait_heartbeat_at but not the
        # waiting_for_answers flag that rides the one-shot pause-publish update).
        if "answer_wait_heartbeat_at" in kw and kw.get("waiting_for_answers") is None:
            hb_calls.append(kw)
            first_beat.set()

    def slow_on_pause(_qs):
        assert first_beat.wait(5.0), "renewer did not heartbeat during on_pause"

    _run_pause_cycle(
        "j", ["Q?"], "src", get_job_fn=lambda j: job, update_fn=update_fn, on_pause=slow_on_pause
    )

    assert hb_calls, "expected at least one answer-wait heartbeat"
    # Every heartbeat pins last_activity_at to the pre-pause value rather than advancing it.
    assert all(c.get("last_activity_at") == pinned for c in hb_calls)


def test_pause_cycle_on_pause_error_is_swallowed(monkeypatch):
    job: Dict[str, Any] = {}
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", _answer_all(job))

    def boom(_qs):
        raise RuntimeError("comment failed")

    _, ok = _run_pause_cycle(
        "j",
        ["Q?"],
        "src",
        get_job_fn=lambda j: job,
        update_fn=lambda **kw: job.update(kw),
        on_pause=boom,
    )
    assert ok is True


def test_pause_cycle_timeout_sets_failed(monkeypatch):
    job: Dict[str, Any] = {}
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", lambda *a, **k: False)
    resolved, ok = _run_pause_cycle(
        "j", ["Q?"], "src", get_job_fn=lambda j: job, update_fn=lambda **kw: job.update(kw)
    )
    assert ok is False
    assert job["status"] == "failed"


def test_pause_cycle_terminal_leaves_status(monkeypatch):
    job: Dict[str, Any] = {}

    def fake_wait(job_id, gj, **kw):
        job["status"] = "cancelled"  # job goes terminal while waiting
        return False

    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", fake_wait)
    _, ok = _run_pause_cycle(
        "j", ["Q?"], "s", get_job_fn=lambda j: job, update_fn=lambda **kw: job.update(kw)
    )
    assert ok is False
    assert job["status"] == "cancelled"  # not overwritten to failed


# --------------------------------------------------------------------------- _run_pause_cycle resilience (orchestrator threads 10/11/12/14)


def test_pause_cycle_pinned_activity_falls_back_on_store_error(monkeypatch):
    """Thread 10+12: if get_job_fn raises during the post-pause pin read, _run_pause_cycle must
    continue with pinned_activity_at=None rather than crashing. Thread 12: with None pin, heartbeats
    must NOT write last_activity_at=None (which would clobber the real field with null)."""
    import threading as _threading

    monkeypatch.setattr(orch_mod.hitl, "ANSWER_WAIT_POLL_INTERVAL_S", 0.01)
    job: Dict[str, Any] = {}
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", _answer_all(job))

    call_count = {"n": 0}

    def get_job_fn(jid):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("store error during pin read")
        return dict(job)

    hb_calls: List[Dict[str, Any]] = []
    first_beat = _threading.Event()

    def update_fn(**kw):
        job.update(kw)
        if "answer_wait_heartbeat_at" in kw and kw.get("waiting_for_answers") is None:
            hb_calls.append(dict(kw))
            first_beat.set()

    def slow_on_pause(_qs):
        assert first_beat.wait(5.0), "renewer did not heartbeat during on_pause"

    _, ok = _run_pause_cycle(
        "j", ["Q?"], "src", get_job_fn=get_job_fn, update_fn=update_fn, on_pause=slow_on_pause
    )
    assert ok is True
    assert hb_calls, "expected heartbeats even when the pin read failed"
    # Thread 12: when pinned_activity_at is unknown (None), heartbeats must not forward
    # last_activity_at=None — that would overwrite real activity data with a null sentinel.
    assert not any("last_activity_at" in c for c in hb_calls), (
        "heartbeats must not write last_activity_at=None when the pin read failed"
    )


def test_pause_cycle_terminal_check_store_error_marks_failed(monkeypatch):
    """Thread 11 (timeout path): if get_job_fn raises during the terminal check after wait_for_answers
    times out, the cycle must default to 'mark failed' rather than crashing — it cannot distinguish a
    cancelled job from a timeout when the store is down, so failing closed is the safe default."""
    job: Dict[str, Any] = {}
    call_count = {"n": 0}

    def get_job_fn(jid):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("store error during pin read")  # Thread 10: falls back to None
        raise RuntimeError("store error during terminal check")  # Thread 11: defaults to failed

    updates: List[Dict[str, Any]] = []
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", lambda *a, **k: False)

    _, ok = _run_pause_cycle(
        "j",
        ["Q?"],
        "src",
        get_job_fn=get_job_fn,
        update_fn=lambda **kw: (job.update(kw), updates.append(dict(kw))),
    )
    assert ok is False
    assert any(u.get("status") == "failed" for u in updates)


def test_pause_cycle_submitted_answers_store_error_continues_with_empty(monkeypatch):
    """Thread 11 (answers path): if get_job_fn raises when reading submitted_answers after answers
    are received, the cycle must proceed with an empty list and return ok=True rather than crashing
    with waiting_for_answers still True (which would strand the run in the paused state)."""
    job: Dict[str, Any] = {}
    answered = {"did": False}

    def fake_wait(job_id, gj, **kw):
        job["waiting_for_answers"] = False
        answered["did"] = True
        return True

    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", fake_wait)

    call_count = {"n": 0}

    def get_job_fn(jid):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return dict(job)  # pin read: succeeds, no last_activity_at → pinned=None
        raise RuntimeError("store error reading submitted_answers")

    resolved, ok = _run_pause_cycle(
        "j",
        ["Q?"],
        "src",
        get_job_fn=get_job_fn,
        update_fn=lambda **kw: job.update(kw),
    )
    assert ok is True
    assert answered["did"]
    assert resolved == [], "must continue with empty answers rather than raising"


def test_pause_renewer_thread_start_failure_is_swallowed(monkeypatch):
    """Thread 14: if threading.Thread.start raises RuntimeError (resource exhaustion), the pause
    cycle must still invoke on_pause and wait for answers — the run continues without background
    heartbeat renewal rather than aborting due to the OS failure."""
    job: Dict[str, Any] = {}
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", _answer_all(job))
    pause_called: List[bool] = []

    class _FailStartThread:
        """Thread stand-in whose start() raises RuntimeError to simulate resource exhaustion."""

        def __init__(self, target=None, daemon=None, name=None):
            self._target = target

        def start(self):
            raise RuntimeError("max threads exceeded")

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(orch_mod.threading, "Thread", _FailStartThread)

    def on_pause(_qs):
        pause_called.append(True)

    _, ok = _run_pause_cycle(
        "j",
        ["Q?"],
        "src",
        get_job_fn=lambda j: job,
        update_fn=lambda **kw: job.update(kw),
        on_pause=on_pause,
    )
    assert ok is True
    assert pause_called, "on_pause must be called even when the renewer thread fails to start"


# --------------------------------------------------------------------------- _plan_with_hitl


def test_plan_with_hitl_returns_immediately_when_no_questions():
    class TL:
        def run_plan_to_task_graph(self, plan):
            return {"tasks": [], "stacks": [], "open_questions": []}

    out = _plan_with_hitl(TL(), CodingTeamPlanInput(repo_path="/tmp"), lambda q, s: ([], True))
    assert out == {"tasks": [], "stacks": [], "open_questions": []}


def test_plan_with_hitl_pauses_then_replans():
    plan = CodingTeamPlanInput(repo_path="/tmp")

    class TL:
        def __init__(self):
            self.n = 0

        def run_plan_to_task_graph(self, p):
            self.n += 1
            if self.n == 1:
                return {
                    "tasks": [],
                    "stacks": [],
                    "open_questions": [{"question_text": "Default?"}],
                }
            return {"tasks": [{"id": "t1"}], "stacks": [], "open_questions": []}

    pauses = []

    def pause(q, s):
        pauses.append((q, s))
        return ([{"question_text": "Default?", "answer": "A"}], True)

    out = _plan_with_hitl(TL(), plan, pause)
    assert out["tasks"] == [{"id": "t1"}]
    assert pauses and pauses[0][1] == "tech_lead"
    assert plan.resolved_questions[0]["answer"] == "A"


def test_plan_with_hitl_aborts_on_unanswered_pause():
    class TL:
        def run_plan_to_task_graph(self, p):
            return {"tasks": [], "stacks": [], "open_questions": [{"question_text": "Q?"}]}

    assert (
        _plan_with_hitl(TL(), CodingTeamPlanInput(repo_path="/tmp"), lambda q, s: ([], False))
        is None
    )


def test_plan_with_hitl_exhausts_rounds_fails_closed():
    class TL:
        def run_plan_to_task_graph(self, p):
            return {"tasks": [], "stacks": [], "open_questions": [{"question_text": "Q?"}]}

    out = _plan_with_hitl(
        TL(),
        CodingTeamPlanInput(repo_path="/tmp"),
        lambda q, s: ([{"question_text": "Q?", "answer": "A"}], True),
        max_rounds=2,
    )
    # A Tech Lead that never stops asking fails closed (None) rather than building tasks around an
    # undecided question.
    assert out is None


# --------------------------------------------------------------------------- Tech Lead agent channel


def test_plan_text_renders_resolved_and_assumptions():
    from software_engineering_team.tech_lead_agent.agent import _plan_text

    plan = CodingTeamPlanInput(
        repo_path="/tmp",
        requirements_title="T",
        architecture_overview="FastAPI + Angular",
        resolved_questions=[{"question_text": "Strictness?", "answer": "strict"}],
        assumptions=["Web-first"],
    )
    txt = _plan_text(plan)
    assert "User decisions" in txt
    assert "Strictness? → strict" in txt
    assert "Web-first" in txt
    assert "FastAPI + Angular" in txt


def test_render_resolved_questions_skips_non_dict_entries():
    from software_engineering_team.tech_lead_agent.agent import (
        _render_resolved_questions,
    )

    out = _render_resolved_questions(["not a dict", {"question_text": "Q?", "answer": "A"}])
    assert "Q? → A" in out
    assert "not a dict" not in out


def test_plan_to_task_graph_parses_open_questions(monkeypatch):
    from software_engineering_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda a, p, required_keys=None: {
            "tasks": [],
            "stacks": [{"name": "backend", "tools_services": []}],
            "open_questions": [{"question_text": "Allergen default?"}],
        },
    )
    out = tl_mod.TechLeadAgent(model=object()).run_plan_to_task_graph(
        CodingTeamPlanInput(repo_path="/tmp")
    )
    assert out["open_questions"][0]["question_text"] == "Allergen default?"


def test_plan_to_task_graph_failure_includes_open_questions_key(monkeypatch):
    from software_engineering_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())

    def boom(a, p, required_keys=None):
        raise RuntimeError("x")

    monkeypatch.setattr(tl_mod, "_agent_call_json", boom)
    out = tl_mod.TechLeadAgent(model=object()).run_plan_to_task_graph(
        CodingTeamPlanInput(repo_path="/tmp")
    )
    assert out["open_questions"] == []


# --------------------------------------------------------------------------- orchestrator end-to-end


def _stub_agents(monkeypatch, tech_lead_cls, swarm_cls):
    class Worker:
        def __init__(self, agent_id: str):
            self.agent_id = agent_id

    monkeypatch.setattr(orch_mod, "TechLeadAgent", tech_lead_cls)
    monkeypatch.setattr(
        orch_mod,
        "_build_implementation_worker",
        lambda agent_id, spec, llm_getter, engine_provider, **kwargs: Worker(agent_id),
    )
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", swarm_cls)


def test_entry_gate_pauses_then_resumes_and_threads_answers(tmp_path, monkeypatch):
    job: Dict[str, Any] = {"submitted_answers": []}
    updates: List[Dict[str, Any]] = []
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", _answer_all(job))

    seen: Dict[str, Any] = {}

    class TL(_DefaultGroomTaskMixin):
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan):
            seen["resolved"] = list(plan.resolved_questions or [])
            seen["open"] = list(plan.open_questions or [])
            return {
                "tasks": [{"id": "t1", "title": "T1"}],
                "stacks": [{"name": "backend", "tools_services": []}],
                "open_questions": [],
            }

    class Swarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            self.aborted = False

        def run(self, **kw):
            self.graph.mark_branch_merged("t1")

    _stub_agents(monkeypatch, TL, Swarm)
    plan = CodingTeamPlanInput(
        repo_path=str(tmp_path), open_questions=["Allergen strictness default?"]
    )
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=lambda **kw: (job.update(kw), updates.append(dict(kw))),
        get_job_fn=lambda jid: job,
        cache_dir=tmp_path,
        get_llm=lambda k: None,
    )
    assert any(u.get("status") == "waiting_for_user" for u in updates)  # paused
    assert seen["resolved"]  # answers threaded into planning
    assert seen["open"] == []  # open questions cleared before planning
    assert updates[-1]["status"] == "completed"


def test_orchestrator_fails_when_tech_lead_never_stops_asking(tmp_path, monkeypatch):
    job: Dict[str, Any] = {"submitted_answers": []}
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", _answer_all(job))
    monkeypatch.setattr(orch_mod, "MAX_TECH_LEAD_QUESTION_ROUNDS", 2)

    class TL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan):
            # Always raises a question, even after each is answered → never converges.
            return {"tasks": [], "stacks": [], "open_questions": [{"question_text": "Q?"}]}

    class Swarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            self.aborted = False

        def run(self, **kw):
            raise AssertionError("swarm must not run when planning never converges")

    _stub_agents(monkeypatch, TL, Swarm)
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=lambda **kw: job.update(kw),
        get_job_fn=lambda jid: job,
        cache_dir=tmp_path,
        get_llm=lambda k: None,
    )
    assert job.get("status") == "failed"  # fail closed, not a clean completion


def test_entry_gate_no_answer_makes_zero_llm_calls(tmp_path, monkeypatch):
    job: Dict[str, Any] = {}
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", lambda *a, **k: False)
    calls = {"plan": 0}

    class TL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan):
            calls["plan"] += 1
            return {"tasks": [], "stacks": [], "open_questions": []}

    class Swarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            self.aborted = False

        def run(self, **kw):
            raise AssertionError("swarm must not run when the gate never resolves")

    _stub_agents(monkeypatch, TL, Swarm)
    plan = CodingTeamPlanInput(repo_path=str(tmp_path), open_questions=["X?"])
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=lambda **kw: job.update(kw),
        get_job_fn=lambda jid: job,
        cache_dir=tmp_path,
        get_llm=lambda k: None,
    )
    assert calls["plan"] == 0  # no task-graph LLM call — the gate stopped before any work
    assert job.get("status") == "failed"


def test_already_resolved_entry_questions_do_not_pause(tmp_path, monkeypatch):
    """plan_input that arrives with resolved_questions covering its open_questions must not pause."""
    job: Dict[str, Any] = {}

    def no_wait(*a, **k):
        raise AssertionError("must not wait when questions are already answered")

    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", no_wait)

    class TL(_DefaultGroomTaskMixin):
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan):
            return {
                "tasks": [{"id": "t1", "title": "T1"}],
                "stacks": [{"name": "b", "tools_services": []}],
                "open_questions": [],
            }

    class Swarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            self.aborted = False

        def run(self, **kw):
            self.graph.mark_branch_merged("t1")

    _stub_agents(monkeypatch, TL, Swarm)
    plan = CodingTeamPlanInput(
        repo_path=str(tmp_path),
        open_questions=["Strictness?"],
        resolved_questions=[{"question_text": "Strictness?", "answer": "strict"}],
    )
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=lambda **kw: job.update(kw),
        get_job_fn=lambda jid: job,
        cache_dir=tmp_path,
        get_llm=lambda k: None,
    )
    assert job["status"] == "completed"


# --------------------------------------------------------------------------- swarm escalation (real swarm)


class _FakeWorktreeManager:
    """Test double for coding_team.worktree_manager.WorktreeManager.

    These orchestrator tests stub the implementation workers entirely (they
    never touch git), so a real WorktreeManager would pay for (and, without
    _patch_git, actually attempt) real `git worktree add` calls against a
    plain tmp_path with no `.git`. This double gives each agent_id a distinct
    child directory under the swarm's own tmp_path instead — no git, no
    filesystem writes outside tmp_path's own pytest-managed cleanup. Real
    worktree mechanics are covered by test_worktree_manager.py.
    """

    def __init__(self, repo_path: Path, agent_ids):
        self._paths = {aid: Path(repo_path) / f"_wt_{aid}" for aid in agent_ids}

    def prepare(self) -> None:
        for path in self._paths.values():
            path.mkdir(parents=True, exist_ok=True)

    def path_for(self, agent_id: str) -> Path:
        return self._paths[agent_id]

    def cleanup(self) -> None:
        pass


def _make_swarm(tmp_path, tech_lead, workers):
    graph = TaskGraphService(job_id="j1")
    swarm = CodingTeamSwarm(
        tech_lead=tech_lead,
        workers=workers,
        graph=graph,
        path=Path(tmp_path),
        agent_ids=[w.agent_id for w in workers],
        llm_getter=lambda k: None,
    )
    swarm._worktrees = _FakeWorktreeManager(swarm.path, [w.agent_id for w in workers])
    swarm._worktrees.prepare()
    swarm._run_quality_gates = lambda *a, **k: True  # type: ignore[method-assign]
    return swarm, graph


class _DecisionWorker:
    def __init__(self, agent_id="a1", ask_rounds=1):
        self.agent_id = agent_id
        self.stack_spec = StackSpec(name=agent_id)
        self.calls = 0
        self._ask_rounds = ask_rounds

    def run_implement(self, task, path):
        self.calls += 1
        if self.calls <= self._ask_rounds:
            return {
                "status": "needs_decision",
                "open_questions": [{"question_text": "Which?"}],
                "feature_branch": f"feature/{task.id}",
                "changes_summary": "",
                "error": None,
            }
        return {
            "status": "in_review",
            "feature_branch": f"feature/{task.id}",
            "changes_summary": "done",
            "files_to_create_or_edit": [],
            "error": None,
        }


def test_swarm_escalates_then_implements_decision(tmp_path, monkeypatch):
    _patch_git(monkeypatch)
    worker = _DecisionWorker()
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")
    paused = []

    def pause(questions, source):
        paused.append((questions, source))
        return ([{"question_text": "Which?", "answer": "strict"}], True)

    swarm.run(max_rounds=20, pause_for_questions=pause)

    assert paused and paused[0][1].startswith("engineer:")
    assert worker.calls >= 2  # re-implemented after the decision
    assert graph.get_task("t1").status == TaskStatus.MERGED
    fb = graph.get_task("t1").revision_feedback
    assert any(e.get("source") == "user_decision" for e in fb)
    assert "strict" in fb[-1]["reason"]


def test_swarm_aborts_when_escalation_unanswered(tmp_path, monkeypatch):
    _patch_git(monkeypatch)
    worker = _DecisionWorker(ask_rounds=99)
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")

    swarm.run(max_rounds=20, pause_for_questions=lambda q, s: ([], False))

    assert swarm.aborted is True
    assert graph.get_task("t1").status != TaskStatus.MERGED


def test_swarm_needs_decision_without_channel_fails_task(tmp_path, monkeypatch):
    _patch_git(monkeypatch)
    worker = _DecisionWorker(ask_rounds=99)
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")

    swarm.run(max_rounds=10)  # no pause_for_questions wired

    assert graph.get_task("t1").status == TaskStatus.FAILED


def test_tech_lead_question_pause_without_answers_aborts(tmp_path, monkeypatch):
    job: Dict[str, Any] = {}
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", lambda *a, **k: False)

    class TL(_DefaultGroomTaskMixin):
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan):
            return {"tasks": [], "stacks": [], "open_questions": [{"question_text": "Q?"}]}

    class Swarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            self.aborted = False

        def run(self, **kw):
            raise AssertionError("swarm must not run when planning never resolves its questions")

    _stub_agents(monkeypatch, TL, Swarm)
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=lambda **kw: job.update(kw),
        get_job_fn=lambda jid: job,
        cache_dir=tmp_path,
        get_llm=lambda k: None,
    )
    assert job.get("status") == "failed"


def test_orchestrator_returns_when_swarm_aborts(tmp_path, monkeypatch):
    job: Dict[str, Any] = {}

    class TL:
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan):
            return {
                "tasks": [{"id": "t1", "title": "T1"}],
                "stacks": [{"name": "b", "tools_services": []}],
                "open_questions": [],
            }

    class Swarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            self.aborted = False

        def run(self, **kw):
            self.aborted = True  # a worker escalation ended without answers mid-swarm

    _stub_agents(monkeypatch, TL, Swarm)
    run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=lambda **kw: job.update(kw),
        get_job_fn=lambda jid: job,
        cache_dir=tmp_path,
        get_llm=lambda k: None,
    )
    # An aborted swarm must NOT be reported as completed.
    assert job.get("status") != "completed"
    assert job.get("phase") != "completed"


def test_escalate_decision_applies_answer_even_at_revision_cap(tmp_path, monkeypatch):
    """A late-stage escalation must still implement the user's answer — an escalation is not a
    failed revision and must not discard the decision when the task is already at the revision cap."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 5)
    worker = _DecisionWorker()
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.update_task("t1", revision_count=4)  # near the revision cap (from quality-gate revisions)
    swarm.pause_for_questions = lambda q, s: ([{"question_text": "Q?", "answer": "use TLS"}], True)

    swarm._escalate_decision(
        graph.get_task("t1"), {"open_questions": [{"question_text": "Q?"}]}, lambda **k: None
    )

    task = graph.get_task("t1")
    assert task.status == TaskStatus.IN_PROGRESS  # answer will be implemented, not discarded
    assert task.assigned_agent_id == "a1"  # same engineer
    assert task.revision_count == 4  # escalation did not consume the revision budget
    assert task.revision_feedback[-1]["source"] == "user_decision"
    assert "use TLS" in task.revision_feedback[-1]["reason"]
    # The structured records are preserved alongside the rendered reason so the review gates
    # can tell the reviewer the question is already settled (see _user_decisions_for).
    assert task.revision_feedback[-1]["decisions"] == [{"question_text": "Q?", "answer": "use TLS"}]


def test_escalate_decision_bounded_by_escalation_cap(tmp_path, monkeypatch):
    """A model that keeps re-raising decisions after they are answered is bounded: after
    MAX_TASK_REVISIONS escalations the task fails rather than pausing a human indefinitely."""
    monkeypatch.setattr(orch_mod, "MAX_TASK_REVISIONS", 3)
    _patch_git(monkeypatch)
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [_DecisionWorker()])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    # Pre-load prior escalations so the next one trips the cap.
    graph.update_task(
        "t1", revision_feedback=[{"source": "user_decision", "reason": "x"} for _ in range(2)]
    )
    swarm.pause_for_questions = lambda q, s: ([{"question_text": "Q?", "answer": "a"}], True)

    swarm._escalate_decision(
        graph.get_task("t1"), {"open_questions": [{"question_text": "Q?"}]}, lambda **k: None
    )

    assert graph.get_task("t1").status == TaskStatus.FAILED  # 2 prior + 1 == cap(3)


# --------------------------------------------------------------------------- pause_strategy="return" (#3987)


def test_entry_gate_returns_paused_without_blocking(tmp_path, monkeypatch):
    """Entry-gate HITL pause under pause_strategy="return" must not block.

    When the input plan already carries an unanswered open question, the orchestrator
    must pause before invoking the Tech Lead or swarm, must never call
    hitl.wait_for_answers, and must persist a resume_token with pause_kind="entry".
    """
    job: Dict[str, Any] = {}

    def no_wait(*a, **k):  # pragma: no cover
        raise AssertionError('must not block in pause_strategy="return" mode')

    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", no_wait)

    class TL(_DefaultGroomTaskMixin):
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan):  # pragma: no cover
            raise AssertionError("must not plan before the entry gate resolves")

    class Swarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            self.aborted = False

        def run(self, **kw):  # pragma: no cover
            raise AssertionError("swarm must not run before the entry gate resolves")

    _stub_agents(monkeypatch, TL, Swarm)
    plan = CodingTeamPlanInput(
        repo_path=str(tmp_path), open_questions=["Allergen strictness default?"]
    )
    result = run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=lambda **kw: job.update(kw),
        get_job_fn=lambda jid: job,
        cache_dir=tmp_path,
        get_llm=lambda k: None,
        pause_strategy="return",
    )

    assert result["outcome"] == "paused"
    assert result["job_id"] == "j1"
    assert result["pause_kind"] == "entry"
    assert result["pause_context"] is None
    assert len(result["pending_questions"]) == 1
    assert result["resume_token"].startswith("j1:")
    # The pause envelope is durably persisted before returning -- a notification, not the
    # source of truth (contract doc §1's precondition).
    assert job.get("resume_token") == result["resume_token"]
    assert job.get("waiting_for_answers") is True
    assert job.get("pause_kind") == "entry"
    assert job.get("pause_context") is None


def test_tech_lead_clarify_returns_paused_without_blocking(tmp_path, monkeypatch):
    """Tech-Lead-clarify HITL pause under pause_strategy="return" must not block.

    When the Tech Lead's planning call raises an open question, the orchestrator must
    pause with pause_kind="tech_lead_clarify" without ever calling hitl.wait_for_answers,
    and planning must run exactly once (no retry-loop re-entry before the pause resolves).
    """
    job: Dict[str, Any] = {}
    plan_calls = {"count": 0}

    def no_wait(*a, **k):  # pragma: no cover
        raise AssertionError('must not block in pause_strategy="return" mode')

    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", no_wait)

    class TL(_DefaultGroomTaskMixin):
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan):
            plan_calls["count"] += 1
            return {"tasks": [], "stacks": [], "open_questions": [{"question_text": "Which DB?"}]}

    class Swarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            self.aborted = False

        def run(self, **kw):  # pragma: no cover
            raise AssertionError("swarm must not run before planning converges")

    _stub_agents(monkeypatch, TL, Swarm)
    result = run_coding_team_orchestrator(
        "j1",
        tmp_path,
        CodingTeamPlanInput(repo_path=str(tmp_path)),
        update_job_fn=lambda **kw: job.update(kw),
        get_job_fn=lambda jid: job,
        cache_dir=tmp_path,
        get_llm=lambda k: None,
        pause_strategy="return",
    )

    assert result["outcome"] == "paused"
    assert result["job_id"] == "j1"
    assert result["pause_kind"] == "tech_lead_clarify"
    assert result["pause_context"] is None
    assert len(result["pending_questions"]) == 1
    assert result["resume_token"].startswith("j1:")
    # The pause envelope is durably persisted before returning -- a notification, not the
    # source of truth (contract doc §1's precondition), mirroring the entry-gate test.
    assert job.get("resume_token") == result["resume_token"]
    assert job.get("pause_kind") == "tech_lead_clarify"
    assert job.get("waiting_for_answers") is True
    assert plan_calls["count"] == 1  # exactly one LLM call before the pause, no retry-loop re-entry


def test_worker_escalation_returns_paused_without_blocking(tmp_path, monkeypatch):
    """Worker-escalation HITL pause under pause_strategy="return" must not block.

    A worker raising a product decision mid-task must raise _ActivityPauseSignal (not
    block on hitl.wait_for_answers) and must atomically publish resume_token, pause_kind
    ("worker_escalation"), and pause_context (the escalating task's id) to the job record.
    The escalated task itself stays IN_PROGRESS, re-evaluated fresh on the next invocation.
    """
    _patch_git(monkeypatch)
    job: Dict[str, Any] = {}
    worker = _DecisionWorker()
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [worker])
    graph.add_task("t1", title="T1")
    graph.assign_task_to_agent("t1", "a1")
    graph.update_task("t1", status=TaskStatus.IN_PROGRESS)

    def bound_cycle(questions, source):
        return _run_pause_cycle(
            "j1",
            questions,
            source,
            get_job_fn=lambda jid: job,
            update_fn=lambda **kw: job.update(kw),
            pause_strategy="return",
        )

    with pytest.raises(_ActivityPauseSignal) as exc_info:
        swarm.run(max_rounds=20, pause_for_questions=bound_cycle, pause_strategy="return")

    sig = exc_info.value
    assert sig.pause_kind == "worker_escalation"
    assert sig.pause_context == {"task_ids": ["t1"]}
    assert job.get("resume_token")
    assert job.get("waiting_for_answers") is True
    # The job-record envelope itself (not just the signal payload) carries pause_kind/
    # pause_context -- the atomic publish in _run_pause_cycle writes both.
    assert job.get("pause_kind") == "worker_escalation"
    assert job.get("pause_context") == {"task_ids": ["t1"]}
    # The task stays IN_PROGRESS (unescalated answer not yet applied) -- it is re-evaluated
    # fresh on the next (Temporal-resumed) orchestrator invocation.
    assert graph.get_task("t1").status == TaskStatus.IN_PROGRESS


def test_reentry_matching_token_consumes_and_continues(tmp_path, monkeypatch):
    """Return-mode re-entry with a matching acknowledged_resume_token consumes the pause.

    When the persisted pause's resume_token matches the token the caller acknowledges, the
    orchestrator must atomically clear the whole pause envelope (resume_token, pause_kind,
    pause_context, waiting_for_answers), run planning exactly once, and reach a terminal
    state instead of emitting a new paused result.
    """
    job: Dict[str, Any] = {
        "waiting_for_answers": True,
        "pending_questions": [{"id": "q1", "question_text": "Allergen strictness default?"}],
        "resume_token": "j1:tok-1",
        "pause_kind": "entry",
        "pause_context": None,
        "submitted_answers": [
            {
                "question_id": "q1",
                "question_text": "Allergen strictness default?",
                "selected_option_id": "strict",
            }
        ],
    }
    plan_calls = {"count": 0}

    class TL(_DefaultGroomTaskMixin):
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan):
            plan_calls["count"] += 1
            return {
                "tasks": [{"id": "t1", "title": "T1"}],
                "stacks": [{"name": "backend", "tools_services": []}],
                "open_questions": [],
            }

    class Swarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            self.aborted = False

        def run(self, **kw):
            self.graph.mark_branch_merged("t1")

    _stub_agents(monkeypatch, TL, Swarm)
    plan = CodingTeamPlanInput(
        repo_path=str(tmp_path), open_questions=["Allergen strictness default?"]
    )
    result = run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=lambda **kw: job.update(kw),
        get_job_fn=lambda jid: job,
        cache_dir=tmp_path,
        get_llm=lambda k: None,
        pause_strategy="return",
        acknowledged_resume_token="j1:tok-1",
    )

    assert result is None  # reached a terminal state, not a new pause
    assert job.get("resume_token") is None  # envelope cleared atomically on consume
    assert job.get("waiting_for_answers") is False
    assert job.get("pause_kind") is None
    assert job.get("pause_context") is None
    assert plan_calls["count"] == 1  # planning proceeded exactly once
    assert job.get("status") == "completed"


def test_reentry_stale_token_reemits_without_rerunning_work(tmp_path, monkeypatch):
    """Return-mode re-entry with a missing/stale acknowledged_resume_token re-emits, unchanged.

    This is the pre-work Temporal activity retry case: the orchestrator must re-emit the
    exact persisted pause (unchanged) without re-running any planning or swarm work, since
    the token does not prove this invocation is the one resolving that pause.
    """
    job: Dict[str, Any] = {
        "waiting_for_answers": True,
        "pending_questions": [{"id": "q1", "question_text": "Allergen strictness default?"}],
        "resume_token": "j1:tok-1",
        "pause_kind": "entry",
        "pause_context": None,
        "submitted_answers": [],
    }

    class TL(_DefaultGroomTaskMixin):
        def __init__(self, llm):
            pass

        def run_plan_to_task_graph(self, plan):  # pragma: no cover
            raise AssertionError("a pre-work retry must not re-run any planning work")

    class Swarm:
        def __init__(self, *a, **k):
            self.graph = k["graph"]
            self.aborted = False

        def run(self, **kw):  # pragma: no cover
            raise AssertionError("a pre-work retry must not run the swarm")

    _stub_agents(monkeypatch, TL, Swarm)
    plan = CodingTeamPlanInput(
        repo_path=str(tmp_path), open_questions=["Allergen strictness default?"]
    )
    # acknowledged_resume_token omitted (None) -- missing/stale, not a genuine resume.
    result = run_coding_team_orchestrator(
        "j1",
        tmp_path,
        plan,
        update_job_fn=lambda **kw: job.update(kw),
        get_job_fn=lambda jid: job,
        cache_dir=tmp_path,
        get_llm=lambda k: None,
        pause_strategy="return",
    )

    assert result == {
        "outcome": "paused",
        "job_id": "j1",
        "resume_token": "j1:tok-1",
        "pause_kind": "entry",
        "pause_context": None,
        "pending_questions": job["pending_questions"],
    }


def test_run_rejects_invalid_pause_strategy(tmp_path):
    """CodingTeamSwarm.run() validates pause_strategy before any worktree I/O.

    An invalid pause_strategy value must raise ValueError immediately, matching the
    docstring's documented precondition, without preparing any worker's git worktree.
    """
    graph = TaskGraphService(job_id="j1")
    worker = _DecisionWorker()
    swarm = CodingTeamSwarm(
        tech_lead=StubTechLead(approved=True),
        workers=[worker],
        graph=graph,
        path=Path(tmp_path),
        agent_ids=[worker.agent_id],
        llm_getter=lambda k: None,
    )

    with pytest.raises(ValueError, match="pause_strategy must be 'block' or 'return'"):
        swarm.run(pause_strategy="invalid")

    assert swarm._worktrees._prepared is False


def test_escalate_decision_defers_when_pause_already_committed_this_round(tmp_path, monkeypatch):
    """Under pause_strategy="return", a second worker escalating in the same round after
    another has already published a pause must defer to next round instead of racing to
    publish its own competing pause (see CodingTeamSwarm.run's parallel_map/wait_for_stragglers
    comment and _escalate_decision's Concurrency note)."""
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [_DecisionWorker()])
    graph.add_task("t2", title="T2")
    graph.assign_task_to_agent("t2", "a1")
    swarm._pause_strategy = "return"
    swarm._escalation_pause_committed = True  # another worker already published this round's pause

    def _must_not_be_called(*a, **k):  # pragma: no cover
        raise AssertionError("must not call pause_for_questions when already committed")

    swarm.pause_for_questions = _must_not_be_called

    swarm._escalate_decision(
        graph.get_task("t2"), {"open_questions": [{"question_text": "Q?"}]}, lambda **k: None
    )

    task = graph.get_task("t2")
    assert task.status == TaskStatus.IN_PROGRESS  # deferred to next round, not lost or failed
    assert not task.revision_feedback  # no user_decision entry was appended


def test_escalate_decision_ignores_committed_flag_in_block_mode(tmp_path, monkeypatch):
    """The commit-flag guard is scoped to pause_strategy="return" only -- block mode must keep
    today's behavior of every escalating worker getting its own full pause-and-resolve cycle,
    even if the flag happens to be set."""
    swarm, graph = _make_swarm(tmp_path, StubTechLead(approved=True), [_DecisionWorker()])
    graph.add_task("t3", title="T3")
    graph.assign_task_to_agent("t3", "a1")
    assert swarm._pause_strategy == "block"  # default
    swarm._escalation_pause_committed = True  # must be ignored outside return mode
    swarm.pause_for_questions = lambda q, s: ([{"question_text": "Q?", "answer": "ok"}], True)

    swarm._escalate_decision(
        graph.get_task("t3"), {"open_questions": [{"question_text": "Q?"}]}, lambda **k: None
    )

    task = graph.get_task("t3")
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.revision_feedback[-1]["source"] == "user_decision"  # answer WAS applied
