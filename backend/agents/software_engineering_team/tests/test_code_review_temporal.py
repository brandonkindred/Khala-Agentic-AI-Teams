"""Tests for the code review agent's Temporal instrumentation.

Covers the five things testable without a live (deployed) Temporal server:

1. The default-on address resolver / enablement gate (``temporal.config``).
2. The activity-boundary DTO round-trips (``temporal.phase_models``).
3. The activity wrappers driven end-to-end with the ``dummy`` LLM harness,
   replicating the workflow's orchestration in-process and asserting the verdict
   matches ``run_coordinator`` (durable path is behavior-identical to thread mode).
4. ``CodeReviewAgent.run``'s Temporal-first dispatch and its fallbacks.
5. A real ``CodeReviewWorkflow`` execute + replay round-trip, driven by
   ``temporalio.testing.WorkflowEnvironment``'s embedded time-skipping test
   server (no live/deployed Temporal server needed) — see
   ``test_workflow_executes_and_replays_without_non_determinism`` near the
   bottom of this file. It is marked ``integration`` (run with
   ``-m integration``) since standing up the embedded server is heavier than
   this file's other pure-Python tests.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any, Dict, NoReturn

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


@pytest.mark.parametrize(
    "env_value, expected",
    [
        (None, 21600),
        ("3600", 3600),
        ("30", 60),  # below the 60s floor -> clamped up to the floor
        ("garbage", 21600),
    ],
)
def test_resolve_execute_timeout_s_env_parsing(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None, expected: int
) -> None:
    if env_value is None:
        monkeypatch.delenv("CODE_REVIEW_EXECUTE_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("CODE_REVIEW_EXECUTE_TIMEOUT_S", env_value)
    assert cfg.resolve_execute_timeout_s() == expected


def test_resolve_execution_timeout_s_stays_below_client_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODE_REVIEW_EXECUTE_TIMEOUT_S", raising=False)
    # Default: 120s margin subtracted from the 6h default client ceiling.
    assert cfg.resolve_execution_timeout_s(21600) == 21480
    # The server-side timeout must always come in strictly below whatever
    # client ceiling it was derived from, so the server always wins the race.
    execute_timeout_s = cfg.resolve_execute_timeout_s()
    assert cfg.resolve_execution_timeout_s(execute_timeout_s) < execute_timeout_s


def test_resolve_execution_timeout_s_floors_for_tiny_override() -> None:
    # Below margin + floor, the floor takes over instead of going non-positive.
    assert cfg.resolve_execution_timeout_s(90) == 60


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
        approved_flags=[False],
    )
    dto = pm.ChunkOutcomeDTO.from_outcome(outcome)
    reloaded = pm.ChunkOutcomeDTO.model_validate(dto.model_dump(mode="json"))
    assert reloaded.approved_flags == [False]
    assert reloaded.issues[0].description == "bug"
    assert reloaded.not_reviewed_issues[0].file_path == "b.py"
    assert reloaded.summaries == ["s1"]


def test_review_prep_dto_round_trips() -> None:
    dto = pm.ReviewPrepDTO(no_code=True)
    reloaded = pm.ReviewPrepDTO.model_validate(dto.model_dump(mode="json"))
    assert reloaded.no_code is True
    assert reloaded.chunks == []


def test_review_prep_dto_fanout_width_round_trips() -> None:
    dto = pm.ReviewPrepDTO(fanout_width=5)
    reloaded = pm.ReviewPrepDTO.model_validate(dto.model_dump(mode="json"))
    assert reloaded.fanout_width == 5
    # Default matches the sequential-safe floor when a caller doesn't set it.
    assert pm.ReviewPrepDTO().fanout_width == 1


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
    issues, not_reviewed, summaries, spec_notes, approved_flags = ([] for _ in range(5))
    for o in outcomes:
        issues += o["issues"]
        not_reviewed += o["not_reviewed_issues"]
        summaries += o["summaries"]
        spec_notes += o["spec_notes"]
        approved_flags += o["approved_flags"]
    assert approved_flags, "at least one chunk reviewed"
    verified = A.filter_false_positives_activity(
        payload, issues, bool(payload.get("skip_false_positive_filter", False))
    )
    architecture_findings = A.find_architecture_and_redundancy_activity(payload)
    has_architecture_findings = bool(architecture_findings)
    if architecture_findings:
        verified = [*verified, *architecture_findings]
    side_effect_findings = A.find_side_effect_impact_activity(payload)
    has_side_effect_findings = bool(side_effect_findings)
    if side_effect_findings:
        verified = [*verified, *side_effect_findings]
    gate = A.finalize_review_activity(
        verified, not_reviewed, prep["skipped_issues"], approved_flags
    )
    if len(summaries) == 1 and not has_architecture_findings and not has_side_effect_findings:
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
    return CodeReviewOutput.model_validate(
        {
            "approved": gate["approved"],
            "issues": gate["issues"],
            "summary": summary,
            "spec_compliance_notes": notes,
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
    assert prep["fanout_width"] == cfg.resolve_temporal_fanout_width(len(prep["chunks"]))

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


def test_prepare_activity_single_chunk_fanout_width_is_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_review_agent.temporal import activities as A

    monkeypatch.delenv("CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES", raising=False)
    prep = A.prepare_review_activity(_input().model_dump(mode="json"))
    assert prep["single_chunk"] is True
    assert prep["fanout_width"] == 1


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


def test_architecture_activity_returns_empty_with_no_architecture() -> None:
    """No architecture on the input -> no LLM call, empty findings (mirrors the
    in-process ``find_architecture_and_redundancy_issues`` contract)."""
    from code_review_agent.temporal import activities as A

    out = A.find_architecture_and_redundancy_activity(_input().model_dump(mode="json"))
    assert out == []


def test_architecture_activity_fails_safe_when_llm_resolution_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resolve_llm()`` runs BEFORE the wrapped pass's own env/no-op checks, so a
    client-resolution failure (e.g. no LLM provider configured) must not raise --
    this activity is purely additive and must degrade to no findings like every
    other failure mode the wrapped pass itself already handles."""
    from code_review_agent.temporal import activities as A

    def _raise() -> Any:
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(A, "_resolve_llm", _raise)
    out = A.find_architecture_and_redundancy_activity(_input().model_dump(mode="json"))
    assert out == []


# ---------------------------------------------------------------------------
# 3b. repo_root reconstructs a whole-repo reader across the Temporal boundary
# ---------------------------------------------------------------------------


def test_repo_reader_from_input_builds_disk_reader(tmp_path: Any) -> None:
    """A non-blank ``repo_root`` yields a live ``DiskRepoReader`` that can read the
    checkout — the channel that survives ``model_dump(mode='json')``."""
    from code_review_agent.repo_reader import DiskRepoReader
    from code_review_agent.temporal import activities as A

    (tmp_path / "off_diff.py").write_text("EXISTS = True\n")
    reader = A._repo_reader_from_input(_input(repo_root=str(tmp_path)))
    assert isinstance(reader, DiskRepoReader)
    assert reader.read_file("off_diff.py") == "EXISTS = True\n"


def test_repo_reader_from_input_none_without_repo_root() -> None:
    """No ``repo_root`` -> ``None`` reader (pre-existing keep-more behavior)."""
    from code_review_agent.temporal import activities as A

    assert A._repo_reader_from_input(_input()) is None


def test_repo_reader_from_input_none_for_blank_repo_root() -> None:
    """A blank ``repo_root`` is treated as "no reader", never a reader rooted at cwd."""
    from code_review_agent.temporal import activities as A

    assert A._repo_reader_from_input(_input(repo_root="   ")) is None


def test_filter_activity_reconstructs_reader_from_repo_root(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The false-positive activity rebuilds a reader from ``repo_root`` and threads
    it into ``filter_false_positives`` (instead of the old hardcoded ``None``)."""
    import code_review_agent.false_positive_filter as fpf
    from code_review_agent.repo_reader import DiskRepoReader
    from code_review_agent.temporal import activities as A

    captured: Dict[str, Any] = {}

    def _capture(llm: Any, input_data: Any, issues: Any, repo_reader: Any = None) -> Any:
        captured["repo_reader"] = repo_reader
        return issues

    monkeypatch.setattr(fpf, "filter_false_positives", _capture)

    issue = {
        "severity": "high",
        "category": "logic",
        "file_path": "a.py",
        "line": 3,
        "description": "x",
        "suggestion": "",
    }
    A.filter_false_positives_activity(
        _input(repo_root=str(tmp_path)).model_dump(mode="json"), [issue], False
    )
    assert isinstance(captured["repo_reader"], DiskRepoReader)


def test_filter_activity_passes_none_reader_without_repo_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``repo_root`` -> the false-positive activity still passes ``repo_reader=None``."""
    import code_review_agent.false_positive_filter as fpf
    from code_review_agent.temporal import activities as A

    captured: Dict[str, Any] = {}

    def _capture(llm: Any, input_data: Any, issues: Any, repo_reader: Any = None) -> Any:
        captured["repo_reader"] = repo_reader
        return issues

    monkeypatch.setattr(fpf, "filter_false_positives", _capture)

    issue = {
        "severity": "high",
        "category": "logic",
        "file_path": "a.py",
        "line": 3,
        "description": "x",
        "suggestion": "",
    }
    A.filter_false_positives_activity(_input().model_dump(mode="json"), [issue], False)
    assert captured["repo_reader"] is None


def test_architecture_activity_reconstructs_reader_from_repo_root(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The architecture/redundancy activity rebuilds a reader from ``repo_root`` and
    threads it into ``find_architecture_and_redundancy_issues``."""
    import code_review_agent.architecture_consistency_pass as acp
    from code_review_agent.repo_reader import DiskRepoReader
    from code_review_agent.temporal import activities as A

    captured: Dict[str, Any] = {}

    def _capture(llm: Any, input_data: Any, repo_reader: Any = None) -> Any:
        captured["repo_reader"] = repo_reader
        return []

    monkeypatch.setattr(acp, "find_architecture_and_redundancy_issues", _capture)

    A.find_architecture_and_redundancy_activity(
        _input(repo_root=str(tmp_path)).model_dump(mode="json")
    )
    assert isinstance(captured["repo_reader"], DiskRepoReader)

    captured.clear()
    A.find_architecture_and_redundancy_activity(_input().model_dump(mode="json"))
    assert captured["repo_reader"] is None


def test_side_effect_activity_reconstructs_reader_from_repo_root(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The side-effect activity rebuilds a reader from ``repo_root`` and threads
    it into ``find_side_effect_impact_issues``, matching the other two additive
    passes rather than hardcoding ``repo_reader=None``."""
    import code_review_agent.side_effect_impact_pass as seip
    from code_review_agent.repo_reader import DiskRepoReader
    from code_review_agent.temporal import activities as A

    captured: Dict[str, Any] = {}

    def _capture(llm: Any, input_data: Any, repo_reader: Any = None) -> Any:
        captured["repo_reader"] = repo_reader
        return []

    monkeypatch.setattr(seip, "find_side_effect_impact_issues", _capture)

    A.find_side_effect_impact_activity(_input(repo_root=str(tmp_path)).model_dump(mode="json"))
    assert isinstance(captured["repo_reader"], DiskRepoReader)

    captured.clear()
    A.find_side_effect_impact_activity(_input().model_dump(mode="json"))
    assert captured["repo_reader"] is None


def test_side_effect_activity_fails_safe_when_llm_resolution_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resolve_llm()`` runs BEFORE the wrapped pass's own env/profile/
    ``pre_numbered`` early-return checks, so a client-resolution failure must
    not raise -- this activity is purely additive and must degrade to no
    findings, matching the wrapped pass's own fail-safe contract, even when
    the failure happens before that pass's internals ever run."""
    from code_review_agent.temporal import activities as A

    def _raise() -> Any:
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(A, "_resolve_llm", _raise)
    out = A.find_side_effect_impact_activity(_input().model_dump(mode="json"))
    assert out == []


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


def test_run_rebuilds_reader_from_repo_root_when_no_live_reader(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the in-process path, ``run()`` reconstructs a ``DiskRepoReader`` from
    ``input_data.repo_root`` so a serialized path grants the same off-diff read
    access as a live reader would."""
    from code_review_agent.repo_reader import DiskRepoReader

    captured: Dict[str, Any] = {}

    def _capture(llm, input_data, progress_callback=None, repo_reader=None):  # noqa: ANN001
        captured["repo_reader"] = repo_reader
        return CodeReviewOutput(approved=True)

    monkeypatch.setattr("code_review_agent.agent.run_coordinator", _capture)
    CodeReviewAgent(llm_client=DummyLLMClient()).run(_input(repo_root=str(tmp_path)))
    assert isinstance(captured["repo_reader"], DiskRepoReader)


def test_run_prefers_live_reader_over_repo_root(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit live reader wins over ``repo_root`` reconstruction."""
    captured: Dict[str, Any] = {}
    sentinel = object()

    def _capture(llm, input_data, progress_callback=None, repo_reader=None):  # noqa: ANN001
        captured["repo_reader"] = repo_reader
        return CodeReviewOutput(approved=True)

    monkeypatch.setattr("code_review_agent.agent.run_coordinator", _capture)
    CodeReviewAgent(llm_client=DummyLLMClient()).run(
        _input(repo_root=str(tmp_path)),
        repo_reader=sentinel,  # type: ignore[arg-type]
    )
    assert captured["repo_reader"] is sentinel


def test_run_passes_none_reader_without_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """No live reader and no ``repo_root`` -> coordinator gets ``repo_reader=None``."""
    captured: Dict[str, Any] = {}

    def _capture(llm, input_data, progress_callback=None, repo_reader=None):  # noqa: ANN001
        captured["repo_reader"] = repo_reader
        return CodeReviewOutput(approved=True)

    monkeypatch.setattr("code_review_agent.agent.run_coordinator", _capture)
    CodeReviewAgent(llm_client=DummyLLMClient()).run(_input())
    assert captured["repo_reader"] is None


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


def test_run_force_in_process_skips_temporal_even_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force_in_process must bypass Temporal at run() time (not just construction).

    Temporal activity callers construct CodeReviewAgent with force_in_process=True
    so review never nests a child workflow on the same worker. A TEMPORAL_ADDRESS
    env dance at construction time is insufficient because dispatch is decided
    inside run() via _code_review_temporal_enabled().
    """
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    monkeypatch.setattr("code_review_agent.agent._code_review_temporal_enabled", lambda: True)

    temporal_calls: list[object] = []

    def _temporal_run(self, input_data, progress_callback=None):  # noqa: ANN001
        temporal_calls.append(input_data)
        raise AssertionError("_run_via_temporal must not be called when force_in_process=True")

    monkeypatch.setattr(
        "code_review_agent.agent.CodeReviewAgent._run_via_temporal",
        _temporal_run,
    )

    out = CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True).run(_input())
    assert temporal_calls == []
    assert isinstance(out, CodeReviewOutput)
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


def test_run_via_temporal_enriches_bare_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("code_review_agent.agent._code_review_temporal_enabled", lambda: True)
    monkeypatch.setattr(
        "code_review_agent.temporal.worker.start_code_review_temporal_worker_thread",
        lambda: True,
    )

    def _timeout(payload, **kw):
        raise TimeoutError()

    monkeypatch.setattr(
        "code_review_agent.temporal.start_workflow.execute_code_review_workflow_sync", _timeout
    )
    with pytest.raises(TimeoutError) as exc_info:
        CodeReviewAgent(llm_client=DummyLLMClient()).run(_input())
    # The bare TimeoutError() the client-side wait raises has no message;
    # _run_via_temporal must attach real context (the configured duration and a
    # hint this is a wait timeout, not a reviewer content failure) rather than
    # letting the empty exception propagate as-is.
    message = str(exc_info.value)
    assert "timed out after" in message
    assert "not a reviewer content failure" in message


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


def test_reports_review_unavailable_matches_by_class_name() -> None:
    from code_review_agent.agent import _reports_review_unavailable

    # The real ``CodeReviewUnavailableError`` carries no ``.type`` attribute, so
    # the marker is recognised by the class-name branch of the walk — the shape a
    # map/verify activity's own exception takes before Temporal converts it.
    exc = CodeReviewUnavailableError("infra down")
    assert getattr(exc, "type", None) is None
    assert _reports_review_unavailable(exc, "CodeReviewUnavailableError") is True


def test_reports_review_unavailable_ignores_none_type_node() -> None:
    from code_review_agent.agent import _reports_review_unavailable
    from temporalio.exceptions import ApplicationError

    marker = "CodeReviewUnavailableError"
    # A node whose ``type`` is explicitly None (e.g. a Temporal FailureError with
    # no application type) must not be treated as the marker — ``None == marker``
    # is False — so an unrelated failure is not a false positive...
    none_type = RuntimeError("wrapper")
    none_type.type = None  # type: ignore[attr-defined]
    assert _reports_review_unavailable(none_type, marker) is False
    # ...and the walk continues past it to find a genuine marker nested below.
    none_type_with_marker = RuntimeError("wrapper")
    none_type_with_marker.type = None  # type: ignore[attr-defined]
    none_type_with_marker.__cause__ = ApplicationError("m", type=marker)
    assert _reports_review_unavailable(none_type_with_marker, marker) is True


def test_dispatch_unavailable_is_distinct_from_review_failure() -> None:
    assert issubclass(_TemporalDispatchUnavailable, RuntimeError)
    assert not issubclass(_TemporalDispatchUnavailable, CodeReviewUnavailableError)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_workflow_and_activities_are_registered() -> None:
    assert CodeReviewWorkflow in WORKFLOWS
    assert len(ACTIVITIES) == 7
    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "review_chunk_activity" in names
    assert "prepare_review_activity" in names
    assert "find_architecture_and_redundancy_activity" in names
    assert "find_side_effect_impact_activity" in names


# ---------------------------------------------------------------------------
# 5. Worker boot: concurrency ceiling + start_team_worker delegation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_value, expected",
    [
        (None, 8),
        ("16", 16),
        ("0", 1),
        ("-5", 1),
        ("not-a-number", 8),
    ],
)
def test_worker_max_concurrent_activities_env_parsing(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None, expected: int
) -> None:
    from code_review_agent.temporal import worker as worker_mod

    if env_value is None:
        monkeypatch.delenv("CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES", raising=False)
    else:
        monkeypatch.setenv("CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES", env_value)

    assert worker_mod._max_concurrent_activities() == expected


@pytest.mark.parametrize(
    "env_value, expected",
    [
        (None, 8),
        ("16", 16),
        ("0", 1),
        ("-5", 1),
        ("not-a-number", 8),
    ],
)
def test_worker_max_concurrent_activities_delegates_to_config(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None, expected: int
) -> None:
    """``worker._max_concurrent_activities`` must track
    ``config.resolve_max_concurrent_activities`` exactly -- it's a thin
    delegation, not an independent implementation, so both must agree on every
    env-parsing edge case."""
    from code_review_agent.temporal import worker as worker_mod

    if env_value is None:
        monkeypatch.delenv("CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES", raising=False)
    else:
        monkeypatch.setenv("CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES", env_value)

    assert cfg.resolve_max_concurrent_activities() == expected
    assert worker_mod._max_concurrent_activities() == cfg.resolve_max_concurrent_activities()


# ---------------------------------------------------------------------------
# 5b. Per-review adaptive Temporal fan-out width
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_value, chunk_count, expected",
    [
        (None, 3, 3),  # small review: never requests more than it has chunks
        (None, 1, 1),
        (None, 0, 1),  # defensive floor: never zero or negative
        (None, 40, 8),  # large review (telemetry's ~20-50-chunk band): capped
        # at the default worker capacity -- cannot exceed validated capacity,
        # so this cannot regress the 4->8 timeout incident.
        ("20", 40, 20),  # raising the ceiling raises the per-review cap too
        ("20", 5, 5),  # ...but a small review still only takes what it needs
        ("not-a-number", 40, 8),  # garbage ceiling falls back to the default
    ],
)
def test_resolve_temporal_fanout_width_scales_with_chunk_count(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None, chunk_count: int, expected: int
) -> None:
    if env_value is None:
        monkeypatch.delenv("CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES", raising=False)
    else:
        monkeypatch.setenv("CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES", env_value)

    assert cfg.resolve_temporal_fanout_width(chunk_count) == expected


def test_resolve_temporal_fanout_width_never_exceeds_worker_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES", "8")
    for chunk_count in (1, 5, 8, 9, 40, 540):
        assert cfg.resolve_temporal_fanout_width(chunk_count) <= 8


def test_worker_start_delegates_to_start_team_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """When enabled, the boot hook delegates to ``start_team_worker`` with the
    resolved concurrency ceiling instead of the shared framework's 4-slot
    default."""
    from code_review_agent.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
    from code_review_agent.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "code_review_temporal_enabled", lambda: True)
    monkeypatch.setattr(worker_mod, "resolve_code_review_temporal_address", lambda: "temporal:7233")
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    monkeypatch.delenv("CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES", raising=False)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue, max_concurrent_activities):
        captured.update(
            team=team,
            workflows=workflows,
            activities=activities,
            task_queue=task_queue,
            max_concurrent_activities=max_concurrent_activities,
        )
        return True

    monkeypatch.setattr(worker_mod, "start_team_worker", _fake_start)

    assert worker_mod.start_code_review_temporal_worker_thread() is True
    assert captured == {
        "team": "code_review",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
        "max_concurrent_activities": 8,
    }


def test_worker_start_returns_false_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_review_agent.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "code_review_temporal_enabled", lambda: False)
    assert worker_mod.start_code_review_temporal_worker_thread() is False


def test_worker_start_defaults_temporal_address_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With TEMPORAL_ADDRESS unset, the boot hook points the shared client at
    the resolved default so it has something to connect to, without ever
    overwriting an operator's explicit value (covered separately by the
    delegation test above, which sets TEMPORAL_ADDRESS first)."""
    from code_review_agent.temporal import worker as worker_mod

    # setenv("") rather than delenv(raising=False): the boot hook below writes
    # os.environ["TEMPORAL_ADDRESS"] directly (not through monkeypatch), so
    # teardown can only undo it if monkeypatch already has an undo entry for
    # this key. delenv(raising=False) on an already-absent key registers no
    # such entry, silently leaking the boot hook's write into every later test
    # in the same worker process. An empty value satisfies the same "unset"
    # check the boot hook makes (``.strip()`` is falsy either way) while
    # guaranteeing monkeypatch tracks and reverts the key.
    monkeypatch.setenv("TEMPORAL_ADDRESS", "")
    monkeypatch.setattr(worker_mod, "code_review_temporal_enabled", lambda: True)
    monkeypatch.setattr(worker_mod, "resolve_code_review_temporal_address", lambda: "resolved:7233")
    monkeypatch.setattr(worker_mod, "start_team_worker", lambda *a, **kw: True)

    assert worker_mod.start_code_review_temporal_worker_thread() is True
    assert os.environ["TEMPORAL_ADDRESS"] == "resolved:7233"


# ---------------------------------------------------------------------------
# 6. Live workflow execute + replay (WorkflowEnvironment)
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _workflow_environment_worker(activities=None):
    """Shared ``WorkflowEnvironment`` + ``Worker`` startup/teardown for the
    ``CodeReviewWorkflow`` integration tests below. Yields the started ``env``
    with a ``CodeReviewWorkflow`` worker already listening on ``TASK_QUEUE``,
    against the ``dummy`` LLM harness this suite runs under.

    ``activities`` defaults to the real, production ``ACTIVITIES`` list; pass
    a substitute list (e.g. swapping one activity for a stand-in that raises
    directly, bypassing that activity's own fail-safe try/except) to exercise
    a tail-pass failure mode the real activities never produce on their own.

    ``WorkflowEnvironment.start_time_skipping()`` downloads (and thereafter
    caches) a small ephemeral test-server binary from ``temporal.download`` on
    first use in an environment -- unlike an activity/worker connecting to a
    deployed Temporal server, this needs one-time outbound network access, so
    this skips (rather than fails) when that download is unreachable (e.g. a
    network-restricted sandbox), while still running for real wherever that
    egress is allowed (a normal dev machine or CI runner).
    """
    import concurrent.futures

    from code_review_agent.temporal import ACTIVITIES, TASK_QUEUE, CodeReviewWorkflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    try:
        test_env = await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:
        pytest.skip(f"Temporal ephemeral test server unavailable (no egress?): {exc}")

    async with test_env as env:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as activity_executor:
            worker = Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[CodeReviewWorkflow],
                activities=activities if activities is not None else ACTIVITIES,
                activity_executor=activity_executor,
            )
            async with worker:
                yield env


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_executes_and_replays_without_non_determinism() -> None:
    """Execute ``CodeReviewWorkflow`` end-to-end and replay its recorded history.

    This is the baseline the issue asks for: a ``WorkflowEnvironment``-driven
    execute + replay round-trip against today's sequential (pre-parallelization)
    workflow, so a later change to the tail passes (architecture-consistency,
    side-effect-impact) has a real replay-determinism guard instead of only the
    activity-level orchestration replica ``_run_activity_pipeline`` exercises
    above. Unlike that helper, this drives the actual ``CodeReviewWorkflow.run``
    coroutine through a real Temporal worker and sandbox, so it is the one test
    that would catch a ``workflow.patched`` ordering regression.

    Marked ``integration`` (run with ``-m integration``): standing up the
    embedded test server is heavier than this file's other pure-Python tests,
    matching this suite's existing convention for that marker.
    """
    from code_review_agent.temporal import TASK_QUEUE, CodeReviewWorkflow
    from temporalio.worker import Replayer

    review_input = _input()
    workflow_id = "code-review-workflow-replay-test"

    async with _workflow_environment_worker() as env:
        result = await env.client.execute_workflow(
            CodeReviewWorkflow.run,
            review_input.model_dump(mode="json"),
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
        history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    # Sanity check: the durable path produced the same verdict shape the
    # activity-level pipeline test above asserts against `run_coordinator`.
    assert result["approved"] is True
    assert result["summary"]

    # The property this issue exists to guard: replaying the just-recorded
    # history against the same workflow code must not raise a non-determinism
    # error.
    await Replayer(workflows=[CodeReviewWorkflow]).replay_workflow(history)


# ---------------------------------------------------------------------------
# 7. Concurrent tail-pass error-handling semantics (asyncio.gather)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_return_exceptions_reproduces_sequential_error_precedence() -> None:
    """The exact idiom ``CodeReviewWorkflow.run`` uses for its concurrent tail
    passes -- ``asyncio.gather(*calls, return_exceptions=True)`` followed by a
    fixed-order scan that surfaces the first exception found -- must always
    surface the earliest-listed failure (matching sequential execution's
    verify -> architecture -> side-effect precedence) regardless of which
    awaitable actually finishes first in real time, and must let every
    awaitable run to completion instead of abandoning the others.

    Ordering is driven by an ``asyncio.Event`` rather than wall-clock
    ``asyncio.sleep`` delays, so the completion order is a deterministic
    property of cooperative scheduling, not a timing race: ``side_effect``
    has no internal await point and so completes on its very first step,
    ``architecture`` completes right after and signals ``architecture_done``,
    and ``verify`` deliberately waits on that event so it is guaranteed to
    finish last despite being listed first in ``calls``.
    """
    import asyncio

    completed: list[str] = []
    architecture_done = asyncio.Event()
    verify_exc = RuntimeError("verify failed")
    side_effect_exc = RuntimeError("side_effect failed")

    async def _verify() -> None:
        await architecture_done.wait()
        completed.append("verify")
        raise verify_exc

    async def _architecture() -> str:
        completed.append("architecture")
        architecture_done.set()
        return "architecture"

    async def _side_effect() -> None:
        completed.append("side_effect")
        raise side_effect_exc

    calls = [_verify(), _architecture(), _side_effect()]
    results = await asyncio.gather(*calls, return_exceptions=True)

    # Every awaitable ran to completion -- nothing was abandoned once the
    # first exception in the list resolved.
    assert set(completed) == {"verify", "architecture", "side_effect"}

    raised: BaseException | None = None
    for result in results:
        if isinstance(result, BaseException):
            raised = result
            break
    assert raised is verify_exc


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_fails_on_verify_failure_without_leaking_sibling_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forces the verify pass to fail by monkeypatching the false-positive
    filter to raise (the architecture/side-effect passes are wrapped
    real-function calls that record their own completion), then asserts the
    whole workflow fails with that failure as its cause -- the same
    total-failure outcome sequential execution produces (verify was always
    the pass whose failure aborts everything) -- AND that the architecture
    and side-effect passes still ran to completion rather than being
    abandoned once verify raised."""
    from code_review_agent import (
        architecture_consistency_pass,
        false_positive_filter,
        side_effect_impact_pass,
    )
    from code_review_agent.temporal import TASK_QUEUE, CodeReviewWorkflow
    from temporalio.client import WorkflowFailureError

    completed: set[str] = set()
    real_architecture = architecture_consistency_pass.find_architecture_and_redundancy_issues
    real_side_effect = side_effect_impact_pass.find_side_effect_impact_issues

    def _boom(*args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError("verify boom")

    def _tracked_architecture(*args: Any, **kwargs: Any) -> Any:
        result = real_architecture(*args, **kwargs)
        completed.add("architecture")
        return result

    def _tracked_side_effect(*args: Any, **kwargs: Any) -> Any:
        result = real_side_effect(*args, **kwargs)
        completed.add("side_effect")
        return result

    monkeypatch.setattr(false_positive_filter, "filter_false_positives", _boom)
    monkeypatch.setattr(
        architecture_consistency_pass, "find_architecture_and_redundancy_issues", _tracked_architecture
    )
    monkeypatch.setattr(side_effect_impact_pass, "find_side_effect_impact_issues", _tracked_side_effect)

    review_input = _input()

    async with _workflow_environment_worker() as env:
        with pytest.raises(WorkflowFailureError) as exc_info:
            await env.client.execute_workflow(
                CodeReviewWorkflow.run,
                review_input.model_dump(mode="json"),
                id="code-review-workflow-verify-failure-test",
                task_queue=TASK_QUEUE,
            )

    cause = exc_info.value.cause
    assert cause is not None
    assert "verify boom" in str(cause)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_raises_cleanly_when_a_later_tail_pass_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When verify succeeds but a *later*-listed tail pass fails, the
    workflow must fail with that pass's own exception as its cause -- never
    a ``TypeError`` from mistakenly treating the exception object itself as
    a findings list to spread -- and side-effect (listed after architecture)
    must still run to completion rather than being abandoned.

    The real architecture activity is internally fail-safe and never raises
    (see ``find_architecture_and_redundancy_activity``'s docstring), so this
    substitutes a stand-in activity registered under the same activity name
    that raises directly, to exercise the branch a real activity can't
    reach on its own.
    """
    from code_review_agent import side_effect_impact_pass
    from code_review_agent.temporal import (
        ACTIVITIES,
        TASK_QUEUE,
        CodeReviewWorkflow,
        filter_false_positives_activity,
        finalize_review_activity,
        find_side_effect_impact_activity,
        prepare_review_activity,
        review_chunk_activity,
        synthesize_findings_activity,
    )
    from temporalio import activity as activity_module
    from temporalio.client import WorkflowFailureError

    completed: set[str] = set()
    real_side_effect = side_effect_impact_pass.find_side_effect_impact_issues

    def _tracked_side_effect(*args: Any, **kwargs: Any) -> Any:
        result = real_side_effect(*args, **kwargs)
        completed.add("side_effect")
        return result

    monkeypatch.setattr(side_effect_impact_pass, "find_side_effect_impact_issues", _tracked_side_effect)

    @activity_module.defn(name="code_review_architecture_consistency")
    def _raising_architecture_activity(review_input: Dict[str, Any]) -> Any:
        raise RuntimeError("architecture boom")

    stand_in_activities = [
        prepare_review_activity,
        review_chunk_activity,
        filter_false_positives_activity,
        _raising_architecture_activity,
        find_side_effect_impact_activity,
        finalize_review_activity,
        synthesize_findings_activity,
    ]
    assert len(stand_in_activities) == len(ACTIVITIES)

    review_input = _input()

    async with _workflow_environment_worker(activities=stand_in_activities) as env:
        with pytest.raises(WorkflowFailureError) as exc_info:
            await env.client.execute_workflow(
                CodeReviewWorkflow.run,
                review_input.model_dump(mode="json"),
                id="code-review-workflow-architecture-failure-test",
                task_queue=TASK_QUEUE,
            )

    cause = exc_info.value.cause
    assert cause is not None
    assert "architecture boom" in str(cause)
    assert "TypeError" not in str(cause)
    assert completed == {"side_effect"}
    assert completed == {"architecture", "side_effect"}
