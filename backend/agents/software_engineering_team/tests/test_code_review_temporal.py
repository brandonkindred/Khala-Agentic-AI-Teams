"""Tests for the code review agent's Temporal instrumentation.

Covers the seven things testable without a live (deployed) Temporal server:

1. The default-on address resolver / enablement gate (``temporal.config``).
2. The activity-boundary DTO round-trips (``temporal.phase_models``).
3. The activity wrappers driven end-to-end with the ``dummy`` LLM harness,
   replicating the workflow's orchestration in-process and asserting the verdict
   matches ``run_coordinator`` (durable path is behavior-identical to thread mode),
   plus ``repo_root`` reader reconstruction across the Temporal boundary.
4. ``CodeReviewAgent.run``'s Temporal-first dispatch and its fallbacks.
5. Worker registration / boot (concurrency ceiling, ``start_team_worker``
   delegation) and per-review adaptive Temporal fan-out width.
6. Real ``CodeReviewWorkflow`` execute + replay round-trips driven by
   ``temporalio.testing.WorkflowEnvironment``'s embedded time-skipping test
   server (no live/deployed Temporal server needed, though the ephemeral test
   server binary itself requires a one-time network fetch — see
   ``test_workflow_executes_and_replays_without_non_determinism`` and related
   tests near the bottom of this file).
7. Concurrent map-phase and tail-pass gather / re-raise semantics: the
   pure-async unit test
   ``test_gather_return_exceptions_reproduces_sequential_error_precedence``,
   plus ``WorkflowEnvironment`` tests for map-chunk sibling completion
   (``test_workflow_fails_on_map_chunk_failure_without_abandoning_siblings``),
   verify-failure precedence, concurrent scheduling, out-of-order completion,
   partial failure, and pre-migration sequential-history replay. The
   ``WorkflowEnvironment`` tests are marked ``integration`` (run with
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


def _error_chain_text(exc: BaseException) -> str:
    """Return messages from Temporal's nested ``cause`` wrappers."""
    messages: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        messages.append(str(current))
        current = getattr(current, "cause", None) or current.__cause__
    return " <- ".join(messages)


# ---------------------------------------------------------------------------
# 1. Resolver + enablement gate
# ---------------------------------------------------------------------------


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("TEMPORAL_ADDRESS", "LLM_PROVIDER"):
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


def test_enabled_is_true_under_pytest_when_env_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pytest alone must not disable Temporal; with env cleared the default
    # address resolves and the gate returns True.
    _clear_env(monkeypatch)
    assert cfg.code_review_temporal_enabled() is True


def test_enabled_is_false_when_address_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("TEMPORAL_ADDRESS", "none")
    assert cfg.code_review_temporal_enabled() is False


def test_dummy_provider_does_not_disable_temporal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LLM_PROVIDER=dummy selects the no-LLM harness only; it must not force
    # the code-review Temporal gate off when an address resolves.
    _clear_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    assert cfg.code_review_temporal_enabled() is True


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
    """Drive the activities sequentially in-process to check coordinator parity.

    Synchronous approximation of ``CodeReviewWorkflow.run`` used only to assert
    that the activity pipeline matches ``run_coordinator``'s final verdict. It
    does NOT replicate the workflow's concurrent tail-pass ``asyncio.gather``
    or its deterministic ``return_exceptions`` error precedence — see
    ``test_gather_return_exceptions_reproduces_sequential_error_precedence``
    and ``test_workflow_gathers_tail_pass_activities_concurrently`` for those
    properties. Call order here does not affect the merged verdict.
    """
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
    merged = A.find_architecture_and_side_effect_activity(payload)
    architecture_findings = merged["architecture_findings"]
    has_architecture_findings = bool(architecture_findings)
    if architecture_findings:
        verified = [*verified, *architecture_findings]
    side_effect_findings = merged["side_effect_findings"]
    has_side_effect_findings = bool(side_effect_findings)
    if side_effect_findings:
        verified = [*verified, *side_effect_findings]
    verified = A.consolidate_side_effect_issues_activity(payload, verified)
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


def test_architecture_activity_fails_safe_on_invalid_review_input() -> None:
    """``CodeReviewInput.model_validate`` runs inside the fail-safe handler, so a
    malformed payload must degrade to ``[]`` rather than raise and fail the
    durable workflow (matching the activity's documented never-raises contract)."""
    from code_review_agent.temporal import activities as A

    # ``files={}`` is rejected by CodeReviewInput's construction validator.
    out = A.find_architecture_and_redundancy_activity({"files": {}})
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


def test_side_effect_activity_fails_safe_on_invalid_review_input() -> None:
    """``CodeReviewInput.model_validate`` runs inside the fail-safe handler, so a
    malformed payload must degrade to ``[]`` rather than raise and fail the
    durable workflow (matching the activity's documented never-raises contract)."""
    from code_review_agent.temporal import activities as A

    out = A.find_side_effect_impact_activity({"files": {}})
    assert out == []


def test_merged_activity_fails_safe_when_llm_resolution_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resolve_llm()`` runs BEFORE the wrapped merged pass's own env/profile
    early-return checks, so a client-resolution failure must not raise -- this
    activity is purely additive and must degrade to empty findings for both
    halves, matching the wrapped pass's own fail-safe contract."""
    from code_review_agent.temporal import activities as A

    def _raise() -> Any:
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(A, "_resolve_llm", _raise)
    out = A.find_architecture_and_side_effect_activity(_input().model_dump(mode="json"))
    assert out == {"architecture_findings": [], "side_effect_findings": []}


def test_merged_activity_fails_safe_on_invalid_review_input() -> None:
    """``CodeReviewInput.model_validate`` runs inside the fail-safe handler, so a
    malformed payload must degrade to empty findings for both halves rather than
    raise and fail the durable workflow (matching the activity's documented
    never-raises contract)."""
    from code_review_agent.temporal import activities as A

    out = A.find_architecture_and_side_effect_activity({"files": {}})
    assert out == {"architecture_findings": [], "side_effect_findings": []}


def test_merged_activity_reconstructs_reader_from_repo_root(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merged activity rebuilds a reader from ``repo_root`` and threads it
    into ``find_architecture_and_side_effect_issues``, matching the other
    additive-pass activities rather than hardcoding ``repo_reader=None``."""
    import code_review_agent.merged_architecture_side_effect_pass as masep
    from code_review_agent.repo_reader import DiskRepoReader
    from code_review_agent.temporal import activities as A

    captured: Dict[str, Any] = {}

    def _capture(llm: Any, input_data: Any, repo_reader: Any = None, index: Any = None) -> Any:
        captured["repo_reader"] = repo_reader
        return [], []

    monkeypatch.setattr(masep, "find_architecture_and_side_effect_issues", _capture)

    A.find_architecture_and_side_effect_activity(
        _input(repo_root=str(tmp_path)).model_dump(mode="json")
    )
    assert isinstance(captured["repo_reader"], DiskRepoReader)

    captured.clear()
    A.find_architecture_and_side_effect_activity(_input().model_dump(mode="json"))
    assert captured["repo_reader"] is None


def test_merged_activity_splits_findings_into_two_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The merged pass's ``(architecture_findings, side_effect_findings)`` tuple
    is returned as two separately-keyed, JSON-native lists -- the shape the
    workflow's merged-pass branch (see ``workflows.py``) unpacks back into the
    two existing finding categories."""
    import code_review_agent.merged_architecture_side_effect_pass as masep
    from code_review_agent.models import CodeReviewIssue
    from code_review_agent.temporal import activities as A

    architecture_issue = CodeReviewIssue(
        severity="medium",
        category="architecture",
        file_path="a.py",
        line=1,
        description="violates layering",
        suggestion="",
    )
    side_effect_issue = CodeReviewIssue(
        severity="high",
        category="side-effects",
        file_path="b.py",
        line=2,
        description="breaks a caller",
        suggestion="",
    )

    def _fake(llm: Any, input_data: Any, repo_reader: Any = None, index: Any = None) -> Any:
        return [architecture_issue], [side_effect_issue]

    monkeypatch.setattr(masep, "find_architecture_and_side_effect_issues", _fake)

    out = A.find_architecture_and_side_effect_activity(_input().model_dump(mode="json"))
    assert [i["description"] for i in out["architecture_findings"]] == ["violates layering"]
    assert [i["description"] for i in out["side_effect_findings"]] == ["breaks a caller"]


def test_consolidation_activity_disabled_passthrough_and_fails_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled toggle returns issues unchanged; consolidation errors keep them."""
    from code_review_agent.temporal import activities as A

    issues = [
        {
            "severity": "high",
            "category": "side-effects",
            "file_path": "a.py",
            "line": 1,
            "description": "caller breaks",
            "suggestion": "",
        }
    ]
    payload = _input().model_dump(mode="json")

    monkeypatch.setenv("CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION", "false")
    assert A.consolidate_side_effect_issues_activity(payload, issues) == issues

    monkeypatch.setenv("CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION", "true")

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("index boom")

    monkeypatch.setattr("code_review_agent.false_positive_filter.CodebaseIndex.from_input", _boom)
    assert A.consolidate_side_effect_issues_activity(payload, issues) == issues


def test_consolidation_activity_enabled_merges_same_function_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled toggle merges same-function side-effects; non-side-effects pass through."""
    from code_review_agent.temporal import activities as A

    content = "def foo():\n    x = 1\n    return x\n"
    inp = _input(code=f"### a.py ###\n{content}")
    payload = inp.model_dump(mode="json")
    issues = [
        {
            "severity": "high",
            "category": "side-effects",
            "file_path": "a.py",
            "line": 2,
            "description": "foo mutates shared state",
            "suggestion": "",
        },
        {
            "severity": "medium",
            "category": "side-effects",
            "file_path": "a.py",
            "line": 3,
            "description": "foo return type changed",
            "suggestion": "",
        },
        {
            "severity": "low",
            "category": "documentation",
            "file_path": "a.py",
            "line": 1,
            "description": "stale docstring",
            "suggestion": "",
        },
    ]

    monkeypatch.setenv("CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION", "true")
    result = A.consolidate_side_effect_issues_activity(payload, issues)
    side_effects = [i for i in result if i["category"] == "side-effects"]
    doc_issues = [i for i in result if i["category"] == "documentation"]
    assert len(side_effects) == 1, "two same-function findings should merge into one"
    assert "foo mutates shared state" in side_effects[0]["description"]
    assert "foo return type changed" in side_effects[0]["description"]
    assert len(doc_issues) == 1, "non-side-effects pass through unchanged"
    assert doc_issues[0]["description"] == "stale docstring"


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


def test_finalize_activity_caps_issues_at_max() -> None:
    from code_review_agent.coordinator import MAX_CODE_REVIEW_ISSUES
    from code_review_agent.temporal import activities as A

    findings = [
        {
            "severity": "low",
            "category": "naming",
            "file_path": "a.py",
            "line": i,
            "description": f"nit-{i}",
            "suggestion": "",
        }
        for i in range(1, MAX_CODE_REVIEW_ISSUES + 6)
    ]
    # One critical last — severity-first cap must keep it and still reject.
    findings.append(
        {
            "severity": "critical",
            "category": "security",
            "file_path": "a.py",
            "line": 999,
            "description": "critical-keep",
            "suggestion": "fix",
        }
    )
    gate = A.finalize_review_activity(findings, [], [], [True])
    assert len(gate["issues"]) == MAX_CODE_REVIEW_ISSUES
    assert gate["issues"][0]["description"] == "critical-keep"
    assert gate["approved"] is False


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


def test_synthesize_activity_fails_safe_when_llm_resolution_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resolve_llm()`` runs before synthesis, so a client-resolution failure
    (e.g. no LLM provider configured) must not raise -- this activity must
    return ``None`` so the workflow falls back to deterministic concatenation,
    matching the fail-safe contract of the sibling architecture / side-effect
    activities."""
    from code_review_agent.temporal import activities as A

    def _raise() -> Any:
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(A, "_resolve_llm", _raise)
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
    out = A.synthesize_findings_activity(
        _input().model_dump(mode="json"),
        approved=False,
        issues=issues,
        chunk_summaries=["s1", "s2"],
        chunk_spec_notes=["n1", "n2"],
    )
    assert out is None


def test_synthesize_activity_fails_safe_on_invalid_review_input() -> None:
    """``CodeReviewInput.model_validate`` is inside the fail-safe handler, so a
    malformed payload must return ``None`` (workflow falls back to deterministic
    concatenation) rather than raise out of the activity."""
    from code_review_agent.temporal import activities as A

    out = A.synthesize_findings_activity(
        {"files": {}},
        approved=False,
        issues=[],
        chunk_summaries=["s1", "s2"],
        chunk_spec_notes=["n1", "n2"],
    )
    assert out is None


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


def test_run_uses_coordinator_when_force_in_process() -> None:
    # force_in_process bypasses Temporal even when the gate would enable it.
    out = CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True).run(_input())
    assert isinstance(out, CodeReviewOutput)
    assert out.approved is True


def test_run_uses_coordinator_when_address_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A disable-sentinel TEMPORAL_ADDRESS turns the gate off so run() takes the
    # in-process coordinator path without force_in_process.
    monkeypatch.setenv("TEMPORAL_ADDRESS", "none")
    assert _code_review_temporal_enabled() is False
    temporal_calls: list[Any] = []

    def _must_not_dispatch(payload, **kw):  # noqa: ANN001
        temporal_calls.append(payload)
        raise AssertionError("Temporal dispatch must not run when address is disabled")

    monkeypatch.setattr(
        "code_review_agent.temporal.start_workflow.execute_code_review_workflow_sync",
        _must_not_dispatch,
    )
    out = CodeReviewAgent(llm_client=DummyLLMClient()).run(_input())
    assert temporal_calls == []
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
    CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True).run(_input(repo_root=str(tmp_path)))
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
    CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True).run(
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
    CodeReviewAgent(llm_client=DummyLLMClient(), force_in_process=True).run(_input())
    assert captured["repo_reader"] is None


def test_run_dispatches_to_temporal_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the Temporal feature gate is enabled, run() executes the code-review
    workflow synchronously and converts the returned dict into a CodeReviewOutput.
    """
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


def test_run_via_temporal_forwards_repo_root_in_dispatch_payload(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run()``'s Temporal dispatch must carry ``input_data.repo_root`` in the
    serialized payload it sends to ``execute_code_review_workflow_sync`` -- that
    field is the only channel the tail-pass activities have to reconstruct a
    ``DiskRepoReader`` worker-side (``_repo_reader_from_input``), since a live
    ``repo_reader`` object cannot cross the Temporal boundary. This is
    distinct from (and a prerequisite for) the activity-side reconstruction
    tests above, which start from a payload dict that already has
    ``repo_root`` set.
    """
    monkeypatch.setattr("code_review_agent.agent._code_review_temporal_enabled", lambda: True)
    monkeypatch.setattr(
        "code_review_agent.temporal.worker.start_code_review_temporal_worker_thread",
        lambda: True,
    )
    captured: Dict[str, Any] = {}

    def _capture(payload, **kw):  # noqa: ANN001
        captured["payload"] = payload
        return {
            "approved": True,
            "issues": [],
            "summary": "durable",
            "spec_compliance_notes": "",
            "suggested_commit_message": "",
        }

    monkeypatch.setattr(
        "code_review_agent.temporal.start_workflow.execute_code_review_workflow_sync",
        _capture,
    )
    repo_root = str(tmp_path)
    CodeReviewAgent(llm_client=DummyLLMClient()).run(_input(repo_root=repo_root))
    assert captured["payload"]["repo_root"] == repo_root


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
    # Matches every other Temporal-enabled test in this file: if run() ever starts
    # the worker thread before the force_in_process bypass, this stubs it out
    # instead of attempting a real gRPC connection to temporal:7233.
    monkeypatch.setattr(
        "code_review_agent.temporal.worker.start_code_review_temporal_worker_thread",
        lambda: True,
    )

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
    """When Temporal dispatch reports no worker/client available, run() must
    fall back to the in-process coordinator and still return a valid verdict.
    """
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


def test_run_propagates_unrelated_runtime_error_from_temporal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RuntimeError that isn't the "no worker client" condition must not be
    misclassified as dispatch-unavailable and silently downgraded to the
    in-process fallback — it must propagate unchanged so the real failure is
    never hidden.
    """
    monkeypatch.setattr("code_review_agent.agent._code_review_temporal_enabled", lambda: True)
    monkeypatch.setattr(
        "code_review_agent.temporal.worker.start_code_review_temporal_worker_thread",
        lambda: True,
    )

    def _raise(payload, **kw):
        raise RuntimeError("unexpected failure inside workflow dispatch")

    monkeypatch.setattr(
        "code_review_agent.temporal.start_workflow.execute_code_review_workflow_sync", _raise
    )
    with pytest.raises(RuntimeError, match="unexpected failure inside workflow dispatch"):
        CodeReviewAgent(llm_client=DummyLLMClient()).run(_input())


def test_run_falls_back_when_worker_loop_closes_mid_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker can close its loop in the window between ``_await_client``
    returning it and the workflow coroutine being scheduled onto it — raising
    ``RuntimeError("Event loop is closed")`` from
    ``asyncio.run_coroutine_threadsafe`` rather than ``_await_client``'s "not
    available" message. Dispatch never started here either, so this must also
    fall back to the in-process coordinator instead of propagating raw.
    """
    monkeypatch.setattr("code_review_agent.agent._code_review_temporal_enabled", lambda: True)
    monkeypatch.setattr(
        "code_review_agent.temporal.worker.start_code_review_temporal_worker_thread",
        lambda: True,
    )

    def _raise(payload, **kw):
        raise RuntimeError("Event loop is closed")

    monkeypatch.setattr(
        "code_review_agent.temporal.start_workflow.execute_code_review_workflow_sync", _raise
    )
    out = CodeReviewAgent(llm_client=DummyLLMClient()).run(_input())
    assert isinstance(out, CodeReviewOutput)
    assert out.approved is True


def test_run_maps_workflow_unavailable_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``CodeReviewUnavailableError`` marker nested inside a
    ``WorkflowFailureError``'s activity-error cause chain must still be
    recognized and re-raised as ``CodeReviewUnavailableError`` -- not
    misclassified as dispatch-unavailable and silently downgraded.
    """
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
    """A bare ``TimeoutError()`` from the client-side wait carries no message;
    ``_run_via_temporal`` must attach real context (the configured duration and
    a hint this is a wait timeout, not a reviewer content failure) before it
    propagates.
    """
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
    """``_reports_review_unavailable`` finds the marker both at the top level
    and nested via ``__cause__``, and returns False for an unrelated failure.
    """
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

    # The ``seen`` set terminates cyclic cause chains; this test verifies it
    # returns for a 2-node cycle. Neither node carries the marker, so the
    # result is False — the point is that it returns at all.
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert _reports_review_unavailable(a, "CodeReviewUnavailableError") is False


def test_reports_review_unavailable_terminates_on_depth_bound() -> None:
    from code_review_agent.agent import _MAX_CAUSE_DEPTH, _reports_review_unavailable
    from temporalio.exceptions import ApplicationError

    marker = "CodeReviewUnavailableError"
    # Acyclic chain longer than ``_MAX_CAUSE_DEPTH`` with the marker only past
    # the bound — this exercises the depth limit (not ``seen``). The walk
    # visits the wrappers and stops before reaching the marker.
    node: BaseException = ApplicationError("m", type=marker)
    for i in range(_MAX_CAUSE_DEPTH):
        wrapper = RuntimeError(f"wrap-{i}")
        wrapper.__cause__ = node
        node = wrapper
    assert _reports_review_unavailable(node, marker) is False


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
    """``CodeReviewWorkflow`` and every activity it may schedule or replay --
    including the superseded-but-still-registered pre-merge activities -- are
    present in the Temporal worker's ``WORKFLOWS``/``ACTIVITIES`` tables.
    """
    assert CodeReviewWorkflow in WORKFLOWS
    # 9 = the pre-existing 8 plus find_architecture_and_side_effect_activity.
    # find_architecture_and_redundancy_activity / find_side_effect_impact_activity
    # stay registered (not replaced) so a worker can still replay/execute them
    # for workflow histories recorded before the merged pass existed.
    assert len(ACTIVITIES) == 9
    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "review_chunk_activity" in names
    assert "prepare_review_activity" in names
    assert "filter_false_positives_activity" in names
    assert "find_architecture_and_redundancy_activity" in names
    assert "find_side_effect_impact_activity" in names
    assert "find_architecture_and_side_effect_activity" in names
    assert "consolidate_side_effect_issues_activity" in names


# ---------------------------------------------------------------------------
# 5. Worker boot: concurrency ceiling + start_team_worker delegation
# ---------------------------------------------------------------------------


_MAX_CONCURRENT_CASES = [
    (None, 8),
    ("16", 16),
    ("0", 1),
    ("-5", 1),
    ("not-a-number", 8),
]


@pytest.mark.parametrize("env_value, expected", _MAX_CONCURRENT_CASES)
def test_worker_max_concurrent_activities_env_parsing(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None, expected: int
) -> None:
    """``worker._max_concurrent_activities()`` parses
    ``CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES`` defensively: unset, a valid
    value, zero, a negative value, and non-numeric garbage must each resolve
    to the documented default or clamped floor rather than raising.
    """
    from code_review_agent.temporal import worker as worker_mod

    if env_value is None:
        monkeypatch.delenv("CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES", raising=False)
    else:
        monkeypatch.setenv("CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES", env_value)

    assert worker_mod._max_concurrent_activities() == expected


@pytest.mark.parametrize("env_value, expected", _MAX_CONCURRENT_CASES)
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
    """``resolve_temporal_fanout_width`` never requests more workers than a
    review has chunks, never returns less than 1, scales up to an operator's
    raised concurrency ceiling, and falls back to the default ceiling when the
    env override is missing or non-numeric.
    """
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
    """The worker boot hook returns False without starting anything when
    Temporal is disabled for the code review agent.
    """
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

# Module level (not nested in a test function): a Temporal workflow class must
# be a plain top-level attribute of its defining module for the SDK's sandbox
# to resolve it consistently across the real execution and every later replay.
from code_review_agent.temporal import workflows as _cr_workflows  # noqa: E402
from temporalio import workflow as _legacy_wf  # noqa: E402 - see comment above


@_legacy_wf.defn(name="CodeReviewWorkflow")
class _LegacySequentialCodeReviewWorkflow:
    """Pre-#2811 ``CodeReviewWorkflow.run``: tail passes awaited one at a time.

    Exists only to produce, via a real ``WorkflowEnvironment`` execution, a
    "pre-migration" history whose three tail-pass activities are scheduled one
    at a time (each in its own workflow task) rather than gathered — see
    ``test_workflow_replays_pre_migration_sequential_tail_pass_history`` below,
    which replays that history through the CURRENT ``CodeReviewWorkflow``
    class to prove ``_CONCURRENT_TAIL_PASS_PATCH`` keeps it replaying
    correctly. Reuses the real activities and the still-current
    ``_ARCHITECTURE_PASS_PATCH``/``_SIDE_EFFECT_PASS_PATCH``/
    ``_ADAPTIVE_FANOUT_PATCH`` markers/retry policies from ``temporal.workflows``
    so this is a faithful, not hand-waved, reproduction of the old code.
    """

    def __init__(self) -> None:
        self._cancel_requested = False

    @_legacy_wf.signal
    def cancel(self) -> None:
        self._cancel_requested = True

    @_legacy_wf.query
    def progress(self) -> Dict[str, Any]:
        return {"phase": "n/a", "fraction": 0.0, "cancel_requested": self._cancel_requested}

    @_legacy_wf.run
    async def run(self, review_input: Dict[str, Any]) -> Dict[str, Any]:
        import asyncio as _asyncio
        from datetime import timedelta as _timedelta

        A = _cr_workflows.A
        task_queue = _cr_workflows.TASK_QUEUE
        prep = await _legacy_wf.execute_activity(
            A.prepare_review_activity,
            args=[review_input],
            task_queue=task_queue,
            start_to_close_timeout=_timedelta(minutes=30),
            retry_policy=_cr_workflows._DEFAULT_RETRY,
        )
        if prep["no_code"]:
            return {
                "approved": True,
                "issues": prep["skipped_issues"],
                "summary": "No code to review.",
                "spec_compliance_notes": "",
            }

        chunks = prep["chunks"]
        base_input = prep["base_input"]
        context_fp = prep["context_fp"]
        surface_by_path = prep["surface_by_path"]
        fanout_width = prep.get("fanout_width", 1) or 1

        def _review_one(chunk: Dict[str, Any]) -> Any:
            return _legacy_wf.execute_activity(
                A.review_chunk_activity,
                args=[chunk, base_input, context_fp, surface_by_path],
                task_queue=task_queue,
                start_to_close_timeout=_timedelta(hours=1),
                heartbeat_timeout=_timedelta(minutes=5),
                retry_policy=_cr_workflows._LLM_RETRY,
            )

        if _legacy_wf.patched(_cr_workflows._ADAPTIVE_FANOUT_PATCH):
            semaphore = _asyncio.Semaphore(fanout_width)

            async def _review_one_bounded(chunk: Dict[str, Any]) -> Any:
                async with semaphore:
                    return await _review_one(chunk)

            outcomes = await _asyncio.gather(*[_review_one_bounded(chunk) for chunk in chunks])
        else:
            outcomes = await _asyncio.gather(*[_review_one(chunk) for chunk in chunks])

        issues, not_reviewed, summaries, spec_notes, approved_flags = ([] for _ in range(5))
        for outcome in outcomes:
            issues.extend(outcome["issues"])
            not_reviewed.extend(outcome["not_reviewed_issues"])
            summaries.extend(outcome["summaries"])
            spec_notes.extend(outcome["spec_notes"])
            approved_flags.extend(outcome["approved_flags"])

        verified = await _legacy_wf.execute_activity(
            A.filter_false_positives_activity,
            args=[
                review_input,
                issues,
                bool(review_input.get("skip_false_positive_filter", False)),
            ],
            task_queue=task_queue,
            start_to_close_timeout=_timedelta(minutes=60),
            retry_policy=_cr_workflows._LLM_RETRY,
        )

        has_architecture_findings = False
        if _legacy_wf.patched(_cr_workflows._ARCHITECTURE_PASS_PATCH):
            architecture_findings = await _legacy_wf.execute_activity(
                A.find_architecture_and_redundancy_activity,
                args=[review_input],
                task_queue=task_queue,
                start_to_close_timeout=_timedelta(minutes=30),
                retry_policy=_cr_workflows._LLM_RETRY,
            )
            if architecture_findings:
                verified = [*verified, *architecture_findings]
                has_architecture_findings = True

        has_side_effect_findings = False
        if _legacy_wf.patched(_cr_workflows._SIDE_EFFECT_PASS_PATCH):
            side_effect_findings = await _legacy_wf.execute_activity(
                A.find_side_effect_impact_activity,
                args=[review_input],
                task_queue=task_queue,
                start_to_close_timeout=_timedelta(minutes=30),
                retry_policy=_cr_workflows._LLM_RETRY,
            )
            if side_effect_findings:
                verified = [*verified, *side_effect_findings]
                has_side_effect_findings = True

        gate = await _legacy_wf.execute_activity(
            A.finalize_review_activity,
            args=[verified, not_reviewed, prep["skipped_issues"], approved_flags],
            task_queue=task_queue,
            start_to_close_timeout=_timedelta(minutes=5),
            retry_policy=_cr_workflows._DEFAULT_RETRY,
        )
        approved = gate["approved"]
        gated_issues = gate["issues"]

        if len(summaries) == 1 and not (has_architecture_findings or has_side_effect_findings):
            summary, notes = summaries[0], (spec_notes[0] if spec_notes else "")
        else:
            synth = await _legacy_wf.execute_activity(
                A.synthesize_findings_activity,
                args=[review_input, approved, gated_issues, summaries, spec_notes],
                task_queue=task_queue,
                start_to_close_timeout=_timedelta(minutes=15),
                retry_policy=_cr_workflows._DEFAULT_RETRY,
            )
            if synth is not None:
                summary, notes = synth["summary"], synth["spec_compliance_notes"]
            else:
                summary = "\n\n".join(s for s in summaries if s.strip())
                notes = "\n\n".join(n for n in spec_notes if n.strip())

        return {
            "approved": approved,
            "issues": gated_issues,
            "summary": summary,
            "spec_compliance_notes": notes,
        }


@_legacy_wf.defn(name="CodeReviewWorkflow")
class _LegacyConcurrentThreeTailPassCodeReviewWorkflow:
    """Pre-merged-pass ``CodeReviewWorkflow.run``: three tail passes gathered
    concurrently as two separate architecture/side-effect activities.

    Exists only to produce, via a real ``WorkflowEnvironment`` execution, a
    "concurrent, pre-merged-pass" history -- the shape ``CodeReviewWorkflow``
    produced after ``_CONCURRENT_TAIL_PASSES_PATCH`` landed but before
    ``_MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH`` existed -- see
    ``test_workflow_replays_pre_merged_pass_concurrent_tail_pass_history``
    below, which replays that history through the CURRENT
    ``CodeReviewWorkflow`` class to prove
    ``_MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH`` keeps it replaying
    correctly. Reuses the real activities and the still-current
    ``_ARCHITECTURE_PASS_PATCH``/``_SIDE_EFFECT_PASS_PATCH`` markers/retry
    policies from ``temporal.workflows`` so this is a faithful, not
    hand-waved, reproduction of the old code (mirrors
    ``_LegacySequentialCodeReviewWorkflow``'s identical rationale for the
    fully-sequential case).
    """

    def __init__(self) -> None:
        self._cancel_requested = False

    @_legacy_wf.signal
    def cancel(self) -> None:
        self._cancel_requested = True

    @_legacy_wf.query
    def progress(self) -> Dict[str, Any]:
        return {"phase": "n/a", "fraction": 0.0, "cancel_requested": self._cancel_requested}

    @_legacy_wf.run
    async def run(self, review_input: Dict[str, Any]) -> Dict[str, Any]:
        import asyncio as _asyncio
        from datetime import timedelta as _timedelta

        A = _cr_workflows.A
        task_queue = _cr_workflows.TASK_QUEUE
        prep = await _legacy_wf.execute_activity(
            A.prepare_review_activity,
            args=[review_input],
            task_queue=task_queue,
            start_to_close_timeout=_timedelta(minutes=30),
            retry_policy=_cr_workflows._DEFAULT_RETRY,
        )
        if prep["no_code"]:
            return {
                "approved": True,
                "issues": prep["skipped_issues"],
                "summary": "No code to review.",
                "spec_compliance_notes": "",
            }

        chunks = prep["chunks"]
        base_input = prep["base_input"]
        context_fp = prep["context_fp"]
        surface_by_path = prep["surface_by_path"]
        fanout_width = prep.get("fanout_width", 1) or 1
        semaphore = _asyncio.Semaphore(fanout_width)

        async def _review_one_bounded(chunk: Dict[str, Any]) -> Any:
            async with semaphore:
                return await _legacy_wf.execute_activity(
                    A.review_chunk_activity,
                    args=[chunk, base_input, context_fp, surface_by_path],
                    task_queue=task_queue,
                    start_to_close_timeout=_timedelta(hours=1),
                    heartbeat_timeout=_timedelta(minutes=5),
                    retry_policy=_cr_workflows._LLM_RETRY,
                )

        outcomes = await _asyncio.gather(*[_review_one_bounded(chunk) for chunk in chunks])

        issues, not_reviewed, summaries, spec_notes, approved_flags = ([] for _ in range(5))
        for outcome in outcomes:
            issues.extend(outcome["issues"])
            not_reviewed.extend(outcome["not_reviewed_issues"])
            summaries.extend(outcome["summaries"])
            spec_notes.extend(outcome["spec_notes"])
            approved_flags.extend(outcome["approved_flags"])

        def _verify() -> Any:
            return _legacy_wf.execute_activity(
                A.filter_false_positives_activity,
                args=[
                    review_input,
                    issues,
                    bool(review_input.get("skip_false_positive_filter", False)),
                ],
                task_queue=task_queue,
                start_to_close_timeout=_timedelta(minutes=60),
                retry_policy=_cr_workflows._LLM_RETRY,
            )

        def _architecture() -> Any:
            return _legacy_wf.execute_activity(
                A.find_architecture_and_redundancy_activity,
                args=[review_input],
                task_queue=task_queue,
                start_to_close_timeout=_timedelta(minutes=30),
                retry_policy=_cr_workflows._LLM_RETRY,
            )

        def _side_effect() -> Any:
            return _legacy_wf.execute_activity(
                A.find_side_effect_impact_activity,
                args=[review_input],
                task_queue=task_queue,
                start_to_close_timeout=_timedelta(minutes=30),
                retry_policy=_cr_workflows._LLM_RETRY,
            )

        async def _empty_tail_pass() -> Any:
            return []

        # Faithful reproduction requires calling patched() in the exact same
        # order the real pre-merged-pass code did: _CONCURRENT_TAIL_PASSES_PATCH
        # first (this synthetic workflow only ever exercises that True path —
        # the fully-sequential False path is _LegacySequentialCodeReviewWorkflow's
        # job, above), THEN _ARCHITECTURE_PASS_PATCH / _SIDE_EFFECT_PASS_PATCH.
        assert _legacy_wf.patched(_cr_workflows._CONCURRENT_TAIL_PASSES_PATCH)
        run_architecture = _legacy_wf.patched(_cr_workflows._ARCHITECTURE_PASS_PATCH)
        run_side_effect = _legacy_wf.patched(_cr_workflows._SIDE_EFFECT_PASS_PATCH)
        calls = [
            _verify(),
            _architecture() if run_architecture else _empty_tail_pass(),
            _side_effect() if run_side_effect else _empty_tail_pass(),
        ]
        verify_result, architecture_result, side_effect_result = await _asyncio.gather(*calls)

        verified = verify_result
        has_architecture_findings = False
        has_side_effect_findings = False
        if architecture_result:
            verified = [*verified, *architecture_result]
            has_architecture_findings = True
        if side_effect_result:
            verified = [*verified, *side_effect_result]
            has_side_effect_findings = True

        gate = await _legacy_wf.execute_activity(
            A.finalize_review_activity,
            args=[verified, not_reviewed, prep["skipped_issues"], approved_flags],
            task_queue=task_queue,
            start_to_close_timeout=_timedelta(minutes=5),
            retry_policy=_cr_workflows._DEFAULT_RETRY,
        )
        approved = gate["approved"]
        gated_issues = gate["issues"]

        if len(summaries) == 1 and not (has_architecture_findings or has_side_effect_findings):
            summary, notes = summaries[0], (spec_notes[0] if spec_notes else "")
        else:
            synth = await _legacy_wf.execute_activity(
                A.synthesize_findings_activity,
                args=[review_input, approved, gated_issues, summaries, spec_notes],
                task_queue=task_queue,
                start_to_close_timeout=_timedelta(minutes=15),
                retry_policy=_cr_workflows._DEFAULT_RETRY,
            )
            if synth is not None:
                summary, notes = synth["summary"], synth["spec_compliance_notes"]
            else:
                summary = "\n\n".join(s for s in summaries if s.strip())
                notes = "\n\n".join(n for n in spec_notes if n.strip())

        return {
            "approved": approved,
            "issues": gated_issues,
            "summary": summary,
            "spec_compliance_notes": notes,
        }


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
    execute + replay round-trip against the current ``CodeReviewWorkflow``,
    including its concurrent tail-pass activities (false-positive verify,
    architecture consistency, side-effect impact), so a regression in that
    concurrent gather / ``workflow.patched`` ordering has a real
    replay-determinism guard instead of only the activity-level orchestration
    replica ``_run_activity_pipeline`` exercises above. Unlike that helper,
    this drives the actual ``CodeReviewWorkflow.run`` coroutine through a real
    Temporal worker and sandbox.

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
    """The exact idiom ``CodeReviewWorkflow.run`` uses for its concurrent map
    fan-out and concurrent tail passes -- ``asyncio.gather(*calls,
    return_exceptions=True)`` followed by a fixed-order scan that surfaces the
    first exception found -- must always surface the earliest-listed failure
    (matching sequential execution's list-order precedence) regardless of which
    awaitable actually finishes first in real time, and must let every
    awaitable run to completion instead of abandoning the others.

    Ordering is driven by ``asyncio.gather``'s list-order scheduling and an
    ``asyncio.Event``: ``_verify`` is scheduled first but immediately
    suspends on ``architecture_done.wait()``; ``_architecture`` then runs
    to completion and sets ``architecture_done``; ``_side_effect`` runs to
    completion next; finally ``_verify`` resumes and finishes last. This
    guarantees ``verify`` is listed first but completes last, so surfacing
    ``verify_exc`` proves list-order precedence rather than
    completion-order precedence.
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
    filter to raise (the merged architecture/side-effect pass is a wrapped
    real-function call that records its own completion), then asserts the
    whole workflow fails with that failure as its cause -- the same
    total-failure outcome sequential execution produces (verify was always
    the pass whose failure aborts everything) -- AND that the merged pass
    still ran to completion rather than being abandoned once verify raised."""
    from code_review_agent import false_positive_filter, merged_architecture_side_effect_pass
    from code_review_agent.temporal import TASK_QUEUE, CodeReviewWorkflow
    from temporalio.client import WorkflowFailureError

    completed: set[str] = set()
    real_merged = merged_architecture_side_effect_pass.find_architecture_and_side_effect_issues

    def _boom(*args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError("verify boom")

    def _tracked_merged(*args: Any, **kwargs: Any) -> Any:
        result = real_merged(*args, **kwargs)
        completed.add("merged")
        return result

    monkeypatch.setattr(false_positive_filter, "filter_false_positives", _boom)
    monkeypatch.setattr(
        merged_architecture_side_effect_pass,
        "find_architecture_and_side_effect_issues",
        _tracked_merged,
    )

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
    assert "verify boom" in _error_chain_text(cause)
    assert completed == {"merged"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_raises_cleanly_when_a_later_tail_pass_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When verify succeeds but the *later*-listed merged tail pass fails, the
    workflow must fail with that pass's own exception as its cause -- never
    a ``TypeError`` from mistakenly treating the exception object itself as
    a findings dict to unpack.

    The real merged activity is internally fail-safe and never raises (see
    ``find_architecture_and_side_effect_activity``'s docstring), so this
    substitutes a stand-in activity registered under the same activity name
    that raises directly, to exercise the branch a real activity can't reach
    on its own.
    """
    from code_review_agent.temporal import (
        ACTIVITIES,
        TASK_QUEUE,
        CodeReviewWorkflow,
        consolidate_side_effect_issues_activity,
        filter_false_positives_activity,
        finalize_review_activity,
        prepare_review_activity,
        review_chunk_activity,
        synthesize_findings_activity,
    )
    from temporalio import activity as activity_module
    from temporalio.client import WorkflowFailureError

    @activity_module.defn(name="code_review_merged_architecture_side_effect")
    def _raising_merged_activity(review_input: Dict[str, Any]) -> Any:
        raise RuntimeError("merged pass boom")

    stand_in_activities = [
        prepare_review_activity,
        review_chunk_activity,
        filter_false_positives_activity,
        _raising_merged_activity,
        consolidate_side_effect_issues_activity,
        finalize_review_activity,
        synthesize_findings_activity,
    ]
    assert len(stand_in_activities) == len(ACTIVITIES) - 2, (
        "one stand-in merged activity replaces the two legacy "
        "architecture/side-effect activities this list omits (neither is "
        "scheduled by a fresh execution, so the worker does not need them)"
    )

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
    error_chain = _error_chain_text(cause)
    assert "merged pass boom" in error_chain
    assert "TypeError" not in error_chain


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_fails_on_map_chunk_failure_without_abandoning_siblings() -> None:
    """Map-phase ``asyncio.gather(..., return_exceptions=True)`` must await
    every chunk activity when one fails, then re-raise that failure.

    Without ``return_exceptions=True``, the first chunk exception in
    completion order would abort the gather and leave sibling map activities
    un-awaited (orphaned Temporal commands + non-deterministic exception
    precedence on replay). This substitutes a selective
    ``code_review_map_chunk`` stand-in that fails the ``main.py`` chunk and
    delegates every other chunk to the real activity, then asserts the
    sibling still completed and the workflow failed with the map boom.
    """
    from code_review_agent.models import ReviewChunk
    from code_review_agent.temporal import (
        ACTIVITIES,
        TASK_QUEUE,
        CodeReviewWorkflow,
        consolidate_side_effect_issues_activity,
        filter_false_positives_activity,
        finalize_review_activity,
        find_architecture_and_side_effect_activity,
        prepare_review_activity,
        review_chunk_activity,
        synthesize_findings_activity,
    )
    from code_review_agent.temporal import activities as A
    from temporalio import activity as activity_module
    from temporalio.client import WorkflowFailureError

    big_1 = "### app/main.py ###\n" + ("a" * 25_000)
    big_2 = "### app/util.py ###\n" + ("b" * 25_000)
    review_input = _input(code=big_1 + "\n\n" + big_2)
    prep = A.prepare_review_activity(review_input.model_dump(mode="json"))
    assert len(prep["chunks"]) > 1, "expected a multi-chunk submission"

    completed: set[str] = set()

    @activity_module.defn(name="code_review_map_chunk")
    def _selective_map_chunk(
        chunk: Dict[str, Any],
        base_input: Dict[str, Any],
        context_fp: str,
        surface_by_path: Dict[str, list[str]],
    ) -> Any:
        label = ReviewChunk.model_validate(chunk).paths_label
        if "main.py" in label:
            raise RuntimeError("map chunk boom")
        result = review_chunk_activity(chunk, base_input, context_fp, surface_by_path)
        completed.add(label)
        return result

    # The workflow fails during the map phase, before it ever reaches the tail
    # passes, so this worker only needs the merged activity (what a fresh
    # execution would schedule if it got that far) -- the two legacy
    # architecture/side-effect activities are never invoked by a fresh
    # execution and are omitted here.
    stand_in_activities = [
        prepare_review_activity,
        _selective_map_chunk,
        filter_false_positives_activity,
        find_architecture_and_side_effect_activity,
        consolidate_side_effect_issues_activity,
        finalize_review_activity,
        synthesize_findings_activity,
    ]
    assert len(stand_in_activities) == len(ACTIVITIES) - 2

    async with _workflow_environment_worker(activities=stand_in_activities) as env:
        with pytest.raises(WorkflowFailureError) as exc_info:
            await env.client.execute_workflow(
                CodeReviewWorkflow.run,
                review_input.model_dump(mode="json"),
                id="code-review-workflow-map-chunk-failure-test",
                task_queue=TASK_QUEUE,
            )

    cause = exc_info.value.cause
    assert cause is not None
    assert "map chunk boom" in _error_chain_text(cause)
    assert any("util.py" in label for label in completed)
    assert not any("main.py" in label for label in completed)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_gathers_tail_pass_activities_concurrently() -> None:
    """The two tail-pass activities are scheduled together, not one at a time.

    Complements ``test_workflow_executes_and_replays_without_non_determinism``:
    that test's output alone can't distinguish a concurrent ``asyncio.gather``
    from the old sequential awaits, since ``DummyLLMClient`` contributes no
    architecture/side-effect findings either way. This inspects the recorded
    history directly: ``filter_false_positives_activity`` and
    ``find_architecture_and_side_effect_activity`` (the merged pass) must both
    be scheduled by the SAME workflow task
    (``workflow_task_completed_event_id``) -- proof they were fanned out
    together. Under the old sequential-await code, each later activity could
    only be scheduled by a NEW workflow task triggered after the previous
    one's ``ActivityTaskCompletedEvent``, so their scheduling events would
    carry different ``workflow_task_completed_event_id``s.

    ``consolidate_side_effect_issues_activity`` is intentionally *not* part of
    that gather (it needs the merged verified list), so this also asserts it
    is scheduled in a later workflow task than the two concurrent passes.

    Also replays the recorded history, the same non-determinism guard as the
    baseline test above, and marked ``integration``/skips the same way for the
    same reason (see that test's docstring).
    """
    import concurrent.futures

    from code_review_agent.temporal import ACTIVITIES, TASK_QUEUE, CodeReviewWorkflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer, Worker

    review_input = _input()
    workflow_id = "code-review-workflow-concurrent-tail-passes-test"
    tail_pass_activity_names = {
        "code_review_verify_false_positives",
        "code_review_merged_architecture_side_effect",
    }
    consolidation_activity_name = "code_review_side_effect_consolidation"

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
                activities=ACTIVITIES,
                activity_executor=activity_executor,
            )
            async with worker:
                await env.client.execute_workflow(
                    CodeReviewWorkflow.run,
                    review_input.model_dump(mode="json"),
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                )

            history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    tail_pass_events = [
        event
        for event in history.events
        if event.activity_task_scheduled_event_attributes.activity_type.name
        in tail_pass_activity_names
    ]
    # A regression that skips scheduling one of the two (e.g. a stub
    # coroutine silently replacing a real activity call) could still leave a
    # single shared workflow_task_completed_event_id below, so the count must
    # be checked independently of the "same task" assertion.
    assert len(tail_pass_events) == 2, (
        f"expected two tail-pass activity scheduled events, got {len(tail_pass_events)}"
    )
    scheduling_workflow_task_ids = {
        event.activity_task_scheduled_event_attributes.workflow_task_completed_event_id
        for event in tail_pass_events
    }
    assert len(scheduling_workflow_task_ids) == 1, (
        "expected both tail-pass activities to be scheduled by the same "
        f"workflow task (gathered together); got {scheduling_workflow_task_ids}"
    )
    concurrent_task_id = next(iter(scheduling_workflow_task_ids))

    consolidation_events = [
        event
        for event in history.events
        if event.activity_task_scheduled_event_attributes.activity_type.name
        == consolidation_activity_name
    ]
    assert len(consolidation_events) == 1, (
        "expected one side-effect consolidation activity scheduled event, "
        f"got {len(consolidation_events)}"
    )
    consolidation_task_id = consolidation_events[
        0
    ].activity_task_scheduled_event_attributes.workflow_task_completed_event_id
    assert consolidation_task_id != concurrent_task_id, (
        "expected consolidation to be scheduled after the concurrent tail-pass "
        f"gather (different workflow task); both used task id {concurrent_task_id}"
    )

    await Replayer(workflows=[CodeReviewWorkflow]).replay_workflow(history)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_tail_passes_in_flight_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tail-pass activities must not just be scheduled together; they must run
    concurrently as real worker threads execute them.

    This uses a barrier inside the patched underlying verification/pass
    functions: if the workflow regressed back to sequential awaiting of the
    tail passes, the first activity would block forever waiting for the
    other party (and this test would fail fast on the barrier timeout).
    """

    import concurrent.futures
    import threading
    import time

    import code_review_agent.false_positive_filter as fpf
    import code_review_agent.merged_architecture_side_effect_pass as masep
    from code_review_agent.models import CodeReviewIssue
    from code_review_agent.temporal import ACTIVITIES, TASK_QUEUE, CodeReviewWorkflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    review_input = _input()
    workflow_id = "code-review-workflow-tail-passes-inflight-test"

    filter_issue = CodeReviewIssue(
        severity="medium",
        category="logic",
        file_path="a.py",
        description="tail-pass filter contribution",
    )
    architecture_issue = CodeReviewIssue(
        severity="medium",
        category="architecture",
        file_path="a.py",
        description="tail-pass architecture contribution",
    )
    side_effect_issue = CodeReviewIssue(
        severity="medium",
        category="side-effects",
        file_path="a.py",
        description="tail-pass side-effect contribution",
    )

    barrier = threading.Barrier(2, timeout=5)
    started: list[str] = []
    lock = threading.Lock()

    def _wait(name: str) -> None:
        with lock:
            started.append(name)
        barrier.wait()

    # Activities are fail-safe; this test only validates orchestration-level
    # concurrency. Sleeps are intentionally tiny (only to allow thread
    # scheduling variance without making the test slow).
    def _filter(*_args: Any, **_kwargs: Any) -> list[CodeReviewIssue]:
        _wait("filter")
        time.sleep(0.01)
        return [filter_issue]

    def _merged(*_args: Any, **_kwargs: Any) -> tuple[list[CodeReviewIssue], list[CodeReviewIssue]]:
        _wait("merged")
        time.sleep(0.01)
        return [architecture_issue], [side_effect_issue]

    monkeypatch.setattr(fpf, "filter_false_positives", _filter)
    monkeypatch.setattr(masep, "find_architecture_and_side_effect_issues", _merged)

    try:
        test_env = await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:
        pytest.skip(f"Temporal ephemeral test server unavailable (no egress?): {exc}")

    barrier_aborted = False
    try:
        async with test_env as env:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as activity_executor:
                worker = Worker(
                    env.client,
                    task_queue=TASK_QUEUE,
                    workflows=[CodeReviewWorkflow],
                    activities=ACTIVITIES,
                    activity_executor=activity_executor,
                )
                async with worker:
                    result = await env.client.execute_workflow(
                        CodeReviewWorkflow.run,
                        review_input.model_dump(mode="json"),
                        id=workflow_id,
                        task_queue=TASK_QUEUE,
                    )
    finally:
        # Always abort so any stray worker threads can't leak into later
        # tests if the barrier throws early.
        barrier.abort()
        barrier_aborted = True

    assert barrier_aborted, "barrier abort should have run"
    assert sorted(started) == ["filter", "merged"]

    descriptions = {i["description"] for i in result["issues"]}
    assert filter_issue.description in descriptions
    assert architecture_issue.description in descriptions
    assert side_effect_issue.description in descriptions


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_aggregates_tail_pass_results_when_completed_out_of_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workflow must merge tail-pass results correctly regardless of the
    order in which the two tail-pass activities actually complete.

    The workflow uses ``asyncio.gather`` (deterministic fan-in order), so
    out-of-order completion should not affect the merged verdict. This test
    forces a completion order that differs from schedule order via an
    explicit event handoff (not a wall-clock sleep) and asserts that the
    merged tail-pass issue descriptions match the sequential activity
    pipeline (``_run_activity_pipeline``).
    """

    import concurrent.futures
    import threading

    import code_review_agent.false_positive_filter as fpf
    import code_review_agent.merged_architecture_side_effect_pass as masep
    from code_review_agent.models import CodeReviewIssue
    from code_review_agent.temporal import ACTIVITIES, TASK_QUEUE, CodeReviewWorkflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    review_input = _input()
    workflow_id = "code-review-workflow-tail-passes-out-of-order-test"

    filter_issue = CodeReviewIssue(
        severity="medium",
        category="logic",
        file_path="a.py",
        description="tail-pass filter (oOO)",
    )
    architecture_issue = CodeReviewIssue(
        severity="medium",
        category="architecture",
        file_path="a.py",
        description="tail-pass architecture (oOO)",
    )
    side_effect_issue = CodeReviewIssue(
        severity="medium",
        category="side-effects",
        file_path="a.py",
        description="tail-pass side-effect (oOO)",
    )

    # Force completion order merged → filter (different from the workflow's
    # schedule order filter → merged) via an explicit event handoff. A start
    # barrier puts both in flight first; filter then waits for merged's
    # completion event before finishing. No wall-clock sleep — CI preemption
    # cannot reorder this handoff.
    expected_completion_order = ["merged", "filter"]

    barrier = threading.Barrier(2, timeout=5)
    coordinate = threading.Event()
    coordinate.set()
    merged_done = threading.Event()

    completion_order: list[str] = []
    lock = threading.Lock()

    def _record_completion(name: str) -> None:
        with lock:
            completion_order.append(name)

    def _filter(*_args: Any, **_kwargs: Any) -> list[CodeReviewIssue]:
        if coordinate.is_set():
            barrier.wait()
            # Wait until merged has finished so filter is last — independent
            # of thread scheduling.
            if not merged_done.wait(timeout=5):
                raise AssertionError("merged did not signal before filter timeout")
        _record_completion("filter")
        return [filter_issue]

    def _merged(*_args: Any, **_kwargs: Any) -> tuple[list[CodeReviewIssue], list[CodeReviewIssue]]:
        if coordinate.is_set():
            barrier.wait()
        _record_completion("merged")
        merged_done.set()
        return [architecture_issue], [side_effect_issue]

    monkeypatch.setattr(fpf, "filter_false_positives", _filter)
    monkeypatch.setattr(masep, "find_architecture_and_side_effect_issues", _merged)

    try:
        test_env = await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:
        pytest.skip(f"Temporal ephemeral test server unavailable (no egress?): {exc}")

    try:
        async with test_env as env:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as activity_executor:
                worker = Worker(
                    env.client,
                    task_queue=TASK_QUEUE,
                    workflows=[CodeReviewWorkflow],
                    activities=ACTIVITIES,
                    activity_executor=activity_executor,
                )
                async with worker:
                    workflow_out = await env.client.execute_workflow(
                        CodeReviewWorkflow.run,
                        review_input.model_dump(mode="json"),
                        id=workflow_id,
                        task_queue=TASK_QUEUE,
                    )
    finally:
        # Abort so a mid-test failure cannot leave a party parked on the
        # barrier and wedge a later test in the same worker process.
        barrier.abort()

    # Confirm forced out-of-order completion; without this assertion the test
    # could pass even if the merge regressed.
    assert completion_order == expected_completion_order, (
        f"expected deterministic out-of-order completion via event handoffs; got {completion_order}"
    )

    # Disable barrier + handoff waits for the sequential pipeline below;
    # otherwise it would deadlock because that path calls the tail passes
    # one at a time.
    coordinate.clear()

    # Validate the merged verdict is equivalent to the sequential activity
    # pipeline (which applies the same tail-pass merge ordering explicitly).
    sequential_out = _run_activity_pipeline(review_input)

    wf_tail_descriptions = [
        i["description"]
        for i in workflow_out["issues"]
        if i["description"]
        in {
            filter_issue.description,
            architecture_issue.description,
            side_effect_issue.description,
        }
    ]
    seq_tail_descriptions = [
        i.description
        for i in sequential_out.issues
        if i.description
        in {
            filter_issue.description,
            architecture_issue.description,
            side_effect_issue.description,
        }
    ]

    assert wf_tail_descriptions == seq_tail_descriptions
    assert wf_tail_descriptions == [
        filter_issue.description,
        architecture_issue.description,
        side_effect_issue.description,
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_tail_pass_partial_failure_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure inside the additive merged tail pass must not fail the whole
    durable review; the other tail pass (verify) still contributes and the
    workflow returns a merged verdict.

    This uses the merged activity's documented fail-safe posture: it catches
    unexpected internal errors and degrades to no additive contribution
    (empty architecture AND side-effect findings) instead of raising.
    Verifies that behavior stays correct under concurrent tail-pass fan-out,
    complementing the narrower activity-level unit test
    (``test_merged_activity_fails_safe_when_llm_resolution_raises``).
    """

    import concurrent.futures

    import code_review_agent.false_positive_filter as fpf
    import code_review_agent.merged_architecture_side_effect_pass as masep
    from code_review_agent.models import CodeReviewIssue
    from code_review_agent.temporal import ACTIVITIES, TASK_QUEUE, CodeReviewWorkflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    review_input = _input()
    workflow_id = "code-review-workflow-tail-passes-partial-failure-test"

    filter_issue = CodeReviewIssue(
        severity="medium",
        category="logic",
        file_path="a.py",
        description="tail-pass filter (partial failure)",
    )
    architecture_issue = CodeReviewIssue(
        severity="medium",
        category="architecture",
        file_path="a.py",
        description="tail-pass architecture (should be absent)",
    )

    merged_calls = 0

    def _filter(*_args: Any, **_kwargs: Any) -> list[CodeReviewIssue]:
        return [filter_issue]

    def _merged(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal merged_calls
        merged_calls += 1
        raise RuntimeError("forced merged pass failure")

    monkeypatch.setattr(fpf, "filter_false_positives", _filter)
    monkeypatch.setattr(masep, "find_architecture_and_side_effect_issues", _merged)

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
                activities=ACTIVITIES,
                activity_executor=activity_executor,
            )
            async with worker:
                workflow_out = await env.client.execute_workflow(
                    CodeReviewWorkflow.run,
                    review_input.model_dump(mode="json"),
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                )

    # Distinguish fail-safe handling of an *invoked* failing pass from a
    # regression that silently skips scheduling the merged activity.
    assert merged_calls == 1, f"expected merged pass to be invoked once; got {merged_calls}"
    wf_tail_descriptions = {i["description"] for i in workflow_out["issues"]}
    assert filter_issue.description in wf_tail_descriptions
    assert architecture_issue.description not in wf_tail_descriptions


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_replays_pre_migration_sequential_tail_pass_history() -> None:
    """A history recorded by the old sequential tail passes still replays.

    ``_CONCURRENT_TAIL_PASS_PATCH`` exists precisely so an in-flight workflow
    whose history was recorded before this change -- three activities
    scheduled one at a time, each in its OWN workflow task, only after the
    previous one completed -- keeps replaying that exact command sequence
    instead of ``CodeReviewWorkflow`` trying to gather all three into a
    single workflow task, which would raise a non-determinism error.

    Executes ``_LegacySequentialCodeReviewWorkflow`` (module level, above --
    a faithful reproduction of the pre-#2811 sequential ``run`` body,
    registered under the same ``CodeReviewWorkflow`` workflow-type name) to
    produce a realistic "pre-migration" history, then replays that history
    through the CURRENT ``CodeReviewWorkflow`` class with ``Replayer`` --
    proving today's code still reproduces it. Without the
    ``_CONCURRENT_TAIL_PASS_PATCH`` gate, this test fails with a
    non-determinism error.
    """
    import concurrent.futures

    from code_review_agent.temporal import ACTIVITIES, TASK_QUEUE, CodeReviewWorkflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

    review_input = _input()
    workflow_id = "code-review-workflow-legacy-sequential-history-test"

    try:
        test_env = await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:
        pytest.skip(f"Temporal ephemeral test server unavailable (no egress?): {exc}")

    async with test_env as env:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as activity_executor:
            worker = Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[_LegacySequentialCodeReviewWorkflow],
                activities=ACTIVITIES,
                activity_executor=activity_executor,
                # This synthetic workflow lives in this test module, whose
                # ordinary test-only imports are not sandbox-safe. We only
                # need it to record a legacy history; the current workflow is
                # still replayed below with the default sandboxed runner.
                workflow_runner=UnsandboxedWorkflowRunner(),
            )
            async with worker:
                await env.client.execute_workflow(
                    _LegacySequentialCodeReviewWorkflow.run,
                    review_input.model_dump(mode="json"),
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                )

            legacy_history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    # The property _CONCURRENT_TAIL_PASS_PATCH exists to guard: today's
    # CodeReviewWorkflow must still replay a pre-migration sequential history
    # without a non-determinism error.
    await Replayer(workflows=[CodeReviewWorkflow]).replay_workflow(legacy_history)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_replays_pre_merged_pass_concurrent_tail_pass_history() -> None:
    """A history recorded by the old three-activity concurrent gather still
    replays.

    ``_MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH`` exists precisely so an
    in-flight workflow whose history was recorded before this change --
    ``code_review_verify_false_positives``,
    ``code_review_architecture_consistency``, and
    ``code_review_side_effect_impact`` all scheduled together in one workflow
    task -- keeps replaying that exact three-activity command sequence
    instead of ``CodeReviewWorkflow`` trying to gather a single
    ``code_review_merged_architecture_side_effect`` activity in its place,
    which would raise a non-determinism error.

    Executes ``_LegacyConcurrentThreeTailPassCodeReviewWorkflow`` (module
    level, above -- a faithful reproduction of the pre-merged-pass concurrent
    ``run`` body, registered under the same ``CodeReviewWorkflow``
    workflow-type name) to produce a realistic "concurrent, pre-merged-pass"
    history, then replays that history through the CURRENT
    ``CodeReviewWorkflow`` class with ``Replayer`` -- proving today's code
    still reproduces it. Without the
    ``_MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH`` gate, this test fails
    with a non-determinism error. Mirrors
    ``test_workflow_replays_pre_migration_sequential_tail_pass_history``'s
    identical structure for the fully-sequential case.
    """
    import concurrent.futures

    from code_review_agent.temporal import ACTIVITIES, TASK_QUEUE, CodeReviewWorkflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

    review_input = _input()
    workflow_id = "code-review-workflow-legacy-concurrent-three-pass-history-test"

    try:
        test_env = await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:
        pytest.skip(f"Temporal ephemeral test server unavailable (no egress?): {exc}")

    async with test_env as env:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as activity_executor:
            worker = Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[_LegacyConcurrentThreeTailPassCodeReviewWorkflow],
                activities=ACTIVITIES,
                activity_executor=activity_executor,
                # This synthetic workflow lives in this test module, whose
                # ordinary test-only imports are not sandbox-safe. We only
                # need it to record a legacy history; the current workflow is
                # still replayed below with the default sandboxed runner.
                workflow_runner=UnsandboxedWorkflowRunner(),
            )
            async with worker:
                await env.client.execute_workflow(
                    _LegacyConcurrentThreeTailPassCodeReviewWorkflow.run,
                    review_input.model_dump(mode="json"),
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                )

            legacy_history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    # The property _MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH exists to
    # guard: today's CodeReviewWorkflow must still replay a concurrent,
    # pre-merged-pass history without a non-determinism error.
    await Replayer(workflows=[CodeReviewWorkflow]).replay_workflow(legacy_history)
