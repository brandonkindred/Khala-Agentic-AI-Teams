"""Shared Execution-phase leaf helpers for the code-v2 teams, including the
gated per-microtask review loop (``run_gated_execution_impl``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_service import LLMClient
from llm_service.strands_model import LlmRunner
from shared.dev_models.models import ReviewContext, SystemArchitecture, Task
from software_engineering_team.shared.agent_review import AgentReviewCache
from software_engineering_team.shared.code_completeness import reject_invalid_python
from software_engineering_team.shared.phases.documentation_phase import _run_documentation_phase
from software_engineering_team.shared.phases.review_cycle import (
    GateOutcome,
    _run_review_cycles,
    write_microtask_output_or_fail,
)
from software_engineering_team.shared.phases.rollback import (
    _MicrotaskRollback,
    _record_prior_values,
)
from software_engineering_team.shared.stack_profile import PhaseModels, StackProfile

logger = logging.getLogger(__name__)


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
        tool_agent_cache: Optional[AgentReviewCache] = None,
    ) -> None:
        self.build_verifier = build_verifier
        self.qa_agent = qa_agent
        self.security_agent = security_agent
        self.code_review_agent = code_review_agent
        self.linting_tool_agent = linting_tool_agent
        self.tool_agents = tool_agents or {}
        # Per-microtask-cycle cache of tool-agent review results, reset by
        # ``_run_review_cycles`` at the start of each microtask's cycle loop.
        # Unused (stays ``None``) unless a team's gate functions read it and
        # thread it into ``run_microtask_review`` — currently only the
        # frontend team does (see docs/GATE_DEPENDENCY_GRAPH.md's "residual
        # 2x" caching design); other callers are unaffected.
        self.tool_agent_cache = tool_agent_cache


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
            mt.tool_agent.value if mt.tool_agent else "none",
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
    # own cross-cycle cache. This ``cache`` keyword is a separate, pre-existing
    # mechanism from ``deps.tool_agent_cache`` (see ``ReviewDependencies``
    # above): it caches the QA/security *LLM* steps, while ``tool_agent_cache``
    # caches tool-agent ``.review()`` results and travels via ``deps`` — a
    # team's gate callables read it off ``deps`` themselves (see the frontend
    # gates in ``frontend_code_v2_team/phases/execution.py``) rather than
    # receiving it as a keyword here, so this callable signature is unchanged.
    run_code_review_gate: Callable[..., GateOutcome]
    run_qa_gate: Callable[..., GateOutcome]
    run_security_gate: Callable[..., GateOutcome]
    # Shared-signature helpers, injected so the team's ``Agent`` patch surface
    # (in its ``problem_solving.py`` / ``review.py``) stays per-team.
    run_batch_coding_fixes: Callable[..., Any]
    run_documentation_self_review: Callable[..., Any]
    # ``mt.status`` set at each gate's entry (backend: distinct per phase;
    # frontend: ``IN_REVIEW`` for all three — a no-op re-assign after code review).
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
    # same post-Code-Review snapshot (see docs/GATE_DEPENDENCY_GRAPH.md).
    # Defaults False (fully sequential QA -> Security) on this shared
    # dataclass; both backend_code_v2_team and frontend_code_v2_team override
    # it to True in their own ``GATE_CONFIG`` now that frontend's tool-agent
    # fan-out is scoped per gate the way the backend's already is.
    parallelize_qa_security: bool = False
    # ``mt.status`` set instead of ``status_qa``/``status_security`` when
    # ``parallelize_qa_security`` is in effect: QA and Security run at once, so
    # there is no per-gate entry point for either to claim its own status at —
    # this single combined status covers the whole concurrent window (see
    # ``_run_review_cycles``'s concurrent branch). Defaults to ``None``, in
    # which case the concurrent branch falls back to ``status_qa``.
    status_qa_security: Any = None


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
    "qa_security_testing", "documentation", "completed". "qa_security_testing"
    is reported only while ``parallelize_qa_security`` is in effect and QA and
    Security are both in flight concurrently -- neither has a confirmed outcome
    yet, so it must not be read as "qa_testing has passed".

    Preconditions:
        ``gate_config.models`` exposes ``MicrotaskStatus``, ``ExecutionResult``,
        ``ToolAgentInput``, ``ToolAgentKind``, ``ReviewResult``,
        ``MicrotaskReviewFailedError`` and ``MicrotaskReviewConfig``.
        ``review_deps`` is an optional :class:`ReviewDependencies` instance
        (a fresh one is constructed when omitted); it is passed through to
        ``_run_review_cycles`` for every microtask, which resets its
        ``tool_agent_cache`` field to a new :class:`AgentReviewCache` at the
        start of each microtask's own cycle loop -- a team's gate callables
        that read ``deps.tool_agent_cache`` see one scoped to the microtask
        currently in progress, never a stale one from an earlier microtask.
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

    _current_mt = [None]

    def _detail_cb(detail: str, _idx: int, _phase: str) -> None:
        """Forward phase detail to progress callback."""
        if progress_callback:
            mt = _current_mt[0]
            progress_callback(_idx, len(completed_ids), total, mt.title or mt.id, _phase, detail)

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
        _current_mt[0] = mt
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
