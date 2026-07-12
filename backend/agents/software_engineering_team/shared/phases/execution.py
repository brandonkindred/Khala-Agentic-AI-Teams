"""
Shared Execution-phase leaf helpers for the code-v2 teams.

Holds the pieces that were byte-identical between the backend and frontend
execution phases — issue dedup, the review-dependency container, the
microtask-file writer, ``generate_microtask_files`` (produce one microtask's
output via its tool-runner or the general coder), the general (non-specialist)
microtask coder, and the non-gated ``run_execution`` loop. The stack-specific
``EXECUTION_PROMPT`` divergence (backend injects ``{language_conventions}``,
frontend does not) is handled via the team's
:class:`~software_engineering_team.shared.stack_profile.StackProfile`.

The gated per-microtask loop is lifted here too, as ``run_gated_execution_impl``
parameterised by a :class:`GatedExecutionConfig` — the same seam ``v2_review.py``
uses with ``ReviewConfig``. The two teams' review-gate architectures still diverge
(backend calls three separate ``run_{code_review,qa,security}_testing_phase``
functions returning a ``PhaseReviewResult``; frontend calls one unified
``run_microtask_review()`` three times and filters issues by ``source``), so each
team injects that difference as three gate-adapter callables that normalise their
result into a :class:`GateOutcome`; every other divergence (per-phase status enum,
retry-cap formula, max-cycles semantics, startup log) is a plain config field. The
loop skeleton itself — dependency skip, coding gate, the ``while not phase_failed``
review cycle, rollback, documentation self-review, progress emission — is shared.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_service import LLMClient
from software_engineering_team.shared.models import ReviewContext, SystemArchitecture, Task
from software_engineering_team.shared.repo_writer import UnsafeRepoPathError, write_repo_text_files
from software_engineering_team.shared.stack_profile import PhaseModels, StackProfile
from software_engineering_team.shared.strands_model import LlmRunner

logger = logging.getLogger(__name__)

# Hard char caps on the existing-code excerpt inlined into the general coder's
# prompt (no LLM-aware compaction here, unlike the code-review coordinator's
# excerpts -- this is a plain truncation to bound prompt size cheaply).
_GENERAL_MICROTASK_EXISTING_CODE_CHARS = 8_000
_TOOL_AGENT_EXISTING_CODE_CHARS = 6_000

# Iteration budget for the gated loop's own final documentation self-review pass.
# Deliberately its own (lower) constants rather than reusing
# review_utils.MIN/MAX_DOC_SELF_REVIEW_ITERATIONS (3/3): this pass runs once per
# microtask on top of the code/QA/security review cycles already spent, so a
# smaller budget here is intentional, not an oversight -- kept separate so tuning
# one never silently changes the other.
_GATED_DOC_SELF_REVIEW_MIN_ITERATIONS = 1
_GATED_DOC_SELF_REVIEW_MAX_ITERATIONS = 2
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
    microtask_file_keys: set,
    review_failed_status: Any,
) -> bool:
    """Write a microtask's output files, converting a rejected path into a failure.

    Shared by both teams' ``run_execution_with_review_gates`` review-cycle write
    sites. A safe write returns ``True``. An :class:`UnsafeRepoPathError` (an LLM
    fix emitted a traversal/empty path) is turned into a handled review failure:
    the microtask is marked with ``review_failed_status``, its files are rolled
    back out of ``all_files``, and the function returns ``False`` so the caller
    stops processing this microtask instead of letting the exception abort the run.

    Preconditions:
        ``review_failed_status`` is the team's ``MicrotaskStatus.REVIEW_FAILED``;
        ``microtask_file_keys`` are the keys this microtask contributed to
        ``all_files``.
    Postconditions:
        On success the files are on disk and ``True`` is returned. On rejection no
        unsafe file is written, ``mt`` is marked review-failed and its keys removed
        from ``all_files``, and ``False`` is returned. Never raises for an unsafe path.
    """
    try:
        _write_microtask_files(repo_path, files)
        return True
    except UnsafeRepoPathError as exc:
        logger.warning("[%s] Microtask %s: unsafe output path rejected: %s", task_id, mt.id, exc)
        mt.status = review_failed_status
        mt.notes = f"Rejected unsafe output path: {exc}"
        review_failed_ids.add(mt.id)
        for fk in microtask_file_keys:
            all_files.pop(fk, None)
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

    Preconditions:
        ``execution_prompt`` carries a ``{language_conventions}`` slot iff
        ``profile.has_language_conventions``.
    Postconditions:
        Returns the parsed ``{path: content}`` map (possibly empty).
    """
    arch_ctx = ""
    if architecture:
        # Lazy import: code_review_agent submodules are imported on demand
        # rather than at module scope elsewhere in the review call chain
        # (e.g. _code_review_step's CodeReviewInput import), so this module
        # follows the same convention rather than adding a new eager edge.
        from code_review_agent.architecture_context import render_architecture_context

        arch_ctx = render_architecture_context(architecture)

    fmt: Dict[str, Any] = dict(
        microtask_description=microtask.description or microtask.title,
        requirements=task.requirements or task.description,
        existing_code=existing_code[:_GENERAL_MICROTASK_EXISTING_CODE_CHARS]
        if existing_code
        else "(none)",
        architecture_context=arch_ctx or "(none)",
    )
    if profile.has_language_conventions:
        fmt["language_conventions"] = profile.conventions_for(language)
    prompt = execution_prompt.format(**fmt)
    raw = runner.run(llm, prompt)
    data = parse_files_and_summary(raw)
    files = data.get("files") or {}

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
            existing_code=existing_code[:_TOOL_AGENT_EXISTING_CODE_CHARS] if existing_code else "",
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
    after filtering issues by ``source``. The skeleton only reads these three
    fields, so it stays decoupled from the per-team review models.

    Invariants:
        ``passed`` is ``True`` iff the gate is satisfied and no fix is needed;
        ``issues`` are exactly the issues to batch-fix when ``not passed``.
    """

    passed: bool
    issues: List[Any] = field(default_factory=list)
    summary: str = ""


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
    # detail_callback) -> GateOutcome``. They own the per-team review-model fork.
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
    the pre-refactor per-team ``run_execution_with_review_gates`` loops.

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
        (both default to ``None``/``""`` so a caller without them yet is
        unaffected).
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
    review_context = ReviewContext(architecture=architecture, spec_content=spec_content)

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
        current_phase = "coding"

        if progress_callback:
            progress_callback(
                current_idx,
                len(completed_ids),
                total,
                mt.title or mt.id,
                "coding",
                "Generating code...",
            )

        def _detail_cb(detail: str, _idx: int = current_idx, _phase: str = current_phase) -> None:
            """Forward phase detail to progress callback."""
            if progress_callback:
                progress_callback(
                    _idx, len(completed_ids), total, mt.title or mt.id, _phase, detail
                )

        # ── Phase 1: Coding ───────────────────────────────────────────────────
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
                run_general_microtask=gate_config.run_general_microtask,
            )
            # Track files this microtask introduced for rollback on failure.
            microtask_file_keys = set(microtask_files.keys())
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
                microtask_file_keys=microtask_file_keys,
                review_failed_status=microtask_status.REVIEW_FAILED,
            ):
                continue
            all_files.update(microtask_files)

        except Exception as exc:
            logger.error("[%s] Microtask %s execution failed: %s", task_id, mt.id, exc)
            mt.status = microtask_status.FAILED
            mt.notes = str(exc)
            if progress_callback:
                progress_callback(
                    current_idx, len(completed_ids), total, mt.title or mt.id, "completed", ""
                )
            continue

        phase_failed = False
        total_cycles = 0
        # Last outcome of each gate — initialised passed so the max-cycles check
        # is well-defined even if a gate never ran this microtask.
        cr_outcome = GateOutcome(passed=True)
        qa_outcome = GateOutcome(passed=True)
        sec_outcome = GateOutcome(passed=True)

        # ── Sequential Review Gates with Batch Fixes ──────────────────────────
        # Flow: Code Review -> QA -> Security -> Documentation
        # After QA/Security fixes, restart from Code Review

        while not phase_failed and total_cycles < max_total_cycles:
            total_cycles += 1

            # ── Code Review Phase ─────────────────────────────────────────────
            mt.status = gate_config.status_code_review
            current_phase = "code_review"
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
                detail_callback=lambda d: _detail_cb(d, current_idx, "code_review"),
            )

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
                    issues=cr_outcome.issues,
                    current_files=microtask_files,
                    language=planning_result.language,
                    repo_path=str(repo_path),
                    task_id=task_id,
                    phase_name="code_review",
                    detail_callback=lambda d: _detail_cb(d, current_idx, "code_review"),
                )

                microtask_files = ps_result.files
                if not write_microtask_output_or_fail(
                    repo_path,
                    microtask_files,
                    mt=mt,
                    task_id=task_id,
                    review_failed_ids=review_failed_ids,
                    all_files=all_files,
                    microtask_file_keys=microtask_file_keys,
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
                    detail_callback=lambda d: _detail_cb(d, current_idx, "code_review"),
                )

            if not cr_outcome.passed:
                phase_failed = True
                mt.status = microtask_status.REVIEW_FAILED
                review_failed_ids.add(mt.id)
                mt.notes = f"Code review failed after {code_review_retry_cap} batch fix attempts: {cr_outcome.summary}"
                logger.warning(
                    "[%s] Microtask %s: CODE_REVIEW_FAILED after %d batch fix attempts. Issues: %s",
                    task_id,
                    mt.id,
                    code_review_retry_cap,
                    cr_outcome.summary,
                )
                # Rollback: remove this microtask's files from all_files
                for fk in microtask_file_keys:
                    all_files.pop(fk, None)
                if config.on_failure == "stop":
                    raise review_failed_error_cls(
                        mt,
                        review_result_cls(
                            passed=False, issues=cr_outcome.issues, summary=cr_outcome.summary
                        ),
                    )
                break

            # ── QA Testing Phase ──────────────────────────────────────────────
            mt.status = gate_config.status_qa
            current_phase = "qa_testing"
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
                detail_callback=lambda d: _detail_cb(d, current_idx, "qa_testing"),
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
                    issues=qa_outcome.issues,
                    current_files=microtask_files,
                    language=planning_result.language,
                    repo_path=str(repo_path),
                    task_id=task_id,
                    phase_name="qa",
                    detail_callback=lambda d: _detail_cb(d, current_idx, "qa_testing"),
                )

                microtask_files = ps_result.files
                if not write_microtask_output_or_fail(
                    repo_path,
                    microtask_files,
                    mt=mt,
                    task_id=task_id,
                    review_failed_ids=review_failed_ids,
                    all_files=all_files,
                    microtask_file_keys=microtask_file_keys,
                    review_failed_status=microtask_status.REVIEW_FAILED,
                ):
                    phase_failed = True
                    break
                mt.output_files = microtask_files
                all_files.update(microtask_files)

                # Restart from code review
                continue

            # ── Security Testing Phase ────────────────────────────────────────
            mt.status = gate_config.status_security
            current_phase = "security_testing"
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
                detail_callback=lambda d: _detail_cb(d, current_idx, "security_testing"),
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
                    issues=sec_outcome.issues,
                    current_files=microtask_files,
                    language=planning_result.language,
                    repo_path=str(repo_path),
                    task_id=task_id,
                    phase_name="security",
                    detail_callback=lambda d: _detail_cb(d, current_idx, "security_testing"),
                )

                microtask_files = ps_result.files
                if not write_microtask_output_or_fail(
                    repo_path,
                    microtask_files,
                    mt=mt,
                    task_id=task_id,
                    review_failed_ids=review_failed_ids,
                    all_files=all_files,
                    microtask_file_keys=microtask_file_keys,
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
                logger.warning(
                    "[%s] Microtask %s: REVIEW_FAILED - exhausted %d total cycles",
                    task_id,
                    mt.id,
                    total_cycles,
                )
                # Rollback: remove this microtask's files from all_files
                for fk in microtask_file_keys:
                    all_files.pop(fk, None)
                # Security failures always stop regardless of on_failure setting
                _force_stop = config.on_failure == "stop" or (
                    getattr(config, "security_failure_always_stops", True)
                    and not sec_outcome.passed
                )
                if _force_stop:
                    raise review_failed_error_cls(
                        mt,
                        review_result_cls(passed=False, issues=[], summary="Max cycles exceeded"),
                    )

        # ── Phase 5: Documentation (Self-Review, Never Fails) ─────────────────
        if not phase_failed:
            mt.status = microtask_status.IN_DOCUMENTATION
            current_phase = "documentation"
            logger.info(
                "[%s] Microtask %s: Running documentation self-review (%d-%d iterations)",
                task_id,
                mt.id,
                _GATED_DOC_SELF_REVIEW_MIN_ITERATIONS,
                _GATED_DOC_SELF_REVIEW_MAX_ITERATIONS,
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
            doc_agent = (
                deps.tool_agents.get(tool_agent_kind.DOCUMENTATION) if deps.tool_agents else None
            )
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
                min_iterations=_GATED_DOC_SELF_REVIEW_MIN_ITERATIONS,
                max_iterations=_GATED_DOC_SELF_REVIEW_MAX_ITERATIONS,
                quality_threshold=_GATED_DOC_SELF_REVIEW_QUALITY_THRESHOLD,
                detail_callback=lambda d: _detail_cb(d, current_idx, "documentation"),
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

        if progress_callback:
            progress_callback(
                current_idx, len(completed_ids), total, mt.title or mt.id, "completed", ""
            )

    completed_count = len(completed_ids)
    failed_count = len(review_failed_ids)
    summary = f"Executed {completed_count}/{total} microtasks successfully; {failed_count} review-failed; {len(all_files)} files produced."
    logger.info("[%s] Execution with batch review flow complete: %s", task_id, summary)

    return models.ExecutionResult(files=all_files, microtasks=microtasks, summary=summary)
