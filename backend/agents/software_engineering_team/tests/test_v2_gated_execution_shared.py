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

import logging
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from llm_service.clients.dummy import DummyLLMClient
from shared.dev_models.models import SystemArchitecture
from software_engineering_team.backend_code_v2_team import models as be_models
from software_engineering_team.shared.phases.execution import (
    GatedExecutionConfig,
    GateOutcome,
    ReviewDependencies,
    _schedule_microtask_batches,
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


def _batch_fix_adds_file(*, detail_callback=None, **kwargs: Any) -> SimpleNamespace:
    """A fix that keeps the current files and introduces one new (safe) key."""
    if detail_callback is not None:
        detail_callback("fixing")
    files = dict(kwargs["current_files"])
    files["src/new_helper.py"] = "def helper():\n    return 1\n"
    return SimpleNamespace(files=files)


def _coder_per_microtask(**kwargs: Any) -> Dict[str, str]:
    """``mt-a`` owns ``shared.py``; every other microtask writes only its own file."""
    if kwargs["microtask"].id == "mt-a":
        return {"shared.py": "owned-by-a\n"}
    return {"src/b.py": "print('b')\n"}


def _recording_coder(order: List[str]):
    """Build a coder that appends the microtask's id to ``order`` before writing a file."""

    def _coder_at(**kwargs: Any) -> Dict[str, str]:
        mid = kwargs["microtask"].id
        order.append(mid)
        return {f"src/{mid}.py": "print(1)\n"}

    return _coder_at


def _cr_gate_fails_for(mid: str):
    """Code-review gate that fails only for the microtask whose id is ``mid``."""

    def _gate(**kwargs: Any) -> GateOutcome:
        if kwargs["microtask"].id == mid:
            return GateOutcome(passed=False, issues=[_issue()], summary="bad")
        return GateOutcome(passed=True)

    return _gate


def _batch_fix_overwrites_shared(**kwargs: Any) -> SimpleNamespace:
    """A fix that also rewrites ``shared.py`` — a file an earlier microtask produced."""
    files = dict(kwargs["current_files"])
    files["shared.py"] = "clobbered-by-b\n"
    return SimpleNamespace(files=files)


def _coder_overwrites_config(**kwargs: Any) -> Dict[str, str]:
    """Coder that overwrites a pre-existing repo file (not produced by any microtask)."""
    return {"config.py": "MODIFIED\n"}


def _batch_fix_alias_rewrite(**kwargs: Any) -> SimpleNamespace:
    """A fix that rewrites the current file through an equivalent (aliased) key.

    ``src/a.py`` and ``/src/a.py`` resolve to the same worktree path; returning the
    alias exercises the canonical-path snapshotting in the rollback manifest.
    """
    return SimpleNamespace(files={"/src/a.py": "fixed-but-rejected\n"})


def _coder_writes(path: str):
    """Build a coder that emits a single file at ``path``."""

    def _coder_at(**kwargs: Any) -> Dict[str, str]:
        return {path: "generated-then-rejected\n"}

    return _coder_at


def _batch_fix_writes(path: str):
    """Build a batch fix that emits a single file at ``path`` (ignoring current files)."""

    def _fix_at(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(files={path: "fixed-but-rejected\n"})

    return _fix_at


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
    status_qa_sec=MS.IN_QA_SECURITY_TESTING,
    parallelize_qa_security: bool = False,
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
        parallelize_qa_security=parallelize_qa_security,
        status_qa_security=status_qa_sec,
    )


def _config(
    *,
    cr: int = 1,
    qa: int = 1,
    sec: int = 1,
    on_failure: str = "stop",
    security_stops: bool = True,
    grounding_limit: int = 3,
    grounding_ratio: float = 0.75,
) -> be_models.MicrotaskReviewConfig:
    return be_models.MicrotaskReviewConfig(
        code_review_max_retries=cr,
        qa_max_retries=qa,
        security_max_retries=sec,
        on_failure=on_failure,
        security_failure_always_stops=security_stops,
        grounding_failure_cycle_limit=grounding_limit,
        grounding_failure_ratio_threshold=grounding_ratio,
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
    llm: Optional[Any] = None,
):
    return run_gated_execution_impl(
        gate_config=gate_config,
        llm=llm if llm is not None else DummyLLMClient(),
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
        "qa_security_testing",
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


def test_unmet_dep_without_review_failure_runs_anyway(tmp_path, caplog):
    """An unmet dep that is merely not-yet-complete (not review-failed) still runs.

    Also pins the "running anyway" soft-dependency log line, unchanged by the
    wave-based scheduler: a ``depends_on`` id with no matching microtask in this
    run never becomes a scheduling edge, so ``mt`` lands in the first batch and
    hits this same runtime branch exactly as it did under the old flat order.
    """
    mt = _microtask("mt-1", depends_on=["ghost"])
    with caplog.at_level(logging.WARNING):
        _run(_make_gate_config(), [mt], tmp_path, review_config=_config())

    assert mt.status == MS.COMPLETED
    assert "has unmet deps" in caplog.text
    assert "running anyway" in caplog.text


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


def test_diamond_dependency_executes_in_wave_order(tmp_path):
    """A -> (B, C) -> D: all four complete, coded in wave order (A, then B/C, then D)."""
    mt_a = _microtask("mt-a")
    mt_b = _microtask("mt-b", depends_on=["mt-a"])
    mt_c = _microtask("mt-c", depends_on=["mt-a"])
    mt_d = _microtask("mt-d", depends_on=["mt-b", "mt-c"])
    order: List[str] = []
    cfg = _make_gate_config(coder=_recording_coder(order))

    result = _run(cfg, [mt_a, mt_b, mt_c, mt_d], tmp_path, review_config=_config())

    assert order == ["mt-a", "mt-b", "mt-c", "mt-d"]
    assert all(mt.status == MS.COMPLETED for mt in (mt_a, mt_b, mt_c, mt_d))
    assert "4/4 microtasks successfully" in result.summary


def test_fully_independent_microtasks_execute_as_single_batch(tmp_path):
    """No cross-dependencies: all microtasks schedule into (and run within) one batch."""
    mt1, mt2, mt3 = _microtask("mt-1"), _microtask("mt-2"), _microtask("mt-3")
    order: List[str] = []
    cfg = _make_gate_config(coder=_recording_coder(order))

    result = _run(cfg, [mt1, mt2, mt3], tmp_path, review_config=_config())

    assert order == ["mt-1", "mt-2", "mt-3"]
    assert all(mt.status == MS.COMPLETED for mt in (mt1, mt2, mt3))
    assert "3/3 microtasks successfully" in result.summary


def test_dependent_of_review_failed_is_skipped_across_batch_boundary(tmp_path):
    """The SKIP check fires correctly even when the failure is two batches upstream."""
    mt_a = _microtask("mt-a")
    mt_b = _microtask("mt-b", depends_on=["mt-a"])
    mt_c = _microtask("mt-c", depends_on=["mt-b"])
    cfg = _make_gate_config(code_review_gate=_cr_gate_fails_for("mt-b"))
    _run(
        cfg,
        [mt_a, mt_b, mt_c],
        tmp_path,
        review_config=_config(cr=1, on_failure="skip_continue"),
    )

    assert mt_a.status == MS.COMPLETED
    assert mt_b.status == MS.REVIEW_FAILED
    assert mt_c.status == MS.SKIPPED
    assert "depends on review-failed" in mt_c.notes


# ---------------------------------------------------------------------------
# Wave-based (topological-batch) scheduler — direct unit tests
# ---------------------------------------------------------------------------


def test_schedule_diamond_dependency_batches_into_three_waves():
    """A -> (B, C) -> D batches as [[A], [B, C], [D]]."""
    mt_a = _microtask("mt-a")
    mt_b = _microtask("mt-b", depends_on=["mt-a"])
    mt_c = _microtask("mt-c", depends_on=["mt-a"])
    mt_d = _microtask("mt-d", depends_on=["mt-b", "mt-c"])

    batches = _schedule_microtask_batches([mt_a, mt_b, mt_c, mt_d])

    assert [[m.id for m in b] for b in batches] == [["mt-a"], ["mt-b", "mt-c"], ["mt-d"]]


def test_schedule_unmet_dependency_not_in_run_does_not_block():
    """A ``depends_on`` id with no matching microtask in this run is not a scheduling edge."""
    mt = _microtask("mt-1", depends_on=["ghost"])

    batches = _schedule_microtask_batches([mt])

    assert batches == [[mt]]


def test_schedule_fully_independent_microtasks_form_single_batch():
    """No cross-dependencies: everything lands in one batch, in original order."""
    mt1, mt2, mt3 = _microtask("mt-1"), _microtask("mt-2"), _microtask("mt-3")

    batches = _schedule_microtask_batches([mt1, mt2, mt3])

    assert batches == [[mt1, mt2, mt3]]


def test_schedule_cycle_flushes_remaining_into_final_batch():
    """A dependency cycle can't be scheduled progressively; it's flushed, not looped forever."""
    mt_x = _microtask("mt-x", depends_on=["mt-y"])
    mt_y = _microtask("mt-y", depends_on=["mt-x"])

    batches = _schedule_microtask_batches([mt_x, mt_y])

    assert batches == [[mt_x, mt_y]]


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


def test_rollback_removes_files_added_by_batch_fix(tmp_path):
    """A file introduced by a batch fix is rolled back with the rest on REVIEW_FAILED.

    Regression test: the rollback manifest must grow to include keys added during
    fix cycles. Here code review keeps failing until the retry cap is exhausted; the
    single batch fix adds ``src/new_helper.py`` on top of the original ``src/a.py``.
    When the microtask rolls back, BOTH keys must be gone from the returned
    ``ExecutionResult.files`` — otherwise the new file leaks out of a failed microtask.
    """
    mt = _microtask()
    cfg = _make_gate_config(code_review_gate=_fail_gate(), batch_fix=_batch_fix_adds_file)
    result = _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert "src/a.py" not in result.files
    assert "src/new_helper.py" not in result.files
    # The commit stages the worktree with ``git add -A``, so the created files must
    # also be gone from disk — not just the in-memory result.
    assert not (tmp_path / "src" / "a.py").exists()
    assert not (tmp_path / "src" / "new_helper.py").exists()
    # The microtask's own output record is cleared, so it reports no surviving files.
    assert mt.output_files == {}


def test_rollback_restores_earlier_microtask_file_on_overlap(tmp_path):
    """A failed microtask's rollback restores an earlier microtask's overlapping file.

    ``mt-a`` completes and owns ``shared.py``. ``mt-b`` then fails code review to
    exhaustion; its batch fix rewrites ``shared.py`` (which ``mt-a`` produced). The
    rollback must restore ``mt-a``'s version of ``shared.py`` — not delete it, and
    not leak ``mt-b``'s clobbered content — while removing ``mt-b``'s own file.
    """
    mt_a = _microtask("mt-a")
    mt_b = _microtask("mt-b")
    cfg = _make_gate_config(
        coder=_coder_per_microtask,
        code_review_gate=_cr_gate_fails_for("mt-b"),
        batch_fix=_batch_fix_overwrites_shared,
    )
    result = _run(
        cfg,
        [mt_a, mt_b],
        tmp_path,
        review_config=_config(cr=1, on_failure="skip_continue"),
    )

    assert mt_a.status == MS.COMPLETED
    assert mt_b.status == MS.REVIEW_FAILED
    assert result.files["shared.py"] == "owned-by-a\n"  # earlier version restored
    assert "src/b.py" not in result.files  # mt-b's own file rolled back
    # The worktree is reverted too, so the ``git add -A`` commit sees mt-a's bytes,
    # not mt-b's clobber, and mt-b's own file is gone from disk.
    assert (tmp_path / "shared.py").read_text(encoding="utf-8") == "owned-by-a\n"
    assert not (tmp_path / "src" / "b.py").exists()


def test_rollback_restores_preexisting_repo_file(tmp_path):
    """Rollback restores a pre-existing repo file the failed microtask overwrote.

    A file already in the worktree (never produced by a microtask, so never in
    ``all_files``) must be restored to its original bytes on disk — never deleted —
    while staying absent from ``ExecutionResult.files`` (it is not execution output).
    This is why the manifest keeps two snapshots: the disk snapshot restores the
    bytes, and the ``all_files`` snapshot (``None`` for a never-produced key) keeps
    it out of the result.
    """
    (tmp_path / "config.py").write_text("PRE\n", encoding="utf-8")
    mt = _microtask()
    cfg = _make_gate_config(coder=_coder_overwrites_config, code_review_gate=_fail_gate())
    result = _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert (tmp_path / "config.py").read_text(encoding="utf-8") == "PRE\n"  # disk restored
    assert "config.py" not in result.files  # not reported as execution output


def test_rollback_canonicalizes_alias_keys(tmp_path):
    """An alias spelling in a later fix does not defeat the worktree rollback.

    The microtask writes ``src/a.py`` initially, then a failing fix rewrites the
    same file through the equivalent key ``/src/a.py``. Both spellings resolve to
    one path; on rollback that path must be removed (the microtask created it) with
    no failed bytes left behind, and neither key may survive in the result.
    """
    mt = _microtask()
    cfg = _make_gate_config(
        coder=_coder,  # writes {"src/a.py": ...}
        code_review_gate=_fail_gate(),
        batch_fix=_batch_fix_alias_rewrite,
    )
    result = _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert not (tmp_path / "src" / "a.py").exists()  # created path removed, no failed bytes
    assert "src/a.py" not in result.files
    assert "/src/a.py" not in result.files


def test_rollback_restores_preexisting_binary_file(tmp_path):
    """A pre-existing non-UTF-8 file at an output path is snapshotted/restored as bytes.

    The prior-state read must not decode as UTF-8 (which would raise and spuriously
    fail the microtask before review); on rollback the exact bytes are restored.
    """
    original = b"\xff\xfe\x00\x01not-utf8"
    (tmp_path / "data.bin").write_bytes(original)
    mt = _microtask()
    cfg = _make_gate_config(coder=_coder_writes("data.bin"), code_review_gate=_fail_gate())
    result = _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    # Reached review (REVIEW_FAILED), not a bare FAILED from a decode error at snapshot.
    assert mt.status == MS.REVIEW_FAILED
    assert (tmp_path / "data.bin").read_bytes() == original  # restored byte-for-byte
    assert "data.bin" not in result.files


def test_rollback_preserves_dangling_symlink(tmp_path):
    """Writing through a pre-existing dangling symlink is undone without orphaning bytes.

    The text write follows the symlink and creates its target; on rollback the created
    target is removed and the symlink itself is left intact — nothing the failed
    microtask produced remains in the worktree.
    """
    (tmp_path / "link.py").symlink_to("real_target.py")  # dangling: target does not exist
    mt = _microtask()
    cfg = _make_gate_config(coder=_coder_writes("link.py"), code_review_gate=_fail_gate())
    result = _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert (tmp_path / "link.py").is_symlink()  # symlink itself preserved
    assert not (tmp_path / "real_target.py").exists()  # created target removed
    assert "link.py" not in result.files


def test_rollback_preserves_dangling_symlink_chain(tmp_path):
    """A chain of symlinks ending at a missing target is undone at the ultimate target.

    ``link.py -> middle.py -> real.py`` (real.py missing): the write follows the whole
    chain and creates ``real.py``; rollback removes only that created ultimate target
    and leaves every symlink in the chain intact.
    """
    (tmp_path / "middle.py").symlink_to("real.py")  # middle -> real (missing)
    (tmp_path / "link.py").symlink_to("middle.py")  # link -> middle -> real
    mt = _microtask()
    cfg = _make_gate_config(coder=_coder_writes("link.py"), code_review_gate=_fail_gate())
    result = _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert (tmp_path / "link.py").is_symlink()  # chain intact
    assert (tmp_path / "middle.py").is_symlink()
    assert not (tmp_path / "real.py").exists()  # created ultimate target removed
    assert "link.py" not in result.files


def test_rollback_restores_target_through_nondangling_symlink(tmp_path):
    """Writing through a symlink to an existing in-repo file clobbers the target; on
    rollback the target's prior bytes are restored and the symlink is left intact."""
    (tmp_path / "real.py").write_text("REAL\n", encoding="utf-8")
    (tmp_path / "alias.py").symlink_to("real.py")
    mt = _microtask()
    cfg = _make_gate_config(coder=_coder_writes("alias.py"), code_review_gate=_fail_gate())
    result = _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert (tmp_path / "alias.py").is_symlink()  # symlink intact
    assert (tmp_path / "real.py").read_text(encoding="utf-8") == "REAL\n"  # target restored
    assert "alias.py" not in result.files


def test_rollback_canonicalizes_symlink_and_direct_path_aliases(tmp_path):
    """One physical file written via a symlink then via its direct path collapses to a
    single rollback entry, so the failed content is not restored over the original.

    The coder writes through ``link.py`` (a symlink to ``real.py``); the failing fix
    writes ``real.py`` directly. Both are the same physical file, so only the earliest
    snapshot (``real.py``'s original bytes) is kept and restored.
    """
    (tmp_path / "real.py").write_text("ORIG\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to("real.py")
    mt = _microtask()
    cfg = _make_gate_config(
        coder=_coder_writes("link.py"),
        code_review_gate=_fail_gate(),
        batch_fix=_batch_fix_writes("real.py"),
    )
    result = _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert (tmp_path / "real.py").read_text(encoding="utf-8") == "ORIG\n"  # not failed bytes
    assert (tmp_path / "link.py").is_symlink()
    assert "link.py" not in result.files
    assert "real.py" not in result.files


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
# Per-piece QA/security verdict cache (AgentReviewCache)
# ---------------------------------------------------------------------------


def test_qa_and_security_gates_share_one_cache_instance_across_cycles(tmp_path):
    """QA and security gates receive the same ``AgentReviewCache`` on every cycle
    of one microtask's review loop; the code-review gate never receives one (it
    has its own, separate cross-cycle cache).
    """
    from software_engineering_team.shared.agent_review import AgentReviewCache

    cr = _CapturingGate()
    qa = _CapturingGate([GateOutcome(passed=False, issues=[_issue("qa")], summary="qa")])
    sec = _CapturingGate()
    mt = _microtask()
    _run(
        _make_gate_config(code_review_gate=cr, qa_gate=qa, security_gate=sec),
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=2, sec=1),
        progress=lambda *a: None,
    )

    assert mt.status == MS.COMPLETED
    assert len(qa.calls_kwargs) == 2  # failed once (-> restart from code review), passed next cycle
    assert len(sec.calls_kwargs) == 1  # cycle 1 never reached security (QA restarted the cycle)

    for call_kwargs in cr.calls_kwargs:
        assert "cache" not in call_kwargs  # code review keeps its own separate cache

    caches = [c["cache"] for c in qa.calls_kwargs] + [c["cache"] for c in sec.calls_kwargs]
    assert all(isinstance(c, AgentReviewCache) for c in caches)
    assert len({id(c) for c in caches}) == 1  # every gate call shares the one instance


# ---------------------------------------------------------------------------
# Concurrent QA/Security fan-out (parallelize_qa_security)
# ---------------------------------------------------------------------------


class _NonDummyLLM:
    """Stand-in "real" LLM client eligible for the concurrent QA/Security fan-out.

    Anything that isn't a ``DummyLLMClient`` (or a wrapper around one) qualifies;
    the stub gates below never actually call it.
    """


def test_parallel_qa_security_runs_both_gates_every_cycle(tmp_path):
    """With ``parallelize_qa_security=True`` and a non-Dummy ``llm``, Security still
    runs on a cycle where QA fails -- unlike the sequential path (see
    ``test_qa_and_security_gates_share_one_cache_instance_across_cycles``), which
    restarts from Code Review before Security is ever reached.
    """
    qa = _CapturingGate([GateOutcome(passed=False, issues=[_issue("qa")], summary="qa")])
    sec = _CapturingGate()
    mt = _microtask()
    _run(
        _make_gate_config(qa_gate=qa, security_gate=sec, parallelize_qa_security=True),
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=2, sec=2),
        progress=lambda *a: None,
        llm=_NonDummyLLM(),
    )

    assert mt.status == MS.COMPLETED
    assert len(qa.calls_kwargs) == 2
    assert len(sec.calls_kwargs) == 2  # ran on cycle 1 too, concurrently with the failing QA call


def test_parallel_qa_security_reports_combined_phase_not_premature_security(tmp_path):
    """The concurrent branch must not announce "qa_testing" then immediately
    "security_testing" before either gate has actually run -- that's the exact
    mechanism behind the false "QA passed" checkmark this test guards against.
    It should announce a single combined "qa_security_testing" phase instead,
    with ``mt.status`` set to the matching status at the same time.
    """
    phases_seen: List[str] = []
    statuses_during_qa_security_phase: List[Any] = []

    def _progress(current_index, completed, total, title, phase, detail):
        phases_seen.append(phase)
        if phase == "qa_security_testing":
            statuses_during_qa_security_phase.append(mt.status)

    qa = _CapturingGate()
    sec = _CapturingGate()
    mt = _microtask()
    _run(
        _make_gate_config(qa_gate=qa, security_gate=sec, parallelize_qa_security=True),
        [mt],
        tmp_path,
        review_config=_config(),
        progress=_progress,
        llm=_NonDummyLLM(),
    )

    assert mt.status == MS.COMPLETED
    # The combined phase was announced (both the top-level announcement and
    # each gate's own detail ticks, which forward through progress_callback),
    # and mt.status was IN_QA_SECURITY_TESTING at every point it fired -- never
    # IN_SECURITY_TESTING, which would imply QA had already passed.
    assert statuses_during_qa_security_phase
    assert set(statuses_during_qa_security_phase) == {MS.IN_QA_SECURITY_TESTING}
    # Neither gate's bare phase name (which would let current_microtask_phase
    # land on "security_testing" mid-run) is ever reported while concurrent.
    assert "qa_testing" not in phases_seen
    assert "security_testing" not in phases_seen


def test_parallel_qa_security_batch_fix_uses_combined_phase(tmp_path):
    """The "batch fixing" progress message on a failing concurrent cycle must also
    use the combined "qa_security_testing" phase, not "qa_testing" alone -- both
    gates' issues are being fixed together, not just QA's.
    """
    phases_seen: List[str] = []
    qa = _CapturingGate([GateOutcome(passed=False, issues=[_issue("qa")], summary="qa")])
    sec = _CapturingGate()
    mt = _microtask()
    _run(
        _make_gate_config(qa_gate=qa, security_gate=sec, parallelize_qa_security=True),
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=2, sec=2),
        progress=lambda *a: phases_seen.append(a[4]),
        llm=_NonDummyLLM(),
    )

    assert mt.status == MS.COMPLETED
    assert "qa_security_testing" in phases_seen
    # No bare "qa_testing"/"security_testing" announcement while the cycle's
    # outcome was still unresolved (only the combined phase).
    assert phases_seen.count("qa_testing") == 0
    assert phases_seen.count("security_testing") == 0


def test_parallel_qa_security_combines_both_gates_issues_into_one_batch_fix(tmp_path):
    """A cycle where both QA and Security fail batch-fixes their issues together in
    one call, rather than fixing QA then restarting before Security ever runs.
    """
    qa_issue = ReviewIssue(source="qa", severity="high", description="qa issue", file_path="f")
    sec_issue = ReviewIssue(
        source="security", severity="high", description="security issue", file_path="f"
    )
    qa = _ScriptedGate([GateOutcome(passed=False, issues=[qa_issue], summary="qa")])
    sec = _ScriptedGate([GateOutcome(passed=False, issues=[sec_issue], summary="sec")])
    batch_fix_calls: List[Dict[str, Any]] = []

    def _batch_fix_capturing(*, detail_callback=None, **kwargs: Any) -> SimpleNamespace:
        batch_fix_calls.append(kwargs)
        if detail_callback is not None:
            detail_callback("fixing")
        return SimpleNamespace(files=kwargs["current_files"])

    mt = _microtask()
    _run(
        _make_gate_config(
            qa_gate=qa,
            security_gate=sec,
            batch_fix=_batch_fix_capturing,
            parallelize_qa_security=True,
        ),
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=2, sec=2),
        progress=lambda *a: None,
        llm=_NonDummyLLM(),
    )

    assert mt.status == MS.COMPLETED
    assert len(batch_fix_calls) == 1  # one combined fix, not two separate ones
    sources = {issue.source for issue in batch_fix_calls[0]["issues"]}
    assert sources == {"qa", "security"}


def test_parallel_qa_security_falls_back_to_sequential_for_dummy_llm(tmp_path):
    """``parallelize_qa_security=True`` still runs sequentially for a
    ``DummyLLMClient`` (the default ``_run`` llm) -- its scripted response index
    is not thread-safe, so the fan-out must not run concurrently for it.
    """
    qa = _CapturingGate([GateOutcome(passed=False, issues=[_issue("qa")], summary="qa")])
    sec = _CapturingGate()
    mt = _microtask()
    _run(
        _make_gate_config(qa_gate=qa, security_gate=sec, parallelize_qa_security=True),
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=2, sec=1),
        progress=lambda *a: None,
    )

    assert mt.status == MS.COMPLETED
    assert len(qa.calls_kwargs) == 2
    assert len(sec.calls_kwargs) == 1  # cycle 1 never reached security (QA restarted the cycle)


def test_parallel_qa_security_unsafe_fix_breaks(tmp_path):
    """Concurrent path's unsafe-write handling mirrors the sequential path's
    (``test_qa_unsafe_fix_breaks``/``test_security_unsafe_fix_breaks``): an
    unsafe fix write marks REVIEW_FAILED via the same
    ``write_microtask_output_or_fail`` call, just reached through the combined
    batch-fix branch instead of the QA-only or security-only one.
    """
    mt = _microtask()
    cfg = _make_gate_config(
        qa_gate=_fail_gate("qa"),
        batch_fix=_batch_fix_unsafe,
        parallelize_qa_security=True,
    )
    _run(
        cfg,
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=1, sec=1, on_failure="skip_continue"),
        llm=_NonDummyLLM(),
    )

    assert mt.status == MS.REVIEW_FAILED


def test_parallel_qa_security_only_batch_fixes_the_failing_gates_issues(tmp_path):
    """A passing gate's (non-blocking) issues never reach the combined batch fix
    -- only the failing gate's issues do, matching the sequential path's
    per-gate behavior (a passing gate's issues were never sent to
    ``run_batch_coding_fixes`` there either).
    """
    qa_medium_issue = ReviewIssue(
        source="qa", severity="medium", description="qa non-blocking note", file_path="f"
    )
    sec_critical_issue = ReviewIssue(
        source="security", severity="critical", description="security blocker", file_path="f"
    )
    # QA "passes" (no critical/high issues) but still reports a non-blocking issue;
    # Security fails outright. Only the security issue should reach batch_fix.
    qa = _ScriptedGate([GateOutcome(passed=True, issues=[qa_medium_issue], summary="qa")])
    sec = _ScriptedGate([GateOutcome(passed=False, issues=[sec_critical_issue], summary="sec")])
    batch_fix_calls: List[Dict[str, Any]] = []

    def _batch_fix_capturing(*, detail_callback=None, **kwargs: Any) -> SimpleNamespace:
        batch_fix_calls.append(kwargs)
        if detail_callback is not None:
            detail_callback("fixing")
        return SimpleNamespace(files=kwargs["current_files"])

    mt = _microtask()
    _run(
        _make_gate_config(
            qa_gate=qa,
            security_gate=sec,
            batch_fix=_batch_fix_capturing,
            parallelize_qa_security=True,
        ),
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=2, sec=2),
        progress=lambda *a: None,
        llm=_NonDummyLLM(),
    )

    assert mt.status == MS.COMPLETED
    assert len(batch_fix_calls) == 1
    sources = {issue.source for issue in batch_fix_calls[0]["issues"]}
    assert sources == {"security"}  # QA's non-blocking issue never reached the fixer


# ---------------------------------------------------------------------------
# Concurrent QA/Security fan-out: ordering, aggregation symmetry, exceptions,
# and timing-variance determinism (issue #2554)
# ---------------------------------------------------------------------------


def test_parallel_qa_security_preserves_global_gate_ordering(tmp_path):
    """Ordering non-regression: Code Review always precedes the QA/Security pair,
    a combined batch-fix always precedes the next Code Review (cycle restart),
    and Documentation only starts after a cycle where both QA and Security
    passed -- while QA vs Security's *relative* order within their concurrent
    pair is deliberately left unconstrained (asserted as a set, not a sequence).

    Cycle 1: Code Review passes, QA fails + Security passes concurrently ->
    combined batch-fix -> restart. Cycle 2: Code Review passes, QA + Security
    both pass -> Documentation runs. Expected global order (positions 1 and 5
    are a free-order pair, not a fixed sequence):
        [code_review, {qa, security}, batch_fix, code_review, {qa, security}, doc_review]
    """
    order: List[str] = []
    order_lock = threading.Lock()

    def _record(name: str) -> None:
        with order_lock:
            order.append(name)

    def _cr_gate(**kwargs: Any) -> GateOutcome:
        _record("code_review")
        return GateOutcome(passed=True)

    qa_calls = {"n": 0}

    def _qa_gate(**kwargs: Any) -> GateOutcome:
        _record("qa")
        qa_calls["n"] += 1
        if qa_calls["n"] == 1:
            return GateOutcome(passed=False, issues=[_issue("qa")], summary="qa")
        return GateOutcome(passed=True)

    def _sec_gate(**kwargs: Any) -> GateOutcome:
        _record("security")
        return GateOutcome(passed=True)

    def _batch_fix_recording(*, detail_callback=None, **kwargs: Any) -> SimpleNamespace:
        _record("batch_fix")
        if detail_callback is not None:
            detail_callback("fixing")
        return SimpleNamespace(files=kwargs["current_files"])

    def _doc_review_recording(
        *, documentation=None, detail_callback=None, **kwargs: Any
    ) -> SimpleNamespace:
        _record("doc_review")
        if detail_callback is not None:
            detail_callback("doc")
        return SimpleNamespace(documentation={}, iterations=1, final_quality_score=0.95)

    mt = _microtask()
    _run(
        _make_gate_config(
            code_review_gate=_cr_gate,
            qa_gate=_qa_gate,
            security_gate=_sec_gate,
            batch_fix=_batch_fix_recording,
            doc_review=_doc_review_recording,
            parallelize_qa_security=True,
        ),
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=2, sec=2),
        progress=lambda *a: None,
        llm=_NonDummyLLM(),
    )

    assert mt.status == MS.COMPLETED
    assert len(order) == 8
    assert order[0] == "code_review"  # cycle 1 CR precedes the QA/security pair
    assert set(order[1:3]) == {"qa", "security"}  # concurrent pair: order is free
    assert order[3] == "batch_fix"  # combined fix precedes the cycle restart
    assert order[4] == "code_review"  # cycle 2 CR restarts before QA/security again
    assert set(order[5:7]) == {"qa", "security"}
    assert order[7] == "doc_review"  # Documentation only after both gates passed


def test_parallel_qa_security_symmetric_case_qa_fails_security_passes_with_noise(tmp_path):
    """Symmetric counterpart to
    ``test_parallel_qa_security_only_batch_fixes_the_failing_gates_issues``
    (which covers QA-passes-with-noise/Security-fails): here QA fails outright
    and Security passes but still reports a non-blocking issue. Only QA's
    issue should reach the combined batch fix.
    """
    qa_critical_issue = ReviewIssue(
        source="qa", severity="critical", description="qa blocker", file_path="f"
    )
    sec_medium_issue = ReviewIssue(
        source="security",
        severity="medium",
        description="security non-blocking note",
        file_path="f",
    )
    qa = _ScriptedGate([GateOutcome(passed=False, issues=[qa_critical_issue], summary="qa")])
    sec = _ScriptedGate([GateOutcome(passed=True, issues=[sec_medium_issue], summary="sec")])
    batch_fix_calls: List[Dict[str, Any]] = []

    def _batch_fix_capturing(*, detail_callback=None, **kwargs: Any) -> SimpleNamespace:
        batch_fix_calls.append(kwargs)
        if detail_callback is not None:
            detail_callback("fixing")
        return SimpleNamespace(files=kwargs["current_files"])

    mt = _microtask()
    _run(
        _make_gate_config(
            qa_gate=qa,
            security_gate=sec,
            batch_fix=_batch_fix_capturing,
            parallelize_qa_security=True,
        ),
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=2, sec=2),
        progress=lambda *a: None,
        llm=_NonDummyLLM(),
    )

    assert mt.status == MS.COMPLETED
    assert len(batch_fix_calls) == 1
    sources = {issue.source for issue in batch_fix_calls[0]["issues"]}
    assert sources == {"qa"}  # security's non-blocking issue never reached the fixer


def test_parallel_qa_security_gate_exception_propagates_without_hanging(tmp_path):
    """Pins the current contract when a gate callable violates the "never raises"
    assumption documented at ``_run_review_cycles``'s ``parallel_map`` call site
    (review_cycle.py): a plain exception from one concurrent gate is **not**
    swallowed or silently lost -- it propagates out of
    ``run_gated_execution_impl`` unchanged (via ``parallel_map``'s fast-fail
    ``fut.result()`` re-raise) -- and, because the call site passes
    ``wait_for_stragglers=True``, the *other*, still-running gate is waited on
    to completion first rather than abandoned in a leaked background thread.
    """
    sec_started = threading.Event()
    sec_finished: List[bool] = []

    def _qa_gate_raises(**kwargs: Any) -> GateOutcome:
        # Wait until security's worker thread has actually entered its body
        # (past ``parallel_map``'s ``_guarded`` abort check) before raising, so
        # this deterministically exercises the "already-running task" path
        # rather than racing thread-pool startup against an instant raise.
        assert sec_started.wait(timeout=2), "security gate never started"
        raise RuntimeError("qa boom")

    def _security_gate_slow(**kwargs: Any) -> GateOutcome:
        sec_started.set()
        time.sleep(0.05)
        sec_finished.append(True)
        return GateOutcome(passed=True)

    mt = _microtask()
    with pytest.raises(RuntimeError, match="qa boom"):
        _run(
            _make_gate_config(
                qa_gate=_qa_gate_raises,
                security_gate=_security_gate_slow,
                parallelize_qa_security=True,
            ),
            [mt],
            tmp_path,
            review_config=_config(cr=1, qa=1, sec=1),
            progress=lambda *a: None,
            llm=_NonDummyLLM(),
        )

    # The exception only surfaces after the already-running security call
    # finished -- it was waited on, not left running in the background.
    assert sec_finished == [True]


@pytest.mark.parametrize(
    "first_to_finish",
    [
        pytest.param("qa", id="qa_finishes_first"),
        pytest.param("security", id="security_finishes_first"),
    ],
)
def test_parallel_qa_security_aggregation_is_order_independent_under_timing_variance(
    tmp_path, first_to_finish: str
) -> None:
    """No flakiness under concurrent execution: each gate's completion order is
    forced deterministically via a ``threading.Event`` (the second gate blocks
    until the first signals) rather than a fixed ``time.sleep`` race -- a bare
    sleep does not guarantee which gate actually finishes first under a loaded
    runner, so it could silently exercise the same order in both parametrized
    cases without the test ever noticing. The actual completion order is
    recorded and asserted here, and the combined batch-fix issue sources must
    be identical (order-independent) regardless of which gate finished first.
    """
    qa_issue = ReviewIssue(source="qa", severity="high", description="qa issue", file_path="f")
    sec_issue = ReviewIssue(
        source="security", severity="high", description="security issue", file_path="f"
    )

    completion_order: List[str] = []
    order_lock = threading.Lock()
    release_event = threading.Event()

    def _make_flaky_gate(name: str, issue: ReviewIssue, *, goes_first: bool):
        # Fails on the first cycle only, then passes -- so the fan-out's single
        # failing cycle still exercises the forced completion order, without
        # exhausting the retry budget.
        calls = {"n": 0}

        def _gate(**kwargs: Any) -> GateOutcome:
            calls["n"] += 1
            if calls["n"] == 1:
                if goes_first:
                    with order_lock:
                        completion_order.append(name)
                    release_event.set()
                else:
                    assert release_event.wait(timeout=2), f"{name} gate: dependency never finished"
                    with order_lock:
                        completion_order.append(name)
                return GateOutcome(passed=False, issues=[issue], summary=name)
            return GateOutcome(passed=True)

        return _gate

    _qa_gate = _make_flaky_gate("qa", qa_issue, goes_first=(first_to_finish == "qa"))
    _sec_gate = _make_flaky_gate("security", sec_issue, goes_first=(first_to_finish == "security"))

    batch_fix_calls: List[Dict[str, Any]] = []

    def _batch_fix_capturing(*, detail_callback=None, **kwargs: Any) -> SimpleNamespace:
        batch_fix_calls.append(kwargs)
        if detail_callback is not None:
            detail_callback("fixing")
        return SimpleNamespace(files=kwargs["current_files"])

    mt = _microtask()
    _run(
        _make_gate_config(
            qa_gate=_qa_gate,
            security_gate=_sec_gate,
            batch_fix=_batch_fix_capturing,
            parallelize_qa_security=True,
        ),
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=2, sec=2),
        progress=lambda *a: None,
        llm=_NonDummyLLM(),
    )

    assert mt.status == MS.COMPLETED
    assert completion_order == (
        ["qa", "security"] if first_to_finish == "qa" else ["security", "qa"]
    )
    assert len(batch_fix_calls) == 1
    sources = {issue.source for issue in batch_fix_calls[0]["issues"]}
    assert sources == {"qa", "security"}


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
    from software_engineering_team.shared.phases.review_cycle import _terminal_failing_outcome

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
        "software_engineering_team.shared.phases.review_cycle.record_gate_outcome",
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
        "software_engineering_team.shared.phases.review_cycle.record_gate_outcome",
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
    assert any(getattr(i, "source", None) == "qa" for i in result.issues)
    assert kw.get("task_id") == "t1"
    assert kw.get("phase") == "execution"
    assert kw.get("job_id") == ""


def test_record_gate_outcome_not_called_on_success(tmp_path, monkeypatch):
    """Happy-path completion must not record a gate rejection."""
    calls: List[tuple] = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.review_cycle.record_gate_outcome",
        lambda *a, **k: calls.append((a, k)) or True,
    )
    _run(_make_gate_config(), [_microtask()], tmp_path, review_config=_config())
    assert calls == []


def test_record_gate_outcome_not_called_on_qa_recovered(tmp_path, monkeypatch):
    """Mid-loop QA fail → fix → pass is not a terminal failure; no recording."""
    calls: List[tuple] = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.review_cycle.record_gate_outcome",
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
        "software_engineering_team.shared.phases.review_cycle.record_gate_outcome",
        lambda *a, **k: calls.append((a, k)) or True,
    )
    mt = _microtask()
    cfg = _make_gate_config(code_review_gate=_fail_gate(), batch_fix=_batch_fix_unsafe)
    _run(cfg, [mt], tmp_path, review_config=_config(cr=1, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert "unsafe output path" in mt.notes
    assert calls == []


# ---------------------------------------------------------------------------
# Grounding-failure circuit breaker + issue dedup
# ---------------------------------------------------------------------------


def _cr_high_ratio_fail(raw: int = 4, kept: int = 0) -> GateOutcome:
    """A failing CR outcome whose rejection ratio is at/above the default 0.75 threshold."""
    return GateOutcome(
        passed=False,
        issues=[_issue() for _ in range(kept)],
        summary="hallucinated issues",
        raw_issue_count=raw,
    )


def test_grounding_circuit_breaker_trips_before_max_cycles(tmp_path, monkeypatch):
    """Three consecutive high-ratio failing CR cycles trip the breaker well before
    the ordinary max-cycles budget would exhaust. QA fails once per cycle to force
    a restart; on the tripping cycle CR itself ends up passed (after its retry fix)
    but the streak already hit the limit, so the breaker preempts QA entirely."""
    calls: List[tuple] = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.review_cycle.record_gate_outcome",
        lambda gate, result, **kw: calls.append((gate, result, kw)) or True,
    )
    cr = _ScriptedGate(
        [
            _cr_high_ratio_fail(),
            GateOutcome(passed=True),
            _cr_high_ratio_fail(),
            GateOutcome(passed=True),
            _cr_high_ratio_fail(),
        ]
    )
    qa = _fail_gate("qa")
    mt = _microtask()
    cfg = _make_gate_config(code_review_gate=cr, qa_gate=qa)
    _run(
        cfg,
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=5, sec=5, on_failure="skip_continue"),
    )

    assert mt.status == MS.REVIEW_FAILED
    assert "circuit breaker" in mt.notes
    assert len(calls) == 1
    gate, result, kw = calls[0]
    assert gate == "review_grounding_circuit_breaker"
    # Telemetry must record a rejected outcome even when the tripping cycle's
    # settled CR passed after a retry — otherwise record_gate_outcome no-ops.
    assert result.passed is False
    assert result.raw_issue_count == 4
    assert kw.get("task_id") == "t1"
    assert kw.get("phase") == "execution"
    assert kw.get("job_id") == ""
    # 3 outer cycles capped by the breaker, not the 11-cycle max_total_cycles budget.
    assert cr.calls <= 6
    assert cr.calls < 2 * (1 + 5 + 5)


def test_grounding_low_ratio_no_trip_retry_exhausted(tmp_path, monkeypatch):
    """A low-ratio failing CR (below threshold) never marks the cycle bad; the
    microtask still fails via ordinary retry exhaustion, not the breaker."""
    calls: List[tuple] = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.review_cycle.record_gate_outcome",
        lambda gate, result, **kw: calls.append((gate, result, kw)) or True,
    )

    def _low_ratio_gate(**kwargs: Any) -> GateOutcome:
        return GateOutcome(
            passed=False,
            issues=[_issue(), _issue(), _issue()],
            summary="low ratio",
            raw_issue_count=4,
        )

    mt = _microtask()
    cfg = _make_gate_config(code_review_gate=_low_ratio_gate)
    _run(cfg, [mt], tmp_path, review_config=_config(cr=2, on_failure="skip_continue"))

    assert mt.status == MS.REVIEW_FAILED
    assert len(calls) == 1
    gate, _, _ = calls[0]
    assert gate == "code_review_retry_exhausted"


def test_grounding_pass_only_never_trips(tmp_path, monkeypatch):
    """CR that always passes but reports a high raw_issue_count (heavy grounding
    drops) never counts as a bad cycle -- passed calls are never grounding-bad."""
    calls: List[tuple] = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.review_cycle.record_gate_outcome",
        lambda gate, result, **kw: calls.append((gate, result, kw)) or True,
    )

    def _pass_high_raw(**kwargs: Any) -> GateOutcome:
        return GateOutcome(passed=True, issues=[], summary="", raw_issue_count=4)

    qa = _ScriptedGate([GateOutcome(passed=False, issues=[_issue("qa")], summary="qa")] * 4)
    mt = _microtask()
    cfg = _make_gate_config(code_review_gate=_pass_high_raw, qa_gate=qa)
    _run(
        cfg,
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=5, sec=1, on_failure="skip_continue"),
    )

    assert mt.status == MS.COMPLETED
    assert calls == []


def test_dedup_suppresses_repeated_issue_across_batch_fixes(tmp_path):
    """The same (file_path, description) issue across two consecutive CR batch-fix
    calls is not passed to the fixer a second time."""
    captured: List[List[Any]] = []

    def _capturing_batch_fix(*, issues, detail_callback=None, **kwargs: Any) -> SimpleNamespace:
        captured.append(list(issues))
        if detail_callback is not None:
            detail_callback("fixing")
        return SimpleNamespace(files=kwargs["current_files"])

    repeat_issue = _issue()
    cr = _ScriptedGate(
        [
            GateOutcome(passed=False, issues=[repeat_issue], summary="bad"),
            GateOutcome(passed=False, issues=[repeat_issue], summary="still bad"),
        ]
    )
    mt = _microtask()
    cfg = _make_gate_config(code_review_gate=cr, batch_fix=_capturing_batch_fix)
    _run(cfg, [mt], tmp_path, review_config=_config(cr=2, on_failure="skip_continue"))

    assert len(captured) == 2
    assert len(captured[0]) == 1  # first attempt: not yet seen
    assert captured[1] == []  # exact repeat suppressed on the second attempt


def test_grounding_breaker_disabled_never_records(tmp_path, monkeypatch):
    """``grounding_failure_cycle_limit=0`` disables the breaker outright, even
    across repeated high-ratio failing CR cycles."""
    calls: List[tuple] = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.review_cycle.record_gate_outcome",
        lambda gate, result, **kw: calls.append((gate, result, kw)) or True,
    )
    cr = _ScriptedGate(
        [
            _cr_high_ratio_fail(),
            GateOutcome(passed=True),
            _cr_high_ratio_fail(),
            GateOutcome(passed=True),
            _cr_high_ratio_fail(),
            GateOutcome(passed=True),
        ]
    )
    qa = _fail_gate("qa")
    mt = _microtask()
    cfg = _make_gate_config(code_review_gate=cr, qa_gate=qa)
    _run(
        cfg,
        [mt],
        tmp_path,
        review_config=_config(cr=1, qa=1, sec=1, grounding_limit=0, on_failure="skip_continue"),
    )

    assert all(gate != "review_grounding_circuit_breaker" for gate, *_ in calls)
