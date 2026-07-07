"""Tests for the code review agent's Temporal instrumentation.

Covers the four things testable without a live Temporal server:

1. The default-on address resolver / enablement gate (``temporal.config``).
2. The activity-boundary DTO round-trips (``temporal.phase_models``).
3. The activity wrappers driven end-to-end with the ``dummy`` LLM harness,
   replicating the workflow's orchestration in-process and asserting the verdict
   matches ``run_coordinator`` (durable path is behavior-identical to thread mode).
4. ``CodeReviewAgent.run``'s Temporal-first dispatch and its fallbacks.

The workflow class itself needs a Temporal worker to execute, so its live
round-trip is an integration concern; here we assert it is importable and
registered.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from code_review_agent import CodeReviewAgent
from code_review_agent.agent import _code_review_temporal_enabled, _TemporalDispatchUnavailable
from code_review_agent.coordinator import run_coordinator
from code_review_agent.models import CodeReviewInput, CodeReviewOutput, CodeReviewUnavailableError
from code_review_agent.temporal import ACTIVITIES, WORKFLOWS, CodeReviewWorkflow
from code_review_agent.temporal import config as cfg
from code_review_agent.temporal import phase_models as pm

from llm_service.clients.dummy import DummyLLMClient


def _input(
    code: str = "### app/main.py ###\ndef foo():\n    return 1", **overrides: Any
) -> CodeReviewInput:
    base: Dict[str, Any] = {"code": code, "task_description": "Add foo()", "language": "python"}
    base.update(overrides)
    return CodeReviewInput(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. Resolver + enablement gate
# ---------------------------------------------------------------------------


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("TEMPORAL_ADDRESS", "LLM_PROVIDER", "CODE_REVIEW_TEMPORAL_FORCE"):
        monkeypatch.delenv(var, raising=False)


def test_resolve_defaults_to_deployed_container(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    assert cfg.resolve_code_review_temporal_address() == "temporal:7233"
    assert cfg.DEFAULT_CODE_REVIEW_TEMPORAL_ADDRESS == "temporal:7233"


def test_resolve_honours_temporal_address_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("TEMPORAL_ADDRESS", "ext.example:7233")
    assert cfg.resolve_code_review_temporal_address() == "ext.example:7233"


@pytest.mark.parametrize(
    "sentinel", ["", "disabled", "none", "off", "0", "false", "no", " DISABLED "]
)
def test_disable_sentinels_resolve_to_none(monkeypatch: pytest.MonkeyPatch, sentinel: str) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("TEMPORAL_ADDRESS", sentinel)
    assert cfg.resolve_code_review_temporal_address() is None


def test_enabled_is_false_under_pytest_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite runs under pytest, so the guard keeps reviews in-process.
    _clear_env(monkeypatch)
    assert cfg.code_review_temporal_enabled() is False


def test_force_flag_enables_under_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("CODE_REVIEW_TEMPORAL_FORCE", "1")
    assert cfg.code_review_temporal_enabled() is True


def test_force_flag_still_requires_an_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("CODE_REVIEW_TEMPORAL_FORCE", "yes")
    monkeypatch.setenv("TEMPORAL_ADDRESS", "none")
    assert cfg.code_review_temporal_enabled() is False


def test_dummy_harness_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    monkeypatch.setenv("CODE_REVIEW_TEMPORAL_FORCE", "")  # not forced
    assert cfg.code_review_temporal_enabled() is False


# ---------------------------------------------------------------------------
# 2. DTO round-trips
# ---------------------------------------------------------------------------


def test_chunk_outcome_dto_round_trips_from_outcome() -> None:
    from code_review_agent.mapping import _ChunkOutcome
    from code_review_agent.models import CodeReviewIssue

    outcome = _ChunkOutcome(
        issues=[CodeReviewIssue(severity="high", file_path="a.py", line=3, description="bug")],
        not_reviewed_issues=[CodeReviewIssue(severity="high", file_path="b.py", description="nr")],
        summaries=["s1"],
        spec_notes=["n1"],
        commit_messages=["feat: x"],
        approved_flags=[False],
    )
    dto = pm.ChunkOutcomeDTO.from_outcome(outcome)
    reloaded = pm.ChunkOutcomeDTO.model_validate(dto.model_dump(mode="json"))
    assert reloaded.approved_flags == [False]
    assert reloaded.issues[0].description == "bug"
    assert reloaded.not_reviewed_issues[0].file_path == "b.py"
    assert reloaded.commit_messages == ["feat: x"]


def test_review_prep_dto_round_trips() -> None:
    dto = pm.ReviewPrepDTO(no_code=True)
    reloaded = pm.ReviewPrepDTO.model_validate(dto.model_dump(mode="json"))
    assert reloaded.no_code is True
    assert reloaded.chunks == []


# ---------------------------------------------------------------------------
# 3. Activity pipeline == coordinator verdict (durable path is identical)
# ---------------------------------------------------------------------------


def _run_activity_pipeline(review_input: CodeReviewInput) -> CodeReviewOutput:
    """Drive the activities exactly as ``CodeReviewWorkflow.run`` would, in-process."""
    from code_review_agent.models import CodeReviewIssue
    from code_review_agent.temporal import activities as A

    payload = review_input.model_dump(mode="json")
    prep = A.prepare_review_activity(payload)
    if prep["no_code"]:
        return CodeReviewOutput(
            approved=True,
            issues=[CodeReviewIssue.model_validate(i) for i in prep["skipped_issues"]],
            summary="No code to review.",
        )
    outcomes = [
        A.review_chunk_activity(c, prep["base_input"], prep["context_fp"], prep["surface_by_path"])
        for c in prep["chunks"]
    ]
    issues, not_reviewed, summaries, spec_notes, commit_messages, approved_flags = (
        [] for _ in range(6)
    )
    for o in outcomes:
        issues += o["issues"]
        not_reviewed += o["not_reviewed_issues"]
        summaries += o["summaries"]
        spec_notes += o["spec_notes"]
        commit_messages += o["commit_messages"]
        approved_flags += o["approved_flags"]
    assert approved_flags, "at least one chunk reviewed"
    verified = A.filter_false_positives_activity(
        payload, issues, bool(payload.get("skip_false_positive_filter", False))
    )
    gate = A.finalize_review_activity(
        verified, not_reviewed, prep["skipped_issues"], approved_flags
    )
    if len(summaries) == 1:
        summary, notes = summaries[0], (spec_notes[0] if spec_notes else "")
    else:
        synth = A.synthesize_findings_activity(
            payload, gate["approved"], gate["issues"], summaries, spec_notes
        )
        if synth is not None:
            summary, notes = synth["summary"], synth["spec_compliance_notes"]
        else:
            summary = "\n\n".join(s for s in summaries if s.strip())
            notes = "\n\n".join(n for n in spec_notes if n.strip())
    commit = commit_messages[0] if (prep["single_chunk"] and len(commit_messages) == 1) else ""
    return CodeReviewOutput.model_validate(
        {
            "approved": gate["approved"],
            "issues": gate["issues"],
            "summary": summary,
            "spec_compliance_notes": notes,
            "suggested_commit_message": commit,
        }
    )


def test_activity_pipeline_matches_coordinator_verdict() -> None:
    review_input = _input()
    coordinator_out = run_coordinator(DummyLLMClient(), review_input)
    pipeline_out = _run_activity_pipeline(review_input)
    assert pipeline_out.approved == coordinator_out.approved
    assert [i.model_dump() for i in pipeline_out.issues] == [
        i.model_dump() for i in coordinator_out.issues
    ]


def test_activity_pipeline_matches_coordinator_verdict_multi_chunk() -> None:
    # Two large files force the submission past a single map chunk, exercising the
    # multi-file/multi-chunk path: multiple ``review_chunk_activity`` fan-outs, the
    # dedupe/reconcile reduce, and the >1-summary synthesis branch. The durable
    # pipeline's verdict must still match ``run_coordinator``'s for the same input.
    big_1 = "### app/main.py ###\n" + ("a" * 25_000)
    big_2 = "### app/util.py ###\n" + ("b" * 25_000)
    review_input = _input(code=big_1 + "\n\n" + big_2)

    # Confirm the input really does split into more than one chunk (otherwise the
    # test would silently degrade to the single-chunk path it means to complement).
    from code_review_agent.temporal import activities as A

    prep = A.prepare_review_activity(review_input.model_dump(mode="json"))
    assert len(prep["chunks"]) > 1, "expected a multi-chunk submission"

    coordinator_out = run_coordinator(DummyLLMClient(), review_input)
    pipeline_out = _run_activity_pipeline(review_input)
    assert pipeline_out.approved == coordinator_out.approved
    assert [i.model_dump() for i in pipeline_out.issues] == [
        i.model_dump() for i in coordinator_out.issues
    ]


def test_prepare_activity_reports_no_code_for_empty_files() -> None:
    from code_review_agent.temporal import activities as A

    prep = A.prepare_review_activity(_input(code="").model_dump(mode="json"))
    assert prep["no_code"] is True


def test_prepare_activity_compacts_architecture_overview() -> None:
    from code_review_agent.temporal import activities as A

    from software_engineering_team.shared.models import SystemArchitecture

    arch = SystemArchitecture(
        overview="A small service that does one thing.",
        architecture_document="# Arch",
        components=[],
        decisions=[],
        diagrams={},
    )
    prep = A.prepare_review_activity(_input(architecture=arch).model_dump(mode="json"))
    assert prep["no_code"] is False
    # The compacted architecture overview rides in base_input for every chunk.
    assert prep["base_input"]["architecture_overview"]


def test_filter_activity_skip_returns_deduped_genuine() -> None:
    from code_review_agent.temporal import activities as A

    issues = [
        {
            "severity": "high",
            "category": "logic",
            "file_path": "a.py",
            "line": 3,
            "description": "dup",
            "suggestion": "",
        },
        {
            "severity": "high",
            "category": "logic",
            "file_path": "a.py",
            "line": 3,
            "description": "dup",
            "suggestion": "",
        },
    ]
    out = A.filter_false_positives_activity(_input().model_dump(mode="json"), issues, True)
    # Deduped to one, and no LLM verification ran (skip=True).
    assert len(out) == 1
    assert out[0]["description"] == "dup"


def test_finalize_activity_reconciles_minor_only_to_approved() -> None:
    from code_review_agent.temporal import activities as A

    minor = [
        {
            "severity": "low",
            "category": "naming",
            "file_path": "a.py",
            "line": 2,
            "description": "nit",
            "suggestion": "",
        }
    ]
    gate = A.finalize_review_activity(minor, [], [], [False])
    # A reject carrying only minor issues flips to approved (anti-loop net).
    assert gate["approved"] is True
    assert len(gate["issues"]) == 1


def test_synthesize_activity_returns_none_or_dict() -> None:
    from code_review_agent.temporal import activities as A

    issues = [
        {
            "severity": "high",
            "category": "logic",
            "file_path": "a.py",
            "line": 1,
            "description": "x",
            "suggestion": "",
        }
    ]
    result = A.synthesize_findings_activity(
        _input().model_dump(mode="json"),
        approved=False,
        issues=issues,
        chunk_summaries=["s1", "s2"],
        chunk_spec_notes=["n1", "n2"],
    )
    # The dummy synthesis harness returns a valid narrative dict; on any failure
    # it would be None (the workflow then concatenates). Either shape is valid.
    assert result is None or set(result) == {"summary", "spec_compliance_notes"}


def test_run_reraises_unrelated_workflow_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import ApplicationError

    monkeypatch.setattr("code_review_agent.agent._code_review_temporal_enabled", lambda: True)
    monkeypatch.setattr(
        "code_review_agent.temporal.worker.start_code_review_temporal_worker_thread",
        lambda: True,
    )

    def _fail(payload, **kw):
        raise WorkflowFailureError(cause=ApplicationError("boom", type="SomethingElse"))

    monkeypatch.setattr(
        "code_review_agent.temporal.start_workflow.execute_code_review_workflow_sync", _fail
    )
    # A non-marker workflow failure is not a review verdict — it propagates as-is.
    with pytest.raises(WorkflowFailureError):
        CodeReviewAgent(llm_client=DummyLLMClient()).run(_input())


# ---------------------------------------------------------------------------
# 4. Dispatch: Temporal-first with fallbacks
# ---------------------------------------------------------------------------


def test_run_uses_coordinator_when_temporal_disabled() -> None:
    # Under pytest the gate is off, so run() must go through the coordinator.
    assert _code_review_temporal_enabled() is False
    out = CodeReviewAgent(llm_client=DummyLLMClient()).run(_input())
    assert isinstance(out, CodeReviewOutput)
    assert out.approved is True


def test_run_dispatches_to_temporal_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("code_review_agent.agent._code_review_temporal_enabled", lambda: True)
    monkeypatch.setattr(
        "code_review_agent.temporal.worker.start_code_review_temporal_worker_thread",
        lambda: True,
    )
    canned = {
        "approved": True,
        "issues": [],
        "summary": "durable",
        "spec_compliance_notes": "",
        "suggested_commit_message": "",
    }
    monkeypatch.setattr(
        "code_review_agent.temporal.start_workflow.execute_code_review_workflow_sync",
        lambda payload, **kw: canned,
    )
    out = CodeReviewAgent(llm_client=DummyLLMClient()).run(_input())
    assert out.summary == "durable"
    assert out.approved is True


def test_run_falls_back_to_coordinator_when_worker_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("code_review_agent.agent._code_review_temporal_enabled", lambda: True)
    monkeypatch.setattr(
        "code_review_agent.temporal.worker.start_code_review_temporal_worker_thread",
        lambda: True,
    )

    def _raise(payload, **kw):
        raise RuntimeError("Temporal client not available; is the team's worker running?")

    monkeypatch.setattr(
        "code_review_agent.temporal.start_workflow.execute_code_review_workflow_sync", _raise
    )
    out = CodeReviewAgent(llm_client=DummyLLMClient()).run(_input())
    # Fell back to the in-process coordinator and still produced a verdict.
    assert isinstance(out, CodeReviewOutput)
    assert out.approved is True


def test_run_maps_workflow_unavailable_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import ApplicationError

    monkeypatch.setattr("code_review_agent.agent._code_review_temporal_enabled", lambda: True)
    monkeypatch.setattr(
        "code_review_agent.temporal.worker.start_code_review_temporal_worker_thread",
        lambda: True,
    )

    def _fail(payload, **kw):
        # Nested chain: a map/verify activity raised the marker, so Temporal wraps
        # it under an intermediate (activity) error rather than at the top level.
        app = ApplicationError("infra down", type="CodeReviewUnavailableError")
        activity_wrapper = RuntimeError("activity failed")
        activity_wrapper.__cause__ = app
        raise WorkflowFailureError(cause=activity_wrapper)

    monkeypatch.setattr(
        "code_review_agent.temporal.start_workflow.execute_code_review_workflow_sync", _fail
    )
    with pytest.raises(CodeReviewUnavailableError):
        CodeReviewAgent(llm_client=DummyLLMClient()).run(_input())


def test_reports_review_unavailable_walks_cause_chain() -> None:
    from code_review_agent.agent import _reports_review_unavailable
    from temporalio.exceptions import ApplicationError

    marker = "CodeReviewUnavailableError"
    # Top-level marker (workflow total-failure guard).
    top = ApplicationError("m", type=marker)
    assert _reports_review_unavailable(top, marker) is True
    # Nested two levels deep (activity infra failure).
    nested = RuntimeError("outer")
    nested.__cause__ = ApplicationError("m", type=marker)
    assert _reports_review_unavailable(nested, marker) is True
    # Unrelated failure is not misclassified.
    assert _reports_review_unavailable(RuntimeError("boom"), marker) is False


def test_reports_review_unavailable_terminates_on_cyclic_chain() -> None:
    from code_review_agent.agent import _reports_review_unavailable

    # A pathological cyclic cause chain must not hang the walk: the ``seen`` set
    # (and depth bound) terminate it. Neither node carries the marker, so the
    # result is False — the point is that it returns at all.
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert _reports_review_unavailable(a, "CodeReviewUnavailableError") is False


def test_dispatch_unavailable_is_distinct_from_review_failure() -> None:
    assert issubclass(_TemporalDispatchUnavailable, RuntimeError)
    assert not issubclass(_TemporalDispatchUnavailable, CodeReviewUnavailableError)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_workflow_and_activities_are_registered() -> None:
    assert CodeReviewWorkflow in WORKFLOWS
    assert len(ACTIVITIES) == 5
    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "review_chunk_activity" in names
    assert "prepare_review_activity" in names
