"""Tests for the coding-team cost guardrails: per-job LLM budget, task-scaled
round ceiling with explicit failure, and per-role thinking levels.

These tests are deliberately free of the job-service / Postgres fixtures: the
guardrails are pure helpers plus an orchestrator state-machine that is driven
here with stubbed agents and a no-op task-graph persist.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

import coding_team._guardrails as g
from coding_team import orchestrator as orch
from coding_team.models import CodingTeamPlanInput

# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_budget_admits_exactly_limit_then_raises() -> None:
    budget = g.CodingTeamLLMBudget(3)
    budget.charge()
    budget.charge()
    budget.charge()
    assert budget.calls_made == 3
    with pytest.raises(g.CodingTeamBudgetExhausted) as exc:
        budget.charge()
    # The refused charge does not increment — exactly ``limit`` charges succeed.
    assert budget.calls_made == 3
    assert exc.value.limit == 3
    assert exc.value.calls_made == 3


def test_budget_exhausted_is_baseexception_not_exception() -> None:
    """The trip must survive the agents' broad ``except Exception`` handlers."""
    assert issubclass(g.CodingTeamBudgetExhausted, BaseException)
    assert not issubclass(g.CodingTeamBudgetExhausted, Exception)


def test_budget_rejects_sub_one_limit() -> None:
    with pytest.raises(AssertionError):
        g.CodingTeamLLMBudget(0)


def test_charge_active_budget_is_noop_when_unbound() -> None:
    # No budget bound → no-op, no raise even when called many times.
    for _ in range(5):
        g.charge_active_budget()


def test_use_budget_binds_and_restores() -> None:
    budget = g.CodingTeamLLMBudget(1)
    assert g.active_budget() is None
    with g.use_budget(budget):
        assert g.active_budget() is budget
        g.charge_active_budget()
        with pytest.raises(g.CodingTeamBudgetExhausted):
            g.charge_active_budget()
    assert g.active_budget() is None


# ---------------------------------------------------------------------------
# _BudgetedClient
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Minimal duck-typed LLMClient that counts how often each method ran."""

    def __init__(self) -> None:
        self.model = "fake-model"
        self.chat_calls = 0
        self.complete_json_calls = 0

    def chat(self, *_a: Any, **_k: Any) -> Dict[str, Any]:
        self.chat_calls += 1
        return {"ok": True}

    def complete_json(self, *_a: Any, **_k: Any) -> Dict[str, Any]:
        self.complete_json_calls += 1
        return {"ok": True}

    def get_max_context_tokens(self) -> int:
        return 4096


def test_budgeted_client_charges_before_delegating() -> None:
    inner = _RecordingClient()
    client = g._BudgetedClient(inner)
    budget = g.CodingTeamLLMBudget(2)
    with g.use_budget(budget):
        client.chat()
        client.complete_json()
    assert budget.calls_made == 2
    assert inner.chat_calls == 1
    assert inner.complete_json_calls == 1


def test_budgeted_client_makes_no_inner_call_after_trip() -> None:
    """Acceptance: no further LLM calls are made once the budget trips."""
    inner = _RecordingClient()
    client = g._BudgetedClient(inner)
    budget = g.CodingTeamLLMBudget(1)
    with g.use_budget(budget):
        client.chat()  # charge 1/1
        with pytest.raises(g.CodingTeamBudgetExhausted):
            client.chat()  # refused before delegating
    assert inner.chat_calls == 1  # the over-budget call never reached the client


def test_budgeted_client_passes_through_non_call_attrs() -> None:
    inner = _RecordingClient()
    client = g._BudgetedClient(inner)
    assert client.model == "fake-model"
    assert client.get_max_context_tokens() == 4096


def test_budgeted_client_charges_complete_and_complete_text() -> None:
    class _C:
        def complete(self, *_a: Any, **_k: Any) -> str:
            return "c"

        def complete_text(self, *_a: Any, **_k: Any) -> str:
            return "ct"

    client = g._BudgetedClient(_C())
    budget = g.CodingTeamLLMBudget(2)
    with g.use_budget(budget):
        assert client.complete() == "c"
        assert client.complete_text() == "ct"
    assert budget.calls_made == 2


def test_budgeted_client_requires_inner() -> None:
    with pytest.raises(AssertionError):
        g._BudgetedClient(None)


# ---------------------------------------------------------------------------
# Env parsing + derived ceilings
# ---------------------------------------------------------------------------


def test_max_llm_calls_default_override_floor_and_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODING_TEAM_MAX_LLM_CALLS", raising=False)
    assert g.max_llm_calls_from_env() == 300
    monkeypatch.setenv("CODING_TEAM_MAX_LLM_CALLS", "42")
    assert g.max_llm_calls_from_env() == 42
    monkeypatch.setenv("CODING_TEAM_MAX_LLM_CALLS", "0")
    assert g.max_llm_calls_from_env() == 1  # floored
    monkeypatch.setenv("CODING_TEAM_MAX_LLM_CALLS", "not-an-int")
    assert g.max_llm_calls_from_env() == 300  # garbage → default


def test_round_ceiling_scales_with_task_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODING_TEAM_ROUND_MULTIPLIER", raising=False)
    monkeypatch.delenv("CODING_TEAM_MIN_ROUNDS", raising=False)
    assert g.max_rounds_for(0) == 10  # min floor
    assert g.max_rounds_for(2) == 10  # 3*2=6 < 10 floor
    assert g.max_rounds_for(5) == 15  # 3*5=15 > 10 floor


def test_round_ceiling_env_overrides_and_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_TEAM_ROUND_MULTIPLIER", "2")
    monkeypatch.setenv("CODING_TEAM_MIN_ROUNDS", "3")
    assert g.max_rounds_for(10) == 20
    assert g.max_rounds_for(1) == 3
    monkeypatch.setenv("CODING_TEAM_ROUND_MULTIPLIER", "0")  # floored to 1
    monkeypatch.setenv("CODING_TEAM_MIN_ROUNDS", "garbage")  # → default 10
    assert g.max_rounds_for(4) == 10  # max(10, 1*4)


def test_max_rounds_for_rejects_negative() -> None:
    with pytest.raises(AssertionError):
        g.max_rounds_for(-1)


# ---------------------------------------------------------------------------
# Per-role thinking
# ---------------------------------------------------------------------------


def test_mechanical_think_value_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODING_TEAM_THINKING_MECHANICAL", raising=False)
    assert g.mechanical_think_value() is False
    monkeypatch.setenv("CODING_TEAM_THINKING_MECHANICAL", "true")
    assert g.mechanical_think_value() is None
    monkeypatch.setenv("CODING_TEAM_THINKING_MECHANICAL", "0")
    assert g.mechanical_think_value() is False


def test_role_think_mechanical_vs_generative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODING_TEAM_THINKING_MECHANICAL", raising=False)
    assert g.role_think("linting_tool_agent") is False
    assert g.role_think("code_review") is False
    assert g.role_think("coding_team") is None  # implementation keeps high level
    assert g.role_think("tech_lead") is None  # planning keeps high level
    assert g.role_think(None) is None


# ---------------------------------------------------------------------------
# Terminal-status decision
# ---------------------------------------------------------------------------


def test_terminal_status_all_branches() -> None:
    assert g.terminal_status("complete", 2, 10)[0] == "completed"
    assert g.terminal_status("complete", 0, 10)[0] == "failed: no_tasks_merged"
    assert g.terminal_status("max_rounds_exhausted", 0, 10)[0] == "failed: max_rounds_exhausted"
    assert g.terminal_status("max_rounds_exhausted", 1, 10)[0] == "failed: max_rounds_exhausted"
    assert g.terminal_status("cancelled", 0, 10) is None


# ---------------------------------------------------------------------------
# Shared llm_service: get_strands_model forwards think + caches per think value
# ---------------------------------------------------------------------------


def test_get_strands_model_forwards_think_and_caches_per_value() -> None:
    from llm_service.strands_provider import (
        _clear_strands_model_cache_for_testing,
        get_strands_model,
    )

    client = _RecordingClient()
    m_off = get_strands_model("tech_lead", client=client, think=False)
    m_default = get_strands_model("tech_lead", client=client, think=None)
    assert m_off.config["think"] is False
    assert m_default.config["think"] is None

    # Cache key includes think: distinct think values are not aliased.
    _clear_strands_model_cache_for_testing()
    a = get_strands_model("coding_team", think=False)
    b = get_strands_model("coding_team", think=None)
    assert a is not b
    again = get_strands_model("coding_team", think=False)
    assert again is a  # same think value → cached
    _clear_strands_model_cache_for_testing()


# ---------------------------------------------------------------------------
# Orchestrator state-machine: terminal statuses with stubbed agents
# ---------------------------------------------------------------------------


class _FakeSWE:
    """Records its model; always hands its task to review."""

    instances: List["_FakeSWE"] = []

    def __init__(self, *, agent_id: str, stack_spec: Any, llm: Any) -> None:
        self.agent_id = agent_id
        self.stack_spec = stack_spec
        self.llm = llm
        _FakeSWE.instances.append(self)

    def run_implement(self, task: Any, path: Any, repo_context: str = "") -> Dict[str, Any]:
        return {
            "status": "in_review",
            "feature_branch": f"feature/{task.id}",
            "changes_summary": "x",
        }


def _make_fake_tech_lead(*, tasks: List[Dict[str, Any]], approve: bool, plan_charges: int = 0):
    """Build a fake TechLeadAgent class with controllable behaviour."""

    class _FakeTechLead:
        instances: List[Any] = []

        def __init__(self, model: Any, *, mechanical_model: Any = None) -> None:
            self.model = model
            self.mechanical_model = mechanical_model
            _FakeTechLead.instances.append(self)

        def run_plan_to_task_graph(self, plan: Any) -> Dict[str, Any]:
            for _ in range(plan_charges):
                g.charge_active_budget()
            return {"tasks": tasks, "stacks": [{"name": "backend", "tools_services": []}]}

        def run_assignments(self, agent_ids, ready_tasks, free_agents) -> Dict[str, Any]:
            if not ready_tasks or not free_agents:
                return {"assignments": []}
            return {"assignments": [{"agent_id": free_agents[0], "task_id": ready_tasks[0]["id"]}]}

        def run_code_review(self, **_kw: Any) -> Dict[str, Any]:
            return {"approved": approve, "reason": "", "requested_changes": []}

    return _FakeTechLead


def _run_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tech_lead_cls,
    quality_gate_ok: bool = True,
    cancel: bool = False,
    inject_llm: bool = True,
) -> "tuple[List[Dict[str, Any]], Optional[str]]":
    """Drive run_coding_team_orchestrator with stubs.

    Returns ``(captured_update_kwargs, return_value)`` — the return value is
    the terminal status the SE/Temporal callers now branch on.
    """
    _FakeSWE.instances = []
    updates: List[Dict[str, Any]] = []

    monkeypatch.setattr(orch, "TechLeadAgent", tech_lead_cls)
    monkeypatch.setattr(orch, "SeniorSWEAgent", _FakeSWE)
    monkeypatch.setattr(orch, "update_job_task_graph", lambda *a, **k: None)
    monkeypatch.setattr(
        orch.CodingTeamSwarm, "_run_quality_gates", lambda self, *a, **k: quality_gate_ok
    )
    # _review_and_merge shells out to git on an approved task; make the merge
    # deterministic so the test exercises the terminal-status logic, not git.
    import software_engineering_team.shared.git_utils as _gu

    monkeypatch.setattr(_gu, "merge_branch", lambda *a, **k: (True, ""))

    plan = CodingTeamPlanInput(
        requirements_title="t", requirements_description="d", repo_path="/tmp/repo"
    )

    class _FakeClient:
        model = "fake"

    get_llm = (lambda key: _FakeClient()) if inject_llm else None
    ret = orch.run_coding_team_orchestrator(
        "job-1",
        "/tmp/repo",
        plan,
        update_job_fn=lambda **kw: updates.append(kw),
        get_job_fn=lambda jid: {orch.CANCEL_KEY: True} if cancel else {},
        get_llm=get_llm,
    )
    return updates, ret


def _final_status(updates: List[Dict[str, Any]]) -> Optional[str]:
    for kw in reversed(updates):
        if "status" in kw:
            return kw["status"]
    return None


def test_orchestrator_completes_when_tasks_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_TEAM_MIN_ROUNDS", "4")
    tl = _make_fake_tech_lead(tasks=[{"id": "t1", "title": "T1"}], approve=True)
    updates, ret = _run_orchestrator(monkeypatch, tech_lead_cls=tl)
    assert _final_status(updates) == "completed"
    assert ret == "completed"


def test_orchestrator_fails_max_rounds_when_never_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_TEAM_MIN_ROUNDS", "2")
    monkeypatch.setenv("CODING_TEAM_ROUND_MULTIPLIER", "1")
    tl = _make_fake_tech_lead(tasks=[{"id": "t1", "title": "T1"}], approve=False)
    updates, ret = _run_orchestrator(monkeypatch, tech_lead_cls=tl)
    assert _final_status(updates) == "failed: max_rounds_exhausted"
    assert ret == "failed: max_rounds_exhausted"
    # The false-success message can never be produced.
    assert all("0 tasks merged" not in (kw.get("status_text") or "") for kw in updates)


def test_orchestrator_fails_no_tasks_merged_when_empty_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODING_TEAM_MIN_ROUNDS", "3")
    tl = _make_fake_tech_lead(tasks=[], approve=True)
    updates, ret = _run_orchestrator(monkeypatch, tech_lead_cls=tl)
    assert _final_status(updates) == "failed: no_tasks_merged"
    assert ret == "failed: no_tasks_merged"


def test_orchestrator_fails_budget_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_TEAM_MAX_LLM_CALLS", "1")
    tl = _make_fake_tech_lead(tasks=[{"id": "t1"}], approve=True, plan_charges=2)
    updates, ret = _run_orchestrator(monkeypatch, tech_lead_cls=tl)
    assert _final_status(updates) == "failed: budget_exhausted"
    assert ret == "failed: budget_exhausted"
    last = updates[-1]
    assert "CODING_TEAM_MAX_LLM_CALLS" in last["status_text"]


def test_orchestrator_cancelled_keeps_cancelled_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_TEAM_MIN_ROUNDS", "4")
    tl = _make_fake_tech_lead(tasks=[{"id": "t1", "title": "T1"}], approve=True)
    updates, ret = _run_orchestrator(monkeypatch, tech_lead_cls=tl, cancel=True)
    # The swarm sets "cancelled"; terminal_status returns None so the
    # orchestrator must not overwrite it — and signals cancellation to callers
    # by returning None so they do not re-mark the job completed.
    assert _final_status(updates) == "cancelled"
    assert ret is None


def test_orchestrator_default_getter_without_injected_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no get_llm, the orchestrator resolves clients via the factory."""
    monkeypatch.setenv("CODING_TEAM_MIN_ROUNDS", "4")
    tl = _make_fake_tech_lead(tasks=[], approve=True)  # empty plan → quick exit
    updates, ret = _run_orchestrator(monkeypatch, tech_lead_cls=tl, inject_llm=False)
    assert _final_status(updates) == "failed: no_tasks_merged"
    assert ret == "failed: no_tasks_merged"


def test_swarm_run_derives_max_rounds_when_none(tmp_path) -> None:
    """run() with max_rounds=None derives the ceiling and completes an empty graph."""
    from coding_team.task_graph import create_task_graph

    graph = create_task_graph("job-x")
    swarm = orch.CodingTeamSwarm(
        tech_lead=object(),
        workers=[],
        graph=graph,
        path=tmp_path,
        agent_ids=[],
        llm_getter=lambda key: object(),
    )
    assert swarm.run() == "complete"  # no tasks → immediately complete


def test_orchestrator_applies_per_role_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODING_TEAM_THINKING_MECHANICAL", raising=False)
    monkeypatch.setenv("CODING_TEAM_MIN_ROUNDS", "4")
    tl = _make_fake_tech_lead(tasks=[{"id": "t1", "title": "T1"}], approve=True)
    _run_orchestrator(monkeypatch, tech_lead_cls=tl)
    tech_lead = tl.instances[-1]
    # Planning keeps the high (platform-default) level; the mechanical model
    # (assignment / review) runs with reasoning disabled.
    assert tech_lead.model.config["think"] is None
    assert tech_lead.mechanical_model.config["think"] is False
    # Implementation worker keeps the high level.
    assert _FakeSWE.instances[-1].llm.config["think"] is None
