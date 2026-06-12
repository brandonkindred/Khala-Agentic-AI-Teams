"""Tests for the coding-team human-in-the-loop decision gate: orchestrator entry gate, Tech-Lead
clarify loop, Senior-SWE escalation, and the agent-level open_questions channels."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from coding_team import orchestrator as orch_mod
from coding_team.models import CodingTeamPlanInput, StackSpec, Task, TaskStatus
from coding_team.orchestrator import (
    CodingTeamSwarm,
    _format_decisions,
    _hydrate_resolved_from_record,
    _plan_with_hitl,
    _run_pause_cycle,
    run_coding_team_orchestrator,
)
from coding_team.task_graph import TaskGraphService

GIT_UTILS = "software_engineering_team.shared.git_utils"


# --------------------------------------------------------------------------- helpers / stubs


class StubTechLead:
    def __init__(self, approved: bool = True) -> None:
        self.approved = approved

    def run_code_review(
        self,
        task_title,
        task_description,
        acceptance_criteria,
        changes_summary,
        progress_callback=None,
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
    out = _format_decisions([{"question_text": "Strictness?", "answer": "strict"}])
    assert "Strictness? → strict" in out
    # empty still returns a non-empty fallback line
    assert _format_decisions([])


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
    # The update that publishes the pause flag also carries the heartbeat timestamp and clears the
    # cross-worker resume-claim lease so this pause is immediately claimable.
    pause_update = next(u for u in updates if u.get("waiting_for_answers") is True)
    assert pause_update.get("answer_wait_heartbeat_at")
    assert "T" in pause_update["answer_wait_heartbeat_at"]  # ISO-8601
    assert "resume_claim_at" in pause_update
    assert pause_update["resume_claim_at"] is None


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
    from coding_team.tech_lead_agent.agent import _plan_text

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
    from coding_team.tech_lead_agent.agent import _render_resolved_questions

    out = _render_resolved_questions(["not a dict", {"question_text": "Q?", "answer": "A"}])
    assert "Q? → A" in out
    assert "not a dict" not in out


def test_plan_to_task_graph_parses_open_questions(monkeypatch):
    from coding_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())
    monkeypatch.setattr(
        tl_mod,
        "_agent_call_json",
        lambda a, p: {
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
    from coding_team.tech_lead_agent import agent as tl_mod

    monkeypatch.setattr(tl_mod, "Agent", lambda **kw: object())

    def boom(a, p):
        raise RuntimeError("x")

    monkeypatch.setattr(tl_mod, "_agent_call_json", boom)
    out = tl_mod.TechLeadAgent(model=object()).run_plan_to_task_graph(
        CodingTeamPlanInput(repo_path="/tmp")
    )
    assert out["open_questions"] == []


# --------------------------------------------------------------------------- Senior SWE channel


def test_run_implement_needs_decision(tmp_path, monkeypatch):
    from coding_team.senior_software_engineer_agent import agent as swe_mod

    class FakeAgent:
        def __init__(self, **kw):
            pass

        def __call__(self, prompt):
            return (
                '{"summary":"need info","files_to_create_or_edit":[],"commands_run":[],'
                '"ready_for_review":true,"open_questions":[{"question_text":"Which default?"}]}'
            )

    monkeypatch.setattr(swe_mod, "Agent", FakeAgent)
    swe = swe_mod.SeniorSWEAgent(agent_id="a1", stack_spec=StackSpec(name="backend"), llm=object())
    out = swe.run_implement(Task(id="t1", title="T", description="d"), tmp_path, repo_context="")
    # needs_decision wins even though the model marked ready_for_review=true.
    assert out["status"] == "needs_decision"
    assert out["open_questions"][0]["question_text"] == "Which default?"


def test_run_implement_no_questions_is_in_review(tmp_path, monkeypatch):
    from coding_team.senior_software_engineer_agent import agent as swe_mod

    class FakeAgent:
        def __init__(self, **kw):
            pass

        def __call__(self, prompt):
            return '{"summary":"ok","files_to_create_or_edit":[],"commands_run":[],"ready_for_review":true}'

    monkeypatch.setattr(swe_mod, "Agent", FakeAgent)
    swe = swe_mod.SeniorSWEAgent(agent_id="a1", stack_spec=StackSpec(name="backend"), llm=object())
    out = swe.run_implement(Task(id="t1", title="T", description="d"), tmp_path, repo_context="")
    assert out["status"] == "in_review"
    assert out["open_questions"] == []


def test_run_implement_no_git_tools_path(tmp_path, monkeypatch):
    from coding_team.senior_software_engineer_agent import agent as swe_mod

    class FakeAgent:
        def __init__(self, **kw):
            pass

        def __call__(self, prompt):
            return (
                '{"summary":"ok","files_to_create_or_edit":[{"path":"a.py","content":"x"}],'
                '"commands_run":["pytest"],"ready_for_review":true}'
            )

    monkeypatch.setattr(swe_mod, "Agent", FakeAgent)
    swe = swe_mod.SeniorSWEAgent(agent_id="a1", stack_spec=StackSpec(name="backend"), llm=object())
    out = swe.run_implement(
        Task(id="t1", title="T", description="d"), tmp_path, use_git_tools=False
    )
    assert out["status"] == "in_review"
    assert out["files_to_create_or_edit"][0]["path"] == "a.py"
    assert out["commands_run"] == ["pytest"]


# --------------------------------------------------------------------------- orchestrator end-to-end


def _stub_agents(monkeypatch, tech_lead_cls, swarm_cls):
    class SWE:
        def __init__(self, *a, **k):
            self.agent_id = k.get("agent_id", "backend")

    monkeypatch.setattr(orch_mod, "TechLeadAgent", tech_lead_cls)
    monkeypatch.setattr(orch_mod, "SeniorSWEAgent", SWE)
    monkeypatch.setattr(orch_mod, "CodingTeamSwarm", swarm_cls)


def test_entry_gate_pauses_then_resumes_and_threads_answers(tmp_path, monkeypatch):
    job: Dict[str, Any] = {"submitted_answers": []}
    updates: List[Dict[str, Any]] = []
    monkeypatch.setattr(orch_mod.hitl, "wait_for_answers", _answer_all(job))

    seen: Dict[str, Any] = {}

    class TL:
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
    swarm._run_quality_gates = lambda *a, **k: True  # type: ignore[method-assign]
    return swarm, graph


class _DecisionWorker:
    def __init__(self, agent_id="a1", ask_rounds=1):
        self.agent_id = agent_id
        self.stack_spec = StackSpec(name=agent_id)
        self.calls = 0
        self._ask_rounds = ask_rounds

    def run_implement(self, task, path, repo_context=""):
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

    class TL:
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


def test_run_implement_llm_exception_returns_failed(tmp_path, monkeypatch):
    from coding_team.senior_software_engineer_agent import agent as swe_mod

    class BoomAgent:
        def __init__(self, **kw):
            pass

        def __call__(self, prompt):
            raise RuntimeError("llm down")

    monkeypatch.setattr(swe_mod, "Agent", BoomAgent)
    swe = swe_mod.SeniorSWEAgent(agent_id="a1", stack_spec=StackSpec(name="backend"), llm=object())
    out = swe.run_implement(Task(id="t1", title="T", description="d"), tmp_path)
    assert out["status"] == "failed"
    assert "llm down" in out["error"]


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
