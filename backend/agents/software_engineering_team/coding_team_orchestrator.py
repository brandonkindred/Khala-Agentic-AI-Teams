"""
Coding team orchestrator: plan → groom → Task Graph → assign → implement → review → merge.

Owns the swarm entrypoint (``run_coding_team_orchestrator``) and ``CodingTeamSwarm``
composition: Tech Lead coordination plus assignment / implementation / review mixins
that drive frontend_v2/backend_v2 workers through quality gates. Also keeps branch-
naming and review-evidence helpers that the mixins late-bind.

Extracted collaborators (import those modules for the concerns they own):
- ``progress_config`` — concurrency/cap env parsers, progress-band math
- ``graph_persist`` — ``GraphPersistCoordinator``: the task graph plus its single-writer
  persist/flush state machine (background flusher, snapshot diffing, live phase/status_text)
- ``pause_cycle``, ``reasoning_capture``, ``team_routing``, ``worker_factory``,
  ``swarm_*`` — HITL pauses, thinking flush, stack routing, worker construction,
  and the assignment / implementation / review mixin bodies

``ActivityBridge`` and ``MAX_TECH_LEAD_QUESTION_ROUNDS`` stay imported here (rather
than only in the modules that define them) because ``swarm_review.py`` and
``pause_cycle.py`` late-bind them via a module reference (``_orch.NAME``, resolved
at call time) instead of a name import, to dodge a circular import and so a
monkeypatch of this module's attribute is observed. Everything else these
collaborators export is imported directly from its definition site by whichever
module actually uses it — this module does not re-export helpers purely for test
convenience.
"""

from __future__ import annotations

import copy
import logging
import threading  # noqa: F401 - re-exported so coding_team.orchestrator.threading stays patchable
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from shared.dev_models import ReviewContext, SystemArchitecture
from shared.observability import current_trace_id
from software_engineering_team import hitl
from software_engineering_team.activity import (
    ActivityBridge,  # noqa: F401 - late-bound via `_orch.ActivityBridge` in swarm_review.py
)
from software_engineering_team.agent_status import derive_stack_roster
from software_engineering_team.engine_provider import get_engine_provider
from software_engineering_team.graph_persist import GraphPersistCoordinator
from software_engineering_team.job_store import (
    DEFAULT_CACHE_DIR,
    get_job,
    update_job,
)
from software_engineering_team.models import (
    CodingTeamPlanInput,
    JobStatus,
    StackSpec,
    Task,
    TaskStatus,
)
from software_engineering_team.pause_cycle import (
    MAX_TECH_LEAD_QUESTION_ROUNDS,  # noqa: F401 - late-bound via `_orch.MAX_TECH_LEAD_QUESTION_ROUNDS` in pause_cycle._plan_with_hitl
    PauseCycle,
    _ActivityPauseSignal,
    _check_pending_pause_reentry,
    _hydrate_resolved_from_record,
    _plan_with_hitl,
    _run_pause_cycle,
)
from software_engineering_team.progress_config import (
    _DEFAULT_PROGRESS_BASE,
    _DEFAULT_PROGRESS_SPAN,
    _groom_concurrency,
    _implementation_concurrency,
)
from software_engineering_team.reasoning_capture import (
    _flush_thinking,
    _make_reasoning_llm_getter,
    _thinking_flush_interval_s,
    _ThinkingBuffer,
)
from software_engineering_team.shared.team_lead_base import TeamLeadSharedState
from software_engineering_team.swarm_assignment import _AssignmentMixin
from software_engineering_team.swarm_implementation import _ImplementationMixin
from software_engineering_team.swarm_review import (
    _ReviewMixin,
    deserialize_review_cache,
    serialize_review_cache,
)
from software_engineering_team.swarm_revision_cap import _RevisionCapMixin
from software_engineering_team.task_graph import TaskGraphService
from software_engineering_team.team_routing import (
    _ensure_target_team_stack_specs,
    _worker_team_key,
)
from software_engineering_team.tech_lead_agent import TechLeadAgent
from software_engineering_team.tech_lead_agent.agent import _plan_text
from software_engineering_team.worker_factory import _build_implementation_worker
from software_engineering_team.worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)

CANCEL_KEY = "cancel_requested"
MAX_TASK_REVISIONS = 20  # max times a task can be returned for revision before accepting


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


# Default coding-team roster when planning/snapshot provide no stacks. Internal template only --
# never hand this list (or its dict entries) to a caller directly, since an in-place mutation
# would corrupt the default for every subsequent job that falls back to it. Use
# _default_stack_specs() below, which returns an independent deep copy.
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


def _default_stack_specs() -> List[Dict[str, Any]]:
    """A fresh, independent copy of the default coding-team roster.

    Postconditions:
        - Returns a deep copy of ``_DEFAULT_STACK_SPECS``; mutating the result (or any nested
          list/dict within it) never affects the module-level template or any other caller's copy.
    """
    return copy.deepcopy(_DEFAULT_STACK_SPECS)


def _groom_one_task(
    tech_lead: TechLeadAgent, task: Dict[str, Any], plan_context: str
) -> Dict[str, Any]:
    """Groom one already-normalized planned task; never raises.

    Contained per-item wrapper for the ``parallel_map`` fan-out in ``_groom_tasks`` —
    ``run_groom_task`` itself already falls back to safe defaults on an LLM/parse failure, but
    ``parallel_map`` is fast-fail by default (an uncaught worker exception cancels every other
    pending task), so this is the outer safety net: one task's grooming failure must never abort
    the whole round.

    Preconditions:
        - ``task`` is an already-normalized planned task: a dict with "id" (non-empty str),
          "title", "description", "dependencies".
    Postconditions:
        - Always returns a dict with the same 6 keys ``run_groom_task`` returns
          (acceptance_criteria/out_of_scope/description_enriched/priority/subtasks/
          task_dependencies) — ungroomed defaults on any failure, so the caller can zip 1:1
          against its task list unconditionally.
    """
    task_id = task["id"]
    try:
        return tech_lead.run_groom_task(
            task_id=task_id,
            task_title=task.get("title") or task_id,
            task_description=task.get("description") or "",
            task_dependencies=list(task.get("dependencies") or []),
            plan_context=plan_context,
        )
    except Exception as e:  # noqa: BLE001 - one task's grooming failure must not abort the round
        logger.warning(
            "Tech Lead grooming failed for task %s: %s",
            task_id,
            e,
            extra={"trace_id": current_trace_id()},
        )
        return {
            "acceptance_criteria": [],
            "out_of_scope": "",
            "description_enriched": task.get("description") or "",
            "priority": "medium",
            "subtasks": [],
            "task_dependencies": list(task.get("dependencies") or []),
        }


def _groom_tasks(
    tech_lead: TechLeadAgent, tasks: List[Dict[str, Any]], plan_context: str
) -> List[Dict[str, Any]]:
    """Groom every planned task concurrently, right after planning and before any task is added
    to the graph — so every task carries real acceptance criteria / scope / subtasks before it can
    reach assignment, review, or revision adjudication.

    Fans the round out via ``shared.concurrency.parallel_map`` (see
    ``progress_config._groom_concurrency``), mirroring ``swarm_review._review_and_merge``'s review
    fan-out — grooming is independent per task (each LLM call only needs that task's own
    id/title/description/dependencies plus the shared ``plan_context``), so k tasks cost ~one
    grooming latency instead of k serial calls.

    Preconditions:
        - ``tasks`` are already-normalized planned tasks (see ``_groom_one_task``); the caller
          zips the result back against this same list positionally, so order matters.
    Postconditions:
        - Returns a list the same length and order as ``tasks``; one grooming result per task (see
          ``_groom_one_task``'s postconditions) — never a missing/None slot, even when a task's own
          grooming call failed. Empty ``tasks`` returns ``[]`` without any LLM call.
    """
    from shared.concurrency import parallel_map

    return parallel_map(
        tasks,
        lambda t: _groom_one_task(tech_lead, t, plan_context),
        max_workers=_groom_concurrency(),
        skip_none=False,
    )


def _sanitized_subtasks(raw: Any) -> List[Dict[str, Any]]:
    """Filter a grooming call's raw ``subtasks`` output to entries ``Task``/``Subtask`` can accept.

    ``run_groom_task``'s own default/fallback never fabricates a subtask, but a real (non-default)
    LLM response is free-form JSON: an entry missing "id" would raise a pydantic ValidationError
    inside ``Task(...)`` construction (``Subtask.id`` is required) with nothing above it in the
    task-creation loop to contain it — crashing the whole job over one malformed subtask.

    Postconditions:
        - Returns only dict entries carrying a non-empty "id"; everything else (non-dicts, id-less
          dicts) is dropped. Never raises.
    """
    return [s for s in (raw or []) if isinstance(s, dict) and s.get("id")]


def _feature_branch_name(task: Task) -> str:
    """The task's feature branch name — its recorded branch, or the deterministic default.

    Postconditions:
        - Returns ``task.feature_branch`` when set, else ``f"feature/{task.id}"``; the single source
          of this fallback so every git/review path names the same branch for a task.
    """
    return task.feature_branch or f"feature/{task.id}"


class _PlanAndGroomResult(NamedTuple):
    """Bundle `_plan_and_groom` hands to `_assign_and_implement` on success.

    Postconditions:
        - ``tech_lead``: the ``TechLeadAgent`` built for this job (planning already ran, or was
          skipped for a snapshot resume); ready for the swarm's assignment/review calls.
        - ``agent_ids``: worker ids in the same order as ``implementation_workers`` (derived from
          ``derive_stack_roster``'s ordering), for ``CodingTeamSwarm(agent_ids=...)``.
        - ``implementation_workers``: constructed v2 implementation workers, one per
          ``agent_ids`` entry, in the same order.
        - ``existing``: the job record ``_plan_and_groom`` was called with, forwarded unchanged
          for ``CodingTeamSwarm(restored_review_cache=...)``.
    """

    tech_lead: TechLeadAgent
    agent_ids: List[str]
    implementation_workers: List[Any]
    existing: Dict[str, Any]


def _plan_and_groom(
    *,
    job_id: str,
    plan_input: CodingTeamPlanInput,
    existing: Dict[str, Any],
    retry_failed: bool,
    coord: GraphPersistCoordinator,
    llm_getter: Callable[[str], Any],
    engine_provider: Any,
    get_job_fn: Callable[[str], Optional[Dict[str, Any]]],
    pause_cycle: Callable[[List[Any], str], "tuple[List[Dict[str, Any]], bool]"],
) -> Optional[_PlanAndGroomResult]:
    """Ingest the plan (or resume a snapshot), groom tasks, and build the Task Graph plus
    implementation workers.

    Extracted from ``run_coding_team_orchestrator`` so the resume-vs-fresh-plan branch, task
    grooming, the ``target_team`` stack-spec repair (``_ensure_target_team_stack_specs``), and
    implementation-worker construction are defined once, isolated from the per-round
    assign/implement loop (``_assign_and_implement``) and from the caller's pause/reentry/
    terminal-status plumbing, which stays in ``run_coding_team_orchestrator`` itself.

    Preconditions:
        - ``existing`` is the job record as of just before this call (already reentry-consumed
          and hydrated by the caller's entry-gate HITL check); ``plan_input`` carries no
          unanswered open questions at this point.
        - ``pause_cycle`` follows ``_run_pause_cycle``'s contract: under ``pause_strategy="block"``
          it returns ``(resolved, ok)``; under ``pause_strategy="return"`` it raises
          ``pause_cycle._ActivityPauseSignal`` instead of returning. This function does not catch
          that signal — it propagates uncaught to ``run_coding_team_orchestrator``'s own
          ``except _ActivityPauseSignal`` handler, exactly as if this code were still inline.
    Postconditions:
        - Returns ``None`` when an early terminal outcome was reached and already recorded: a HITL
          pause cycle that ended without answers (mid-planning, via ``_plan_with_hitl``), planning
          never converging (Tech Lead exceeded the open-question round cap — status set to
          ``FAILED`` here unless the job is already terminal), the Tech Lead judging the work
          already complete (status set to ``ALREADY_COMPLETE`` here), or implementation-worker
          construction raising (status set to ``FAILED`` here). In every ``None`` case the caller's
          contract is to return immediately, mirroring the inline code's bare ``return``.
        - Otherwise returns a ``_PlanAndGroomResult`` with the built ``tech_lead``, the graph
          (``coord.graph``, mutated in place) fully populated, ``agent_ids``/
          ``implementation_workers`` ready for ``CodingTeamSwarm``, and ``existing`` forwarded
          unchanged for the caller's next phase.
        - On a snapshot resume (``existing["task_graph_snapshot"]`` non-empty), the Tech Lead
          planning path is never invoked — only ``TechLeadAgent(llm)`` construction runs
          unconditionally, matching the entry point's need for the same Tech Lead object for the
          swarm coordinator regardless of whether we plan fresh or resume.
        - Before returning successfully, ``coord.persist_sync()`` has been called at least once
          (after any stack-spec repair), so the just-built graph and (when repaired) stack specs
          are durably persisted before workers are built.
    """
    graph = coord.graph

    # The Tech Lead object is needed for the swarm coordinator (assignments/reviews) regardless of
    # whether we plan fresh or resume, so build it unconditionally.
    llm = llm_getter("tech_lead")
    tech_lead = TechLeadAgent(llm)

    # Resume from a persisted snapshot (e.g. a Temporal retry of the same job_id) instead of
    # re-running the planning LLM and re-doing finished work. `coord.persist_async` writes the
    # task snapshot every round; the stacks are persisted alongside it on the fresh path below.
    snapshot_tasks = existing.get("task_graph_snapshot") or []

    if snapshot_tasks:
        logger.info(
            "Resuming job %s from snapshot (%d tasks)",
            job_id,
            len(snapshot_tasks),
            extra={"trace_id": current_trace_id()},
        )
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
        stacks_raw = existing.get("stack_specs") or _default_stack_specs()
    else:
        # Plan the task graph, pausing for the user if the Tech Lead raises a decision it must not
        # make. None means either a pause ended without answers (the pause cycle already set the
        # failure status) or the Tech Lead never stopped asking — fail closed in the latter case so
        # the job does not linger in an ambiguous running state.
        out = _plan_with_hitl(tech_lead, plan_input, pause_cycle)
        if out is None:
            # Only set 'failed' when the job is not already terminal — a pause that ended because the
            # job went terminal (failed/cancelled/completed) must keep that status, not be relabeled.
            if not hitl.is_terminal(get_job_fn(job_id) or {}):
                coord.update(
                    status=JobStatus.FAILED.value,
                    phase="completed",
                    status_text="Design did not converge: open questions were never resolved",
                    error="Tech Lead exceeded the open-question round cap",
                )
            return None
        if out.get("already_complete"):
            # The Tech Lead, now seeing the already-completed work, judged the issue's work already
            # done and returned no tasks. Short-circuit to a clean terminal outcome instead of
            # building duplicate tasks the engineers would spin on. The GitHub publish hook turns
            # this status into a "recommend closing" comment with the evidence and creates no PR.
            evidence = str(out.get("completion_evidence") or "").strip()
            logger.info(
                "Job %s: Tech Lead judged the work already complete: %s",
                job_id,
                evidence,
                extra={"trace_id": current_trace_id()},
            )
            coord.update(
                status=JobStatus.ALREADY_COMPLETE.value,
                phase="completed",
                status_text="Work already complete; no changes needed",
                already_complete=True,
                completion_evidence=evidence,
                progress=100,
                current_activity=None,
            )
            return None
        tasks_raw = out.get("tasks") or []
        stacks_raw = out.get("stacks") or _default_stack_specs()
        normalized_tasks: List[Dict[str, Any]] = []
        for idx, t in enumerate(tasks_raw, start=1):
            if not isinstance(t, dict):
                logger.warning(
                    "Skipping malformed task graph entry at index %s: %r",
                    idx,
                    t,
                    extra={"trace_id": current_trace_id()},
                )
                continue
            raw_id = t.get("id")
            task_id = str(raw_id) if raw_id is not None else f"task_{idx}"
            normalized_tasks.append(
                {
                    "id": task_id,
                    "title": t.get("title") or task_id,
                    "description": t.get("description", ""),
                    "dependencies": t.get("dependencies", []),
                    "target_team": t.get("target_team") or None,
                }
            )
        # Groom every planned task (acceptance criteria, out-of-scope, enriched description,
        # priority, subtasks) before it is added to the graph, so it reaches assignment/review/
        # revision-adjudication with real criteria instead of the empty defaults those prompts
        # already reference.
        groomed = _groom_tasks(tech_lead, normalized_tasks, _plan_text(plan_input))
        for t, groom in zip(normalized_tasks, groomed):
            graph.add_task(
                task_id=t["id"],
                title=t["title"],
                description=groom.get("description_enriched") or t["description"],
                dependencies=t["dependencies"],
                acceptance_criteria=groom.get("acceptance_criteria") or [],
                out_of_scope=groom.get("out_of_scope") or "",
                priority=groom.get("priority") or "medium",
                subtasks=_sanitized_subtasks(groom.get("subtasks")),
                target_team=t["target_team"],
            )
    original_stacks_raw = stacks_raw
    stacks_raw = _ensure_target_team_stack_specs(stacks_raw, graph.get_tasks())
    if not snapshot_tasks or stacks_raw != original_stacks_raw:
        # Persist the stacks so a later retry can rebuild the workers without re-planning. On
        # resume, only write when we repaired an old/incomplete roster from target_team hints.
        coord.update(stack_specs=stacks_raw)
    coord.persist_sync()

    # Build v2 implementation workers. derive_stack_roster is the single source of
    # truth for worker-id naming, shared with the status endpoint's roster builder so the two
    # cannot drift — a mismatch would make per-agent status lookups silently miss.
    roster = derive_stack_roster(stacks_raw)
    stack_specs: List[StackSpec] = [
        StackSpec(name=name, tools_services=tools) for (_aid, name, tools) in roster
    ]
    agent_ids = [aid for (aid, _name, _tools) in roster]
    # The plan's architecture overview and final spec content, when available, are forwarded
    # into each implementation worker so its code-review gate can check the change against
    # the established architecture and the real project spec (not just the microtask
    # description) -- see software_engineering_team's code_review_agent for how these reach
    # the review prompt.
    plan_architecture = (
        SystemArchitecture(overview=plan_input.architecture_overview)
        if plan_input.architecture_overview
        else None
    )
    plan_review_context = ReviewContext(
        architecture=plan_architecture, spec_content=plan_input.final_spec_content or ""
    )
    implementation_workers: List[Any] = []
    try:
        for aid, spec in zip(agent_ids, stack_specs):
            implementation_workers.append(
                _build_implementation_worker(
                    aid,
                    spec,
                    llm_getter,
                    engine_provider,
                    review_context=plan_review_context,
                )
            )
    except Exception as exc:  # noqa: BLE001 - fail the job cleanly with the unsupported stack
        logger.error(
            "Job %s: failed to build coding-team implementation workers: %s",
            job_id,
            exc,
            extra={"trace_id": current_trace_id()},
        )
        coord.update(
            status=JobStatus.FAILED.value,
            phase="completed",
            status_text="Could not build coding-team implementation workers",
            error=str(exc),
        )
        return None

    return _PlanAndGroomResult(
        tech_lead=tech_lead,
        agent_ids=agent_ids,
        implementation_workers=implementation_workers,
        existing=existing,
    )


def _assign_and_implement(
    *,
    job_id: str,
    path: Path,
    coord: GraphPersistCoordinator,
    tech_lead: TechLeadAgent,
    implementation_workers: List[Any],
    agent_ids: List[str],
    llm_getter: Callable[[str], Any],
    plan_input: CodingTeamPlanInput,
    engine_provider: Any,
    existing: Dict[str, Any],
    thinking: "_ThinkingBuffer",
    check_cancel: Callable[[], bool],
    pause_cycle: Callable[[List[Any], str], "tuple[List[Dict[str, Any]], bool]"],
    pause_strategy: str,
) -> "CodingTeamSwarm":
    """Build the coding-team swarm and run the per-round assign/implement/review loop.

    Extracted from ``run_coding_team_orchestrator`` so ``CodingTeamSwarm`` construction, the
    review-cache export wiring, the thinking-flush heartbeat, and the ``swarm.run(...)`` call are
    defined once, isolated from plan/grooming/Task Graph setup (``_plan_and_groom``). The post-run
    ``swarm.aborted`` check and all terminal job-status computation stay in
    ``run_coding_team_orchestrator`` itself (out of scope for this split) since they need the
    caller's own control flow (``except``/``finally`` handling) to run correctly on every exit path.

    Preconditions:
        - ``coord.graph`` already holds every task this round will assign (``_plan_and_groom`` has
          already run); ``tech_lead`` and ``implementation_workers``/``agent_ids`` are
          ``_plan_and_groom``'s output, in matching order. ``pause_cycle``/``check_cancel`` are the
          caller's own closures (unchanged contracts — see ``_plan_and_groom``'s Preconditions for
          ``pause_cycle``).
    Postconditions:
        - ``coord``'s phase/status_text/status are updated to reflect the coding round has started
          before the swarm runs.
        - Returns the constructed, already-run ``CodingTeamSwarm``; by the time this returns,
          ``swarm.run(...)`` has completed (normally, via cancellation, or by raising) and any
          captured "thinking" has been flushed to the job record at least once more (in a
          ``finally``), regardless of how ``swarm.run`` exited.
        - ``coord.review_cache_export`` is set to ``swarm.export_review_cache`` before
          ``swarm.run`` is called, so every persist from this point on (including ones triggered
          from inside ``swarm.run``) can include ``review_verdict_cache``.
        - Does not itself catch ``pause_cycle._ActivityPauseSignal`` or any other exception raised
          from ``swarm.run`` (via a worker's HITL escalation or otherwise); such an exception
          propagates uncaught to the caller.
    """
    graph = coord.graph

    # No progress write here: coord.persist_sync above already published the band value
    # derived from the graph, which on a resume reflects previously merged tasks —
    # an unconditional base write would regress the bar (e.g. 52 → 10 → 52). update() commits
    # the new phase/status_text into the coordinator so later persists carry them.
    coord.update(
        phase="coding",
        status_text="Assigning and implementing tasks",
        status=JobStatus.RUNNING.value,
    )

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
        spec_content=plan_input.final_spec_content or "",
        restored_review_cache=existing.get("review_verdict_cache"),
    )
    # Attach the swarm's cache export so persist_sync can include review_verdict_cache in the
    # job record from here on (pre-swarm persist_sync calls above leave this unset, so that
    # field is simply omitted — see GraphPersistCoordinator.review_cache_export).
    coord.review_cache_export = swarm.export_review_cache
    # Flush captured "thinking" to the job record on an interval for the UI poll.
    # beat_first surfaces any planning-phase reasoning immediately; the final flush
    # after the block captures the tail emitted since the last tick.
    from shared.concurrency import (
        BackgroundHeartbeat,  # noqa: PLC0415 - local, optional dep path
    )

    thinking_hb = BackgroundHeartbeat(
        lambda: _flush_thinking(thinking, coord.update),
        _thinking_flush_interval_s(),
        name=f"coding-thinking-{job_id}",
        beat_first=True,
    )
    try:
        with thinking_hb:
            swarm.run(
                check_cancel=check_cancel,
                persist_fn=coord.persist_sync,
                update_fn=coord.update,
                pause_for_questions=pause_cycle,
                pause_strategy=pause_strategy,
            )
    finally:
        _flush_thinking(thinking, coord.update)

    return swarm


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
    pause_strategy: str = "block",
    acknowledged_resume_token: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Run the coding_team pipeline: plan → groom → Task Graph → assign → implement → review → merge.
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

    pause_strategy / acknowledged_resume_token: per-caller HITL pause behavior, per
    ``system_design/hitl_pause_resume_contract.md`` §1. ``"block"`` (the default) preserves
    every prior caller's behavior byte-for-byte — a HITL gate blocks until answered, exactly
    as before this parameter existed; ``run_orchestrator_wired``'s thread-mode uses and
    ``_run_with_github_hooks`` both rely on this default and pass neither parameter.
    ``"return"`` (used only by the Temporal activity path) makes a HITL gate return this
    function immediately instead of blocking; see the Postconditions below.

    Preconditions:
        - ``progress_base``/``progress_span`` are non-negative and sum to <= 100 (the band this run
          owns on the job's overall progress bar); violated by raising ``ValueError``.
        - ``repo_path`` is a git checkout the pipeline can branch/merge in; ``plan_input`` carries
          the plan text (and any already-resolved HITL decisions) the Tech Lead plans from.
        - ``pause_strategy`` is ``"block"`` or ``"return"``; violated by raising ``ValueError``.
          ``acknowledged_resume_token`` is only meaningful when ``pause_strategy == "return"``.
    Postconditions:
        - ``pause_strategy == "block"``: return value is always ``None`` — unchanged from every
          caller's behavior before this parameter existed. On a normal (non-raising) return, the
          pipeline has reached a terminal job status (completed, completed-with-failures,
          already-complete, failed, or cancelled) via ``update_job_fn``/the default job store, or
          ended early via a HITL pause whose own cycle already recorded the terminal status. Only
          specific, individually-handled failures (the progress-band precondition, worker
          construction) are guaranteed to end this way; an exception from planning, job-store I/O,
          persistence, or ``swarm.run()`` itself is not caught here and propagates to the caller,
          which is then responsible for recording/handling it — the job may be left without a
          terminal status in that case.
        - ``pause_strategy == "return"``: first checks the job record for an already-persisted,
          unresolved pause (``_check_pending_pause_reentry``) before doing any other work — a
          match against ``acknowledged_resume_token`` atomically consumes it and proceeds
          normally; a stale/missing token re-emits that exact pause immediately without
          re-running any work (a pre-work Temporal activity retry). Otherwise, if a HITL gate
          pauses during this invocation, returns a dict with keys ``outcome`` (``"paused"``),
          ``job_id``, ``resume_token``, ``pause_kind``, ``pause_context``, and
          ``pending_questions`` promptly instead of blocking — the pause envelope has already
          been durably persisted to the job record before this returns (a notification, not the
          source of truth, per the contract doc). Returns ``None`` when the pipeline instead
          reaches a terminal state (terminal status is still reported via the job record, as in
          block mode — this function's return is a pause notification only).
        - The task graph's persist/flush coordinator is always stopped before this function exits,
          on every exit path including an unexpected exception or a pause return.
    """
    if not (0 <= progress_base and 0 <= progress_span and progress_base + progress_span <= 100):
        raise ValueError(
            f"progress_base ({progress_base}) and progress_span ({progress_span}) "
            "must be non-negative and sum to <= 100"
        )
    if pause_strategy not in ("block", "return"):
        raise ValueError(f"pause_strategy must be 'block' or 'return', got {pause_strategy!r}")
    # The implementation engines (v2 team leads, quality gates, code review) are injected, not
    # imported: prefer the provider passed explicitly (the software-engineering team supplies one
    # per call) and fall back to the process-wide default the standalone service installs at
    # startup. Presence check, not truthiness: an injected provider is an arbitrary object whose
    # __bool__/__len__ are not part of the contract, so a falsy-but-valid provider must not be
    # silently swapped for the ambient default.
    if engine_provider is None:
        engine_provider = get_engine_provider()
    path = Path(repo_path).resolve()
    _raw_update = update_job_fn or (lambda **kw: update_job(job_id, cache_dir=cache_dir, **kw))
    _get_job = get_job_fn or (lambda jid: get_job(jid, cache_dir=cache_dir))

    # The task-graph persist/flush state machine (background single-writer flusher, the task
    # graph it persists, the last-confirmed snapshot bookkeeping, and the live phase/status_text
    # every write carries) is one cohesive concern owned by GraphPersistCoordinator. The
    # entrypoint holds a plain handle and calls coord.update()/coord.persist_sync(); the graph's
    # own mutators call coord.persist_async() via the persist_callback wired at construction. The
    # single-writer ordering guarantees (a background graph write can never clobber a fresher
    # direct status write; the async path reads phase/status_text live at write time) live on the
    # coordinator's methods, next to the code they govern.
    coord = GraphPersistCoordinator(
        job_id,
        _raw_update,
        progress_base=progress_base,
        progress_span=progress_span,
        phase="task_graph",
        status_text="Building task graph from plan",
    )

    try:
        # Capture agents' streamed reasoning ("thinking") so the UI can show what is
        # happening. Tokens land in an in-memory buffer (cheap, off the DB path); a
        # heartbeat below flushes the tail to the job record's ``thinking`` field.
        thinking = _ThinkingBuffer()
        llm_getter = get_llm or _make_reasoning_llm_getter(thinking.append)

        def _check_cancel() -> bool:
            data = _get_job(job_id)
            return bool(data and data.get(CANCEL_KEY))

        def _pause_cycle(questions: List[Any], source: str) -> "tuple[List[Dict[str, Any]], bool]":
            """Thin binding of ``_run_pause_cycle`` to this job's context.

            The return type annotation describes the ``pause_strategy="block"`` contract only:
            under ``pause_strategy="return"`` (closed over above), ``_run_pause_cycle`` raises
            ``pause_cycle._ActivityPauseSignal`` instead of returning — see that function's own
            contract for the discriminating detail this annotation can't express.
            """
            return _run_pause_cycle(
                job_id,
                questions,
                source,
                get_job_fn=_get_job,
                update_fn=coord.update,
                on_pause=on_pause,
                pause_strategy=pause_strategy,
            )

        existing = _get_job(job_id) or {}

        # pause_strategy="return" re-entry check: tell a genuine resume (acknowledged_resume_token
        # matches the persisted, unresolved pause) apart from a pre-work Temporal activity retry
        # (token missing/stale — re-emit the exact same pause, do not re-run any work) apart from
        # "no pause outstanding" (proceed normally below). Must run BEFORE _hydrate_resolved_from_record
        # so a consumed pause's freshly appended submitted_answers are picked up by that call, and
        # before any planning work so a retry never re-runs the Tech Lead LLM call. Block-mode callers
        # never persist resume_token, so this is a no-op for them regardless.
        if pause_strategy == "return":
            reentry = _check_pending_pause_reentry(existing, acknowledged_resume_token)
            if reentry is not None:
                if not reentry["consume"]:
                    return {
                        "outcome": "paused",
                        "job_id": job_id,
                        "resume_token": reentry["resume_token"],
                        "pause_kind": reentry["pause_kind"],
                        "pause_context": reentry["pause_context"],
                        "pending_questions": reentry["pending_questions"],
                    }
                # Consume: atomically clear the pause envelope (sole responsibility of the
                # orchestrator, never the answers-submission route — see contract doc §1's
                # ownership invariant) and continue normally; _hydrate_resolved_from_record below
                # folds the already-appended submitted_answers into plan_input.resolved_questions.
                coord.update(
                    waiting_for_answers=False,
                    pending_questions=[],
                    resume_token=None,
                    pause_kind=None,
                    pause_context=None,
                )
                existing = _get_job(job_id) or existing

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

        plan_result = _plan_and_groom(
            job_id=job_id,
            plan_input=plan_input,
            existing=existing,
            retry_failed=retry_failed,
            coord=coord,
            llm_getter=llm_getter,
            engine_provider=engine_provider,
            get_job_fn=_get_job,
            pause_cycle=_pause_cycle,
        )
        if plan_result is None:
            return

        swarm = _assign_and_implement(
            job_id=job_id,
            path=path,
            coord=coord,
            tech_lead=plan_result.tech_lead,
            implementation_workers=plan_result.implementation_workers,
            agent_ids=plan_result.agent_ids,
            llm_getter=llm_getter,
            plan_input=plan_input,
            engine_provider=engine_provider,
            existing=plan_result.existing,
            thinking=thinking,
            check_cancel=_check_cancel,
            pause_cycle=_pause_cycle,
            pause_strategy=pause_strategy,
        )
        graph = coord.graph

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
            all_terminal
            and failed_count == 0
            and merged_count > 0
            and resolved_count == merged_count
        )
        # A job with failed tasks must not be presented as a clean success — surface a distinct
        # terminal status so downstream consumers (and the GitHub publish flow) can flag the gap.
        # current_activity=None travels in the terminal write itself so a transient
        # failure of an earlier best-effort clear cannot leave a terminal job serving
        # a stale mid-review activity entry.
        if already_complete:
            coord.update(
                status=JobStatus.ALREADY_COMPLETE.value,
                phase="completed",
                status_text="Work already complete; no changes needed",
                already_complete=True,
                completion_evidence="The requested work was already present; no changes were needed.",
                progress=100,
                current_activity=None,
            )
            return
        coord.update(
            status=(
                JobStatus.COMPLETED_WITH_FAILURES.value
                if failed_count
                else JobStatus.COMPLETED.value
            ),
            phase="completed",
            status_text=f"Completed: {merged_count} merged, {failed_count} failed",
            progress=100,
            current_activity=None,
        )
    except _ActivityPauseSignal as sig:
        # Raised only under pause_strategy="return" (see _run_pause_cycle) — a HITL gate paused
        # somewhere in this call's stack (the entry-gate call above, _plan_with_hitl's loop, or a
        # worker escalation deep inside swarm.run()). The pause envelope is already durably
        # persisted to the job record by _run_pause_cycle before this was raised; this return
        # value is a notification to the Temporal activity caller, not the source of truth.
        return {"outcome": "paused", "job_id": job_id, **sig.payload}
    finally:
        # Guaranteed on every exit path (normal completion, every early return above, or an
        # unexpected exception): drains any pending write, then tears down the daemon thread so
        # a long-running process handling many jobs over its lifetime never leaks one per job.
        coord.stop()


class CodingTeamSwarm(
    TeamLeadSharedState,
    _AssignmentMixin,
    _ImplementationMixin,
    _ReviewMixin,
    _RevisionCapMixin,
):
    """Coordinator (Tech Lead) + frontend/backend v2 implementation-worker swarm pattern.

    The coordinator assigns ready tasks to free workers. Each worker implements
    the task and runs quality gates (build, lint), and signals completion. The
    coordinator reviews (the swarm's sole code-review pass) and merges approved
    tasks.

    Shares ``TeamLeadSharedState`` (LLM getter / shared config / status callback)
    with the code-v2 team-lead stack, but deliberately does not adopt
    ``BaseTeamLead``'s gated phase-sequencing template or the
    ``build_team_failure_result`` / ``apply_team_failure`` envelope. Its
    round-based assign → implement → review → merge loop, worktree management,
    and pause/merge locking are structurally incompatible with single-pass phase
    sequencing; failures stay task-graph / dict-shaped rather than
    ``success`` / ``failure_reason`` team results. Behavior is otherwise spread
    across four mixins by responsibility (assignment, implementation, review,
    revision-cap bookkeeping) — see swarm_assignment.py, swarm_implementation.py,
    swarm_review.py, swarm_revision_cap.py.

    Invariants:
        - ``_worktrees`` (a ``WorktreeManager``) is constructed in ``__init__`` but does no
          filesystem/git I/O until ``run()`` calls ``prepare()``; ``run()`` always calls
          ``cleanup()`` before returning, on every exit path, so the worktree lifecycle is
          scoped exactly to one ``run()`` call and never leaks past it.
        - ``_pause_lock``, ``_merge_lock``, and ``_review_verdict_cache_lock`` each guard one
          piece of state shared across concurrently-running workers within a single ``run()``
          (the pause-cycle round-trip, the shared checkout's merge/abort-merge calls, and
          ``_review_verdict_cache`` respectively); they are held only for the instance's
          lifetime and are never reused across a resume.
        - ``aborted`` starts ``False`` and is the sole flag that both stops the ``run()`` loop
          early and tells ``run_coding_team_orchestrator`` not to report the job as completed;
          once set it is never cleared within the instance's lifetime.
        - ``_pause_strategy`` defaults to ``"block"`` in ``__init__`` and is set from ``run()``'s
          own ``pause_strategy`` argument (same pattern as ``pause_for_questions``); in this
          codebase a swarm instance is constructed fresh per orchestrator invocation and
          ``run()`` is called at most once on it, so it is effectively constant for the
          instance's lifetime even though nothing prevents a second ``run()`` call from
          changing it. Read by ``_escalate_decision`` to decide whether the one-shot pause
          guard below applies.
        - ``_escalation_pause_committed`` is reset to ``False`` at the start of every round in
          ``run()`` and is only ever set to ``True`` while ``_pause_lock`` is held; under
          ``pause_strategy="return"`` it guarantees at most one worker-escalation pause is
          published per round (see ``_escalate_decision``'s Concurrency note). It is a no-op
          in block mode.
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
        spec_content: str = "",
        restored_review_cache: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Construct the swarm; performs no I/O (worktrees are created in ``run()``).

        Preconditions:
            - Every worker in ``workers`` has an ``agent_id`` that appears in ``agent_ids``
              (the two rosters correspond 1:1); ``graph`` is a ``TaskGraphService`` the caller
              owns and continues to control after construction.
            - ``restored_review_cache`` is ``None`` (fresh run) or whatever
              ``export_review_cache``/``serialize_review_cache`` last produced, persisted and
              handed back by the caller on a snapshot resume; the caller never validates it —
              ``deserialize_review_cache`` degrades any malformed/corrupt value to ``{}`` on its
              own (see that function's contract), so this constructor never raises on bad input.
        Postconditions:
            - Constructs exactly one ``WorktreeManager`` for ``path``/``agent_ids`` (unprepared —
              see class invariants); ``aborted`` is ``False``. ``resolved_questions`` is copied
              into an independent list, so later mutation by the caller's original list does not
              affect this instance.
            - ``_review_verdict_cache`` is seeded from
              ``deserialize_review_cache(restored_review_cache)`` — empty when
              ``restored_review_cache`` is ``None``/empty/malformed (fresh run, no regression),
              otherwise populated with the caller's previously-persisted verdicts (snapshot
              resume, including under ``retry_failed=True``). ``_review_verdict_cache_lock`` is
              always a freshly constructed ``threading.Lock()`` — lock objects are never restored
              from storage.
        """
        TeamLeadSharedState.__init__(
            self,
            llm_getter=llm_getter,
            shared_config={},
        )
        self.tech_lead = tech_lead
        self.workers = workers
        self.graph = graph
        self.path = path
        self.agent_ids = agent_ids
        self.agent_team_keys = {w.agent_id: _worker_team_key(w) for w in workers}
        # Injected implementation engines (build/lint); None → quality gates are skipped.
        self.engine_provider = engine_provider
        # The plan's final spec content (CodingTeamPlanInput.final_spec_content), forwarded to the
        # Tech Lead's per-task code review (see swarm_review._compute_review) so the reviewer can
        # check compliance against the actual spec, not just the task's own description/acceptance
        # criteria — this is the swarm's sole code-review call, so it is the only place spec
        # constraints outside a task's own summary can be caught.
        self.spec_content = spec_content
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
        # pause_strategy="block"|"return" (set in run()); read by _escalate_decision to decide
        # whether the one-shot "pause already committed this round" guard applies (see that
        # guard's own comment — it must be a no-op in block mode to preserve today's behavior of
        # every escalating worker getting its own full pause-and-resolve cycle in sequence). In
        # "return" mode the guard causes a second concurrently-escalating worker to defer its
        # task to next round (IN_PROGRESS, unescalated) rather than racing to publish a competing
        # pause; in "block" mode it never fires (see run()'s docstring on _escalation_pause_committed).
        self._pause_strategy = "block"
        # One-shot, _pause_lock-protected guard: True once some worker has published this round's
        # escalation pause under pause_strategy="return". In that mode _pause_lock releases almost
        # immediately as the pause exception unwinds (unlike block mode, where it's held for the
        # whole answer round-trip), so without this a second concurrently-escalating worker could
        # publish a competing pause overwriting the first's job-record write. Reset at the top of
        # every round in run().
        self._escalation_pause_committed = False
        # Per-task Tech Lead review-verdict cache: task.id -> (cache_key, verdict), where cache_key
        # (see swarm_review._review_verdict_cache_key) covers every input run_code_review actually
        # sees (changes_summary/evidence, which embeds the branch diff, plus user_decisions) — not
        # the branch diff alone, so a changed changes_summary or a newly answered HITL decision still
        # misses the cache even when the branch itself is unchanged. Reusing the cached verdict skips
        # paying for another run_code_review call (see swarm_review._compute_review). Seeded from
        # restored_review_cache on a snapshot resume (see export_review_cache and
        # GraphPersistCoordinator, which round-trip it through the job record's
        # "review_verdict_cache" field); empty on a fresh run. Locked because _review_and_merge
        # fans _compute_review out across multiple in-review tasks via parallel_map — the lock
        # itself is always freshly constructed here, never restored.
        self._review_verdict_cache: Dict[str, tuple[str, Dict[str, Any]]] = (
            deserialize_review_cache(restored_review_cache)
        )
        self._review_verdict_cache_lock = threading.Lock()
        # One isolated git worktree per worker (see coding_team.worktree_manager) — created up
        # front in run(), never lazily from inside a worker thread. Construction itself does no
        # filesystem/git I/O.
        self._worktrees = WorktreeManager(path, agent_ids)

    def export_review_cache(self) -> List[Dict[str, Any]]:
        """JSON-safe snapshot of the review verdict cache for durable persistence.

        Preconditions:
            - None — safe to call at any point in the instance's lifetime, including before any
              task has been reviewed (``_review_verdict_cache`` empty).

        Postconditions:
            - Returns ``serialize_review_cache(self._review_verdict_cache)`` (see that function's
              own contract for the exact shape/cap), computed while holding
              ``_review_verdict_cache_lock`` so a concurrent ``_review_and_merge`` cache write
              (fanned out via ``parallel_map``) can never be observed half-written.
            - Does not mutate ``_review_verdict_cache``.
        """
        with self._review_verdict_cache_lock:
            return serialize_review_cache(self._review_verdict_cache)

    def _is_complete(self) -> bool:
        """Whether this round's work is fully drained: nothing left to assign, run, or review.

        Postconditions:
            - Returns ``True`` iff no task is ``TO_DO``, no agent in ``agent_ids`` has a task
              assigned to it, and no task is ``IN_REVIEW``. Pure: reads ``graph`` and
              ``agent_ids``, no side effects.
        """
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
        pause_strategy: str = "block",
    ) -> None:
        """Main swarm loop: assign → implement + quality gates → review → merge.

        ``pause_for_questions`` is the bound HITL gate used to escalate a worker-raised decision to
        the user; when omitted, a worker that raises a decision fails its task closed (no silent
        decide). ``pause_strategy`` mirrors ``run_coding_team_orchestrator``'s own parameter of the
        same name — ``"block"`` (default) preserves today's behavior exactly; ``"return"`` makes a
        worker-escalation pause raise instead of blocking (see
        ``swarm_implementation._escalate_decision``) and additionally makes the implement-phase
        ``parallel_map`` fan-out wait for every in-flight worker to finish before that exception is
        allowed to propagate, so no sibling worker is left mutating the checkout/job-record
        unsupervised after the activity has already reported "paused". The loop stops early if a
        pause ends without answers (``self.aborted``) — in block mode only; in return mode, a
        worker-escalation pause instead raises out of this method entirely (see Postconditions).

        Preconditions:
            - The swarm was constructed with a non-empty ``workers``/``agent_ids`` roster and a
              ``graph`` already seeded with the job's tasks (or empty, for a no-op run).
            - ``pause_strategy`` is ``"block"`` or ``"return"``; violated by raising ``ValueError``.
        Postconditions:
            - Every worker's git worktree (see WorktreeManager) is removed before this method
              returns OR raises, on every exit path (normal completion, cancellation, abort, a
              worktree-setup failure, an unexpected exception, or a ``pause_strategy="return"``
              worker-escalation pause propagating out) — the worktree lifecycle is scoped exactly
              to one ``run()`` call.
        Outcomes:
            - Returns ``None`` normally: the swarm completed, was cancelled, or aborted because a
              block-mode pause ended without answers (``self.aborted``).
            - Raises ``pause_cycle._ActivityPauseSignal`` when ``pause_strategy="return"`` and a
              worker escalates a HITL decision (from ``swarm_implementation._escalate_decision``,
              via the bound ``pause_for_questions`` cycle) — the pause envelope is already
              durably persisted to the job record before this raises; the caller
              (``run_coding_team_orchestrator``) catches it and returns the discriminated
              ``{"outcome": "paused", ...}`` result. Worktree cleanup still runs first (see
              Postconditions above).
        """
        if pause_strategy not in ("block", "return"):
            raise ValueError(f"pause_strategy must be 'block' or 'return', got {pause_strategy!r}")
        _update = update_fn or (lambda **kw: None)
        _persist = persist_fn or (lambda: None)
        self.pause_for_questions = pause_for_questions
        self._pause_strategy = pause_strategy

        try:
            # Check before doing any work — including worktree setup, which is neither free
            # nor guaranteed to succeed — so a job cancelled before run() was even entered
            # (or between phases) is honored immediately rather than reported "failed" if
            # prepare() happens to error, or made to wait out a setup it will just discard.
            if check_cancel and check_cancel():
                _update(status=JobStatus.CANCELLED.value, status_text="Cancelled by user")
                return

            try:
                self._worktrees.prepare()
            except Exception as exc:  # noqa: BLE001 - a broken worktree setup fails the job, not the process
                logger.exception(
                    "Failed to prepare implementation-worker git worktrees",
                    extra={"trace_id": current_trace_id()},
                )
                _update(
                    status=JobStatus.FAILED.value,
                    phase="completed",
                    status_text="Could not prepare implementation-worker git worktrees",
                    error=str(exc),
                )
                self.aborted = True
                return

            for round_num in range(max_rounds):
                if check_cancel and check_cancel():
                    _update(status=JobStatus.CANCELLED.value, status_text="Cancelled by user")
                    return
                # Fresh per round: a round in which no one has escalated yet may freely publish
                # one; see the flag's own comment in __init__ for why this only matters in
                # pause_strategy="return".
                self._escalation_pause_committed = False

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
                    from shared.concurrency import parallel_map

                    _update(status_text=f"Implementing {len(active)} task(s)")
                    parallel_map(
                        active,
                        lambda swe: self._implement_and_verify(swe, _update, live_progress=False),
                        max_workers=_implementation_concurrency(),
                        skip_none=False,
                        # Only under pause_strategy="return": a worker escalation raises
                        # _ActivityPauseSignal instead of returning normally (see
                        # swarm_implementation._escalate_decision), and parallel_map's default
                        # fast-fail (wait_for_stragglers=False) would let that exception propagate
                        # while sibling workers are still running unsupervised in the background —
                        # exactly the "silent corruption" risk the pause is trying to report
                        # cleanly. wait_for_stragglers=True makes every in-flight worker finish
                        # first. Block mode never raises here, so this is a no-op there.
                        wait_for_stragglers=(self._pause_strategy == "return"),
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
