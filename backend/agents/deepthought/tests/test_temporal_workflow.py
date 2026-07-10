"""Orchestration tests for ``DeepthoughtWorkflow``.

The full server-backed ``WorkflowEnvironment`` needs a test-server binary that is
unavailable in the offline CI network, so (as in
``investment_team/tests/test_strategy_lab_temporal_sandbox.py``) these tests drive
the workflow's ``run`` coroutine directly with ``asyncio`` while patching the
workflow-context primitives it relies on:

- ``workflow.execute_activity`` — a fake dispatcher keyed on activity ``__name__``
  (activities run outside the workflow, so they are stubbed);
- ``workflow.uuid4`` — a deterministic counter (real ``uuid.uuid4`` must never be
  called from workflow code);
- ``workflow.patched`` — forced True/False to select the decomposed vs legacy path;
- ``workflow.logger`` — a plain logger (it needs a workflow runtime otherwise).

A determinism guard additionally boobytraps ``uuid.uuid4``/``uuid.uuid1`` and
statically scans the run path for restricted names, so an accidental
nondeterministic call would fail the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import logging
import uuid as _uuid
from typing import Any
from unittest import mock

import pytest
from temporalio import workflow as _wf

from deepthought.models import (
    AgentEventType,
    AgentSpec,
    KnowledgeEntry,
    QueryAnalysis,
    SkillRequirement,
)
from deepthought.temporal import workflows as wfmod


def _request(strategy: str = "auto") -> dict[str, Any]:
    return {
        "message": "root question",
        "max_depth": 3,
        "conversation_history": [],
        "decomposition_strategy": strategy,
    }


def _boom(*_a: Any, **_k: Any):
    raise AssertionError("workflow run path called a sandbox-restricted callable")


@contextlib.contextmanager
def _driver(handlers: dict[str, Any], calls: list, *, patched: bool = True):
    """Patch the workflow-context primitives and record every activity call."""
    counter = itertools.count()

    async def _fake_exec(fn, *pos, **kw):
        name = getattr(fn, "__name__", str(fn))
        args = list(kw["args"]) if "args" in kw else list(pos)
        calls.append((name, args))
        handler = handlers.get(name)
        if handler is None:
            raise AssertionError(f"unexpected activity call: {name}")
        return handler(args) if callable(handler) else handler

    with (
        mock.patch.object(_wf, "execute_activity", _fake_exec),
        mock.patch.object(_wf, "uuid4", lambda: f"uuid-{next(counter)}"),
        mock.patch.object(_wf, "patched", lambda _id: patched),
        mock.patch.object(_wf, "logger", logging.getLogger("deepthought.workflow.test")),
    ):
        yield


def _analyse_handler(args: list) -> dict:
    """Root decomposes into two specialists; deeper nodes answer directly."""
    payload = args[0]
    if payload["spec"]["depth"] == 0:
        return QueryAnalysis(
            summary="root",
            can_answer_directly=False,
            skill_requirements=[
                SkillRequirement(
                    name="expert_a", description="A", focus_question="qa", reasoning="r"
                ),
                SkillRequirement(
                    name="expert_b", description="B", focus_question="qb", reasoning="r"
                ),
            ],
        ).model_dump()
    return QueryAnalysis(
        summary="child",
        can_answer_directly=True,
        direct_answer=f"ans-{payload['spec']['name']}",
        confidence=0.7,
    ).model_dump()


def _happy_handlers(finalize_sink: list | None = None) -> dict[str, Any]:
    def _finalize(args: list) -> None:
        if finalize_sink is not None:
            finalize_sink.append(args)

    return {
        "start_job_activity": lambda a: True,
        "classify_strategy_activity": lambda a: "by_discipline",
        "analyse_activity": _analyse_handler,
        "deliberate_activity": lambda a: "deliberation notes",
        "synthesise_activity": lambda a: "SYNTHESISED",
        "finalize_job_activity": _finalize,
    }


# --------------------------------------------------------------------------- #
# Happy path — full decomposed tree
# --------------------------------------------------------------------------- #


def test_workflow_runs_decomposed_tree():
    calls: list = []
    finalize: list = []
    with _driver(_happy_handlers(finalize), calls):
        result = asyncio.run(wfmod.DeepthoughtWorkflow().run("job-1", _request()))

    # Root synthesised, with the specialists footer appended.
    assert result["answer"].startswith("SYNTHESISED")
    assert "Specialists consulted:" in result["answer"]
    assert "- **expert_a**: qa" in result["answer"]

    assert result["total_agents_spawned"] == 3  # root + two specialists
    assert result["max_depth_reached"] == 1
    tree = result["agent_tree"]
    assert tree["was_decomposed"] is True
    assert [c["answer"] for c in tree["child_results"]] == ["ans-expert_a", "ans-expert_b"]
    assert tree["deliberation_notes"] == "deliberation notes"

    # Knowledge base accrued one finding per completed node.
    assert len(result["knowledge_entries"]) == 3

    # Event log carries the key lifecycle events.
    event_types = {e["event_type"] for e in result["events"]}
    for expected in (
        AgentEventType.AGENT_ANALYSING,
        AgentEventType.AGENT_DECOMPOSING,
        AgentEventType.AGENT_ANSWERING,
        AgentEventType.AGENT_DELIBERATING,
        AgentEventType.AGENT_SYNTHESISING,
        AgentEventType.AGENT_COMPLETE,
    ):
        assert expected in event_types

    # Activity call shape: strategy classified once, one analyse per node, a
    # single deliberate + synthesise at the root, finalize once.
    names = [c[0] for c in calls]
    assert names[0] == "start_job_activity"
    assert names.count("classify_strategy_activity") == 1
    assert names.count("analyse_activity") == 3
    assert names.count("deliberate_activity") == 1
    assert names.count("synthesise_activity") == 1
    assert names.count("finalize_job_activity") == 1
    # Finalize records success with the response payload.
    assert finalize[0][2] is True
    assert finalize[0][1]["answer"].startswith("SYNTHESISED")


def test_workflow_direct_answer_without_force():
    """A node that can answer directly (depth below the limit) skips force-direct."""

    def _analyse_direct_no_text(args: list) -> dict:
        # can_answer_directly with no direct_answer text, at depth 0 < max_depth:
        # the answer is empty and force_direct is NOT triggered (that only fires at
        # the depth limit).
        return QueryAnalysis(summary="s", can_answer_directly=True, direct_answer=None).model_dump()

    handlers = {
        "start_job_activity": lambda a: True,
        "analyse_activity": _analyse_direct_no_text,
        "force_direct_answer_activity": lambda a: "forced answer",
        "finalize_job_activity": lambda a: None,
    }
    calls: list = []
    request = {
        "message": "q",
        "max_depth": 3,
        "conversation_history": [],
        "decomposition_strategy": "none",
    }
    with _driver(handlers, calls):
        result = asyncio.run(wfmod.DeepthoughtWorkflow().run("job-x", request))

    assert result["agent_tree"]["was_decomposed"] is False
    assert result["agent_tree"]["answer"] == ""
    assert "force_direct_answer_activity" not in [c[0] for c in calls]


def test_workflow_forces_direct_answer_when_depth_exhausted():
    def _analyse_decompose_then_leaf(args: list) -> dict:
        if args[0]["spec"]["depth"] == 0:
            return QueryAnalysis(
                summary="root",
                can_answer_directly=False,
                skill_requirements=[
                    SkillRequirement(name="e", description="d", focus_question="cq", reasoning="r")
                ],
            ).model_dump()
        # Child at depth 1 == max_depth, cannot answer, no direct answer -> force.
        return QueryAnalysis(
            summary="child", can_answer_directly=False, direct_answer=None
        ).model_dump()

    handlers = {
        "start_job_activity": lambda a: True,
        "analyse_activity": _analyse_decompose_then_leaf,
        "force_direct_answer_activity": lambda a: "forced child answer",
        "synthesise_activity": lambda a: "root synth",
        "finalize_job_activity": lambda a: None,
    }
    calls: list = []
    request = {
        "message": "q",
        "max_depth": 1,
        "conversation_history": [],
        "decomposition_strategy": "none",
    }
    with _driver(handlers, calls):
        result = asyncio.run(wfmod.DeepthoughtWorkflow().run("job-y", request))

    assert "force_direct_answer_activity" in [c[0] for c in calls]
    assert result["agent_tree"]["child_results"][0]["answer"] == "forced child answer"


# --------------------------------------------------------------------------- #
# Control-flow branches
# --------------------------------------------------------------------------- #


def test_workflow_legacy_patched_path_runs_single_activity():
    calls: list = []
    with _driver({"run_pipeline_activity": lambda a: {"answer": "legacy"}}, calls, patched=False):
        result = asyncio.run(wfmod.DeepthoughtWorkflow().run("job-legacy", _request()))

    assert result == {"answer": "legacy"}
    assert [c[0] for c in calls] == ["run_pipeline_activity"]


def test_workflow_short_circuits_when_cancelled_before_start():
    calls: list = []
    with _driver({"start_job_activity": lambda a: False}, calls):
        result = asyncio.run(wfmod.DeepthoughtWorkflow().run("job-cancelled", _request()))

    assert result == {}
    assert [c[0] for c in calls] == ["start_job_activity"]


def test_workflow_records_failed_and_reraises():
    def _boom_analyse(args: list) -> dict:
        raise RuntimeError("kaboom")

    handlers = {
        "start_job_activity": lambda a: True,
        "analyse_activity": _boom_analyse,
        "finalize_job_activity": lambda a: None,
    }
    calls: list = []
    request = {
        "message": "q",
        "max_depth": 3,
        "conversation_history": [],
        "decomposition_strategy": "none",
    }
    with _driver(handlers, calls):
        with pytest.raises(RuntimeError, match="kaboom"):
            asyncio.run(wfmod.DeepthoughtWorkflow().run("job-fail", request))

    finalize = [c for c in calls if c[0] == "finalize_job_activity"]
    assert finalize and finalize[0][1][2] is False  # success flag
    assert finalize[0][1][3] == "kaboom"  # error message


# --------------------------------------------------------------------------- #
# Knowledge-base dedup + budget (workflow state)
# --------------------------------------------------------------------------- #


def test_run_agent_reuses_similar_finding():
    wf = wfmod.DeepthoughtWorkflow()
    wf._kb = [
        KnowledgeEntry(
            agent_id="prev",
            agent_name="prior_expert",
            focus_question="explain quantum entanglement in simple terms",
            finding="entanglement links particle states",
            confidence=0.9,
        )
    ]
    spec = AgentSpec(
        agent_id="c1",
        name="child",
        role_description="r",
        focus_question="explain quantum entanglement in simple terms",
        depth=1,
        parent_id="root",
    )
    calls: list = []
    with _driver({}, calls):
        res = asyncio.run(wf._run_agent(spec, "parent q", {"message": "orig"}, "auto", 3))

    assert res.reused_from_cache is True
    assert res.answer == "entanglement links particle states"
    assert res.confidence == 0.9
    assert not any(c[0] == "analyse_activity" for c in calls)  # short-circuited


def test_run_children_vetoes_over_budget():
    wf = wfmod.DeepthoughtWorkflow()
    wf._budget = 1
    wf._spawned = 1  # root already counted -> no budget left for children
    specs = [
        AgentSpec(
            agent_id="a",
            name="na",
            role_description="r",
            focus_question="qa",
            depth=1,
            parent_id="root",
        ),
        AgentSpec(
            agent_id="b",
            name="nb",
            role_description="r",
            focus_question="qb",
            depth=1,
            parent_id="root",
        ),
    ]
    parent = AgentSpec(
        agent_id="root", name="root", role_description="r", focus_question="pq", depth=0
    )
    calls: list = []
    with _driver({}, calls):
        res = asyncio.run(wf._run_children(specs, parent, {"message": "m"}, "auto", 3))

    assert len(res) == 2
    assert all(r.answer == "Agent budget exceeded — analysis truncated." for r in res)
    assert not any(c[0] == "analyse_activity" for c in calls)
    # Budget-warning events were emitted for both vetoed children.
    assert sum(1 for e in wf._events if e.event_type == AgentEventType.BUDGET_WARNING) == 2


def test_run_children_returns_empty_for_no_specs():
    wf = wfmod.DeepthoughtWorkflow()
    parent = AgentSpec(
        agent_id="root", name="root", role_description="r", focus_question="pq", depth=0
    )
    with _driver({}, []):
        res = asyncio.run(wf._run_children([], parent, {"message": "m"}, "auto", 3))
    assert res == []


def test_run_child_guarded_substitutes_error_result():
    wf = wfmod.DeepthoughtWorkflow()
    wf._budget = 50
    spec = AgentSpec(
        agent_id="a",
        name="na",
        role_description="r",
        focus_question="qa",
        depth=1,
        parent_id="root",
    )
    parent = AgentSpec(
        agent_id="root", name="root", role_description="r", focus_question="pq", depth=0
    )
    handlers = {"analyse_activity": lambda a: (_ for _ in ()).throw(RuntimeError("child boom"))}
    with _driver(handlers, []):
        res = asyncio.run(wf._run_child_guarded(spec, parent, {"message": "m"}, "auto", 3))
    assert res.answer == "Error analysing: qa"
    assert res.confidence == 0.0


# --------------------------------------------------------------------------- #
# Determinism guard
# --------------------------------------------------------------------------- #


def test_workflow_happy_path_never_calls_stdlib_uuid():
    """The run path uses ``workflow.uuid4`` only; ``uuid.uuid4`` must not be hit."""
    calls: list = []
    with (
        mock.patch.object(_uuid, "uuid4", _boom),
        mock.patch.object(_uuid, "uuid1", _boom),
        _driver(_happy_handlers(), calls),
    ):
        result = asyncio.run(wfmod.DeepthoughtWorkflow().run("job-det", _request()))
    assert result["total_agents_spawned"] == 3


def test_workflow_run_source_has_no_restricted_names():
    banned = (
        "uuid.uuid4(",
        "uuid.uuid1(",
        "datetime.now",
        "datetime.utcnow",
        ".utcnow(",
        "date.today",
        "time.time(",
        "time.monotonic(",
        "random.",
    )
    sources = [
        inspect.getsource(wfmod.DeepthoughtWorkflow.run),
        inspect.getsource(wfmod.DeepthoughtWorkflow._resolve_strategy),
        inspect.getsource(wfmod.DeepthoughtWorkflow._run_agent),
        inspect.getsource(wfmod.DeepthoughtWorkflow._analyse),
        inspect.getsource(wfmod.DeepthoughtWorkflow._run_children),
        inspect.getsource(wfmod.DeepthoughtWorkflow._run_child_guarded),
        inspect.getsource(wfmod.DeepthoughtWorkflow._register_spawn),
        inspect.getsource(wfmod.DeepthoughtWorkflow._store_finding),
    ]
    for src in sources:
        for name in banned:
            assert name not in src, f"workflow run path references restricted {name!r}"
