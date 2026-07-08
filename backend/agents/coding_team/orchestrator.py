"""
Coding team orchestrator: plan → Task Graph → assign → implement → review → merge.

Uses a swarm pattern: a Coordinator (Tech Lead) assigns tasks from the graph
to frontend_v2/backend_v2 implementation workers. Quality gate tools run after each implementation.
Exposes run_coding_team_orchestrator for in-process call from software_engineering_team.
"""

from __future__ import annotations

import logging
import os
import threading  # noqa: F401 - re-exported so coding_team.orchestrator.threading stays patchable
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from coding_team import hitl
from coding_team.activity import ActivityBridge  # noqa: F401 - re-exported, test-patched
from coding_team.agent_status import derive_stack_roster
from coding_team.engine_provider import get_engine_provider
from coding_team.job_store import (
    DEFAULT_CACHE_DIR,
    get_job,
    update_job,
)
from coding_team.models import (
    CodingTeamPlanInput,
    StackSpec,
    Task,
    TaskStatus,
)
from coding_team.pause_cycle import (  # noqa: F401 - re-exported for test monkeypatching
    MAX_TECH_LEAD_QUESTION_ROUNDS,
    PauseCycle,
    _format_decisions,  # noqa: F401 - re-exported, test-imported
    _hydrate_resolved_from_record,
    _plan_with_hitl,
    _run_pause_cycle,
)
from coding_team.reasoning_capture import (  # noqa: F401 - re-exported, test-imported
    _DEFAULT_THINKING_FLUSH_INTERVAL_S,
    _flush_thinking,
    _make_reasoning_llm_getter,
    _thinking_flush_interval_s,
    _ThinkingBuffer,
)
from coding_team.swarm_assignment import _AssignmentMixin
from coding_team.swarm_implementation import _ImplementationMixin
from coding_team.swarm_review import _ReviewMixin
from coding_team.task_graph import TaskGraphService, create_task_graph
from coding_team.team_routing import (
    _BACKEND_V2_STACK_SPEC,  # noqa: F401 - re-exported for tests
    _ensure_target_team_stack_specs,
    _quality_gate_agent_type,  # noqa: F401 - re-exported for tests
    _target_matches_agent,  # noqa: F401 - re-exported for tests
    _team_key,  # noqa: F401 - re-exported for tests
    _v2_team_kind_for_stack,  # noqa: F401 - re-exported for tests
    _worker_team_key,
)
from coding_team.tech_lead_agent import TechLeadAgent
from coding_team.worker_factory import (
    _build_implementation_worker,
    _v2_text_mode_llm,  # noqa: F401 - re-exported, test-imported
)
from coding_team.worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)

CANCEL_KEY = "cancel_requested"
MAX_TASK_REVISIONS = 20  # max times a task can be returned for revision before accepting

# Cap on CONSECUTIVE no-change revision rounds — rounds where the engineer revisited a task it
# already flagged done and produced no change to the branch diff. This is deliberately distinct from
# MAX_TASK_REVISIONS: a revision that actually changes the code resets this counter and keeps the
# full revision budget, so productive work is never throttled. Only zero-progress re-evaluation is
# bounded — on reaching the cap the task is handed to the Tech Lead for direction instead of being
# bounced again.
NO_CHANGE_REVISIT_CAP = 3


def _no_change_revisit_cap() -> int:
    """Consecutive no-change revision rounds tolerated before escalating to the Tech Lead.

    Configurable via CODING_TEAM_NO_CHANGE_REVISIT_CAP (default 3; garbage/empty → default; floored
    at 1 so the guard can never be disabled into an unbounded no-progress loop).

    Preconditions:
        - None (reads only the optional environment variable).
    Postconditions:
        - Returns an int >= 1.
    """
    # Use the canonical dependency-free shared parser directly (garbage/empty →
    # default, value clamped to the floor) rather than the SE team's thin wrapper —
    # this keeps the coding team off a cross-team import. The floor of 1 keeps the
    # guard from ever being disabled into an unbounded no-progress loop.
    from shared_env import parse_int

    return parse_int("CODING_TEAM_NO_CHANGE_REVISIT_CAP", NO_CHANGE_REVISIT_CAP, minimum=1)


# Max Tech-Lead review LLM calls dispatched concurrently in one review round. Reviews are
# independent (read-only diff + an LLM call), so a round with k tasks in review costs ~one review
# latency instead of k. The effective pool is min(this, number of tasks in review).
REVIEW_CONCURRENCY = 4


def _review_concurrency() -> int:
    """Max concurrent Tech-Lead reviews per round.

    Configurable via CODING_TEAM_REVIEW_CONCURRENCY (default 4; garbage/empty → default; floored at
    1 so review always makes progress even if the value is set to 0/negative).

    Preconditions:
        - None (reads only the optional environment variable).
    Postconditions:
        - Returns an int >= 1.
    """
    from shared_env import parse_int

    return parse_int("CODING_TEAM_REVIEW_CONCURRENCY", REVIEW_CONCURRENCY, minimum=1)


# Max implementation workers dispatched concurrently in one round. Each worker operates in its
# own git worktree (see coding_team.worktree_manager), so concurrent workers no longer share (and
# corrupt) a single working tree. The effective pool is min(this, number of workers with an
# assigned task this round) — the default 2-worker roster (frontend_v2/backend_v2) rarely reaches
# this ceiling.
IMPLEMENTATION_CONCURRENCY = 4


def _implementation_concurrency() -> int:
    """Max concurrent implementation workers per round.

    Configurable via CODING_TEAM_IMPLEMENTATION_CONCURRENCY (default 4; garbage/empty → default;
    floored at 1 so implementation always makes progress even if the value is set to 0/negative).

    Preconditions:
        - None (reads only the optional environment variable).
    Postconditions:
        - Returns an int >= 1.
    """
    from shared_env import parse_int

    return parse_int(
        "CODING_TEAM_IMPLEMENTATION_CONCURRENCY", IMPLEMENTATION_CONCURRENCY, minimum=1
    )


class _NoopBridge:
    """Stand-in progress bridge used when a real ActivityBridge can't be built.

    Progress reporting is observability only: if the bridge fails to construct,
    the code review must still run (without live progress) rather than be
    silently skipped. ``__call__`` and ``clear`` are no-ops.
    """

    def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        """Drop a progress report.

        Preconditions:
            - None.
        Postconditions:
            - No-op; no side effects.
        """
        return None

    def clear(self) -> None:
        """Clear the (absent) progress activity.

        Preconditions:
            - None.
        Postconditions:
            - No-op; no side effects.
        """
        return None


# Default job-level progress band for the coding phase. The caller owns the band
# allocation: the parent pipeline (software_engineering_team) maps its earlier phases
# onto lower sub-ranges and passes the coding team its slice via the
# ``progress_base``/``progress_span`` parameters; a standalone coding-team job uses
# the full bar. Terminal completion always writes 100.
_DEFAULT_PROGRESS_BASE = 0
_DEFAULT_PROGRESS_SPAN = 95


def _coding_progress(tasks: List[Dict[str, Any]], base: int, span: int) -> int:
    """Map the terminal-task share onto the job progress band [base, base + span].

    Preconditions:
        - ``tasks`` are snapshot dicts whose ``status`` is a TaskStatus value string.
        - ``0 <= base``, ``0 <= span``, ``base + span <= 100``.
    Postconditions:
        - Returns an int in [base, base + span]; an empty graph yields the base (no
          division by zero). Monotone in the number of terminal (merged/failed)
          tasks, so within the coding phase the bar only ever advances.
    """
    assert 0 <= base and 0 <= span and base + span <= 100, (base, span)
    total = len(tasks)
    if total == 0:
        return base
    done = sum(1 for t in tasks if t.get("status") in ("merged", "failed"))
    return base + int(span * done / total)


def _build_review_evidence(summary: str, diff: str) -> str:
    """Assemble review evidence (summary + full diff) for the Tech Lead review.

    The reviewer must see the complete change to judge it; the diff is never truncated. If the
    evidence genuinely exceeds the model context, the review call fails and the caller fails that
    single task cleanly (see ``_review_and_merge``) rather than silently reviewing partial evidence.

    Postconditions:
        - The full summary and the full diff (when present) both appear verbatim in the result.
    """
    if not diff:
        return summary
    return f"{summary}\n\n--- DIFF ---\n{diff}"


# Repo-context file selection. The shared full-stack code extensions / exclude dirs live in
# shared_repo_context.repo_utils; this summariser additionally surfaces the doc and
# config formats below (so a docs/spec task is not blind to specs, plans, and READMEs). The
# directories it skips come from repo_utils.REPO_INSPECT_EXCLUDE_DIRS (imported in
# `_context_file_filters`), shared with the active inspection tools so the two views of the repo
# cannot drift.
_CONTEXT_EXTRA_EXTENSIONS: frozenset[str] = frozenset(
    {".js", ".html", ".json", ".md", ".txt", ".rst"}
)

# Default coding-team roster when planning/snapshot provide no stacks.
_DEFAULT_STACK_SPECS: List[Dict[str, Any]] = [
    {
        "name": "frontend_v2",
        "tools_services": ["Angular", "TypeScript", "React", "CSS", "HTML"],
    },
    {
        "name": "backend_v2",
        "tools_services": ["Java", "Python", "Node.js", "Databases", "APIs", "DevOps"],
    },
]

# Full file-selection sets for repo-context scanning, built once from the shared repo_utils
# constants + the extras above and cached (the import lives below to keep the SE dependency
# function-level; the sets are static so there is no need to rebuild them on every call).
_CONTEXT_EXTENSIONS: Optional[frozenset[str]] = None
_CONTEXT_EXCLUDE_DIRS: Optional[frozenset[str]] = None


def _context_file_filters() -> tuple[frozenset[str], frozenset[str]]:
    """Return (extensions, exclude_dirs) for repo-context scanning, computed once and cached.

    Reuses the shared full-stack code extensions / exclude dirs (so adding a code file type in one
    place keeps every repo scanner consistent), unioned with this summariser's doc/config extras.
    """
    global _CONTEXT_EXTENSIONS, _CONTEXT_EXCLUDE_DIRS
    if _CONTEXT_EXTENSIONS is None or _CONTEXT_EXCLUDE_DIRS is None:
        from shared_repo_context.repo_utils import (
            FULL_STACK_EXTENSIONS,
            REPO_INSPECT_EXCLUDE_DIRS,
        )

        _CONTEXT_EXTENSIONS = frozenset(FULL_STACK_EXTENSIONS) | _CONTEXT_EXTRA_EXTENSIONS
        _CONTEXT_EXCLUDE_DIRS = REPO_INSPECT_EXCLUDE_DIRS
    return _CONTEXT_EXTENSIONS, _CONTEXT_EXCLUDE_DIRS


# Ceiling on how many eligible files the repo briefing covers (a cap on breadth,
# never a truncation of any single file's content — see ``_read_repo_context``).
_CONTEXT_FILE_CEILING = 80


def _enumerate_context_files(repo_path: Path) -> List[Path]:
    """Return the sorted, capped list of context-eligible files under ``repo_path``.

    Walks with ``os.walk`` and prunes excluded dirs in place so the traversal never
    descends into node_modules/.git/etc. The old ``sorted(repo_path.rglob("*"))``
    stat-ed the *entire* tree (tens of thousands of files for any frontend repo)
    and sorted it before slicing — and worse, those excluded entries consumed the
    file budget, starving real source files. Collecting eligible files first, then
    sorting and capping, both fixes the stat storm and guarantees the cap covers
    real files.

    Preconditions:
        - ``repo_path`` is an existing directory.
    Postconditions:
        - Returns at most ``_CONTEXT_FILE_CEILING`` files, sorted deterministically;
          every entry matches ``_context_file_filters`` and ``is_file()`` is True.
    """
    extensions, exclude_dirs = _context_file_filters()
    eligible: List[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(repo_path):
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
            for name in filenames:
                f = Path(dirpath) / name
                # is_file() (not just suffix) guards against special files: a FIFO /
                # socket / device named e.g. ``pipe.py`` would otherwise pass the
                # suffix check and block read_text() forever (a hang the try/except
                # in the renderer cannot catch). is_file() is False for those and for
                # broken symlinks, matching the previous rglob path's filter.
                if f.suffix in extensions and f.is_file():
                    eligible.append(f)
    except Exception:
        # Best-effort repo scan: a walk error (e.g. a permission-denied directory)
        # must not abort context-building, but log it at debug so it is diagnosable
        # rather than silently swallowed.
        logger.debug("os.walk failed while building repo context", exc_info=True)
    return sorted(eligible)[:_CONTEXT_FILE_CEILING]


def _render_context_file(f: Path, repo_path: Path) -> Optional[str]:
    """Render one eligible file as its full-contents briefing part, or None on read failure.

    Preconditions:
        - ``f`` is a file under ``repo_path``.
    Postconditions:
        - Returns ``"--- {rel} ---\\n{content}\\n"`` with the file's COMPLETE contents (never a
          prefix); returns None when the file cannot be read (the caller skips it), matching the
          prior behavior where an unreadable file was silently dropped.
    """
    try:
        content = f.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    rel = str(f.relative_to(repo_path))
    return f"--- {rel} ---\n{content}\n"


def _join_context_parts(parts: List[str]) -> str:
    """Join rendered briefing parts, or return the empty-repo sentinel.

    The single source of the "No files found" sentinel and the part separator, so the pure
    ``_read_repo_context`` and the incremental ``_RepoContextCache`` cannot drift apart (the cache's
    byte-identical invariant depends on them producing the same joined form).

    Postconditions:
        - Returns ``"No files found"`` for an empty list, else the parts joined by a blank line.
    """
    return "\n".join(parts) if parts else "No files found"


def _read_repo_context(repo_path: Path) -> str:
    """Read the repo structure/code briefing for implementation-worker context.

    Every file the briefing includes is rendered with its FULL contents — the
    engineer reasons over this to implement a task, and clipping a file would
    hide code from it (mirroring the team's "inputs are never truncated"
    contract for the plan text, task description, and review diff). The file
    ceiling on the eligible-file list is a deliberate cap on how many files the
    briefing covers, not truncation of any file's content.

    Preconditions:
        - ``repo_path`` is an existing directory.
    Postconditions:
        - Each context-eligible file (matching ``_context_file_filters`` and
          within the file-count ceiling) appears with its complete contents,
          never a prefix; no eligible file is dropped to fit a size budget.
        - Returns ``"No files found"`` when no eligible file is present.
    """
    parts: List[str] = []
    for f in _enumerate_context_files(repo_path):
        part = _render_context_file(f, repo_path)
        if part is not None:
            parts.append(part)
    return _join_context_parts(parts)


class _RepoContextCache:
    """Incremental cache over ``_read_repo_context`` that re-reads only changed files.

    The repo briefing is rebuilt whenever merged work lands (see ``run()``), but a merge typically
    touches only a handful of the (up to ceiling) files. Re-reading every file each time is the cost
    this cache removes. It keeps the rendered briefing part per file keyed by ``(st_mtime_ns,
    st_size)``: on ``read`` it re-enumerates eligible files (a cheap ``os.walk`` + ``stat``) and
    reuses a cached part whenever the key is unchanged, re-rendering (reading the file) only when the
    key differs or the file is new. Entries for files no longer eligible are dropped so the cache
    cannot grow without bound or resurrect stale content.

    Invariants:
        - The string returned by ``read`` is byte-identical to ``_read_repo_context(repo_path)`` for
          the same on-disk state — the cache changes *when* files are read, never *what* is rendered.
        - ``st_mtime_ns`` (nanosecond resolution) plus size is the freshness key; a content change
          that leaves both identical would not be detected, but a merge always rewrites the file
          (advancing mtime), so this cannot occur in the swarm's usage.

    Preconditions (``read``):
        - ``repo_path`` is an existing directory.
    Postconditions (``read``):
        - Returns the same value ``_read_repo_context(repo_path)`` would; the internal cache holds an
          entry for exactly the currently-eligible, successfully-rendered files.
    """

    def __init__(self) -> None:
        # path -> (mtime_ns, size, rendered_part)
        self._entries: Dict[Path, tuple[int, int, str]] = {}

    def read(self, repo_path: Path) -> str:
        files = _enumerate_context_files(repo_path)
        fresh: Dict[Path, tuple[int, int, str]] = {}
        parts: List[str] = []
        for f in files:
            try:
                st = f.stat()
                key = (st.st_mtime_ns, st.st_size)
            except Exception:
                # A file that vanished or cannot be stat-ed between walk and stat is skipped, exactly
                # as _render_context_file would drop an unreadable file; it also leaves the cache.
                continue
            cached = self._entries.get(f)
            if cached is not None and cached[:2] == key:
                part = cached[2]
            else:
                rendered = _render_context_file(f, repo_path)
                if rendered is None:
                    # Unreadable: drop from cache and skip, mirroring _read_repo_context.
                    continue
                part = rendered
            fresh[f] = (*key, part)
            parts.append(part)
        # Replace wholesale so entries for now-ineligible/removed files are evicted.
        self._entries = fresh
        return _join_context_parts(parts)


def _feature_branch_name(task: Task) -> str:
    """The task's feature branch name — its recorded branch, or the deterministic default.

    Postconditions:
        - Returns ``task.feature_branch`` when set, else ``f"feature/{task.id}"``; the single source
          of this fallback so every git/review path names the same branch for a task.
    """
    return task.feature_branch or f"feature/{task.id}"


def run_coding_team_orchestrator(
    job_id: str,
    repo_path: str | Path,
    plan_input: CodingTeamPlanInput,
    *,
    update_job_fn: Optional[Callable[..., None]] = None,
    get_job_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    get_llm: Optional[Callable[[str], Any]] = None,
    on_pause: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    progress_base: int = _DEFAULT_PROGRESS_BASE,
    progress_span: int = _DEFAULT_PROGRESS_SPAN,
    engine_provider: Optional[Any] = None,
    retry_failed: bool = False,
) -> None:
    """
    Run the coding_team pipeline: plan → Task Graph → groom/assign → implement → review → merge.
    Uses in-process job store (coding_team/job_store) for task graph persistence.
    update_job_fn / get_job_fn: if provided (e.g. from software_engineering_team), used for phase/status and cancel check.
    progress_base / progress_span: the slice of the job progress bar this run owns
    (see _coding_progress); a parent pipeline passes its coding-phase band, standalone
    jobs use the full bar.
    retry_failed: on the snapshot-resume branch, also demote terminal FAILED tasks back to TO_DO
    (via graph.reset_failed) so the swarm re-attempts them. This is the "retry the failed tasks"
    entry point; default False preserves FAILED for a plain crash-recovery resume.

    ``last_activity_at`` (read by the UI's stall warning) is stamped centrally by the
    job service on every real update — see job_service/db.py — so plain ``_update``
    writes count as activity while the 120s liveness heartbeat does not.
    """
    assert 0 <= progress_base and 0 <= progress_span and progress_base + progress_span <= 100
    # The implementation engines (v2 team leads, quality gates, code review) are injected, not
    # imported: prefer the provider passed explicitly (the software-engineering team supplies one
    # per call) and fall back to the process-wide default the standalone service installs at
    # startup. Presence check, not truthiness: an injected provider is an arbitrary object whose
    # __bool__/__len__ are not part of the contract, so a falsy-but-valid provider must not be
    # silently swapped for the ambient default.
    if engine_provider is None:
        engine_provider = get_engine_provider()
    path = Path(repo_path).resolve()
    _update = update_job_fn or (lambda **kw: update_job(job_id, cache_dir=cache_dir, **kw))
    _get_job = get_job_fn or (lambda jid: get_job(jid, cache_dir=cache_dir))

    # Capture agents' streamed reasoning ("thinking") so the UI can show what is
    # happening. Tokens land in an in-memory buffer (cheap, off the DB path); a
    # heartbeat below flushes the tail to the job record's ``thinking`` field.
    thinking = _ThinkingBuffer()
    llm_getter = get_llm or _make_reasoning_llm_getter(thinking.append)

    def _check_cancel() -> bool:
        data = _get_job(job_id)
        return bool(data and data.get(CANCEL_KEY))

    # Create Task Graph with persist
    # Tracks the last persisted (graph revision, phase, status_text) so a no-op
    # call skips the snapshot + job-service write entirely. The swarm loop persists
    # 3x per round and every graph mutation persists too, so on an idle round (or
    # back-to-back triggers for the same state) most calls are redundant; durability
    # is preserved because any real mutation bumps graph.revision and any phase /
    # status change is part of the key, so every actual state change still writes.
    _persist_state: Dict[str, Any] = {"revision": -1, "phase": None, "status_text": None}

    def _persist_graph() -> None:
        # Persist the snapshot through the SAME store used for the resume read and cancel checks
        # (the injected update_job_fn). On the software-engineering path that is the SE job record;
        # the hardcoded coding_team store targets a record that is never created on that path, so
        # the central job service's UPDATE-WHERE matches no row and the write — hence resume — is
        # silently lost. The standalone coding_team path's default callback writes the same keys to
        # the coding_team record exactly as before.
        if (
            graph.revision == _persist_state["revision"]
            and phase == _persist_state["phase"]
            and status_text == _persist_state["status_text"]
        ):
            return
        snap = graph.snapshot()
        _update(
            task_graph_snapshot=snap["tasks"],
            agent_task_map=snap["agent_task_map"],
            phase=phase,
            status_text=status_text,
            progress=_coding_progress(snap["tasks"], progress_base, progress_span),
        )
        _persist_state["revision"] = graph.revision
        _persist_state["phase"] = phase
        _persist_state["status_text"] = status_text

    graph: TaskGraphService = create_task_graph(job_id, persist_callback=_persist_graph)
    phase = "task_graph"
    status_text = "Building task graph from plan"

    # The Tech Lead object is needed for the swarm coordinator (assignments/reviews) regardless of
    # whether we plan fresh or resume, so build it unconditionally.
    llm = llm_getter("tech_lead")
    tech_lead = TechLeadAgent(llm)

    def _pause_cycle(questions: List[Any], source: str) -> "tuple[List[Dict[str, Any]], bool]":
        return _run_pause_cycle(
            job_id,
            questions,
            source,
            get_job_fn=_get_job,
            update_fn=_update,
            on_pause=on_pause,
        )

    # Resume from a persisted snapshot (e.g. a Temporal retry of the same job_id) instead of
    # re-running the planning LLM and re-doing finished work. `_persist_graph` writes the task
    # snapshot every round; the stacks are persisted alongside it on the fresh path below.
    existing = _get_job(job_id) or {}
    snapshot_tasks = existing.get("task_graph_snapshot") or []

    # Human-in-the-loop decision gate (entry). Fold any answers persisted from a prior attempt,
    # then if open questions handed in still have no answer, pause for the user before doing any
    # work. Deterministic and fail-closed — the swarm is never entered while an unanswered open
    # question exists. On a pause that ends without answers (terminal/timeout) the cycle has
    # already set the failure status, so we just stop.
    _hydrate_resolved_from_record(plan_input, existing)
    entry_unanswered = hitl.unanswered_questions(
        plan_input.open_questions, plan_input.resolved_questions
    )
    if entry_unanswered:
        resolved, ok = _pause_cycle(entry_unanswered, "plan_input")
        if not ok:
            return
        plan_input.resolved_questions = list(plan_input.resolved_questions or []) + resolved
        plan_input.open_questions = []

    if snapshot_tasks:
        logger.info("Resuming job %s from snapshot (%d tasks)", job_id, len(snapshot_tasks))
        graph.restore(
            {
                "tasks": snapshot_tasks,
                "agent_task_map": existing.get("agent_task_map") or {},
            }
        )
        # In-flight tasks from the dead attempt may be half-done and their agent mapping is stale,
        # so demote them to unassigned TO_DO; MERGED/FAILED are preserved (no re-work).
        graph.reset_in_flight()
        if retry_failed:
            # Explicit "retry the failed tasks" entry (e.g. the SE retry path): also demote terminal
            # FAILED tasks to TO_DO so the swarm re-attempts them. Default resume leaves them FAILED.
            graph.reset_failed()
        stacks_raw = existing.get("stack_specs") or _DEFAULT_STACK_SPECS
    else:
        # Plan the task graph, pausing for the user if the Tech Lead raises a decision it must not
        # make. None means either a pause ended without answers (the pause cycle already set the
        # failure status) or the Tech Lead never stopped asking — fail closed in the latter case so
        # the job does not linger in an ambiguous running state.
        out = _plan_with_hitl(tech_lead, plan_input, _pause_cycle)
        if out is None:
            # Only set 'failed' when the job is not already terminal — a pause that ended because the
            # job went terminal (failed/cancelled/completed) must keep that status, not be relabeled.
            if not hitl.is_terminal(_get_job(job_id) or {}):
                _update(
                    status="failed",
                    phase="completed",
                    status_text="Design did not converge: open questions were never resolved",
                    error="Tech Lead exceeded the open-question round cap",
                )
            return
        if out.get("already_complete"):
            # The Tech Lead, now seeing the already-completed work, judged the issue's work already
            # done and returned no tasks. Short-circuit to a clean terminal outcome instead of
            # building duplicate tasks the engineers would spin on. The GitHub publish hook turns
            # this status into a "recommend closing" comment with the evidence and creates no PR.
            evidence = str(out.get("completion_evidence") or "").strip()
            logger.info("Job %s: Tech Lead judged the work already complete: %s", job_id, evidence)
            _update(
                status="already_complete",
                phase="completed",
                status_text="Work already complete; no changes needed",
                already_complete=True,
                completion_evidence=evidence,
                progress=100,
                current_activity=None,
            )
            return
        tasks_raw = out.get("tasks") or []
        stacks_raw = out.get("stacks") or _DEFAULT_STACK_SPECS
        for idx, t in enumerate(tasks_raw, start=1):
            if not isinstance(t, dict):
                logger.warning("Skipping malformed task graph entry at index %s: %r", idx, t)
                continue
            task_id = str(t.get("id") or f"task_{idx}")
            graph.add_task(
                task_id=task_id,
                title=t.get("title") or task_id,
                description=t.get("description", ""),
                dependencies=t.get("dependencies", []),
                target_team=t.get("target_team") or None,
            )
    original_stacks_raw = stacks_raw
    stacks_raw = _ensure_target_team_stack_specs(stacks_raw, graph.get_tasks())
    if not snapshot_tasks or stacks_raw != original_stacks_raw:
        # Persist the stacks so a later retry can rebuild the workers without re-planning. On
        # resume, only write when we repaired an old/incomplete roster from target_team hints.
        _update(stack_specs=stacks_raw)
    _persist_graph()

    # Build v2 implementation workers. derive_stack_roster is the single source of
    # truth for worker-id naming, shared with the status endpoint's roster builder so the two
    # cannot drift — a mismatch would make per-agent status lookups silently miss.
    roster = derive_stack_roster(stacks_raw)
    stack_specs: List[StackSpec] = [
        StackSpec(name=name, tools_services=tools) for (_aid, name, tools) in roster
    ]
    agent_ids = [aid for (aid, _name, _tools) in roster]
    implementation_workers: List[Any] = []
    try:
        for aid, spec in zip(agent_ids, stack_specs):
            implementation_workers.append(
                _build_implementation_worker(aid, spec, llm_getter, engine_provider)
            )
    except Exception as exc:  # noqa: BLE001 - fail the job cleanly with the unsupported stack
        logger.error("Failed to build coding-team implementation workers: %s", exc)
        _update(
            status="failed",
            phase="completed",
            status_text="Could not build coding-team implementation workers",
            error=str(exc),
        )
        return

    phase = "coding"
    status_text = "Assigning and implementing tasks"
    # No progress write here: _persist_graph above already published the band value
    # derived from the graph, which on a resume reflects previously merged tasks —
    # an unconditional base write would regress the bar (e.g. 52 → 10 → 52).
    _update(phase=phase, status_text=status_text, status="running")

    # Run the swarm: coordinator (Tech Lead) + v2 implementation workers.
    swarm = CodingTeamSwarm(
        tech_lead=tech_lead,
        workers=implementation_workers,
        graph=graph,
        path=path,
        agent_ids=agent_ids,
        llm_getter=llm_getter,
        resolved_questions=plan_input.resolved_questions,
        engine_provider=engine_provider,
    )
    # Flush captured "thinking" to the job record on an interval for the UI poll.
    # beat_first surfaces any planning-phase reasoning immediately; the final flush
    # after the block captures the tail emitted since the last tick.
    from shared_concurrency import BackgroundHeartbeat  # noqa: PLC0415 - local, optional dep path

    thinking_hb = BackgroundHeartbeat(
        lambda: _flush_thinking(thinking, _update),
        _thinking_flush_interval_s(),
        name=f"coding-thinking-{job_id}",
        beat_first=True,
    )
    try:
        with thinking_hb:
            swarm.run(
                check_cancel=_check_cancel,
                persist_fn=_persist_graph,
                update_fn=_update,
                pause_for_questions=_pause_cycle,
            )
    finally:
        _flush_thinking(thinking, _update)

    # A worker raising a decision that ended without answers (terminal/timeout) aborts the swarm;
    # the pause cycle has already set the failure status, so do not overwrite it with "completed".
    if getattr(swarm, "aborted", False):
        return

    all_tasks = graph.get_tasks()
    merged_tasks = [t for t in all_tasks if t.status == TaskStatus.MERGED]
    merged_count = len(merged_tasks)
    failed_count = graph.count_with_status(TaskStatus.FAILED)
    # Tasks the Tech Lead adjudicated as already-done (terminal MERGED but no real diff landed).
    resolved_count = sum(1 for t in merged_tasks if t.resolved_without_changes)
    # When nothing failed and every "merged" task was actually already-done (no real changes
    # landed), the issue's work was already complete — report that distinct terminal status so the
    # publish flow recommends closure instead of opening a no-op PR. A mixed result (some real
    # merges) stays a normal completion and publishes the real work.
    #
    # Require EVERY task to be terminal (MERGED or FAILED) before claiming already-complete: the
    # swarm loop can exit at max_rounds with a task still TO_DO/IN_PROGRESS/IN_REVIEW, and reporting
    # already_complete there (recommend-closing, no PR) would abandon genuinely unfinished work.
    # Since this branch also requires failed_count == 0, "all terminal" means all MERGED.
    all_terminal = (merged_count + failed_count) == len(all_tasks)
    already_complete = (
        all_terminal and failed_count == 0 and merged_count > 0 and resolved_count == merged_count
    )
    # A job with failed tasks must not be presented as a clean success — surface a distinct
    # terminal status so downstream consumers (and the GitHub publish flow) can flag the gap.
    # current_activity=None travels in the terminal write itself so a transient
    # failure of an earlier best-effort clear cannot leave a terminal job serving
    # a stale mid-review activity entry.
    if already_complete:
        _update(
            status="already_complete",
            phase="completed",
            status_text="Work already complete; no changes needed",
            already_complete=True,
            completion_evidence="The requested work was already present; no changes were needed.",
            progress=100,
            current_activity=None,
        )
        return
    _update(
        status="completed_with_failures" if failed_count else "completed",
        phase="completed",
        status_text=f"Completed: {merged_count} merged, {failed_count} failed",
        progress=100,
        current_activity=None,
    )


class CodingTeamSwarm(_AssignmentMixin, _ImplementationMixin, _ReviewMixin):
    """Coordinator (Tech Lead) + frontend/backend v2 implementation-worker swarm pattern.

    The coordinator assigns ready tasks to free workers. Each worker implements
    the task and runs quality gates (build, lint), and signals completion. The
    coordinator reviews (the swarm's sole code-review pass) and merges approved
    tasks.

    Behavior is spread across three mixins by responsibility (assignment,
    implementation, review) — see coding_team/swarm_assignment.py,
    swarm_implementation.py, swarm_review.py.
    """

    def __init__(
        self,
        tech_lead: TechLeadAgent,
        workers: List[Any],
        graph: TaskGraphService,
        path: Path,
        agent_ids: List[str],
        llm_getter: Callable[[str], Any],
        resolved_questions: Optional[List[Dict[str, Any]]] = None,
        engine_provider: Any = None,
    ) -> None:
        self.tech_lead = tech_lead
        self.workers = workers
        self.graph = graph
        self.path = path
        self.agent_ids = agent_ids
        self.agent_team_keys = {w.agent_id: _worker_team_key(w) for w in workers}
        self.llm_getter = llm_getter
        # Injected implementation engines (build/lint); None → quality gates are skipped.
        self.engine_provider = engine_provider
        # Plan-level decisions the user already answered (entry gate + Tech Lead planning), folded
        # into plan_input.resolved_questions before the swarm is built. Surfaced to both review
        # gates so a reviewer never re-raises a question the user has settled.
        self.resolved_questions: List[Dict[str, Any]] = list(resolved_questions or [])
        # Bound pause cycle (set in run()) used to escalate a worker-raised decision to the user.
        self.pause_for_questions: Optional[PauseCycle] = None
        # Serializes the pause_for_questions round-trip across concurrently-running workers: the
        # pause cycle stores exactly one outstanding question batch in job-level state (see
        # swarm_implementation._escalate_decision's Concurrency note), so two workers escalating a
        # decision at once must not race it.
        self._pause_lock = threading.Lock()
        # Serializes merge_branch/abort_merge calls against the shared checkout (self.path) made
        # from within a worker's own no-change escalation (see
        # swarm_implementation._escalate_to_tech_lead's Concurrency note) — two workers in the same
        # round's fan-out can each independently hit their no-change cap and get a "done" verdict,
        # and without this lock their merges would race the same working directory/index.
        self._merge_lock = threading.Lock()
        # Set True when a pause ended without answers (terminal/timeout); aborts the loop and tells
        # the orchestrator not to overwrite the failure status with "completed".
        self.aborted = False
        # Incremental repo-context cache: re-reads only files whose (mtime, size) changed instead of
        # re-reading every eligible file on each refresh (see _RepoContextCache and run()).
        self._repo_context_cache = _RepoContextCache()
        self.repo_context = self._repo_context_cache.read(path)
        # Repo context only changes when merged work lands new files on the working tree, so cache
        # the merged-task count the context reflects and re-read only when it advances (see run()).
        self._context_merged_count = self._merged_count()
        # One isolated git worktree per worker (see coding_team.worktree_manager) — created up
        # front in run(), never lazily from inside a worker thread. Construction itself does no
        # filesystem/git I/O.
        self._worktrees = WorktreeManager(path, agent_ids)

    def _is_complete(self) -> bool:
        tasks = self.graph.get_tasks()
        remaining = [t for t in tasks if t.status == TaskStatus.TO_DO]
        active = sum(1 for aid in self.agent_ids if self.graph.get_task_for_agent(aid) is not None)
        in_review = [t for t in tasks if t.status == TaskStatus.IN_REVIEW]
        return not remaining and active == 0 and not in_review

    def run(
        self,
        max_rounds: int = 50,
        check_cancel: Optional[Callable[[], bool]] = None,
        persist_fn: Optional[Callable] = None,
        update_fn: Optional[Callable] = None,
        pause_for_questions: Optional[PauseCycle] = None,
    ) -> None:
        """Main swarm loop: assign → implement + quality gates → review → merge.

        ``pause_for_questions`` is the bound HITL gate used to escalate a worker-raised decision to
        the user; when omitted, a worker that raises a decision fails its task closed (no silent
        decide). The loop stops early if a pause ends without answers (``self.aborted``).

        Postconditions:
            - Every worker's git worktree (see WorktreeManager) is removed before this method
              returns, on every exit path (normal completion, cancellation, abort, a worktree-setup
              failure, or an unexpected exception) — the worktree lifecycle is scoped exactly to
              one run() call.
        """
        _update = update_fn or (lambda **kw: None)
        _persist = persist_fn or (lambda: None)
        self.pause_for_questions = pause_for_questions

        try:
            # Check before doing any work — including worktree setup, which is neither free
            # nor guaranteed to succeed — so a job cancelled before run() was even entered
            # (or between phases) is honored immediately rather than reported "failed" if
            # prepare() happens to error, or made to wait out a setup it will just discard.
            if check_cancel and check_cancel():
                _update(status="cancelled", status_text="Cancelled by user")
                return

            try:
                self._worktrees.prepare()
            except Exception as exc:  # noqa: BLE001 - a broken worktree setup fails the job, not the process
                logger.exception("Failed to prepare implementation-worker git worktrees")
                _update(
                    status="failed",
                    phase="completed",
                    status_text="Could not prepare implementation-worker git worktrees",
                    error=str(exc),
                )
                self.aborted = True
                return

            for round_num in range(max_rounds):
                if check_cancel and check_cancel():
                    _update(status="cancelled", status_text="Cancelled by user")
                    return

                # Refresh the repo context when merged work has landed since the last read. The
                # merged count is the right signal here: a task's files become part of the
                # shared/integrated tree only once it merges (work in progress lives on per-worker
                # feature branches), and a dependent task is not assignable until its dependencies
                # are MERGED — so it always sees its prerequisites' code. This avoids a full repo
                # walk on every idle round (e.g. while tasks sit in review or blocked); a one-time
                # snapshot at construction would make a worker blind to earlier merged work.
                merged_now = self._merged_count()
                if merged_now != self._context_merged_count:
                    self.repo_context = self._repo_context_cache.read(self.path)
                    self._context_merged_count = merged_now

                # Coordinator: assign ready tasks to free workers
                ready = self._find_ready_tasks()
                free = self._find_free_agents()
                self._assign_tasks(ready, free)
                _persist()

                # Workers: implement + quality gates, each isolated to its own git worktree.
                # Reviews already fan out this way (see _review_and_merge) — compute concurrently
                # when there is more than one active worker this round, run inline with live
                # progress when there is at most one (the common case: the roster is usually
                # 2 workers with disjoint stacks, and a round rarely has both active at once).
                active = [
                    swe
                    for swe in self.workers
                    if (task := self.graph.get_task_for_agent(swe.agent_id)) is not None
                    and task.status == TaskStatus.IN_PROGRESS
                ]
                if len(active) <= 1:
                    for swe in active:
                        self._implement_and_verify(swe, _update)
                        if self.aborted:
                            break
                else:
                    from shared_concurrency import parallel_map

                    _update(status_text=f"Implementing {len(active)} task(s)")
                    parallel_map(
                        active,
                        lambda swe: self._implement_and_verify(swe, _update, live_progress=False),
                        max_workers=_implementation_concurrency(),
                        skip_none=False,
                    )
                _persist()
                # A worker escalation that ended without answers aborts the loop; the orchestrator
                # sees self.aborted and does not report the job as completed.
                if self.aborted:
                    return

                # Coordinator: review and merge
                self._review_and_merge(_update)
                _persist()

                if self._is_complete():
                    break
        finally:
            self._worktrees.cleanup()
