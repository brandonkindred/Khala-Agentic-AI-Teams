"""Shared Execution-phase leaf helpers for the code-v2 teams, including the
gated per-microtask review loop (``run_gated_execution_impl``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_service import LLMClient
from software_engineering_team.shared.agent_review import AgentReviewCache
from software_engineering_team.shared.code_completeness import reject_invalid_python
from software_engineering_team.shared.gate_outcomes import record_gate_outcome
from software_engineering_team.shared.models import ReviewContext, SystemArchitecture, Task
from software_engineering_team.shared.phases.rollback import (
    _MicrotaskRollback,
    _record_prior_values,
    _rollback_microtask_files,
)
from software_engineering_team.shared.repo_writer import (
    UnsafeRepoPathError,
    write_repo_text_files,
)
from software_engineering_team.shared.stack_profile import PhaseModels, StackProfile
from software_engineering_team.shared.strands_model import LlmRunner
from software_engineering_team.shared.v2_review import _review_steps_run_sequentially

logger = logging.getLogger(__name__)

# Iteration budget for the gated loop's own final documentation self-review pass.
# Deliberately its own (lower) constants rather than reusing
# review_utils.MIN/MAX_DOC_SELF_REVIEW_ITERATIONS (3/3): this pass runs once per
# microtask on top of the code/QA/security review cycles already spent, so a
# smaller budget here is intentional, not an oversight -- kept separate so tuning
# one never silently changes the other.
_GATED_DOC_SELF_REVIEW_MIN_ITERS = 1
_GATED_DOC_SELF_REVIEW_MAX_ITERS = 2
_GATED_DOC_SELF_REVIEW_QUALITY_THRESHOLD = 0.9


def _dedup_issues(issues: List[Any], seen: set[tuple[str, str]]) -> List[Any]:
    """Remove duplicate issues across review cycles based on (file_path, description).

    Preconditions:
        ``seen`` accumulates ``(file_path, description)`` keys across calls.
    Postconditions:
        Returns issues whose key was not already in ``seen``; mutates ``seen``.
    """
    unique: List[Any] = []
    for issue in issues:
        key = (issue.file_path or "", issue.description or "")
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


class ReviewDependencies:
    """Container for all review-related agents and callbacks.

    Invariants:
        ``tool_agents`` is always a dict (never ``None``).
    """

    def __init__(
        self,
        *,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        linting_tool_agent: Any = None,
        tool_agents: Optional[Dict[Any, Any]] = None,
    ) -> None:
        self.build_verifier = build_verifier
        self.qa_agent = qa_agent
        self.security_agent = security_agent
        self.code_review_agent = code_review_agent
        self.linting_tool_agent = linting_tool_agent
        self.tool_agents = tool_agents or {}


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


def _run_general_microtask_impl(
    *,
    llm: LLMClient,
    microtask: Any,
    task: Task,
    language: str,
    existing_code: str,
    architecture: Optional[SystemArchitecture],
    execution_prompt: str,
    parse_files_and_summary: Callable[[str], Dict[str, Any]],
    profile: StackProfile,
    runner: LlmRunner,
) -> Dict[str, str]:
    """Use the LLM to implement a general (non-specialist) microtask.

    Stack-specific ``EXECUTION_PROMPT`` divergence is owned by ``StackProfile``:
    backend templates include a ``{language_conventions}`` slot; frontend
    templates do not.

    Preconditions:
        ``execution_prompt`` carries a ``{language_conventions}`` slot iff
        ``profile.has_language_conventions``.
    Postconditions:
        Returns the parsed ``{path: content}`` map (possibly empty). Any
        ``.py`` file whose content fails ``ast.parse`` is dropped and logged
        rather than returned; the caller (the build-verification retry loop)
        is expected to detect the missing file.
    """
    arch_ctx = ""
    if architecture:
        # Lazy import: code_review_agent submodules are imported on demand
        # rather than at module scope elsewhere in the review call chain
        # (e.g. _code_review_step's CodeReviewInput import), so this module
        # follows the same convention rather than adding a new eager edge.
        from software_engineering_team.code_review_agent.architecture_context import (
            render_architecture_context,
        )

        arch_ctx = render_architecture_context(architecture)

    fmt: Dict[str, Any] = dict(
        microtask_description=microtask.description or microtask.title,
        requirements=task.requirements or task.description,
        existing_code=existing_code or "(none)",
        architecture_context=arch_ctx or "(none)",
    )
    if profile.has_language_conventions:
        fmt["language_conventions"] = profile.conventions_for(language)
    prompt = execution_prompt.format(**fmt)
    raw = runner.run(llm, prompt)
    data = parse_files_and_summary(raw)
    files = data.get("files") or {}

    files, rejected_files = reject_invalid_python(files)
    if rejected_files:
        logger.warning(
            "Microtask %s: codegen returned unparsable Python for %d file(s); "
            "dropping so the build-verification retry loop can catch it: %s",
            microtask.id,
            len(rejected_files),
            sorted(rejected_files),
        )

    return files


def generate_microtask_files(
    *,
    llm: LLMClient,
    mt: Any,
    task: Task,
    planning_result: Any,
    repo_path: Path,
    existing_code: str,
    architecture: Optional[SystemArchitecture],
    runners: Dict[Any, Any],
    models: PhaseModels,
    run_general_microtask: Callable[..., Dict[str, str]],
) -> Dict[str, str]:
    """Produce a microtask's output files via its tool-runner or the general coder.

    Was duplicated as a standalone helper in backend's execution.py and inlined
    directly in frontend's review-gated loop; both bodies were identical.

    Preconditions:
        ``runners`` maps tool-agent-kind → callable(ToolAgentInput); a microtask
        whose ``tool_agent`` has no runner falls back to ``run_general_microtask``.
        ``models`` exposes ``ToolAgentInput`` (see ``run_execution_impl``).
    Postconditions:
        Returns the ``{path: content}`` map and sets ``mt.output_files`` (and
        ``mt.notes`` on the tool-runner path). Never writes to disk.
    """
    runner = runners.get(mt.tool_agent)
    if runner is not None:
        inp = models.ToolAgentInput(
            microtask=mt,
            repo_path=str(repo_path),
            existing_code=existing_code or "",
            language=planning_result.language,
        )
        out = runner(inp)
        mt.output_files = out.files
        mt.notes = out.summary
    else:
        mt.output_files = run_general_microtask(
            llm=llm,
            microtask=mt,
            task=task,
            language=planning_result.language,
            existing_code=existing_code,
            architecture=architecture,
        )
    return dict(mt.output_files)


def run_execution_impl(
    *,
    llm: LLMClient,
    task: Task,
    planning_result: Any,
    repo_path: Path,
    architecture: Optional[SystemArchitecture],
    existing_code: str,
    tool_runners: Optional[Dict[Any, Any]],
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]],
    only_microtask_ids: Optional[List[str]],
    models: PhaseModels,
    run_general_microtask: Callable[..., Dict[str, str]],
) -> Any:
    """Execute microtasks in the planner's stated order, best-effort on dependencies.

    If ``only_microtask_ids`` is set, only those microtasks are run (e.g. fix
    microtasks from ``plan_fixes_for_unresolved_issues``). ``tool_runners`` maps
    ToolAgentKind → callable(ToolAgentInput) → ToolAgentOutput; microtasks whose
    tool_agent has no runner fall back to ``run_general_microtask``.

    Preconditions:
        ``models`` exposes ``MicrotaskStatus``, ``ExecutionResult``, ``ToolAgentInput``;
        ``run_general_microtask`` is the team's general coder (the monkeypatch
        boundary for its LLM ``Agent``).
    Postconditions:
        Returns an ``ExecutionResult``; a failed microtask is marked FAILED and
        execution continues with the rest. An unmet ``depends_on`` is logged and
        the microtask still runs ("running anyway") rather than being skipped or
        failed outright: ``depends_on`` is an LLM-planned ordering hint, not a
        verified DAG, so failing closed on it would silently drop a legitimately
        runnable microtask over a planner inconsistency (a stale id, a reordering
        quirk). The unmet-deps case is intentionally soft here, unlike the gated
        loop's ``run_gated_execution_impl``, which SKIPS a microtask depending on
        one that already REVIEW_FAILED — a stronger signal than "not yet run".
    """
    microtask_status_enum = models.MicrotaskStatus
    execution_result_cls = models.ExecutionResult

    runners = tool_runners or {}
    all_files: Dict[str, str] = {}
    microtasks = list(planning_result.microtasks)
    if only_microtask_ids is not None:
        id_set = set(only_microtask_ids)
        microtasks = [mt for mt in microtasks if mt.id in id_set]
    completed_ids: set[str] = set()
    total = len(microtasks)

    for idx, mt in enumerate(microtasks):
        deps_met = all(d in completed_ids for d in mt.depends_on)
        if not deps_met:
            logger.warning(
                "[%s] Microtask %s has unmet deps %s — running anyway",
                task.id,
                mt.id,
                mt.depends_on,
            )

        mt.status = microtask_status_enum.IN_PROGRESS
        logger.info(
            "[%s] Execution: microtask %d/%d — %s (%s)",
            task.id,
            idx + 1,
            total,
            mt.id,
            mt.tool_agent.value,
        )

        if progress_callback:
            progress_callback(
                idx + 1,
                len(completed_ids),
                total,
                mt.title or mt.id,
                "coding",
                "Generating code...",
            )

        try:
            generate_microtask_files(
                llm=llm,
                mt=mt,
                task=task,
                planning_result=planning_result,
                repo_path=repo_path,
                existing_code=existing_code,
                architecture=architecture,
                runners=runners,
                models=models,
                run_general_microtask=run_general_microtask,
            )

            all_files.update(mt.output_files)
            mt.status = microtask_status_enum.COMPLETED
            completed_ids.add(mt.id)
        except Exception as exc:
            logger.error("[%s] Microtask %s failed: %s", task.id, mt.id, exc)
            mt.status = microtask_status_enum.FAILED
            mt.notes = str(exc)

        if progress_callback:
            progress_callback(
                idx + 1, len(completed_ids), total, mt.title or mt.id, "completed", ""
            )

    summary = f"Executed {len(completed_ids)}/{total} microtasks; {len(all_files)} files produced."
    return execution_result_cls(files=all_files, microtasks=microtasks, summary=summary)


# ---------------------------------------------------------------------------
# Gated per-microtask execution loop (shared skeleton)
# ---------------------------------------------------------------------------


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
) -> bool:
    """Mark retry-exhaustion REVIEW_FAILED unless write-path already failed.

    Preconditions:
        Caller has confirmed ``not cr_outcome.passed`` after the retry loop.
        When ``phase_failed`` is True, ``write_microtask_output_or_fail`` already
        marked the microtask and rolled back files.
    Postconditions:
        When ``phase_failed`` was False: sets REVIEW_FAILED notes/status, records
        ``code_review_retry_exhausted``, rolls back this microtask's contributions
        from ``all_files`` and the worktree, and returns True.
        When ``phase_failed`` was True: leaves notes/status/telemetry untouched
        and returns True (caller still stops the outer review cycle).
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
    _rollback_microtask_files(rollback, all_files, mt)
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
    _rollback_microtask_files(rollback, all_files, mt)
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
        )

    if phase_failed and on_failure == "stop":
        raise review_failed_error_cls(
            mt,
            review_result_cls(passed=False, issues=cr_outcome.issues, summary=cr_outcome.summary),
        )
    return phase_failed, grounding_failure_streak


@dataclass(frozen=True)
class GatedExecutionConfig:
    """Per-team knobs for :func:`run_gated_execution_impl`.

    Mirrors the ``ReviewConfig`` seam in ``shared/v2_review.py``: scalars/enums
    for the plain divergences and callables for the behavioural ones. Each team
    builds one instance in its ``phases/execution.py`` next to its
    ``_run_general_microtask`` boundary.

    Invariants:
        The three ``run_*_gate`` callables return a :class:`GateOutcome`; the
        ``status_*`` values are members of ``models.MicrotaskStatus``; the two
        cap callables return non-negative ints for any ``MicrotaskReviewConfig``.
    """

    # Team model surface + general (non-specialist) coder (see ``run_execution_impl``).
    models: PhaseModels
    run_general_microtask: Callable[..., Dict[str, str]]
    # Gate adapters: ``(*, llm, task, microtask, repo_path, files, deps,
    # detail_callback) -> GateOutcome``. Backend wraps separate
    # ``run_{code_review,qa,security}_testing_phase`` calls returning
    # ``PhaseReviewResult``; frontend calls unified ``run_microtask_review()``
    # three times and filters issues by ``source``. Both normalize to
    # ``GateOutcome``. ``run_qa_gate``/``run_security_gate`` additionally
    # receive a ``cache: AgentReviewCache`` keyword (see ``_run_review_cycles``)
    # — ``run_code_review_gate`` does not, since code review already has its
    # own cross-cycle cache.
    run_code_review_gate: Callable[..., GateOutcome]
    run_qa_gate: Callable[..., GateOutcome]
    run_security_gate: Callable[..., GateOutcome]
    # Shared-signature helpers, injected so the team's ``Agent`` patch surface
    # (in its ``problem_solving.py`` / ``review.py``) stays per-team.
    run_batch_coding_fixes: Callable[..., Any]
    run_documentation_self_review: Callable[..., Any]
    # ``mt.status`` set at each gate's entry (backend: distinct per phase;
    # frontend: ``IN_REVIEW`` for all three — a no-op re-assign after code review).
    # When ``parallelize_qa_security`` is in effect, QA and Security run at once,
    # so there is no per-gate entry point for Security to set ``status_security``
    # at — ``mt.status`` is set to ``status_qa`` once for the whole combined
    # phase and intentionally never reaches ``status_security`` (see
    # ``_run_review_cycles``'s concurrent branch).
    status_code_review: Any
    status_qa: Any
    status_security: Any
    # Retry-cap formulas over the resolved ``MicrotaskReviewConfig``.
    max_total_cycles: Callable[[Any], int]
    code_review_retry_cap: Callable[[Any], int]
    # Backend guards the max-cycles REVIEW_FAILED on "some gate still failing";
    # frontend marks it unconditionally.
    max_cycles_requires_failing_gate: bool
    # Startup INFO line (backend omits ``max_retries``; frontend includes it).
    startup_log_message: Callable[..., str]
    # Verb in the QA/security "…%s %d issues" INFO line (backend "failed with";
    # frontend "found").
    gate_issue_log_verb: str
    # When True, QA and Security run concurrently via parallel_map against the
    # same post-Code-Review snapshot (backend only — see
    # docs/GATE_DEPENDENCY_GRAPH.md). Defaults False (today's fully sequential
    # QA -> Security behavior), so the frontend config is unaffected until its
    # tool-agent fan-out is scoped per gate the way the backend's already is.
    parallelize_qa_security: bool = False


def _execute_coding_phase(
    *,
    llm: LLMClient,
    mt: Any,
    task: Task,
    task_id: str,
    planning_result: Any,
    repo_path: Path,
    existing_code: str,
    architecture: Optional[SystemArchitecture],
    runners: Dict[Any, Any],
    models: PhaseModels,
    run_general_microtask: Callable[..., Dict[str, str]],
    all_files: Dict[str, str],
    review_failed_ids: set,
    microtask_status: Any,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]],
    current_idx: int,
    completed_ids: set,
    total: int,
) -> Optional[Tuple[Dict[str, str], _MicrotaskRollback]]:
    """Run a microtask's coding phase: generate its files, then guard-write them.

    Split out of :func:`run_gated_execution_impl` (Phase 1 of the gated loop).

    Preconditions:
        ``mt.status`` is already ``IN_PROGRESS``; ``all_files`` is the running
        ``{path: content}`` map across all microtasks executed so far.
    Postconditions:
        On success, returns ``(microtask_files, rollback)`` with a fresh
        :class:`_MicrotaskRollback` snapshotting the pre-write state, and
        ``all_files`` updated in place. Returns ``None`` when this microtask is
        finished and the caller must move on to the next one without running
        the review-gate cycles, in one of two ways that intentionally differ in
        their progress-callback tick (both pinned by existing tests):
        - A coding exception: ``mt`` is marked ``FAILED`` with the exception text
          in ``mt.notes``, and exactly one ``"completed"`` progress tick is fired
          here (the caller's own trailing tick never runs for a microtask this
          function finishes).
        - An unsafe initial write: :func:`write_microtask_output_or_fail` has
          already marked ``mt`` ``REVIEW_FAILED`` and rolled back ``all_files``/
          the worktree; no extra progress tick is fired for this case.
    """
    try:
        microtask_files = generate_microtask_files(
            llm=llm,
            mt=mt,
            task=task,
            planning_result=planning_result,
            repo_path=repo_path,
            existing_code=existing_code,
            architecture=architecture,
            runners=runners,
            models=models,
            run_general_microtask=run_general_microtask,
        )
        # Rollback manifest: before every write (initial + fixes), snapshot what
        # to restore on failure — the prior ``all_files`` value per raw key and the
        # prior on-disk content per resolved path — so a rollback reverts both the
        # result and the worktree to the pre-microtask state. Recorded ahead of the
        # write and of ``all_files.update``.
        microtask_rollback = _MicrotaskRollback()
        _record_prior_values(microtask_rollback, repo_path, all_files, microtask_files)
        # Route the initial write through the same guarded helper the review
        # cycles use, so an unsafe path in the first emission is a handled
        # REVIEW_FAILED (rolled back + recorded in review_failed_ids so
        # dependents SKIP) rather than a bare FAILED that skips that bookkeeping.
        if not write_microtask_output_or_fail(
            repo_path,
            microtask_files,
            mt=mt,
            task_id=task_id,
            review_failed_ids=review_failed_ids,
            all_files=all_files,
            rollback=microtask_rollback,
            review_failed_status=microtask_status.REVIEW_FAILED,
        ):
            return None
        all_files.update(microtask_files)
        return microtask_files, microtask_rollback

    except Exception as exc:
        logger.error("[%s] Microtask %s execution failed: %s", task_id, mt.id, exc)
        mt.status = microtask_status.FAILED
        mt.notes = str(exc)
        if progress_callback:
            progress_callback(
                current_idx, len(completed_ids), total, mt.title or mt.id, "completed", ""
            )
        return None


def _run_review_cycles(
    *,
    gate_config: GatedExecutionConfig,
    llm: LLMClient,
    task: Task,
    task_id: str,
    mt: Any,
    microtask_files: Dict[str, str],
    repo_path: Path,
    deps: ReviewDependencies,
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
        (:func:`_execute_coding_phase`) for ``microtask_files``'s initial write.
        ``detail_cb(detail, idx, phase)`` forwards to ``progress_callback``.
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
                repo_path=str(repo_path),
                task_id=task_id,
                phase_name="code_review",
                detail_callback=lambda d: detail_cb(d, current_idx, "code_review"),
            )

            microtask_files = ps_result.files
            # Snapshot prior values for any keys the fix introduced, before the
            # write, so a later rollback restores them (or removes newly-created ones).
            _record_prior_values(microtask_rollback, repo_path, all_files, microtask_files)
            if not write_microtask_output_or_fail(
                repo_path,
                microtask_files,
                mt=mt,
                task_id=task_id,
                review_failed_ids=review_failed_ids,
                all_files=all_files,
                rollback=microtask_rollback,
                review_failed_status=microtask_status.REVIEW_FAILED,
            ):
                phase_failed = True
                break
            mt.output_files = microtask_files
            all_files.update(microtask_files)

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
        )
        if phase_failed:
            break

        # ── QA + Security Testing Phase ────────────────────────────────────
        if gate_config.parallelize_qa_security and not _qa_security_run_sequentially(llm):
            # Concurrent path (backend only): QA and Security are independent
            # analysis calls over the same immutable post-Code-Review snapshot
            # (see docs/GATE_DEPENDENCY_GRAPH.md), so they run at once via
            # parallel_map and their issues are collected and batch-fixed
            # together in a single restart-from-Code-Review, rather than
            # fixing QA and Security one gate at a time.
            mt.status = gate_config.status_qa
            logger.info(
                "[%s] Microtask %s: Cycle %d - Running QA + security testing phases concurrently",
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
                progress_callback(
                    current_idx,
                    len(completed_ids),
                    total,
                    mt.title or mt.id,
                    "security_testing",
                    f"Security testing (cycle {total_cycles})...",
                )

            from shared.concurrency import parallel_map

            qa_outcome, sec_outcome = parallel_map(
                [
                    lambda: gate_config.run_qa_gate(
                        llm=llm,
                        task=task,
                        microtask=mt,
                        repo_path=repo_path,
                        files=microtask_files,
                        deps=deps,
                        detail_callback=lambda d: detail_cb(d, current_idx, "qa_testing"),
                        cache=agent_review_cache,
                    ),
                    lambda: gate_config.run_security_gate(
                        llm=llm,
                        task=task,
                        microtask=mt,
                        repo_path=repo_path,
                        files=microtask_files,
                        deps=deps,
                        detail_callback=lambda d: detail_cb(d, current_idx, "security_testing"),
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
                combined_issues = (
                    (list(qa_outcome.issues) if not qa_outcome.passed else [])
                    + (list(sec_outcome.issues) if not sec_outcome.passed else [])
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
                        "qa_testing",
                        f"Batch fixing {len(combined_issues)} QA/security issues...",
                    )

                ps_result = gate_config.run_batch_coding_fixes(
                    llm=llm,
                    microtask=mt,
                    issues=_dedup_issues(combined_issues, seen_issues),
                    current_files=microtask_files,
                    language=planning_result.language,
                    repo_path=str(repo_path),
                    task_id=task_id,
                    phase_name="qa_security",
                    detail_callback=lambda d: detail_cb(d, current_idx, "qa_testing"),
                )

                microtask_files = ps_result.files
                # Snapshot prior values for any keys the fix introduced, before the
                # write, so a later rollback restores them (or removes newly-created ones).
                _record_prior_values(microtask_rollback, repo_path, all_files, microtask_files)
                if not write_microtask_output_or_fail(
                    repo_path,
                    microtask_files,
                    mt=mt,
                    task_id=task_id,
                    review_failed_ids=review_failed_ids,
                    all_files=all_files,
                    rollback=microtask_rollback,
                    review_failed_status=microtask_status.REVIEW_FAILED,
                ):
                    phase_failed = True
                    break
                mt.output_files = microtask_files
                all_files.update(microtask_files)

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
                    repo_path=str(repo_path),
                    task_id=task_id,
                    phase_name="qa",
                    detail_callback=lambda d: detail_cb(d, current_idx, "qa_testing"),
                )

                microtask_files = ps_result.files
                # Snapshot prior values for any keys the fix introduced, before the
                # write, so a later rollback restores them (or removes newly-created ones).
                _record_prior_values(microtask_rollback, repo_path, all_files, microtask_files)
                if not write_microtask_output_or_fail(
                    repo_path,
                    microtask_files,
                    mt=mt,
                    task_id=task_id,
                    review_failed_ids=review_failed_ids,
                    all_files=all_files,
                    rollback=microtask_rollback,
                    review_failed_status=microtask_status.REVIEW_FAILED,
                ):
                    phase_failed = True
                    break
                mt.output_files = microtask_files
                all_files.update(microtask_files)

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
                    repo_path=str(repo_path),
                    task_id=task_id,
                    phase_name="security",
                    detail_callback=lambda d: detail_cb(d, current_idx, "security_testing"),
                )

                microtask_files = ps_result.files
                # Snapshot prior values for any keys the fix introduced, before the
                # write, so a later rollback restores them (or removes newly-created ones).
                _record_prior_values(microtask_rollback, repo_path, all_files, microtask_files)
                if not write_microtask_output_or_fail(
                    repo_path,
                    microtask_files,
                    mt=mt,
                    task_id=task_id,
                    review_failed_ids=review_failed_ids,
                    all_files=all_files,
                    rollback=microtask_rollback,
                    review_failed_status=microtask_status.REVIEW_FAILED,
                ):
                    phase_failed = True
                    break
                mt.output_files = microtask_files
                all_files.update(microtask_files)

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
            _rollback_microtask_files(microtask_rollback, all_files, mt)
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


def _run_documentation_phase(
    *,
    gate_config: GatedExecutionConfig,
    llm: LLMClient,
    task: Task,
    task_id: str,
    mt: Any,
    microtask_files: Dict[str, str],
    repo_path: Path,
    deps: ReviewDependencies,
    tool_agent_kind: Any,
    all_files: Dict[str, str],
    microtask_status: Any,
    completed_ids: set,
    total_cycles: int,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]],
    current_idx: int,
    total: int,
    detail_cb: Callable[[str, int, str], None],
) -> None:
    """Run a microtask's documentation self-review phase (Phase 5, never fails).

    Split out of :func:`run_gated_execution_impl`; called only once the review-
    gate cycles (Phases 2-4) have not failed for ``mt``.

    Preconditions:
        ``microtask_files`` reflects the last review-gate-accepted write.
    Postconditions:
        ``mt.status`` becomes ``COMPLETED`` and ``mt.id`` is added to
        ``completed_ids``. ``microtask_files``, ``all_files``, and
        ``mt.output_files`` gain any refined documentation the self-review
        produced. A documentation-agent exception or an unsafe documentation
        write path is logged and skipped rather than propagated — this phase
        never fails the microtask.
    """
    mt.status = microtask_status.IN_DOCUMENTATION
    logger.info(
        "[%s] Microtask %s: Running documentation self-review (%d-%d iterations)",
        task_id,
        mt.id,
        _GATED_DOC_SELF_REVIEW_MIN_ITERS,
        _GATED_DOC_SELF_REVIEW_MAX_ITERS,
    )

    if progress_callback:
        progress_callback(
            current_idx,
            len(completed_ids),
            total,
            mt.title or mt.id,
            "documentation",
            "Starting documentation self-review...",
        )

    # Generate initial documentation
    doc_agent = deps.tool_agents.get(tool_agent_kind.DOCUMENTATION) if deps.tool_agents else None
    doc_files: Dict[str, str] = {}
    if doc_agent and hasattr(doc_agent, "document_microtask"):
        try:
            doc_result = doc_agent.document_microtask(
                microtask=mt,
                files=microtask_files,
                task_description=task.description or "",
            )
            if doc_result.files:
                doc_files = doc_result.files
                logger.info(
                    "[%s] Microtask %s: initial documentation generated %d file(s)",
                    task_id,
                    mt.id,
                    len(doc_files),
                )
        except Exception as e:
            logger.warning(
                "[%s] Microtask %s: initial documentation generation failed: %s",
                task_id,
                mt.id,
                e,
            )

    # Run self-review iterations (capped to avoid excessive LLM calls)
    self_review_result = gate_config.run_documentation_self_review(
        llm=llm,
        documentation=doc_files,
        code_files=microtask_files,
        task_description=task.description or "",
        min_iterations=_GATED_DOC_SELF_REVIEW_MIN_ITERS,
        max_iterations=_GATED_DOC_SELF_REVIEW_MAX_ITERS,
        quality_threshold=_GATED_DOC_SELF_REVIEW_QUALITY_THRESHOLD,
        detail_callback=lambda d: detail_cb(d, current_idx, "documentation"),
    )

    # Update files with refined documentation. A rejected (unsafe) doc
    # path is best-effort: log and skip it — the microtask still completes.
    if self_review_result.documentation:
        try:
            _write_microtask_files(repo_path, self_review_result.documentation)
            microtask_files.update(self_review_result.documentation)
            mt.output_files = microtask_files
            all_files.update(self_review_result.documentation)
        except UnsafeRepoPathError as exc:
            logger.warning(
                "[%s] Microtask %s: unsafe documentation path rejected, skipping: %s",
                task_id,
                mt.id,
                exc,
            )

    logger.info(
        "[%s] Microtask %s: documentation self-review complete after %d iterations (score: %.2f)",
        task_id,
        mt.id,
        self_review_result.iterations,
        self_review_result.final_quality_score,
    )

    mt.status = microtask_status.COMPLETED
    completed_ids.add(mt.id)
    logger.info(
        "[%s] Microtask %s: COMPLETED (passed all review phases in %d cycles)",
        task_id,
        mt.id,
        total_cycles,
    )


def run_gated_execution_impl(
    *,
    gate_config: GatedExecutionConfig,
    llm: LLMClient,
    task: Task,
    planning_result: Any,
    repo_path: Path,
    architecture: Optional[SystemArchitecture] = None,
    spec_content: str = "",
    existing_code: str = "",
    tool_runners: Optional[Dict[Any, Any]] = None,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]] = None,
    only_microtask_ids: Optional[List[str]] = None,
    review_config: Optional[Any] = None,
    review_deps: Optional[ReviewDependencies] = None,
) -> Any:
    """Execute microtasks with batch-based review cycles (shared skeleton).

    After each microtask is coded it must pass sequential review gates:
    1. Code Review (build + lint + code review) — batch-fix all issues, retrying
       in place up to ``code_review_retry_cap`` before failing the microtask.
    2. QA Testing — batch-fix all issues, then restart from Code Review.
    3. Security Testing — batch-fix all issues, then restart from Code Review.
    4. Documentation — self-review loop (never fails).

    ``gate_config`` supplies every per-team divergence; the control flow, retry
    behaviour, rollback, and the ``progress_callback`` contract are identical to
    the pre-refactor per-team ``run_execution_with_review_gates`` loops. The
    per-microtask work itself is delegated to three helpers, one per group of
    phases: :func:`_execute_coding_phase` (Phase 1), :func:`_run_review_cycles`
    (Phases 2-4: code review/QA/security with retries, the grounding circuit
    breaker, and max-cycles resolution), and :func:`_run_documentation_phase`
    (Phase 5). This function is the orchestrator: per-microtask setup (the
    dependency/SKIPPED check, ``IN_PROGRESS`` bookkeeping, the shared detail
    callback), calling those three helpers in order, and the final summary.

    ``progress_callback(current_index, completed, total, title, microtask_phase, phase_detail)``
    is called during execution; ``current_index`` is 1-based and ``microtask_phase``
    is one of "coding", "code_review", "qa_testing", "security_testing",
    "documentation", "completed".

    Preconditions:
        ``gate_config.models`` exposes ``MicrotaskStatus``, ``ExecutionResult``,
        ``ToolAgentInput``, ``ToolAgentKind``, ``ReviewResult``,
        ``MicrotaskReviewFailedError`` and ``MicrotaskReviewConfig``.
        ``gate_config.run_code_review_gate`` accepts a ``review_context`` keyword
        argument (the per-team adapter forwards it into its code-review call); a
        ``review_context`` is built here from ``architecture``/``spec_content``
        only when at least one is actually populated, and is ``None`` otherwise
        (both default to ``None``/``""`` so a caller without them yet is
        unaffected, and the LLM fallback reviewers' context-bounding path is
        never entered with nothing to bound).
        ``review_config.enable_llm_review_grounding`` (default True) is forwarded
        to the code-review gate so the LLM-fallback path can drop ungrounded
        proper-noun findings; set it False to disable that filter.
    Postconditions:
        Returns an ``ExecutionResult``; each microtask ends COMPLETED, SKIPPED,
        FAILED or REVIEW_FAILED. When a microtask's review fails and
        ``on_failure == "stop"`` (or a security failure with
        ``security_failure_always_stops``), raises ``MicrotaskReviewFailedError``.
    """
    models = gate_config.models
    microtask_status = models.MicrotaskStatus
    review_result_cls = models.ReviewResult
    review_failed_error_cls = models.MicrotaskReviewFailedError
    tool_agent_kind = models.ToolAgentKind

    config = review_config or models.MicrotaskReviewConfig()
    deps = review_deps or ReviewDependencies()
    runners = tool_runners or {}
    # None (not an empty ReviewContext()) when there is nothing to add: the LLM
    # fallback reviewers only enter their context-bounding path (which calls
    # compute_code_review_*_chars(llm), requiring get_max_context_tokens()) when
    # review_context is not None -- an always-non-None ReviewContext would touch
    # that path even for a caller whose llm handle doesn't support it, for no
    # actual context to bound.
    review_context = (
        ReviewContext(architecture=architecture, spec_content=spec_content)
        if architecture is not None or spec_content
        else None
    )

    all_files: Dict[str, str] = {}
    microtasks = list(planning_result.microtasks)
    if only_microtask_ids is not None:
        id_set = set(only_microtask_ids)
        microtasks = [mt for mt in microtasks if mt.id in id_set]
    completed_ids: set[str] = set()
    review_failed_ids: set[str] = set()
    total = len(microtasks)

    task_id = task.id
    logger.info("%s", gate_config.startup_log_message(task_id, total, config))

    max_total_cycles = gate_config.max_total_cycles(config)
    code_review_retry_cap = gate_config.code_review_retry_cap(config)

    for idx, mt in enumerate(microtasks):
        deps_met = all(d in completed_ids for d in mt.depends_on)
        if not deps_met:
            unmet = [d for d in mt.depends_on if d not in completed_ids]
            if any(d in review_failed_ids for d in unmet):
                logger.warning(
                    "[%s] Microtask %s depends on review-failed microtasks %s — skipping",
                    task_id,
                    mt.id,
                    unmet,
                )
                mt.status = microtask_status.SKIPPED
                mt.notes = f"Skipped: depends on review-failed microtasks {unmet}"
                continue
            # Unmet but not review-failed (not yet run, or run out of order): soft
            # dependency hint, not a verified DAG -- run anyway rather than fail
            # closed on a planner ordering quirk (see run_execution_impl's docstring
            # for the fuller rationale, shared by both loops).
            logger.warning(
                "[%s] Microtask %s has unmet deps %s — running anyway", task_id, mt.id, unmet
            )

        mt.status = microtask_status.IN_PROGRESS
        logger.info(
            "[%s] Execution: microtask %d/%d — %s (%s)",
            task_id,
            idx + 1,
            total,
            mt.id,
            mt.tool_agent.value,
        )

        current_idx = idx + 1

        if progress_callback:
            progress_callback(
                current_idx,
                len(completed_ids),
                total,
                mt.title or mt.id,
                "coding",
                "Generating code...",
            )

        def _detail_cb(detail: str, _idx: int, _phase: str) -> None:
            """Forward phase detail to progress callback."""
            if progress_callback:
                progress_callback(
                    _idx, len(completed_ids), total, mt.title or mt.id, _phase, detail
                )

        coding_result = _execute_coding_phase(
            llm=llm,
            mt=mt,
            task=task,
            task_id=task_id,
            planning_result=planning_result,
            repo_path=repo_path,
            existing_code=existing_code,
            architecture=architecture,
            runners=runners,
            models=models,
            run_general_microtask=gate_config.run_general_microtask,
            all_files=all_files,
            review_failed_ids=review_failed_ids,
            microtask_status=microtask_status,
            progress_callback=progress_callback,
            current_idx=current_idx,
            completed_ids=completed_ids,
            total=total,
        )
        if coding_result is None:
            continue
        microtask_files, microtask_rollback = coding_result

        phase_failed, microtask_files, total_cycles = _run_review_cycles(
            gate_config=gate_config,
            llm=llm,
            task=task,
            task_id=task_id,
            mt=mt,
            microtask_files=microtask_files,
            repo_path=repo_path,
            deps=deps,
            review_context=review_context,
            config=config,
            planning_result=planning_result,
            all_files=all_files,
            review_failed_ids=review_failed_ids,
            microtask_rollback=microtask_rollback,
            microtask_status=microtask_status,
            review_result_cls=review_result_cls,
            review_failed_error_cls=review_failed_error_cls,
            max_total_cycles=max_total_cycles,
            code_review_retry_cap=code_review_retry_cap,
            progress_callback=progress_callback,
            current_idx=current_idx,
            completed_ids=completed_ids,
            total=total,
            detail_cb=_detail_cb,
        )

        # ── Phase 5: Documentation (Self-Review, Never Fails) ─────────────────
        if not phase_failed:
            _run_documentation_phase(
                gate_config=gate_config,
                llm=llm,
                task=task,
                task_id=task_id,
                mt=mt,
                microtask_files=microtask_files,
                repo_path=repo_path,
                deps=deps,
                tool_agent_kind=tool_agent_kind,
                all_files=all_files,
                microtask_status=microtask_status,
                completed_ids=completed_ids,
                total_cycles=total_cycles,
                progress_callback=progress_callback,
                current_idx=current_idx,
                total=total,
                detail_cb=_detail_cb,
            )

        if progress_callback:
            progress_callback(
                current_idx, len(completed_ids), total, mt.title or mt.id, "completed", ""
            )

    completed_count = len(completed_ids)
    failed_count = len(review_failed_ids)
    summary = f"Executed {completed_count}/{total} microtasks successfully; {failed_count} review-failed; {len(all_files)} files produced."
    logger.info("[%s] Execution with batch review flow complete: %s", task_id, summary)

    return models.ExecutionResult(files=all_files, microtasks=microtasks, summary=summary)
