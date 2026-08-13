"""Sequential Code Review / QA / Security gate loop for the gated execution
skeleton in ``execution.py``.

The gated per-microtask loop (``run_gated_execution_impl``) runs each
microtask's coded output through a sequential Code Review → QA → Security
review, batch-fixing issues and restarting from Code Review on a QA/security
failure, up to a per-run cycle budget. This module holds that loop
(``_run_review_cycles``) and its supporting gate-outcome/circuit-breaker
machinery — factored out of ``execution.py`` so the review loop can be read
and tested independently of the coding/documentation phases that surround it.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from llm_service import LLMClient
from shared.concurrency import KeyedLockManager, parallel_map
from shared.dev_models.models import ReviewContext, Task
from software_engineering_team.shared.agent_review import AgentReviewCache
from software_engineering_team.shared.gate_outcomes import record_gate_outcome
from software_engineering_team.shared.phases.rollback import (
    _file_lock_keys,
    _MicrotaskRollback,
    _record_prior_values,
    _rollback_microtask_files,
)
from software_engineering_team.shared.repo_writer import (
    UnsafeRepoPathError,
    write_repo_text_files,
)
from software_engineering_team.shared.v2_review import _review_steps_run_sequentially

if TYPE_CHECKING:
    from software_engineering_team.shared.phases.execution import (
        GatedExecutionConfig,
        ReviewDependencies,
    )

logger = logging.getLogger(__name__)


class _HeldFileLockManager(KeyedLockManager[str]):
    """No-op :class:`KeyedLockManager` used when the caller already holds the pipeline locks.

    :class:`KeyedLockManager` is not reentrant. Inner snapshot/write/rollback/doc
    helpers must not re-acquire keys the outer per-microtask ``with file_locks.lock``
    already holds for the full overlapping pipeline.
    """

    @contextmanager
    def lock(self, keys: Iterable[str]) -> Iterator[None]:
        del keys
        yield


_HELD_FILE_LOCKS: KeyedLockManager[str] = _HeldFileLockManager()


def _dedup_issues(issues: List[Any], seen: set[tuple[str, str]]) -> List[Any]:
    """Remove duplicate issues across review cycles based on (file_path, description).

    Preconditions:
        ``seen`` accumulates ``(file_path, description)`` keys across calls.
    Postconditions:
        Returns issues whose key was not already in ``seen``; mutates ``seen``.
        Elements of ``issues`` are read via ``getattr`` rather than assumed to
        carry ``file_path``/``description`` attributes, so a shapeless element
        (e.g. a dict) is deduped on an empty-string key instead of raising.
    """
    unique: List[Any] = []
    for issue in issues:
        key = (getattr(issue, "file_path", None) or "", getattr(issue, "description", None) or "")
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


# Writing microtask output files is the same guarded operation as the
# documentation phase's writer — both delegate to the one shared implementation.
_write_microtask_files = write_repo_text_files


def write_microtask_output_or_fail(
    repo_path: Path,
    files: Dict[str, str],
    *,
    mt: Any,
    task_id: str,
    review_failed_ids: set,
    all_files: Dict[str, str],
    rollback: _MicrotaskRollback,
    review_failed_status: Any,
) -> bool:
    """Write a microtask's output files, converting a rejected path into a failure.

    Shared by both teams' ``run_execution_with_review_gates`` review-cycle write
    sites. A safe write returns ``True``. An :class:`UnsafeRepoPathError` (an LLM
    fix emitted a traversal/empty path) is turned into a handled review failure:
    the microtask is marked with ``review_failed_status``, its contributions are
    rolled back out of ``all_files`` and the worktree, and the function returns
    ``False`` so the caller stops processing this microtask instead of letting the
    exception abort the run.

    Preconditions:
        ``review_failed_status`` is the team's ``MicrotaskStatus.REVIEW_FAILED``;
        ``rollback`` was populated by :func:`_record_prior_values` before each write.
    Postconditions:
        On success the files are on disk and ``True`` is returned. On rejection no
        unsafe file is written, ``mt`` is marked review-failed, this microtask's
        contributions are rolled back out of ``all_files`` and the worktree (prior
        values restored), and ``False`` is returned. Never raises for an unsafe path.
    """
    try:
        _write_microtask_files(repo_path, files)
        return True
    except UnsafeRepoPathError as exc:
        logger.warning("[%s] Microtask %s: unsafe output path rejected: %s", task_id, mt.id, exc)
        mt.status = review_failed_status
        mt.notes = f"Rejected unsafe output path: {exc}"
        review_failed_ids.add(mt.id)
        _rollback_microtask_files(rollback, all_files, mt)
        return False


def _locked_write_and_merge(
    *,
    file_locks: KeyedLockManager[str],
    repo_path: Path,
    files: Dict[str, str],
    mt: Any,
    task_id: str,
    review_failed_ids: set,
    all_files: Dict[str, str],
    rollback: _MicrotaskRollback,
    review_failed_status: Any,
) -> bool:
    """Snapshot, write, and merge ``files`` as one locked critical section.

    Holds the per-path write lock across the rollback snapshot, the worktree
    write, and the ``all_files`` / ``mt.output_files`` merge so concurrent
    microtasks that touch overlapping paths cannot tear those two views apart.
    Lock keys are physical (``realpath``) paths, so ``shared.py`` and
    ``./shared.py`` serialize against each other.

    Preconditions:
        ``file_locks`` is the per-run :class:`KeyedLockManager`; ``rollback`` is
        this microtask's rollback manifest (earliest snapshot still wins).
    Postconditions:
        On success the worktree and ``all_files`` both contain ``files``,
        ``mt.output_files`` equals ``files``, and True is returned. On an
        unsafe-path rejection, rollback has already run under the same lock
        and False is returned.
    """
    with file_locks.lock(_file_lock_keys(repo_path, files.keys())):
        _record_prior_values(rollback, repo_path, all_files, files)
        if not write_microtask_output_or_fail(
            repo_path,
            files,
            mt=mt,
            task_id=task_id,
            review_failed_ids=review_failed_ids,
            all_files=all_files,
            rollback=rollback,
            review_failed_status=review_failed_status,
        ):
            return False
        mt.output_files = files
        all_files.update(files)
        return True


def _locked_rollback(
    file_locks: KeyedLockManager[str],
    rollback: _MicrotaskRollback,
    all_files: Dict[str, str],
    mt: Any,
) -> None:
    """Run :func:`_rollback_microtask_files` while holding this microtask's file keys.

    Preconditions:
        ``rollback.disk_prior`` lists the physical paths this microtask wrote
        (preferred lock keys); ``rollback.all_files_prior`` is the fallback
        when nothing was snapshotted on disk.
    Postconditions:
        ``all_files`` and the worktree match the pre-microtask snapshot. An
        empty key set is a no-op lock (the rollback still runs).
    """
    lock_keys = [str(path) for path in rollback.disk_prior] or list(rollback.all_files_prior)
    with file_locks.lock(lock_keys):
        _rollback_microtask_files(rollback, all_files, mt)


@dataclass
class GateOutcome:
    """Normalised result a review gate reports back to the shared loop.

    Each team's gate adapter maps its own review type into this shape: the
    backend maps a ``PhaseReviewResult``; the frontend maps a ``ReviewResult``
    after filtering issues by ``source``. The skeleton only reads these
    fields, so it stays decoupled from the per-team review models.

    Invariants:
        ``passed`` is ``True`` iff the gate is satisfied and no fix is needed;
        ``issues`` are exactly the issues to batch-fix when ``not passed``.
    """

    passed: bool
    issues: List[Any] = field(default_factory=list)
    summary: str = ""
    raw_issue_count: Optional[int] = None


def grounding_rejection_ratio(raw_issue_count: Optional[int], kept_count: int) -> Optional[float]:
    """Compute the fraction of raw LLM issues rejected by grounding.

    Preconditions:
        ``kept_count`` is a non-negative integer count of issues retained after
        grounding (callers may pass negative values; they are clamped).
    Postconditions:
        Returns ``None`` when ``raw_issue_count`` is ``None`` or ``<= 0``;
        otherwise returns ``(raw - kept) / raw`` with ``kept`` clamped to
        ``[0, raw_issue_count]``.
    """
    if raw_issue_count is None or raw_issue_count <= 0:
        return None
    kept = max(0, min(kept_count, raw_issue_count))
    return (raw_issue_count - kept) / float(raw_issue_count)


def cr_call_is_grounding_bad(
    *,
    passed: bool,
    raw_issue_count: Optional[int],
    kept_count: int,
    ratio_threshold: float,
) -> bool:
    """Return whether a failed code-review call is grounding-heavy.

    Preconditions:
        ``passed`` reflects the gate outcome; ``kept_count`` is a non-negative
        integer; ``ratio_threshold`` is a float (clamped to ``[0.0, 1.0]``).
    Postconditions:
        Returns ``False`` when ``passed`` is ``True``, when the rejection ratio
        is undefined, or when the ratio is below the clamped threshold;
        otherwise returns ``True``.
    """
    if passed:
        return False
    ratio = grounding_rejection_ratio(raw_issue_count, kept_count)
    if ratio is None:
        return False
    threshold = max(0.0, min(1.0, float(ratio_threshold)))
    return ratio >= threshold


def _terminal_failing_outcome(cr: GateOutcome, qa: GateOutcome, sec: GateOutcome) -> GateOutcome:
    """Pick the GateOutcome that best explains a max-cycles REVIEW_FAILED.

    Preconditions:
        ``cr``, ``qa``, and ``sec`` are the last outcomes of each gate for this
        microtask (may be the initial passed=True placeholders if a gate never ran).
    Postconditions:
        Returns the first outcome with ``passed=False`` in order code review →
        QA → security; otherwise a synthetic ``GateOutcome(passed=False,
        summary=\"Max cycles exceeded\")``.
    """
    for outcome in (cr, qa, sec):
        if not outcome.passed:
            return outcome
    return GateOutcome(passed=False, summary="Max cycles exceeded")


# Reused (not duplicated) from ``shared.v2_review``, which already exports it
# in ``__all__`` for exactly this cross-module use — both ``backend_code_v2_team``'s
# and ``frontend_code_v2_team``'s ``phases/review.py`` already import it the same
# way. ``DummyLLMClient`` doubles use a shared non-thread-safe scripted response
# index, so they are not safe under concurrent fan-out; this same check gates
# ``shared.v2_review``'s own code-review/QA/security ``parallel_map`` fan-out.
_qa_security_run_sequentially = _review_steps_run_sequentially


def _record_terminal_gate_failure(gate: str, outcome: Any, task_id: str) -> None:
    """Best-effort DORA + learning record for a terminal REVIEW_FAILED.

    Preconditions:
        ``gate`` is a non-empty string (e.g. ``\"code_review_retry_exhausted\"``,
        ``\"review_max_cycles\"``, or a future ``\"review_grounding_circuit_breaker\"``);
        ``outcome`` is duck-typed for ``is_rejected`` (``passed`` / ``approved`` /
        ``all_satisfied``).
    Postconditions:
        Calls ``record_gate_outcome`` once with ``job_id=\"\"`` and
        ``phase=\"execution\"``; never raises into the gated loop.
    """
    record_gate_outcome(gate, outcome, job_id="", task_id=task_id, phase="execution")


def _apply_code_review_retry_exhausted(
    *,
    phase_failed: bool,
    mt: Any,
    cr_outcome: GateOutcome,
    code_review_retry_cap: int,
    task_id: str,
    review_failed_ids: set,
    all_files: Dict[str, str],
    rollback: _MicrotaskRollback,
    review_failed_status: Any,
    file_locks: KeyedLockManager[str],
) -> bool:
    """Mark retry-exhaustion REVIEW_FAILED unless write-path already failed.

    Preconditions:
        Caller has confirmed ``not cr_outcome.passed`` after the retry loop.
        When ``phase_failed`` is True, ``write_microtask_output_or_fail`` already
        marked the microtask and rolled back files.
    Postconditions:
        When ``phase_failed`` was False: sets ``mt.status`` to
        ``review_failed_status``, adds ``mt.id`` to ``review_failed_ids``, sets
        ``mt.notes`` to a retry-exhaustion message, records
        ``code_review_retry_exhausted``, rolls back this microtask's
        contributions from ``all_files`` and the worktree, and returns True.
        When ``phase_failed`` was True: leaves ``mt.status``/``mt.notes``/
        ``review_failed_ids``/telemetry untouched and returns True (caller
        still stops the outer review cycle).
    """
    if phase_failed:
        return True
    mt.status = review_failed_status
    review_failed_ids.add(mt.id)
    mt.notes = (
        f"Code review failed after {code_review_retry_cap} batch fix attempts: {cr_outcome.summary}"
    )
    _record_terminal_gate_failure("code_review_retry_exhausted", cr_outcome, task_id)
    logger.warning(
        "[%s] Microtask %s: CODE_REVIEW_FAILED after %d batch fix attempts. Issues: %s",
        task_id,
        mt.id,
        code_review_retry_cap,
        cr_outcome.summary,
    )
    _locked_rollback(file_locks, rollback, all_files, mt)
    return True


def _apply_grounding_circuit_breaker_trip(
    *,
    mt: Any,
    cr_outcome: GateOutcome,
    telemetry_outcome: Optional[GateOutcome],
    grounding_failure_streak: int,
    cycle_limit: int,
    task_id: str,
    review_failed_ids: set,
    all_files: Dict[str, str],
    rollback: _MicrotaskRollback,
    review_failed_status: Any,
    file_locks: KeyedLockManager[str],
) -> bool:
    """Trip the grounding-failure circuit breaker for a hallucination-driven CR loop.

    Mirrors :func:`_apply_code_review_retry_exhausted`'s shape, but for the
    distinct "consecutive high-rejection-ratio cycles" failure mode: unlike
    retry exhaustion, this can fire even when ``cr_outcome.passed`` is True for
    the current cycle (the streak was built by earlier bad calls in this same
    outer cycle, including ones a later retry then fixed).

    Preconditions:
        Caller has already confirmed the current outer cycle did not end via a
        write-path failure (those are marked and rolled back by
        ``write_microtask_output_or_fail`` and must never be attributed to this
        breaker), and that ``cycle_limit > 0`` and
        ``grounding_failure_streak >= cycle_limit``.
    Postconditions:
        Sets REVIEW_FAILED status with breaker-specific notes (distinct from
        retry-exhaustion's message), records ``review_grounding_circuit_breaker``
        once via ``_record_terminal_gate_failure`` using a *rejected* telemetry
        outcome (the last grounding-bad CR call when available; otherwise a
        synthetic ``passed=False`` copy of ``cr_outcome`` — never the final
        ``passed=True`` settle, which ``record_gate_outcome`` would silently
        skip), rolls back this microtask's contributions from ``all_files`` and
        the worktree, and returns True.
    """
    mt.status = review_failed_status
    review_failed_ids.add(mt.id)
    mt.notes = (
        f"Grounding-failure circuit breaker tripped after {grounding_failure_streak} "
        f"consecutive high-rejection-ratio code review cycles (limit {cycle_limit})."
    )
    record_outcome = telemetry_outcome
    if record_outcome is None or record_outcome.passed:
        record_outcome = GateOutcome(
            passed=False,
            issues=list(cr_outcome.issues),
            summary=cr_outcome.summary or "Grounding-failure circuit breaker tripped",
            raw_issue_count=cr_outcome.raw_issue_count,
        )
    _record_terminal_gate_failure("review_grounding_circuit_breaker", record_outcome, task_id)
    logger.warning(
        "[%s] Microtask %s: REVIEW_FAILED - grounding circuit breaker tripped after %d cycles",
        task_id,
        mt.id,
        grounding_failure_streak,
    )
    _locked_rollback(file_locks, rollback, all_files, mt)
    return True


def _apply_cr_section_exit(
    *,
    mt: Any,
    cr_outcome: GateOutcome,
    cycle_bad: bool,
    last_bad_cr_outcome: Optional[GateOutcome],
    grounding_failure_streak: int,
    cycle_limit: int,
    code_review_retry_cap: int,
    task_id: str,
    review_failed_ids: set,
    all_files: Dict[str, str],
    rollback: _MicrotaskRollback,
    review_failed_status: Any,
    phase_failed: bool,
    on_failure: str,
    review_failed_error_cls: Any,
    review_result_cls: Any,
    file_locks: KeyedLockManager[str],
) -> Tuple[bool, int]:
    """Resolve one outer cycle's code-review section exit: breaker vs retry exhaustion.

    Ticks (or resets) the grounding-failure streak and decides which terminal
    path — if any — applies, preferring the circuit breaker over ordinary retry
    exhaustion when both would fire (see the grounding circuit-breaker design doc).
    Factored out of :func:`run_gated_execution_impl` to keep that function's
    branch count from growing past the module's C901 budget.

    Preconditions:
        Called exactly once per outer cycle, immediately after the CR retry
        sub-loop ends (whether via pass, exhaustion, or an already-handled
        write-path failure reflected in ``phase_failed``).
        ``last_bad_cr_outcome`` is the last CR ``GateOutcome`` this cycle that
        marked ``cycle_bad`` (or ``None`` when the cycle was not bad).
    Postconditions:
        Returns ``(phase_failed, grounding_failure_streak)``. The streak is only
        updated when ``phase_failed`` was False on entry (a write-path failure is
        never attributed to the breaker) and ``cycle_limit > 0``. If leaving with
        ``phase_failed=True`` and ``on_failure == \"stop\"``, raises
        ``review_failed_error_cls`` instead of returning.
    """
    if not phase_failed and cycle_limit > 0:
        grounding_failure_streak = grounding_failure_streak + 1 if cycle_bad else 0

    breaker_tripped = (
        not phase_failed and cycle_limit > 0 and grounding_failure_streak >= cycle_limit
    )
    if breaker_tripped:
        phase_failed = _apply_grounding_circuit_breaker_trip(
            mt=mt,
            cr_outcome=cr_outcome,
            telemetry_outcome=last_bad_cr_outcome,
            grounding_failure_streak=grounding_failure_streak,
            cycle_limit=cycle_limit,
            task_id=task_id,
            review_failed_ids=review_failed_ids,
            all_files=all_files,
            rollback=rollback,
            review_failed_status=review_failed_status,
            file_locks=file_locks,
        )
    elif not cr_outcome.passed:
        phase_failed = _apply_code_review_retry_exhausted(
            phase_failed=phase_failed,
            mt=mt,
            cr_outcome=cr_outcome,
            code_review_retry_cap=code_review_retry_cap,
            task_id=task_id,
            review_failed_ids=review_failed_ids,
            all_files=all_files,
            rollback=rollback,
            review_failed_status=review_failed_status,
            file_locks=file_locks,
        )

    if phase_failed and on_failure == "stop":
        raise review_failed_error_cls(
            mt,
            review_result_cls(passed=False, issues=cr_outcome.issues, summary=cr_outcome.summary),
        )
    return phase_failed, grounding_failure_streak


def _run_review_cycles(
    *,
    gate_config: "GatedExecutionConfig",
    llm: LLMClient,
    task: Task,
    task_id: str,
    mt: Any,
    microtask_files: Dict[str, str],
    repo_path: Path,
    deps: "ReviewDependencies",
    review_context: Optional[ReviewContext],
    config: Any,
    planning_result: Any,
    all_files: Dict[str, str],
    review_failed_ids: set,
    microtask_rollback: _MicrotaskRollback,
    microtask_status: Any,
    review_result_cls: Any,
    review_failed_error_cls: Any,
    max_total_cycles: int,
    code_review_retry_cap: int,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]],
    current_idx: int,
    completed_ids: set,
    total: int,
    detail_cb: Callable[[str, int, str], None],
    file_locks: KeyedLockManager[str],
) -> Tuple[bool, Dict[str, str], int]:
    """Run the code review / QA / security gate cycles (Phases 2-4).

    Flow per outer cycle: Code Review (with in-place batch-fix retries up to
    ``code_review_retry_cap``) → QA Testing → Security Testing; a failing QA or
    security gate is batch-fixed and restarts the outer cycle from Code Review.
    When ``gate_config.parallelize_qa_security`` is True and ``llm`` doesn't
    require sequencing (see ``_qa_security_run_sequentially``), QA and Security
    instead run concurrently via ``parallel_map`` against the same
    post-Code-Review snapshot, and a failure from either is batch-fixed together
    in a single restart from Code Review (see docs/GATE_DEPENDENCY_GRAPH.md).
    Split out of :func:`run_gated_execution_impl`, which supplies the coded
    ``microtask_files`` from its own Phase 1 and runs Phase 5 (documentation)
    afterward using this function's return value.

    Preconditions:
        ``microtask_rollback`` was created and pre-populated by Phase 1
        (:func:`_commit_coding_write`) for ``microtask_files``'s initial write.
        ``detail_cb(detail, idx, phase)`` forwards to ``progress_callback``.
        ``file_locks`` is the per-run manager, or ``_HELD_FILE_LOCKS`` when the
        caller already holds the physical-path locks for this microtask's
        overlapping pipeline (KeyedLockManager is not reentrant).
    Postconditions:
        Returns ``(phase_failed, microtask_files, total_cycles)``: ``phase_failed``
        is True iff the microtask's review was rejected (retry exhaustion, the
        grounding circuit breaker, an unsafe fix-write, or exhausted cycles);
        ``microtask_files`` reflects the last accepted write. Every gate rejection
        path rolls back this microtask's contributions to ``all_files`` and the
        worktree before returning/raising. Raises ``review_failed_error_cls`` when
        a terminal rejection occurs and ``config.on_failure == \"stop\"`` (or, at
        max cycles, when a still-failing security gate has
        ``security_failure_always_stops``), matching
        :func:`_apply_cr_section_exit`'s and the max-cycles check's raise paths.
    Invariants:
        ``deps.tool_agent_cache`` is reset to a fresh :class:`AgentReviewCache`
        at the start of this call. Unlike the local ``agent_review_cache``
        (truly discarded on return), it is assigned onto the shared ``deps``
        object, so it persists there after this call returns — but the next
        microtask's call to this function overwrites it with another fresh
        instance before that microtask's gates run, so no gate ever sees an
        entry left over from a prior microtask. A team's gate callables that
        read it off ``deps`` (currently only the frontend team) therefore
        always see a cache scoped to the microtask currently in progress.
    """
    phase_failed = False
    total_cycles = 0
    # Last outcome of each gate — initialised passed so the max-cycles check
    # is well-defined even if a gate never ran this microtask.
    cr_outcome = GateOutcome(passed=True)
    qa_outcome = GateOutcome(passed=True)
    sec_outcome = GateOutcome(passed=True)

    # Per-piece QA/security verdict cache, scoped to this microtask's own review
    # cycles (constructed here, discarded on return) — see AgentReviewCache. A
    # cycle's batch fix typically rewrites only some of ``microtask_files``
    # (``run_batch_coding_fixes_impl`` returns the full set with just the fixed
    # keys overlaid), so files a fix didn't touch are byte-identical across
    # cycles and skip their QA/security LLM call. Code review is not threaded
    # through here: it already has its own cross-cycle chunk cache
    # (code_review_agent.mapping._cached_review_chunk).
    agent_review_cache = AgentReviewCache()

    # Per-tool-agent result cache, freshly constructed here and attached to
    # ``deps`` so gate callables can access it. Unlike the local
    # ``agent_review_cache`` above (truly discarded on return), this instance
    # is assigned onto the shared ``deps`` object and so persists there after
    # this call returns -- its effective per-microtask-cycle lifetime is
    # enforced by this same reset running again at the start of every
    # ``_run_review_cycles`` call, so the next microtask never sees a stale
    # entry from this one. Stored on ``deps`` (rather than threaded as a new
    # gate-call keyword) so teams whose gate functions don't read it —
    # currently only the backend team — are completely unaffected; see
    # docs/GATE_DEPENDENCY_GRAPH.md's "residual 2x" caching design.
    deps.tool_agent_cache = AgentReviewCache()

    # Grounding-failure circuit breaker + issue dedup state, scoped to this
    # microtask's own review lifecycle (see grounding circuit-breaker design doc).
    grounding_failure_streak = 0
    seen_issues: set[tuple[str, str]] = set()
    cycle_limit = int(getattr(config, "grounding_failure_cycle_limit", 3))
    ratio_threshold = float(getattr(config, "grounding_failure_ratio_threshold", 0.75))

    # ── Sequential Review Gates with Batch Fixes ──────────────────────────
    # Flow: Code Review -> QA -> Security -> Documentation
    # After QA/Security fixes, restart from Code Review

    while not phase_failed and total_cycles < max_total_cycles:
        total_cycles += 1
        # True iff any CR gate call this outer cycle (initial or retry) was
        # both failing and grounding-heavy; drives the streak below.
        # ``last_bad_cr_outcome`` keeps the last such call for telemetry when
        # the breaker trips after a later retry has already flipped CR to pass.
        cycle_bad = False
        last_bad_cr_outcome: Optional[GateOutcome] = None

        # ── Code Review Phase ─────────────────────────────────────────────
        mt.status = gate_config.status_code_review
        logger.info(
            "[%s] Microtask %s: Cycle %d - Running code review phase",
            task_id,
            mt.id,
            total_cycles,
        )

        if progress_callback:
            progress_callback(
                current_idx,
                len(completed_ids),
                total,
                mt.title or mt.id,
                "code_review",
                f"Code review (cycle {total_cycles})...",
            )

        cr_outcome = gate_config.run_code_review_gate(
            llm=llm,
            task=task,
            microtask=mt,
            repo_path=repo_path,
            files=microtask_files,
            deps=deps,
            review_context=review_context,
            enable_llm_review_grounding=getattr(config, "enable_llm_review_grounding", True),
            detail_callback=lambda d: detail_cb(d, current_idx, "code_review"),
        )
        if cr_call_is_grounding_bad(
            passed=cr_outcome.passed,
            raw_issue_count=cr_outcome.raw_issue_count,
            kept_count=len(cr_outcome.issues),
            ratio_threshold=ratio_threshold,
        ):
            cycle_bad = True
            last_bad_cr_outcome = cr_outcome

        cr_retry = 0
        while not cr_outcome.passed and cr_retry < code_review_retry_cap:
            cr_retry += 1
            logger.info(
                "[%s] Microtask %s: Code review failed with %d issues. Batch fixing (attempt %d/%d)",
                task_id,
                mt.id,
                len(cr_outcome.issues),
                cr_retry,
                code_review_retry_cap,
            )

            if progress_callback:
                progress_callback(
                    current_idx,
                    len(completed_ids),
                    total,
                    mt.title or mt.id,
                    "code_review",
                    f"Batch fixing {len(cr_outcome.issues)} issues (attempt {cr_retry})...",
                )

            ps_result = gate_config.run_batch_coding_fixes(
                llm=llm,
                microtask=mt,
                issues=_dedup_issues(list(cr_outcome.issues), seen_issues),
                current_files=microtask_files,
                language=planning_result.language,
                task_id=task_id,
                phase_name="code_review",
                detail_callback=lambda d: detail_cb(d, current_idx, "code_review"),
            )

            microtask_files = ps_result.files
            if not _locked_write_and_merge(
                file_locks=file_locks,
                repo_path=repo_path,
                files=microtask_files,
                mt=mt,
                task_id=task_id,
                review_failed_ids=review_failed_ids,
                all_files=all_files,
                rollback=microtask_rollback,
                review_failed_status=microtask_status.REVIEW_FAILED,
            ):
                phase_failed = True
                break

            if progress_callback:
                progress_callback(
                    current_idx,
                    len(completed_ids),
                    total,
                    mt.title or mt.id,
                    "code_review",
                    "Re-running code review...",
                )

            cr_outcome = gate_config.run_code_review_gate(
                llm=llm,
                task=task,
                microtask=mt,
                repo_path=repo_path,
                files=microtask_files,
                deps=deps,
                review_context=review_context,
                enable_llm_review_grounding=getattr(config, "enable_llm_review_grounding", True),
                detail_callback=lambda d: detail_cb(d, current_idx, "code_review"),
            )
            if cr_call_is_grounding_bad(
                passed=cr_outcome.passed,
                raw_issue_count=cr_outcome.raw_issue_count,
                kept_count=len(cr_outcome.issues),
                ratio_threshold=ratio_threshold,
            ):
                cycle_bad = True
                last_bad_cr_outcome = cr_outcome

        # Leaving the CR section: tick the streak and resolve breaker-vs-retry-
        # exhaustion once per outer cycle (may raise on-failure="stop").
        phase_failed, grounding_failure_streak = _apply_cr_section_exit(
            mt=mt,
            cr_outcome=cr_outcome,
            cycle_bad=cycle_bad,
            last_bad_cr_outcome=last_bad_cr_outcome,
            grounding_failure_streak=grounding_failure_streak,
            cycle_limit=cycle_limit,
            code_review_retry_cap=code_review_retry_cap,
            task_id=task_id,
            review_failed_ids=review_failed_ids,
            all_files=all_files,
            rollback=microtask_rollback,
            review_failed_status=microtask_status.REVIEW_FAILED,
            phase_failed=phase_failed,
            on_failure=config.on_failure,
            review_failed_error_cls=review_failed_error_cls,
            review_result_cls=review_result_cls,
            file_locks=file_locks,
        )
        if phase_failed:
            break

        # ── QA + Security Testing Phase ────────────────────────────────────
        if gate_config.parallelize_qa_security and not _qa_security_run_sequentially(llm):
            # Concurrent path (any team with parallelize_qa_security=True --
            # currently both backend and frontend): QA and Security are
            # independent analysis calls over the same immutable
            # post-Code-Review snapshot (see docs/GATE_DEPENDENCY_GRAPH.md),
            # so they run at once via parallel_map and their issues are
            # collected and batch-fixed together in a single
            # restart-from-Code-Review, rather than fixing QA and Security one
            # gate at a time. Both gate calls below share the same
            # ``agent_review_cache`` and (via ``deps``) the same
            # ``tool_agent_cache`` instance; this is safe because each gate
            # only ever reads/writes entries keyed by its own kind
            # ("qa"/"testing_qa" vs "security") -- disjoint keys, so
            # concurrent dict access never contends on the same entry, and
            # this mirrors ``agent_review_cache``'s pre-existing sharing
            # pattern in this same branch.
            mt.status = gate_config.status_qa_security or gate_config.status_qa
            logger.info(
                "[%s] Microtask %s: Cycle %d - Running QA + security testing phases concurrently",
                task_id,
                mt.id,
                total_cycles,
            )

            if progress_callback:
                # A single combined-phase announcement, made before parallel_map
                # even starts either gate. Reporting "qa_testing" then
                # "security_testing" here (as this used to) would make the
                # persisted current_microtask_phase land on "security_testing"
                # for virtually the whole concurrent run, and the frontend's
                # phase tracker infers "later phase observed" as "earlier phase
                # passed" -- a false "QA passed" checkmark before QA's outcome
                # is even known.
                progress_callback(
                    current_idx,
                    len(completed_ids),
                    total,
                    mt.title or mt.id,
                    "qa_security_testing",
                    f"QA + security testing (cycle {total_cycles})...",
                )

            # Both gates' detail callbacks are tagged with the same combined
            # "qa_security_testing" phase (not their individual "qa_testing" /
            # "security_testing" names) -- each forwards into progress_callback
            # via detail_cb, and since the two gates run concurrently their
            # ticks can interleave. Tagging either with its own bare phase name
            # would flip the persisted current_microtask_phase to
            # "security_testing" mid-run, reintroducing the false "QA passed"
            # checkmark this branch exists to avoid.
            qa_outcome, sec_outcome = parallel_map(
                [
                    lambda: gate_config.run_qa_gate(
                        llm=llm,
                        task=task,
                        microtask=mt,
                        repo_path=repo_path,
                        files=microtask_files,
                        deps=deps,
                        detail_callback=lambda d: detail_cb(d, current_idx, "qa_security_testing"),
                        cache=agent_review_cache,
                    ),
                    lambda: gate_config.run_security_gate(
                        llm=llm,
                        task=task,
                        microtask=mt,
                        repo_path=repo_path,
                        files=microtask_files,
                        deps=deps,
                        detail_callback=lambda d: detail_cb(d, current_idx, "qa_security_testing"),
                        cache=agent_review_cache,
                    ),
                ],
                lambda fn: fn(),
                max_workers=2,
                skip_none=False,
                # Both callables write into the shared ``agent_review_cache`` and
                # report progress via ``detail_cb`` — never leave one straggling
                # in the background after the other raises (see
                # ``run_qa_gate``/``run_security_gate``'s "never raises"
                # contract this relies on being upheld).
                wait_for_stragglers=True,
            )

            if not qa_outcome.passed or not sec_outcome.passed:
                # Only a *failing* gate's issues are fixable material — a
                # passing gate's ``issues`` can still carry non-blocking
                # (e.g. medium-severity) findings (``GateOutcome.passed``
                # reflects only critical/high issues, per
                # ``_run_agent_testing_phase``), and those were never sent to
                # ``run_batch_coding_fixes`` in the old sequential path either.
                combined_issues = (list(qa_outcome.issues) if not qa_outcome.passed else []) + (
                    list(sec_outcome.issues) if not sec_outcome.passed else []
                )
                logger.info(
                    "[%s] Microtask %s: QA/security testing %s %d issue(s) (QA: %d, security: %d). "
                    "Batch fixing and restarting from code review.",
                    task_id,
                    mt.id,
                    gate_config.gate_issue_log_verb,
                    len(combined_issues),
                    len(qa_outcome.issues),
                    len(sec_outcome.issues),
                )

                if progress_callback:
                    progress_callback(
                        current_idx,
                        len(completed_ids),
                        total,
                        mt.title or mt.id,
                        "qa_security_testing",
                        f"Batch fixing {len(combined_issues)} QA/security issues...",
                    )

                ps_result = gate_config.run_batch_coding_fixes(
                    llm=llm,
                    microtask=mt,
                    issues=_dedup_issues(combined_issues, seen_issues),
                    current_files=microtask_files,
                    language=planning_result.language,
                    task_id=task_id,
                    phase_name="qa_security",
                    detail_callback=lambda d: detail_cb(d, current_idx, "qa_security_testing"),
                )

                microtask_files = ps_result.files
                if not _locked_write_and_merge(
                    file_locks=file_locks,
                    repo_path=repo_path,
                    files=microtask_files,
                    mt=mt,
                    task_id=task_id,
                    review_failed_ids=review_failed_ids,
                    all_files=all_files,
                    rollback=microtask_rollback,
                    review_failed_status=microtask_status.REVIEW_FAILED,
                ):
                    phase_failed = True
                    break

                # Restart from code review
                continue
        else:
            # ── QA Testing Phase (sequential) ──────────────────────────────
            mt.status = gate_config.status_qa
            logger.info(
                "[%s] Microtask %s: Cycle %d - Running QA testing phase",
                task_id,
                mt.id,
                total_cycles,
            )

            if progress_callback:
                progress_callback(
                    current_idx,
                    len(completed_ids),
                    total,
                    mt.title or mt.id,
                    "qa_testing",
                    f"QA testing (cycle {total_cycles})...",
                )

            qa_outcome = gate_config.run_qa_gate(
                llm=llm,
                task=task,
                microtask=mt,
                repo_path=repo_path,
                files=microtask_files,
                deps=deps,
                detail_callback=lambda d: detail_cb(d, current_idx, "qa_testing"),
                cache=agent_review_cache,
            )

            if not qa_outcome.passed:
                logger.info(
                    "[%s] Microtask %s: QA testing %s %d issues. Batch fixing and restarting from code review.",
                    task_id,
                    mt.id,
                    gate_config.gate_issue_log_verb,
                    len(qa_outcome.issues),
                )

                if progress_callback:
                    progress_callback(
                        current_idx,
                        len(completed_ids),
                        total,
                        mt.title or mt.id,
                        "qa_testing",
                        f"Batch fixing {len(qa_outcome.issues)} QA issues...",
                    )

                ps_result = gate_config.run_batch_coding_fixes(
                    llm=llm,
                    microtask=mt,
                    issues=_dedup_issues(list(qa_outcome.issues), seen_issues),
                    current_files=microtask_files,
                    language=planning_result.language,
                    task_id=task_id,
                    phase_name="qa",
                    detail_callback=lambda d: detail_cb(d, current_idx, "qa_testing"),
                )

                microtask_files = ps_result.files
                if not _locked_write_and_merge(
                    file_locks=file_locks,
                    repo_path=repo_path,
                    files=microtask_files,
                    mt=mt,
                    task_id=task_id,
                    review_failed_ids=review_failed_ids,
                    all_files=all_files,
                    rollback=microtask_rollback,
                    review_failed_status=microtask_status.REVIEW_FAILED,
                ):
                    phase_failed = True
                    break

                # Restart from code review
                continue

            # ── Security Testing Phase (sequential) ─────────────────────────
            mt.status = gate_config.status_security
            logger.info(
                "[%s] Microtask %s: Cycle %d - Running security testing phase",
                task_id,
                mt.id,
                total_cycles,
            )

            if progress_callback:
                progress_callback(
                    current_idx,
                    len(completed_ids),
                    total,
                    mt.title or mt.id,
                    "security_testing",
                    f"Security testing (cycle {total_cycles})...",
                )

            sec_outcome = gate_config.run_security_gate(
                llm=llm,
                task=task,
                microtask=mt,
                repo_path=repo_path,
                files=microtask_files,
                deps=deps,
                detail_callback=lambda d: detail_cb(d, current_idx, "security_testing"),
                cache=agent_review_cache,
            )

            if not sec_outcome.passed:
                logger.info(
                    "[%s] Microtask %s: Security testing %s %d issues. Batch fixing and restarting from code review.",
                    task_id,
                    mt.id,
                    gate_config.gate_issue_log_verb,
                    len(sec_outcome.issues),
                )

                if progress_callback:
                    progress_callback(
                        current_idx,
                        len(completed_ids),
                        total,
                        mt.title or mt.id,
                        "security_testing",
                        f"Batch fixing {len(sec_outcome.issues)} security issues...",
                    )

                ps_result = gate_config.run_batch_coding_fixes(
                    llm=llm,
                    microtask=mt,
                    issues=_dedup_issues(list(sec_outcome.issues), seen_issues),
                    current_files=microtask_files,
                    language=planning_result.language,
                    task_id=task_id,
                    phase_name="security",
                    detail_callback=lambda d: detail_cb(d, current_idx, "security_testing"),
                )

                microtask_files = ps_result.files
                if not _locked_write_and_merge(
                    file_locks=file_locks,
                    repo_path=repo_path,
                    files=microtask_files,
                    mt=mt,
                    task_id=task_id,
                    review_failed_ids=review_failed_ids,
                    all_files=all_files,
                    rollback=microtask_rollback,
                    review_failed_status=microtask_status.REVIEW_FAILED,
                ):
                    phase_failed = True
                    break

                # Restart from code review
                continue

        # All review phases passed - proceed to documentation
        break

    # Check if we exceeded max cycles
    if total_cycles >= max_total_cycles and not phase_failed:
        still_failing = not cr_outcome.passed or not qa_outcome.passed or not sec_outcome.passed
        if still_failing or not gate_config.max_cycles_requires_failing_gate:
            phase_failed = True
            mt.status = microtask_status.REVIEW_FAILED
            review_failed_ids.add(mt.id)
            mt.notes = f"Review cycles exhausted after {total_cycles} iterations"
            _record_terminal_gate_failure(
                "review_max_cycles",
                _terminal_failing_outcome(cr_outcome, qa_outcome, sec_outcome),
                task_id,
            )
            logger.warning(
                "[%s] Microtask %s: REVIEW_FAILED - exhausted %d total cycles",
                task_id,
                mt.id,
                total_cycles,
            )
            # Rollback: undo this microtask's contributions to all_files and the
            # worktree, restoring any file an earlier microtask or the repo had.
            _locked_rollback(file_locks, microtask_rollback, all_files, mt)
            # Security failures always stop regardless of on_failure setting
            _force_stop = config.on_failure == "stop" or (
                getattr(config, "security_failure_always_stops", True) and not sec_outcome.passed
            )
            if _force_stop:
                raise review_failed_error_cls(
                    mt,
                    review_result_cls(passed=False, issues=[], summary="Max cycles exceeded"),
                )

    return phase_failed, microtask_files, total_cycles
