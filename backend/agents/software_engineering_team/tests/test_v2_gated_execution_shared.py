"""Branch coverage for the shared gated-execution skeleton (``run_gated_execution_impl``).

The per-team gated-loop tests (``test_microtask_review_gates``) drive both teams'
``run_execution_with_review_gates`` wrappers through the real ``review.py`` /
``problem_solving.py`` agents via the scripted ``DummyLLMClient`` boundary, pinning
the externally observable behaviour. They do not, on their own, reach every branch
of the shared skeleton — the max-cycles guard variants, the unsafe-path-during-fix
breaks, the dependency-skip vs run-anyway split, and the documentation-agent paths.

This file calls the shared body directly with a synthetic
:class:`GatedExecutionConfig` and stub gate callables (returning
:class:`GateOutcome`), so each branch is exercised on its own, independent of the
per-team ``Agent`` patch surface — the same seam ``test_v2_review_shared`` uses for
``run_review``. The team ``models`` surface is the real backend module (it supplies
``MicrotaskStatus`` / ``ExecutionResult`` / ``ReviewResult`` /
``MicrotaskReviewFailedError`` / ``ToolAgentKind``); every per-team *divergence* is
supplied through the config, so a config flag/verb here does not depend on which
team's ``models`` is passed.

Preconditions:
    - ``llm`` is a ``DummyLLMClient`` (the skeleton never calls it directly; the
      stub gates/coder stand in for the real agents).
    - ``repo_path`` is a real temp dir so the guarded file writes exercise the true
      ``UnsafeRepoPathError`` path for traversal keys.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.backend_code_v2_team import models as be_models
from software_engineering_team.shared.models import SystemArchitecture
from software_engineering_team.shared.phases.execution import (
    GatedExecutionConfig,
    GateOutcome,
    ReviewDependencies,
    run_gated_execution_impl,
)
from software_engineering_team.shared.v2_models import ReviewIssue

MS = be_models.MicrotaskStatus


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _issue(source: str = "code_review") -> ReviewIssue:
    return ReviewIssue(source=source, severity="high", description="d", file_path="f")


def _task() -> SimpleNamespace:
    return SimpleNamespace(id="t1", title="T", description="desc", requirements="reqs")


def _microtask(mid: str = "mt-1", depends_on: Optional[List[str]] = None) -> be_models.Microtask:
    # A real ``Microtask`` — the skeleton returns them inside a pydantic
    # ``ExecutionResult`` (validated) and routes on the hashable ``tool_agent`` enum.
    return be_models.Microtask(
        id=mid,
        title=f"Task {mid}",
        description="do the thing",
        tool_agent=be_models.ToolAgentKind.GENERAL,
        depends_on=depends_on or [],
    )


def _planning(microtasks: List[Any], language: str = "python") -> SimpleNamespace:
    return SimpleNamespace(microtasks=microtasks, language=language)


class _ScriptedGate:
    """Return queued outcomes then pass; count calls and bridge the detail callback."""

    def __init__(self, outcomes: Optional[List[GateOutcome]] = None) -> None:
        self._q = list(outcomes or [])
        self.calls = 0

    def __call__(self, *, detail_callback=None, **kwargs: Any) -> GateOutcome:
        self.calls += 1
        if detail_callback is not None:
            detail_callback("gate tick")
        if self._q:
            return self._q.pop(0)
        return GateOutcome(passed=True)


def _pass_gate(**kwargs: Any) -> GateOutcome:
    return GateOutcome(passed=True)


def _fail_gate(source: str = "code_review"):
    def _gate(**kwargs: Any) -> GateOutcome:
        return GateOutcome(passed=False, issues=[_issue(source)], summary="bad")

    return _gate


class _CapturingGate:
    """Records every call's kwargs, then returns queued outcomes (default: always pass).

    Used to assert what the shared loop actually forwards into a gate — e.g. that
    ``architecture``/``spec_content`` reach BOTH the initial code-review call and
    the re-review call after a batch fix, not just one of them.
    """

    def __init__(self, outcomes: Optional[List[GateOutcome]] = None) -> None:
        self._q = list(outcomes or [])
        self.calls_kwargs: List[Dict[str, Any]] = []

    def __call__(self, *, detail_callback=None, **kwargs: Any) -> GateOutcome:
        self.calls_kwargs.append(kwargs)
        if detail_callback is not None:
            detail_callback("gate tick")
        if self._q:
            return self._q.pop(0)
        return GateOutcome(passed=True)


def _coder(**kwargs: Any) -> Dict[str, str]:
    return {"src/a.py": "print(1)\n"}


def _coder_unsafe(**kwargs: Any) -> Dict[str, str]:
    return {"../evil.py": "x"}


def _coder_raises(**kwargs: Any) -> Dict[str, str]:
    raise RuntimeError("boom")


def _batch_fix(*, detail_callback=None, **kwargs: Any) -> SimpleNamespace:
    if detail_callback is not None:
        detail_callback("fixing")
    return SimpleNamespace(files=kwargs["current_files"])


def _batch_fix_unsafe(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(files={"../evil.py": "x"})


def _doc_review(*, documentation=None, detail_callback=None, **kwargs: Any) -> SimpleNamespace:
    if detail_callback is not None:
        detail_callback("doc")
    return SimpleNamespace(documentation={}, iterations=1, final_quality_score=0.95)


def _make_gate_config(
    *,
    code_review_gate=None,
    qa_gate=None,
    security_gate=None,
    batch_fix=_batch_fix,
    doc_review=_doc_review,
    coder=_coder,
    requires_failing: bool = True,
    verb: str = "failed with",
    status_cr=MS.IN_CODE_REVIEW,
    status_qa=MS.IN_QA_TESTING,
    status_sec=MS.IN_SECURITY_TESTING,
) -> GatedExecutionConfig:
    return GatedExecutionConfig(
        models=be_models,
        run_general_microtask=coder,
        run_code_review_gate=code_review_gate or _pass_gate,
        run_qa_gate=qa_gate or _pass_gate,
        run_security_gate=security_gate or _pass_gate,
        run_batch_coding_fixes=batch_fix,
        run_documentation_self_review=doc_review,
        status_code_review=status_cr,
        status_qa=status_qa,
        status_security=status_sec,
        max_total_cycles=lambda c: (
            c.code_review_max_retries + c.qa_max_retries + c.security_max_retries
        ),
        code_review_retry_cap=lambda c: c.code_review_max_retries,
        max_cycles_requires_failing_gate=requires_failing,
        startup_log_message=lambda tid, total, c: f"start {tid} {total} on_failure={c.on_failure}",
        gate_issue_log_verb=verb,
    )


def _config(
    *,
    cr: int = 1,
    qa: int = 1,
    sec: int = 1,
    on_failure: str = "stop",
    security_stops: bool = True,
) -> be_models.MicrotaskReviewConfig:
    return be_models.MicrotaskReviewConfig(
        code_review_max_retries=cr,
        qa_max_retries=qa,
        security_max_retries=sec,
        on_failure=on_failure,
        security_failure_always_stops=security_stops,
    )


def _run(
    gate_config: GatedExecutionConfig,
    microtasks: List[Any],
    tmp_path,
    *,
    review_config: Optional[Any] = None,
    only_ids: Optional[List[str]] = None,
    progress=None,
    review_deps: Optional[ReviewDependencies] = None,
    architecture: Optional[Any] = None,
    spec_content: str = "",
):
    return run_gated_execution_impl(
        gate_config=gate_config,
        llm=DummyLLMClient(),
        task=_task(),
        planning_result=_planning(microtasks),
        repo_path=tmp_path,
        architecture=architecture,
        spec_content=spec_content,
        review_config=review_config,
        review_deps=review_deps,
        only_microtask_ids=only_ids,
        progress_callback=progress,
    )


# ---------------------------------------------------------------------------
# Happy path + progress contract
# ---------------------------------------------------------------------------


def test_happy_path_completes_and_writes_file(tmp_path):
    """All gates pass on the first cycle → microtask COMPLETED, file on disk."""
    mt = _microtask()
    # review_config=None exercises the ``or models.MicrotaskReviewConfig()`` default.
    result = _run(_make_gate_config(), [mt], tmp_path, review_config=None)

    assert mt.status == MS.COMPLETED
    assert "src/a.py" in result.files
    assert (tmp_path / "src" / "a.py").read_text() == "print(1)\n"
    assert "1/1 microtasks successfully" in result.summary


def test_progress_callback_contract(tmp_path):
    """Every progress call is the documented 6-tuple with a known phase, index 1-based."""
    calls: List[tuple] = []
    allowed = {
        "coding",
        "code_review",
        "qa_testing",
        "security_testing",
        "documentation",
        "completed",
    }
    _run(
        _make_gate_config(),
        [_microtask()],
        tmp_path,
        review_config=_config(),
        progress=lambda *a: calls.append(a),
    )

    assert calls, "progress callback was never invoked"
    phases_seen = set()
    for call in calls:
        assert len(call) == 6
        current_index, completed, total, title, phase, detail = call
        assert current_index == 1
        assert total == 1
        assert phase in allowed
        assert isinstance(detail, str)
        phases_seen.add(phase)
    # The full lifecycle was emitted.
    assert {
        "coding",
        "code_review",
        "qa_testing",
        "security_testing",
        "documentation",
        "completed",
    } <= phases_seen


# ---------------------------------------------------------------------------
# Dependency handling / microtask selection
# ---------------------------------------------------------------------------


def test_dependent_of_review_failed_is_skipped(tmp_path):
    """A microtask depending on a review-failed one is SKIPPED, not run."""
    mt1 = _microtask("mt-1")
    mt2 = _microtask("mt-2", depends_on=["mt-1"])
    cfg = _make_gate_config(code_review_gate=_fail_gate())
    _run(cfg, [mt1, mt2], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    assert mt1.status == MS.REVIEW_FAILED
    assert mt2.status == MS.SKIPPED
    assert "depends on review-failed" in mt2.notes


def test_unmet_dep_without_review_failure_runs_anyway(tmp_path):
    """An unmet dep that is merely not-yet-complete (not review-failed) still runs."""
    mt = _microtask("mt-1", depends_on=["ghost"])
    _run(_make_gate_config(), [mt], tmp_path, review_config=_config())

    assert mt.status == MS.COMPLETED


def test_only_microtask_ids_filters(tmp_path):
    """``only_microtask_ids`` restricts execution to the named microtasks."""
    mt1 = _microtask("mt-1")
    mt2 = _microtask("mt-2")
    result = _run(
        _make_gate_config(), [mt1, mt2], tmp_path, review_config=_config(), only_ids=["mt-2"]
    )

    assert mt1.status == MS.PENDING  # never touched (default status)
    assert mt2.status == MS.COMPLETED
    assert len(result.microtasks) == 1


# ---------------------------------------------------------------------------
# Coding gate failures
# ---------------------------------------------------------------------------


def test_coding_exception_marks_failed(tmp_path):
    calls: List[tuple] = []
    mt = _microtask()
    _run(
        _make_gate_config(coder=_coder_raises),
        [mt],
        tmp_path,
        review_config=_config(),
        progress=lambda *a: calls.append(a),
    )

    assert mt.status == MS.FAILED
    assert "boom" in mt.notes
    # The failure path still closes out the microtask with a "completed" tick.
    assert calls[-1][4] == "completed"


def test_unsafe_initial_write_marks_review_failed(tmp_path):
    mt = _microtask()
    _run(_make_gate_config(coder=_coder_unsafe), [mt], tmp_path, review_config=_config())

    assert mt.status == MS.REVIEW_FAILED


# ---------------------------------------------------------------------------
# Code-review gate
# ---------------------------------------------------------------------------


def test_code_review_retry_then_pass(tmp_path):
    """A failing code review is batch-fixed and re-run; success proceeds to COMPLETED."""
    cr = _ScriptedGate([GateOutcome(passed=False, issues=[_issue()], summary="fixme")])
    mt = _microtask()
    _run(
        _make_gate_config(code_review_gate=cr),
        [mt],
        tmp_path,
        review_config=_config(cr=2),
        progress=lambda *a: None,
    )

    assert cr.calls == 2  # initial fail + one re-review after the batch fix
    assert mt.status == MS.COMPLETED


def test_code_review_gate_receives_architecture_and_spec_content_on_every_call(tmp_path):
    """``architecture``/``spec_content`` reach the code-review gate on the initial
    call AND the re-review call after a batch fix.

    Regression test for a confirmed bug: ``run_gated_execution_impl`` already had
    ``architecture`` in scope (used for microtask coding) but never forwarded it
    to either of its two ``gate_config.run_code_review_gate(...)`` call sites.
    """
    architecture = SystemArchitecture(overview="layered architecture")
    cr = _CapturingGate([GateOutcome(passed=False, issues=[_issue()], summary="fixme")])
    mt = _microtask()
    _run(
        _make_gate_config(code_review_gate=cr),
        [mt],
        tmp_path,
        review_config=_config(cr=2),
        architecture=architecture,
        spec_content="the full project spec",
    )

    assert len(cr.calls_kwargs) == 2  # initial call + one re-review after the batch fix
    for call_kwargs in cr.calls_kwargs:
        assert call_kwargs["review_context"].architecture == architecture
        assert call_kwargs["review_context"].spec_content == "the full project spec"
    assert mt.status == MS.COMPLETED


def test_code_review_gate_defaults_architecture_and_spec_content(tmp_path):
    """A caller that does not pass ``architecture``/``spec_content`` to ``_run``
    (i.e. the existing default-free call shape) reaches the gate with
    ``review_context=None`` -- not an empty ``ReviewContext()`` -- so existing
    callers are unaffected and the LLM fallback reviewers' context-bounding
    path (which calls ``compute_code_review_*_chars(llm)``, requiring
    ``get_max_context_tokens()``) is never entered with nothing to bound."""
    cr = _CapturingGate()
    mt = _microtask()
    _run(_make_gate_config(code_review_gate=cr), [mt], tmp_path, review_config=_config())

    assert cr.calls_kwargs
    assert cr.calls_kwargs[0]["review_context"] is None


def test_code_review_gate_context_none_when_spec_content_blank(tmp_path):
    """Passing an explicit blank ``spec_content=""`` and no ``architecture``
    must not build a non-None ``ReviewContext`` either -- only an actually
    populated field should turn it on."""
    cr = _CapturingGate()
    mt = _microtask()
    _run(
        _make_gate_config(code_review_gate=cr),
        [mt],
        tmp_path,
        review_config=_config(),
        architecture=None,
        spec_content="",
    )

    assert cr.calls_kwargs
    assert cr.calls_kwargs[0]["review_context"] is None


def test_code_review_fail_stop_raises(tmp_path):
    mt = _microtask()
    cfg = _make_gate_config(code_review_gate=_fail_gate())
    with pytest.raises(be_models.MicrotaskReviewFailedError) as exc:
        _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="stop"))

    assert exc.value.microtask.id == "mt-1"
    assert exc.value.review_result.summary == "bad"


def test_code_review_fail_skip_continue_marks_review_failed(tmp_path):
    mt = _microtask()
    cfg = _make_gate_config(code_review_gate=_fail_gate())
    _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert "Code review failed after 1 batch fix attempts" in mt.notes


def test_code_review_unsafe_fix_breaks(tmp_path):
    """An unsafe path emitted by the batch fix during code-review retry ends the microtask."""
    mt = _microtask()
    cfg = _make_gate_config(code_review_gate=_fail_gate(), batch_fix=_batch_fix_unsafe)
    _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED


# ---------------------------------------------------------------------------
# QA / security gates (fail → batch-fix → restart from code review)
# ---------------------------------------------------------------------------


def test_qa_fail_then_pass_restarts_and_completes(tmp_path):
    qa = _ScriptedGate([GateOutcome(passed=False, issues=[_issue("qa")], summary="qa")])
    mt = _microtask()
    _run(
        _make_gate_config(qa_gate=qa),
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=2, sec=1),
        progress=lambda *a: None,
    )

    assert qa.calls == 2  # failed once (→ restart), passed on the next cycle
    assert mt.status == MS.COMPLETED


def test_qa_unsafe_fix_breaks(tmp_path):
    mt = _microtask()
    cfg = _make_gate_config(qa_gate=_fail_gate("qa"), batch_fix=_batch_fix_unsafe)
    _run(cfg, [mt], tmp_path, review_config=_config(cr=1, qa=1, sec=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED


def test_security_fail_then_pass_restarts_and_completes(tmp_path):
    sec = _ScriptedGate([GateOutcome(passed=False, issues=[_issue("security")], summary="sec")])
    mt = _microtask()
    _run(
        _make_gate_config(security_gate=sec),
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=1, sec=2),
        progress=lambda *a: None,
    )

    assert sec.calls == 2
    assert mt.status == MS.COMPLETED


def test_security_unsafe_fix_breaks(tmp_path):
    mt = _microtask()
    cfg = _make_gate_config(security_gate=_fail_gate("security"), batch_fix=_batch_fix_unsafe)
    _run(cfg, [mt], tmp_path, review_config=_config(cr=1, qa=1, sec=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED


# ---------------------------------------------------------------------------
# Max-cycles semantics (the one genuine per-team behavioural fork)
# ---------------------------------------------------------------------------


def test_max_cycles_guarded_all_passing_completes(tmp_path):
    """Backend guard: reaching max cycles with every gate passing is NOT a failure."""
    # cr=1,qa=0,sec=0 → max_total_cycles=1; all gates pass so the loop breaks with
    # total_cycles == max_total_cycles, entering the guarded max-cycles check.
    mt = _microtask()
    _run(
        _make_gate_config(requires_failing=True),
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=0, sec=0),
    )

    assert mt.status == MS.COMPLETED


def test_max_cycles_guarded_still_failing_review_failed(tmp_path):
    """Backend guard: max cycles with a gate still failing → REVIEW_FAILED (no stop)."""
    mt = _microtask()
    cfg = _make_gate_config(requires_failing=True, qa_gate=_fail_gate("qa"))
    _run(cfg, [mt], tmp_path, review_config=_config(cr=0, qa=2, sec=0, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert "cycles exhausted" in mt.notes


def test_max_cycles_security_failure_always_stops(tmp_path):
    """A still-failing security gate at max cycles force-stops even under skip_continue."""
    mt = _microtask()
    cfg = _make_gate_config(requires_failing=True, security_gate=_fail_gate("security"))
    with pytest.raises(be_models.MicrotaskReviewFailedError):
        _run(
            cfg,
            [mt],
            tmp_path,
            review_config=_config(
                cr=0, qa=0, sec=2, on_failure="skip_continue", security_stops=True
            ),
        )


def test_max_cycles_unconditional_marks_review_failed(tmp_path):
    """Frontend-style flag=False marks REVIEW_FAILED unconditionally at max cycles."""
    # max_total_cycles=0 → the review loop never runs; the unconditional branch fires.
    mt = _microtask()
    cfg = _make_gate_config(requires_failing=False, verb="found")
    _run(cfg, [mt], tmp_path, review_config=_config(cr=0, qa=0, sec=0, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED


def test_max_cycles_unconditional_stop_raises(tmp_path):
    mt = _microtask()
    cfg = _make_gate_config(requires_failing=False, verb="found")
    with pytest.raises(be_models.MicrotaskReviewFailedError):
        _run(cfg, [mt], tmp_path, review_config=_config(cr=0, qa=0, sec=0, on_failure="stop"))


# ---------------------------------------------------------------------------
# Documentation phase
# ---------------------------------------------------------------------------


def test_documentation_agent_and_self_review_write(tmp_path):
    """A DOCUMENTATION tool agent runs first; self-review's refined docs are written."""

    def _doc_with_files(
        *, documentation=None, detail_callback=None, **kwargs: Any
    ) -> SimpleNamespace:
        return SimpleNamespace(
            documentation={"README.md": "# docs\n"}, iterations=2, final_quality_score=0.99
        )

    doc_agent = SimpleNamespace(
        document_microtask=lambda **kw: SimpleNamespace(files={"DOC.md": "seed"})
    )
    deps = ReviewDependencies(tool_agents={be_models.ToolAgentKind.DOCUMENTATION: doc_agent})
    mt = _microtask()
    result = _run(
        _make_gate_config(doc_review=_doc_with_files),
        [mt],
        tmp_path,
        review_config=_config(),
        review_deps=deps,
        progress=lambda *a: None,
    )

    assert mt.status == MS.COMPLETED
    assert "README.md" in result.files
    assert (tmp_path / "README.md").read_text() == "# docs\n"


def test_documentation_agent_exception_is_swallowed(tmp_path):
    def _raises(**kw: Any):
        raise RuntimeError("doc boom")

    doc_agent = SimpleNamespace(document_microtask=_raises)
    deps = ReviewDependencies(tool_agents={be_models.ToolAgentKind.DOCUMENTATION: doc_agent})
    mt = _microtask()
    _run(_make_gate_config(), [mt], tmp_path, review_config=_config(), review_deps=deps)

    assert mt.status == MS.COMPLETED  # documentation never fails the microtask


def test_documentation_unsafe_path_is_skipped(tmp_path):
    def _doc_unsafe(*, documentation=None, detail_callback=None, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            documentation={"../escape.md": "x"}, iterations=1, final_quality_score=0.9
        )

    mt = _microtask()
    result = _run(
        _make_gate_config(doc_review=_doc_unsafe), [mt], tmp_path, review_config=_config()
    )

    assert mt.status == MS.COMPLETED  # unsafe doc path is best-effort, skipped
    assert "../escape.md" not in result.files


# ---------------------------------------------------------------------------
# Terminal gate-outcome observability
# ---------------------------------------------------------------------------


def test_terminal_failing_outcome_prefers_cr_then_qa_then_sec():
    """_terminal_failing_outcome returns the first still-failing gate in cr→qa→sec order."""
    from software_engineering_team.shared.phases.execution import _terminal_failing_outcome

    cr_fail = GateOutcome(passed=False, issues=[_issue("code_review")], summary="cr")
    qa_fail = GateOutcome(passed=False, issues=[_issue("qa")], summary="qa")
    sec_fail = GateOutcome(passed=False, issues=[_issue("security")], summary="sec")
    all_pass = GateOutcome(passed=True)

    assert _terminal_failing_outcome(cr_fail, qa_fail, sec_fail) is cr_fail
    assert _terminal_failing_outcome(all_pass, qa_fail, sec_fail) is qa_fail
    assert _terminal_failing_outcome(all_pass, all_pass, sec_fail) is sec_fail
    synthetic = _terminal_failing_outcome(all_pass, all_pass, all_pass)
    assert synthetic.passed is False
    assert synthetic.summary == "Max cycles exceeded"


def test_record_gate_outcome_on_code_review_retry_exhausted(tmp_path, monkeypatch):
    """Code-review retry exhaustion records exactly one terminal gate outcome."""
    calls: List[tuple] = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.execution.record_gate_outcome",
        lambda gate, result, **kw: calls.append((gate, result, kw)) or True,
    )
    mt = _microtask()
    _run(
        _make_gate_config(code_review_gate=_fail_gate()),
        [mt],
        tmp_path,
        review_config=_config(cr=1, on_failure="skip_continue"),
    )

    assert mt.status == MS.REVIEW_FAILED
    assert len(calls) == 1
    gate, result, kw = calls[0]
    assert gate == "code_review_retry_exhausted"
    assert result.passed is False
    assert kw.get("task_id") == "t1"
    assert kw.get("phase") == "execution"
    assert kw.get("job_id") == ""


def test_record_gate_outcome_on_max_cycles(tmp_path, monkeypatch):
    """Max-cycles REVIEW_FAILED records exactly one outcome with gate=review_max_cycles."""
    calls: List[tuple] = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.execution.record_gate_outcome",
        lambda gate, result, **kw: calls.append((gate, result, kw)) or True,
    )
    mt = _microtask()
    cfg = _make_gate_config(requires_failing=True, qa_gate=_fail_gate("qa"))
    _run(cfg, [mt], tmp_path, review_config=_config(cr=0, qa=2, sec=0, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert len(calls) == 1
    gate, result, kw = calls[0]
    assert gate == "review_max_cycles"
    assert result.passed is False
    assert kw.get("task_id") == "t1"
    assert kw.get("phase") == "execution"
    assert kw.get("job_id") == ""


def test_record_gate_outcome_not_called_on_success(tmp_path, monkeypatch):
    """Happy-path completion must not record a gate rejection."""
    calls: List[tuple] = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.execution.record_gate_outcome",
        lambda *a, **k: calls.append((a, k)) or True,
    )
    _run(_make_gate_config(), [_microtask()], tmp_path, review_config=_config())
    assert calls == []


def test_record_gate_outcome_not_called_on_qa_recovered(tmp_path, monkeypatch):
    """Mid-loop QA fail → fix → pass is not a terminal failure; no recording."""
    calls: List[tuple] = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.execution.record_gate_outcome",
        lambda *a, **k: calls.append((a, k)) or True,
    )
    qa = _ScriptedGate([GateOutcome(passed=False, issues=[_issue("qa")], summary="qa")])
    _run(
        _make_gate_config(qa_gate=qa),
        [_microtask()],
        tmp_path,
        review_config=_config(cr=1, qa=2, sec=1),
    )
    assert calls == []


def test_record_gate_outcome_not_called_on_unsafe_cr_write(tmp_path, monkeypatch):
    """Write-path failure during code-review retry must not record retry exhaustion."""
    calls: List[tuple] = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.execution.record_gate_outcome",
        lambda *a, **k: calls.append((a, k)) or True,
    )
    mt = _microtask()
    cfg = _make_gate_config(code_review_gate=_fail_gate(), batch_fix=_batch_fix_unsafe)
    _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert "unsafe output path" in mt.notes
    assert calls == []
