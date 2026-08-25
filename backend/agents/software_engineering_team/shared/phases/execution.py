"""Shared Execution-phase leaf helpers for the code-v2 teams, including the
gated per-microtask review loop (``run_gated_execution_impl``).
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from llm_service import LLMClient
from llm_service.strands_model import LlmRunner
from shared.concurrency import KeyedLockManager, parallel_map
from shared.dev_models.models import ReviewContext, SystemArchitecture, Task
from shared.env import parse_int
from software_engineering_team.shared.agent_review import AgentReviewCache
from software_engineering_team.shared.code_completeness import reject_invalid_python
from software_engineering_team.shared.phases.dbc_phase import _run_dbc_self_review
from software_engineering_team.shared.phases.documentation_phase import _run_documentation_phase
from software_engineering_team.shared.phases.review_cycle import (
    GateOutcome,
    _locked_write_and_merge,
    _run_review_cycles,
)
from software_engineering_team.shared.phases.rollback import (
    _file_lock_keys,
    _MicrotaskRollback,
)
from software_engineering_team.shared.stack_profile import PhaseModels, StackProfile

logger = logging.getLogger(__name__)

# Cap concurrent microtask workers in one independent wave, independent of wave
# size. ``parallel_map`` further mins this with ``len(batch)``.
_WAVE_EXECUTION_CONCURRENCY = 4


def _execution_wave_concurrency() -> int:
    """Max concurrent microtask workers in one independent wave.

    Configurable via ``SE_EXECUTION_WAVE_CONCURRENCY`` (default 4; garbage/empty
    → default; floored at 1 so a wave always makes progress).

    Preconditions:
        None (reads only the optional environment variable).
    Postconditions:
        Returns an int >= 1.
    """
    return parse_int("SE_EXECUTION_WAVE_CONCURRENCY", _WAVE_EXECUTION_CONCURRENCY, minimum=1)


def _wave_max_workers(n: int) -> int:
    """Return the pool width for a wave of ``n`` independent microtasks.

    Preconditions:
        ``n >= 1``.
    Postconditions:
        Returns an int in ``[1, n]``.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    return min(n, _execution_wave_concurrency())


def _expect_progress_indices(
    progress_callback: Optional[Callable[..., None]],
    indices: Sequence[int],
) -> None:
    """Register wave members with the coalescer before concurrent fan-out.

    Preconditions:
        ``indices`` are the 1-based progress indices of the current independent
        wave.
    Postconditions:
        No-op when ``progress_callback`` has no ``expect``; otherwise those
        indices block ``completed`` publication until each has ticked or been
        released.
    """
    expect = getattr(progress_callback, "expect", None)
    if expect is not None:
        expect(indices)


def _release_progress_index(
    progress_callback: Optional[Callable[..., None]],
    current_idx: int,
) -> None:
    """Drop a coalescer registration if the worker finished without a tick.

    Preconditions:
        ``current_idx`` is the 1-based progress index of this worker.
    Postconditions:
        No-op when ``progress_callback`` has no ``release``.
    """
    release = getattr(progress_callback, "release", None)
    if release is not None:
        release(current_idx)


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
    """Execute microtasks in dependency-respecting waves, concurrently within a wave.

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
        execution continues with the rest. Microtasks are grouped into
        dependency-respecting waves via :func:`_schedule_microtask_batches`;
        independent members of a wave run concurrently via ``parallel_map``,
        with the pool capped by ``SE_EXECUTION_WAVE_CONCURRENCY`` (default 4)
        rather than wave size. An unmet ``depends_on`` is logged and
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
    file_locks: KeyedLockManager[str] = KeyedLockManager()
    progress_lock = threading.Lock()
    progress_callback = _serialize_progress(progress_callback, progress_lock)

    batches = _schedule_microtask_batches(microtasks)
    idx = 0
    for batch in batches:
        indexed = []
        for mt in batch:
            idx += 1
            indexed.append((mt, idx))

        def _run_one(pair: Tuple[Any, int]) -> None:
            mt, current_idx = pair
            try:
                _run_one_ungated_microtask(
                    llm=llm,
                    mt=mt,
                    task=task,
                    current_idx=current_idx,
                    planning_result=planning_result,
                    repo_path=repo_path,
                    architecture=architecture,
                    existing_code=existing_code,
                    runners=runners,
                    models=models,
                    run_general_microtask=run_general_microtask,
                    all_files=all_files,
                    completed_ids=completed_ids,
                    file_locks=file_locks,
                    progress_callback=progress_callback,
                    total=total,
                    microtask_status_enum=microtask_status_enum,
                )
            finally:
                _release_progress_index(progress_callback, current_idx)

        if len(indexed) == 1 or _batch_has_intra_dependencies(batch):
            for pair in indexed:
                _run_one(pair)
        else:
            _expect_progress_indices(progress_callback, [i for _, i in indexed])
            parallel_map(
                indexed,
                _run_one,
                max_workers=_wave_max_workers(len(indexed)),
                skip_none=False,
                wait_for_stragglers=True,
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
    builds one instance via
    ``software_engineering_team.shared.v2_execution_bindings.build_execution_bindings``,
    called once from its ``phases/_profile.py``.

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
    # gates in ``frontend_code_v2_team/phases/_profile.py``) rather than
    # receiving it as a keyword here, so this callable signature is unchanged.
    # A team whose gate architecture calls one unified review function per
    # gate (rather than a gate-scoped shared phase function) should narrow
    # ``deps.tool_agents`` via the shared, public
    # ``software_engineering_team.shared.v2_execution_bindings.scope_tool_agents_by_kind``
    # helper rather than hand-rolling its own copy.
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
    # Injected DbC comments self-review callable (the inner
    # ``gate_config.run_dbc_self_review`` that ``dbc_phase._run_dbc_self_review``
    # calls). ``None`` (the default) means the team has not wired DbC yet, and the
    # gated loop skips the phase entirely -- fully backward-compatible until a
    # team's ``GATE_CONFIG`` sets it to a real review callable.
    run_dbc_self_review: Optional[Callable[..., Any]] = None


def _generate_coding_phase(
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
    microtask_status: Any,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]],
    current_idx: int,
    completed_ids: set,
    total: int,
) -> Optional[Dict[str, str]]:
    """Generate a microtask's files without writing them (Phase 1, generate half).

    Runs outside the worktree lock so overlapping siblings can generate
    concurrently; the caller then holds that lock from the first write through
    review, docs, and rollback.

    Preconditions:
        ``mt.status`` is already ``IN_PROGRESS``.
    Postconditions:
        On success, returns the generated ``{path: content}`` map. On a coding
        exception, ``mt`` is marked ``FAILED`` with the exception text in
        ``mt.notes``, exactly one ``"completed"`` progress tick is fired here
        (the caller's own trailing tick never runs for a microtask this
        function finishes), and ``None`` is returned.
    """
    try:
        return generate_microtask_files(
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
    except Exception as exc:
        logger.error("[%s] Microtask %s execution failed: %s", task_id, mt.id, exc)
        mt.status = microtask_status.FAILED
        mt.notes = str(exc)
        if progress_callback:
            progress_callback(
                current_idx, len(completed_ids), total, mt.title or mt.id, "completed", ""
            )
        return None


def _commit_coding_write(
    *,
    file_locks: KeyedLockManager[str],
    repo_path: Path,
    microtask_files: Dict[str, str],
    mt: Any,
    task_id: str,
    review_failed_ids: set,
    all_files: Dict[str, str],
    microtask_status: Any,
) -> Optional[Tuple[Dict[str, str], _MicrotaskRollback]]:
    """Guard-write generated files and return the rollback manifest (Phase 1, write half).

    Preconditions:
        ``microtask_files`` was produced by :func:`_generate_coding_phase`;
        ``file_locks`` is the per-run manager. The caller holds
        ``worktree_lock`` for this write, so the per-path locks are uncontended.
    Postconditions:
        On success, returns ``(microtask_files, rollback)`` with a fresh
        :class:`_MicrotaskRollback` snapshotting the pre-write state, and
        ``all_files`` updated in place. On an unsafe initial write,
        :func:`write_microtask_output_or_fail` has already marked ``mt``
        ``REVIEW_FAILED`` and rolled back ``all_files`` / the worktree, and
        ``None`` is returned. The caller emits the terminal ``completed``
        progress tick so ``_serialize_progress`` can drop this index from
        its active set.
    """
    microtask_rollback = _MicrotaskRollback()
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
        return None
    return microtask_files, microtask_rollback


def _schedule_microtask_batches(microtasks: List[Any]) -> List[List[Any]]:
    """Group ``microtasks`` into ordered, dependency-respecting batches (waves).

    Kahn's-algorithm-style level scheduling: batch 0 holds every microtask whose
    ``depends_on`` ids are all either not the id of any microtask in this call, or
    already placed in an earlier batch; batch *k* holds every remaining microtask
    once all of its in-run dependencies are in batches ``< k``. This function only
    determines *ordering* -- it does not consult ``completed_ids``/``review_failed_ids``
    and does not itself skip anything; ``run_gated_execution_impl`` still performs
    its runtime SKIP-on-review-failed-dependency check against those sets for each
    microtask as it iterates the flattened batch order, exactly as it did against
    ``enumerate(microtasks)`` before this change.

    Preconditions:
        Every ``mt`` in ``microtasks`` has a ``.id: str`` (unique within the list)
        and a ``.depends_on: List[str]``. Uniqueness is enforced here -- a
        duplicate ``id`` raises ``ValueError`` rather than being silently
        accepted, since the id-keyed bookkeeping below cannot distinguish two
        microtasks sharing one id (``parse_planning_output`` is the intended
        upstream guard that keeps this precondition satisfied in practice).
    Postconditions:
        Returns a list of non-empty batches whose concatenation is a permutation
        of ``microtasks`` containing each microtask exactly once, preserving each
        microtask's original relative order within its batch. A ``depends_on`` id
        that is not the ``.id`` of any microtask in this call (a stale planner id,
        or one excluded by ``only_microtask_ids`` filtering upstream) never blocks
        placement -- mirroring the caller's existing "unmet but not review-failed --
        running anyway" soft-dependency semantics, which is a *hint*, not a
        verified DAG. A dependency cycle among the microtasks (never expected from
        a real planner) does not raise or loop forever: once no further microtask
        can be placed, every still-unplaced microtask is flushed into one final
        batch, in original relative order.
    Invariants:
        ``sum(len(b) for b in batches) == len(microtasks)``.
    """
    id_set = {mt.id for mt in microtasks}
    if len(id_set) != len(microtasks):
        counts = Counter(mt.id for mt in microtasks)
        duplicates = sorted(mid for mid, count in counts.items() if count > 1)
        raise ValueError(
            f"Duplicate microtask id(s) {duplicates} in scheduling input; "
            "ids must be unique within a single run."
        )
    indegree: Dict[str, int] = {}
    dependents: Dict[str, List[str]] = {}
    for mt in microtasks:
        in_run_deps = {d for d in mt.depends_on if d in id_set}
        indegree[mt.id] = len(in_run_deps)
        for dep_id in in_run_deps:
            dependents.setdefault(dep_id, []).append(mt.id)

    by_id = {mt.id: mt for mt in microtasks}
    placed: set = set()
    batches: List[List[Any]] = []

    while len(placed) < len(microtasks):
        frontier_ids = [mt.id for mt in microtasks if mt.id not in placed and indegree[mt.id] == 0]
        if not frontier_ids:
            # Cycle (or otherwise unresolvable): flush the rest in original order
            # rather than looping forever -- today's runtime "running anyway"
            # hint-not-enforced fallback covers the actual dependency check.
            frontier_ids = [mt.id for mt in microtasks if mt.id not in placed]

        batches.append([by_id[mid] for mid in frontier_ids])
        for mid in frontier_ids:
            placed.add(mid)
        for mid in frontier_ids:
            for dependent_id in dependents.get(mid, []):
                indegree[dependent_id] -= 1

    return batches


def _batch_has_intra_dependencies(batch: List[Any]) -> bool:
    """Return True when any microtask in ``batch`` depends on another in ``batch``.

    Preconditions:
        Every ``mt`` has ``.id`` and ``.depends_on``.
    Postconditions:
        True iff at least one ``depends_on`` id is the ``.id`` of another member
        of this same batch — the cycle-flush case from
        :func:`_schedule_microtask_batches`. Independent waves return False.
    """
    ids = {mt.id for mt in batch}
    return any(any(dep in ids for dep in mt.depends_on) for mt in batch)


def _clone_review_deps(deps: ReviewDependencies) -> ReviewDependencies:
    """Shallow-copy ``deps`` so a worker can reset ``tool_agent_cache`` privately.

    Preconditions:
        ``deps`` is a :class:`ReviewDependencies` instance.
    Postconditions:
        Returns a new instance sharing the same agents/verifiers/tool map, with
        ``tool_agent_cache`` left ``None`` (the review-cycle helper constructs a
        fresh per-microtask cache on entry).
    """
    return ReviewDependencies(
        build_verifier=deps.build_verifier,
        qa_agent=deps.qa_agent,
        security_agent=deps.security_agent,
        code_review_agent=deps.code_review_agent,
        linting_tool_agent=deps.linting_tool_agent,
        tool_agents=deps.tool_agents,
        tool_agent_cache=None,
    )


class _ProgressCoalescer:
    """Serialize concurrent progress ticks so the dashboard cannot regress.

    Invariants:
        ``_pending`` holds expected wave indices that have not ticked yet;
        ``_active`` holds indices whose last tick was not ``completed``.
    """

    def __init__(
        self,
        progress_callback: Callable[[int, int, int, str, str, str], None],
        lock: threading.Lock,
    ) -> None:
        self._cb = progress_callback
        self._lock = lock
        self._pending: set[int] = set()
        self._active: set[int] = set()
        self._in_progress: Dict[int, Tuple[int, str, str, str]] = {}
        self._max_completed = 0

    def expect(self, indices: Sequence[int]) -> None:
        """Register wave members that have not ticked yet.

        Preconditions:
            ``indices`` are the 1-based progress indices of the current
            independent wave; called before ``parallel_map`` starts workers.
        Postconditions:
            Those indices block ``completed`` publication until each has
            either emitted a tick or been :meth:`release`d.
        """
        with self._lock:
            self._pending.update(indices)

    def release(self, current_index: int) -> None:
        """Drop a registered index that finished without a further tick.

        Preconditions:
            ``current_index`` is a 1-based progress index previously passed
            to :meth:`expect` or to ``__call__``.
        Postconditions:
            ``current_index`` is no longer in ``_pending`` or ``_active``.
        """
        with self._lock:
            self._pending.discard(current_index)
            self._active.discard(current_index)
            self._in_progress.pop(current_index, None)

    def __call__(
        self,
        current_index: int,
        completed: int,
        total: int,
        title: str,
        microtask_phase: str,
        phase_detail: str,
    ) -> None:
        with self._lock:
            if microtask_phase == "completed":
                self._pending.discard(current_index)
                self._active.discard(current_index)
                self._in_progress.pop(current_index, None)
            else:
                self._pending.discard(current_index)
                self._active.add(current_index)
                self._in_progress[current_index] = (total, title, microtask_phase, phase_detail)
            self._max_completed = max(self._max_completed, completed)
            blockers = self._pending | self._active
            if microtask_phase == "completed" and blockers:
                if self._in_progress:
                    live_idx = max(self._in_progress)
                    tot, live_title, live_phase, live_detail = self._in_progress[live_idx]
                    self._cb(
                        live_idx, self._max_completed, tot, live_title, live_phase, live_detail
                    )
                return
            self._cb(
                current_index, self._max_completed, total, title, microtask_phase, phase_detail
            )


def _serialize_progress(
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]],
    lock: threading.Lock,
) -> Optional[_ProgressCoalescer]:
    """Wrap ``progress_callback`` so concurrent workers cannot regress dashboard state.

    ``shared.v2_orchestrator._build_progress_callback`` persists every tick's
    index, phase, and completed count directly. A mutex alone still lets a
    slower sibling overwrite a later snapshot (index 2 / ``completed`` /
    ``completed`` phase) with an earlier one (index 1 / ``coding``).

    Coalescing under ``lock``:
        - Published ``completed`` is monotonic.
        - A ``completed``-phase tick is not forwarded while another index is
          still in a non-completed phase, or while a wave member registered
          via :meth:`_ProgressCoalescer.expect` has not started. Instead the
          still-active sibling's last in-progress snapshot is re-emitted
          with the updated completed count (or the tick is suppressed when
          no sibling has started yet).
        - In-progress ticks always forward (they are live work).

    Preconditions:
        ``lock`` is dedicated to this callback (not reused for file writes).
    Postconditions:
        Returns ``None`` when ``progress_callback`` is ``None``; otherwise a
        coalescer that holds ``lock`` for the duration of each call.
    """
    if progress_callback is None:
        return None
    return _ProgressCoalescer(progress_callback, lock)


def _run_one_gated_microtask(
    *,
    gate_config: GatedExecutionConfig,
    llm: LLMClient,
    task: Task,
    task_id: str,
    mt: Any,
    current_idx: int,
    planning_result: Any,
    repo_path: Path,
    architecture: Optional[SystemArchitecture],
    existing_code: str,
    runners: Dict[Any, Any],
    models: PhaseModels,
    review_context: Optional[ReviewContext],
    config: Any,
    deps: ReviewDependencies,
    all_files: Dict[str, str],
    review_failed_ids: set,
    completed_ids: set,
    microtask_status: Any,
    review_result_cls: Any,
    review_failed_error_cls: Any,
    tool_agent_kind: Any,
    max_total_cycles: int,
    code_review_retry_cap: int,
    total: int,
    file_locks: KeyedLockManager[str],
    worktree_lock: threading.Lock,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]],
    build_verify_label: str = "",
) -> None:
    """Run one microtask's full gated pipeline (coding, review cycles, docs).

    Preconditions:
        ``mt`` belongs to the current wave; ``deps`` is a worker-private
        :class:`ReviewDependencies` (not shared with a sibling worker);
        ``file_locks`` is the per-run manager; ``worktree_lock`` is the
        per-run exclusive lock for worktree mutation and repo-wide review
        tools.
    Postconditions:
        ``mt`` ends COMPLETED, SKIPPED, FAILED, or REVIEW_FAILED. A
        ``review_failed_error_cls`` is raised when review fails and
        ``config.on_failure == "stop"`` (or a security failure with
        ``security_failure_always_stops``). Shared ``all_files`` /
        ``completed_ids`` / ``review_failed_ids`` are updated in place.
        Generation runs unlocked; the first write through review, docs, and
        rollback holds ``worktree_lock`` so siblings cannot interleave
        snapshot/write/rollback, introduce colliding new paths, or run
        repo-wide build/lint against an in-progress sibling worktree. After
        the ``coding`` progress tick, a terminal ``completed`` tick always
        fires so ``_serialize_progress`` can drop this index from its
        active set — including on an unsafe initial write and when
        ``review_failed_error_cls`` is raised. ``_generate_coding_phase``
        already fires that tick on a coding exception, so this function
        returns without a second one.
    """
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
            if progress_callback:
                progress_callback(
                    current_idx, len(completed_ids), total, mt.title or mt.id, "completed", ""
                )
            return
        logger.warning(
            "[%s] Microtask %s has unmet deps %s — running anyway", task_id, mt.id, unmet
        )

    mt.status = microtask_status.IN_PROGRESS
    logger.info(
        "[%s] Execution: microtask %d/%d — %s (%s)",
        task_id,
        current_idx,
        total,
        mt.id,
        mt.tool_agent,
    )

    def _detail_cb(detail: str, _idx: int, _phase: str) -> None:
        """Forward phase detail to the (already serialized) progress callback."""
        if progress_callback:
            progress_callback(_idx, len(completed_ids), total, mt.title or mt.id, _phase, detail)

    if progress_callback:
        progress_callback(
            current_idx,
            len(completed_ids),
            total,
            mt.title or mt.id,
            "coding",
            "Generating code...",
        )

    microtask_files = _generate_coding_phase(
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
        microtask_status=microtask_status,
        progress_callback=progress_callback,
        current_idx=current_idx,
        completed_ids=completed_ids,
        total=total,
    )
    if microtask_files is None:
        return

    # Generation is unlocked (no worktree writes). Write through review, docs,
    # and rollback hold ``worktree_lock``: review tools (build/lint) observe
    # the whole repo, and review/docs can introduce paths that were not in
    # the initial generation set, so per-file locks on those initial keys
    # are not enough. The terminal ``completed`` tick lives in ``finally`` so
    # an unsafe initial write or a stop-on-review-failure raise still clears
    # this index from ``_serialize_progress``'s active set.
    try:
        with worktree_lock:
            coding_result = _commit_coding_write(
                file_locks=file_locks,
                repo_path=repo_path,
                microtask_files=microtask_files,
                mt=mt,
                task_id=task_id,
                review_failed_ids=review_failed_ids,
                all_files=all_files,
                microtask_status=microtask_status,
            )
            if coding_result is None:
                return
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
                file_locks=file_locks,
            )

            if not phase_failed:
                # Optional DbC comments self-review, mirroring the Documentation
                # phase: a non-blocking, best-effort step that runs only once the
                # review-gate cycles have passed, and before Documentation so the
                # latter sees any DbC-augmented code as its ``code_files``. Inert
                # unless a team wired ``run_dbc_self_review`` and left
                # ``enable_dbc_comments`` on. ``_run_dbc_self_review`` never raises,
                # never sets ``mt.status``/``completed_ids``, and mutates
                # ``microtask_files`` in place.
                if gate_config.run_dbc_self_review is not None and config.enable_dbc_comments:
                    _run_dbc_self_review(
                        gate_config=gate_config,
                        task=task,
                        task_id=task_id,
                        mt=mt,
                        microtask_files=microtask_files,
                        repo_path=repo_path,
                        all_files=all_files,
                        architecture=architecture,
                        language=planning_result.language,
                        deps=deps,
                        build_verify_label=build_verify_label,
                        progress_callback=progress_callback,
                        current_idx=current_idx,
                        completed_ids=completed_ids,
                        total=total,
                        detail_cb=_detail_cb,
                    )

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
                    file_locks=file_locks,
                )
    finally:
        if progress_callback:
            progress_callback(
                current_idx, len(completed_ids), total, mt.title or mt.id, "completed", ""
            )


def _run_one_ungated_microtask(
    *,
    llm: LLMClient,
    mt: Any,
    task: Task,
    current_idx: int,
    planning_result: Any,
    repo_path: Path,
    architecture: Optional[SystemArchitecture],
    existing_code: str,
    runners: Dict[Any, Any],
    models: PhaseModels,
    run_general_microtask: Callable[..., Dict[str, str]],
    all_files: Dict[str, str],
    completed_ids: set,
    file_locks: KeyedLockManager[str],
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]],
    total: int,
    microtask_status_enum: Any,
) -> None:
    """Run one microtask of the non-gated execution loop.

    Preconditions:
        ``mt`` belongs to the current wave.
    Postconditions:
        On success ``mt`` is COMPLETED, ``all_files`` gains its output, and
        ``completed_ids`` gains ``mt.id``. On exception ``mt`` is FAILED and
        execution of other wave members is unaffected.
    """
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
        current_idx,
        total,
        mt.id,
        mt.tool_agent if mt.tool_agent else "none",
    )

    if progress_callback:
        progress_callback(
            current_idx,
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
        with file_locks.lock(_file_lock_keys(repo_path, mt.output_files.keys())):
            all_files.update(mt.output_files)
        mt.status = microtask_status_enum.COMPLETED
        completed_ids.add(mt.id)
    except Exception as exc:
        logger.error("[%s] Microtask %s failed: %s", task.id, mt.id, exc)
        mt.status = microtask_status_enum.FAILED
        mt.notes = str(exc)

    if progress_callback:
        progress_callback(
            current_idx, len(completed_ids), total, mt.title or mt.id, "completed", ""
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
    build_verify_label: str = "",
) -> Any:
    """Execute microtasks with batch-based review cycles (shared skeleton).

    After each microtask is coded it must pass sequential review gates:
    1. Code Review (build + lint + code review) — batch-fix all issues, retrying
       in place up to ``code_review_retry_cap`` before failing the microtask.
    2. QA Testing — batch-fix all issues, then restart from Code Review.
    3. Security Testing — batch-fix all issues, then restart from Code Review.
    4. DbC comments — optional, non-blocking self-review (never fails), run only
       when ``gate_config.run_dbc_self_review`` is wired and
       ``config.enable_dbc_comments`` is on, before Documentation so it sees any
       DbC-augmented code.
    5. Documentation — self-review loop (never fails).

    ``gate_config`` supplies every per-team divergence; the control flow, retry
    behaviour, rollback, and the ``progress_callback`` contract are identical to
    the pre-refactor per-team ``run_execution_with_review_gates`` loops. The
    per-microtask work itself is delegated to per-phase helpers:
    :func:`_generate_coding_phase` / :func:`_commit_coding_write` (Phase 1),
    :func:`_run_review_cycles` (Phases 2-4: code review/QA/security with retries,
    the grounding circuit breaker, and max-cycles resolution), the optional
    ``dbc_phase._run_dbc_self_review`` (DbC comments), and
    :func:`_run_documentation_phase` (Documentation). This function is the
    orchestrator: per-microtask setup (the dependency/SKIPPED check,
    ``IN_PROGRESS`` bookkeeping, the shared detail callback), calling those
    helpers in order, and the final summary.

    ``progress_callback(current_index, completed, total, title, microtask_phase, phase_detail)``
    is called during execution; ``current_index`` is 1-based and ``microtask_phase``
    is one of "coding", "code_review", "qa_testing", "security_testing",
    "qa_security_testing", "dbc", "documentation", "completed". "qa_security_testing"
    is reported only while ``parallelize_qa_security`` is in effect and QA and
    Security are both in flight concurrently -- neither has a confirmed outcome
    yet, so it must not be read as "qa_testing has passed".

    Preconditions:
        ``gate_config.models`` exposes ``MicrotaskStatus``, ``ExecutionResult``,
        ``ToolAgentInput``, ``ToolAgentKind``, ``ReviewResult``,
        ``MicrotaskReviewFailedError`` and ``MicrotaskReviewConfig``.
        ``review_deps`` is an optional :class:`ReviewDependencies` instance
        (a fresh one is constructed when omitted). Each concurrent worker
        receives a shallow clone so ``_run_review_cycles`` can reset
        ``tool_agent_cache`` without racing a sibling; a team's gate callables
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
        to the code-review gate for call-signature compatibility only; both V2
        teams' coordinator-backed LLM fallback treats it as a no-op (see
        ``_run_llm_review``'s docstring in either team's ``phases/review.py``).
    Postconditions:
        Returns an ``ExecutionResult``; each microtask ends COMPLETED, SKIPPED,
        FAILED or REVIEW_FAILED. Independent members of a scheduled wave run
        concurrently via ``parallel_map``, with the pool capped by
        ``SE_EXECUTION_WAVE_CONCURRENCY`` (default 4) rather than wave size.
        Generation of independent wave
        members runs concurrently; write through review, docs, and rollback
        hold a per-run worktree lock because review tools observe the whole
        repo and review/docs can introduce paths not present in the initial
        generation set. A wave whose members depend on each other (the
        scheduler's cycle-flush batch) still runs sequentially so
        SKIP-on-review-failed can observe an in-batch predecessor. When a
        microtask's review fails and ``on_failure == "stop"`` (or a security
        failure with ``security_failure_always_stops``), raises
        ``MicrotaskReviewFailedError`` and the next wave is not started;
        ``wait_for_stragglers=True`` so independent siblings already running
        in the failing wave finish their worktree writes before the exception
        propagates.
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

    # Wave-based (topological-batch) scheduling only reorders iteration; it does
    # not change which microtasks run or how many, and ``microtasks`` itself (used
    # for the final ``ExecutionResult.microtasks``) is left in its original,
    # planner-emitted order. Independent members of a wave run concurrently;
    # a batch with intra-batch edges (cycle flush) stays sequential.
    batches = _schedule_microtask_batches(microtasks)
    file_locks: KeyedLockManager[str] = KeyedLockManager()
    worktree_lock = threading.Lock()
    progress_lock = threading.Lock()
    progress_callback = _serialize_progress(progress_callback, progress_lock)

    task_id = task.id
    logger.info("%s", gate_config.startup_log_message(task_id, total, config))

    max_total_cycles = gate_config.max_total_cycles(config)
    code_review_retry_cap = gate_config.code_review_retry_cap(config)

    idx = 0
    for batch in batches:
        indexed: List[Tuple[Any, int]] = []
        for mt in batch:
            idx += 1
            indexed.append((mt, idx))

        def _run_one(pair: Tuple[Any, int]) -> None:
            mt, current_idx = pair
            try:
                _run_one_gated_microtask(
                    gate_config=gate_config,
                    llm=llm,
                    task=task,
                    task_id=task_id,
                    mt=mt,
                    current_idx=current_idx,
                    planning_result=planning_result,
                    repo_path=repo_path,
                    architecture=architecture,
                    existing_code=existing_code,
                    runners=runners,
                    models=models,
                    review_context=review_context,
                    config=config,
                    deps=_clone_review_deps(deps),
                    all_files=all_files,
                    review_failed_ids=review_failed_ids,
                    completed_ids=completed_ids,
                    microtask_status=microtask_status,
                    review_result_cls=review_result_cls,
                    review_failed_error_cls=review_failed_error_cls,
                    tool_agent_kind=tool_agent_kind,
                    max_total_cycles=max_total_cycles,
                    code_review_retry_cap=code_review_retry_cap,
                    total=total,
                    file_locks=file_locks,
                    worktree_lock=worktree_lock,
                    progress_callback=progress_callback,
                    build_verify_label=build_verify_label,
                )
            finally:
                _release_progress_index(progress_callback, current_idx)

        if len(indexed) == 1 or _batch_has_intra_dependencies(batch):
            for pair in indexed:
                _run_one(pair)
        else:
            _expect_progress_indices(progress_callback, [i for _, i in indexed])
            parallel_map(
                indexed,
                _run_one,
                max_workers=_wave_max_workers(len(indexed)),
                skip_none=False,
                wait_for_stragglers=True,
            )

    completed_count = len(completed_ids)
    failed_count = len(review_failed_ids)
    summary = f"Executed {completed_count}/{total} microtasks successfully; {failed_count} review-failed; {len(all_files)} files produced."
    logger.info("[%s] Execution with batch review flow complete: %s", task_id, summary)

    return models.ExecutionResult(files=all_files, microtasks=microtasks, summary=summary)
