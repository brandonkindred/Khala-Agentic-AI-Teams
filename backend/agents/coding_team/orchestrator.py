"""
Coding team orchestrator: plan → Task Graph → assign → implement → review → merge.

Uses a swarm pattern: a Coordinator (Tech Lead) assigns tasks from the graph
to Workers (Senior SWEs). Quality gate tools run after each implementation.
Exposes run_coding_team_orchestrator for in-process call from software_engineering_team.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from coding_team import hitl
from coding_team.activity import ActivityBridge
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
from coding_team.senior_software_engineer_agent import SeniorSWEAgent
from coding_team.task_graph import TaskGraphService, create_task_graph
from coding_team.tech_lead_agent import TechLeadAgent

logger = logging.getLogger(__name__)

CANCEL_KEY = "cancel_requested"
MAX_TASK_REVISIONS = 20  # max times a task can be returned for revision before accepting

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


# Cap on the Tech-Lead clarify→answer→re-plan loop. Each round is one pause for the user plus one
# re-plan; bounding it stops a model that keeps asking from looping forever. On exhaustion the
# orchestrator fails closed rather than building tasks around an undecided question.
MAX_TECH_LEAD_QUESTION_ROUNDS = 5

# Type alias for the bound pause cycle: given questions + a source label, surface them to the user,
# block until answered, and return (resolved_answers, ok). ok=False means the job went terminal or
# timed out while waiting (the cycle has already set the failure status) and the caller must stop.
PauseCycle = Callable[[List[Any], str], "tuple[List[Dict[str, Any]], bool]"]

# How often the orchestrator flushes captured agent "thinking" (reasoning tokens)
# to the job record so the UI poll can surface it. Defaults to 2s; garbage/non-positive
# falls back to the default.
_ENV_THINKING_FLUSH_INTERVAL_S = "AGENT_THINKING_FLUSH_INTERVAL_S"
_DEFAULT_THINKING_FLUSH_INTERVAL_S = 2.0
# Keep only the most recent tail of reasoning so the field (and DB write) stays bounded.
_THINKING_MAX_CHARS = 8000


def _thinking_flush_interval_s() -> float:
    """Resolve the thinking-flush interval (seconds) from env, defensively.

    Preconditions: none.
    Postconditions: returns a finite, positive float; missing/garbage/non-positive/
        non-finite (``inf``/``nan``) yields ``_DEFAULT_THINKING_FLUSH_INTERVAL_S``.
        Never raises. A non-finite interval would make the heartbeat's
        ``Event.wait(interval)`` block forever, defeating periodic flushing.
    """
    raw = os.environ.get(_ENV_THINKING_FLUSH_INTERVAL_S)
    if not raw:
        return _DEFAULT_THINKING_FLUSH_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_THINKING_FLUSH_INTERVAL_S
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_THINKING_FLUSH_INTERVAL_S
    return value


class _ThinkingBuffer:
    """Thread-safe, capped accumulator for streamed reasoning ("thinking") tokens.

    The LLM client's ``on_reasoning`` hook (called from a streaming worker thread)
    appends tokens; a heartbeat thread periodically reads ``pending`` and, only
    after a SUCCESSFUL write, calls ``commit`` to mark that tail flushed. Splitting
    read from commit means a failed write is NOT recorded as flushed, so the next
    beat retries it rather than silently dropping it. Only the most recent
    ``_THINKING_MAX_CHARS`` characters are retained so memory and the persisted
    field stay bounded.

    Invariants: all access is guarded by an internal lock; ``pending`` returns the
    current tail only while it differs from the last committed tail.
    """

    def __init__(self, max_chars: int = _THINKING_MAX_CHARS) -> None:
        self._lock = threading.Lock()
        # Floor to >=1: a non-positive cap would make the tail slice ``text[-0:]``
        # return the WHOLE string, defeating the bound and growing unbounded.
        self._max_chars = max(1, max_chars)
        self._text = ""
        self._flushed = ""

    def append(self, token: str) -> None:
        """Append a reasoning delta. Amortized O(len(token)): the buffer is only
        re-sliced to the cap once it grows past ``2 * max_chars``, so a long stream
        does not pay an O(max_chars) copy on every token."""
        with self._lock:
            self._text += token
            if len(self._text) > self._max_chars * 2:
                self._text = self._text[-self._max_chars :]

    def pending(self) -> Optional[str]:
        """Return the current (capped) tail if it differs from the last committed
        flush, else ``None``. Does NOT mark it flushed — call ``commit`` after a
        successful write."""
        with self._lock:
            tail = self._text[-self._max_chars :]
            return tail if tail != self._flushed else None

    def commit(self, text: str) -> None:
        """Record ``text`` as the last successfully-flushed tail."""
        with self._lock:
            self._flushed = text


def _flush_thinking(buffer: _ThinkingBuffer, update_fn: Callable[..., None]) -> None:
    """Write the buffer's latest thinking tail to the job record, if it changed.

    Preconditions: ``update_fn`` accepts a ``thinking=`` keyword.
    Postconditions: calls ``update_fn(thinking=<tail>)`` exactly when the tail
        changed since the last *successful* flush AND is non-blank, and only marks
        the tail flushed (``commit``) when that write did not raise — so a failed
        write is retried on the next beat instead of being silently dropped. A write
        failure is swallowed (surfacing thinking must never break the job); never
        raises. Blank tails are skipped so the first ``beat_first`` tick on an empty
        buffer — and every tick on a path where no reasoning is captured — does not
        write an empty ``thinking`` field (which the UI would render as a blank panel).
    """
    text = buffer.pending()
    if not text or not text.strip():
        return
    try:
        update_fn(thinking=text)
    except Exception:  # noqa: BLE001 — surfacing thinking must never break the job
        logger.debug("failed to flush thinking to job record", exc_info=True)
        return  # do NOT commit — retry this tail on the next beat
    buffer.commit(text)


def _make_reasoning_llm_getter(record_reasoning: Callable[[str], None]) -> Callable[[str], Any]:
    """Build the default per-job model getter that streams reasoning into ``record_reasoning``.

    Each role gets a hook-bearing client that is UNCACHED in the global factory cache
    (so the per-job callback never leaks into the shared singleton), but memoized
    PER JOB by key: repeated ``getter(key)`` calls (e.g. one per task revision through
    the quality gates) reuse the same client, so its model-context (``/api/show``)
    lookup happens once per role rather than on every call.

    Preconditions: ``record_reasoning`` is callable.
    Postconditions: returns a getter ``key -> strands model`` whose underlying client
        invokes ``record_reasoning`` for each reasoning delta; the same ``key`` yields
        the same model (and underlying client) for the life of this getter — so both
        the ``/api/show`` context lookup and the wrapper allocation happen once per
        role. Today it is only called from the swarm thread, but the memo is
        lock-guarded so the one-build-per-key invariant holds even if a future caller
        invokes it concurrently.
    """
    models: Dict[str, Any] = {}
    lock = threading.Lock()

    def _getter(key: str) -> Any:
        resolved_key = key or "coding_team"
        with lock:
            model = models.get(resolved_key)
            if model is None:
                factory = __import__("llm_service.factory", fromlist=["get_client"])
                sp = __import__("llm_service.strands_provider", fromlist=["get_strands_model"])
                client = factory.get_client(resolved_key, on_reasoning=record_reasoning)
                model = sp.get_strands_model(resolved_key, client=client)
                models[resolved_key] = model
            return model

    return _getter


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
# software_engineering_team.shared.repo_utils; this summariser additionally surfaces the doc and
# config formats below (so a docs/spec task is not blind to specs, plans, and READMEs). The
# directories it skips come from repo_utils.REPO_INSPECT_EXCLUDE_DIRS (imported in
# `_context_file_filters`), shared with the active inspection tools so the two views of the repo
# cannot drift.
_CONTEXT_EXTRA_EXTENSIONS: frozenset[str] = frozenset(
    {".js", ".html", ".json", ".md", ".txt", ".rst"}
)

# Fallback stack when planning/snapshot provide none (one Senior SWE on a generic stack).
_DEFAULT_STACK_SPECS: List[Dict[str, Any]] = [{"name": "default", "tools_services": []}]

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
        from software_engineering_team.shared.repo_utils import (
            FULL_STACK_EXTENSIONS,
            REPO_INSPECT_EXCLUDE_DIRS,
        )

        _CONTEXT_EXTENSIONS = frozenset(FULL_STACK_EXTENSIONS) | _CONTEXT_EXTRA_EXTENSIONS
        _CONTEXT_EXCLUDE_DIRS = REPO_INSPECT_EXCLUDE_DIRS
    return _CONTEXT_EXTENSIONS, _CONTEXT_EXCLUDE_DIRS


def _read_repo_context(repo_path: Path) -> str:
    """Read the repo structure/code briefing for Senior SWE context.

    Every file the briefing includes is rendered with its FULL contents — the
    engineer reasons over this to implement a task, and clipping a file would
    hide code from it (mirroring the team's "inputs are never truncated"
    contract for the plan text, task description, and review diff). The 80-file
    ceiling on ``sorted(repo_path.rglob("*"))`` is a deliberate cap on how many
    files the briefing covers, not truncation of any file's content.

    Preconditions:
        - ``repo_path`` is an existing directory.
    Postconditions:
        - Each context-eligible file (matching ``_context_file_filters`` and
          within the 80-file scan ceiling) appears with its complete contents,
          never a prefix; no eligible file is dropped to fit a size budget.
        - Returns ``"No files found"`` when no eligible file is present.
    """
    extensions, exclude_dirs = _context_file_filters()

    parts: List[str] = []
    try:
        for f in sorted(repo_path.rglob("*"))[:80]:
            if not f.is_file() or f.suffix not in extensions:
                continue
            if any(skip in f.parts for skip in exclude_dirs):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            rel = str(f.relative_to(repo_path))
            parts.append(f"--- {rel} ---\n{content}\n")
    except Exception:
        pass
    return "\n".join(parts) if parts else "No files found"


def _format_decisions(resolved: List[Dict[str, Any]]) -> str:
    """Render resolved decisions as a 'question → answer' block for an engineer's revision feedback."""
    lines = []
    for r in resolved or []:
        if not isinstance(r, dict):
            continue
        q, a = hitl.decision_qa(r)
        if q or a:
            lines.append(f"{q} → {a}" if q else a)
    body = "\n".join(f"- {ln}" for ln in lines if ln)
    return (
        (
            "The user answered the open question(s) you raised. Implement these decisions exactly; "
            "do not ask again:\n" + body
        )
        if body
        else "The user answered the open question(s) you raised."
    )


def _hydrate_resolved_from_record(
    plan_input: CodingTeamPlanInput, job_data: Dict[str, Any]
) -> None:
    """Fold answers already persisted on the job record into ``plan_input.resolved_questions``.

    Used on a fresh process resuming a job (e.g. a Temporal retry) so answers from a prior attempt
    are carried forward. Persisted answers carry their ``question_id`` but not the original question
    text, so they only clear an open question when the persisted record also carries a matching
    ``question_text``; the coverage check (``hitl.unanswered_questions``) is strictly text-based and
    fails closed, so a resume whose answers lack question text re-asks rather than guessing.

    Postconditions:
        - ``plan_input.resolved_questions`` contains an entry for every persisted answer not already
          present (matched by ``question_id``); pre-existing resolved entries are untouched.
    """
    submitted = (job_data or {}).get("submitted_answers") or []
    if not submitted:
        return
    existing = list(plan_input.resolved_questions or [])
    existing_ids = {r.get("question_id") for r in existing if isinstance(r, dict)}
    for a in submitted:
        if not isinstance(a, dict) or a.get("question_id") in existing_ids:
            continue
        existing.append(
            {
                "question_id": a.get("question_id"),
                "question_text": a.get("question_text", ""),
                "answer": a.get("other_text") or a.get("selected_option_id") or "",
                "selected_option_id": a.get("selected_option_id", ""),
                "other_text": a.get("other_text", ""),
            }
        )
    plan_input.resolved_questions = existing


def _run_pause_cycle(
    job_id: str,
    questions: List[Any],
    source: str,
    *,
    get_job_fn: Callable[[str], Optional[Dict[str, Any]]],
    update_fn: Callable[..., None],
    on_pause: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
) -> "tuple[List[Dict[str, Any]], bool]":
    """Surface open questions, pause the job, block until answered, and return resolved answers.

    This is the single deterministic gate the whole coding team funnels decisions through. It sets
    the job ``waiting_for_user`` (flag ``waiting_for_answers``), records the structured questions,
    optionally invokes ``on_pause`` (e.g. to post a GitHub issue comment), then blocks until the
    answer endpoint clears the flag.

    Postconditions:
        - Returns ``([], True)`` immediately when there is nothing to ask.
        - On answers: returns ``(resolved, True)`` and the job is back to ``running``.
        - On timeout: sets the job ``failed`` and returns ``([], False)``.
        - On the job going terminal while waiting (e.g. cancelled): leaves the status as-is and
          returns ``([], False)``.
        - Never fabricates or defaults an answer.
    """
    structured = hitl.convert_to_structured_questions(questions, source=source)
    if not structured:
        return [], True
    # Record the wait-loop lease (the first heartbeat) ATOMICALLY with the pause flag: the same
    # update publishes ``waiting_for_answers=True`` and a fresh ``answer_wait_heartbeat_at``, so a
    # concurrent answer that lands on another worker can never observe the pause without also
    # observing a live heartbeat. Doing the heartbeat after on_pause (which may post a slow GitHub
    # comment) or only on the first wait_for_answers tick would leave a window where another worker
    # sees the flag but no heartbeat, declares the orchestrator dead, and double-drives the job.
    update_fn(
        status=hitl.WAITING_STATUS,
        phase="paused",
        status_text=f"Waiting for {len(structured)} decision(s) from the user",
        waiting_for_answers=True,
        pending_questions=structured,
        answer_wait_heartbeat_at=hitl.heartbeat_timestamp(),
        # Clear the cross-worker resume-claim lease so THIS pause is immediately claimable without
        # waiting out the prior lease's TTL (the seq counter is left monotonic).
        resume_claim_at=None,
    )
    if on_pause is not None:
        # on_pause can post a GitHub comment whose client uses ~30s timeouts with retries, which
        # can exceed the answer endpoint's heartbeat-staleness window. Periodic wait-loop heartbeats
        # don't start until on_pause returns, so without renewal the single initial heartbeat could
        # go stale mid-callback and let another worker conclude the orchestrator died and spawn a
        # second run. Keep renewing the lease in the background for the callback's full duration.
        _renew_stop = threading.Event()

        def _renew_lease() -> None:
            while not _renew_stop.wait(hitl.ANSWER_WAIT_POLL_INTERVAL_S):
                try:
                    update_fn(answer_wait_heartbeat_at=hitl.heartbeat_timestamp())
                except Exception:  # noqa: BLE001 — renewal must never abort the pause
                    logger.debug(
                        "answer-wait lease renewal during on_pause failed for job %s",
                        job_id,
                        exc_info=True,
                    )

        _renew_thread = threading.Thread(
            target=_renew_lease, name=f"pause-lease-{job_id}", daemon=True
        )
        _renew_thread.start()
        try:
            on_pause(structured)
        except Exception as e:  # noqa: BLE001 — surfacing the pause must never abort the job
            logger.warning("on_pause callback failed for job %s: %s", job_id, e)
        finally:
            _renew_stop.set()
            _renew_thread.join(timeout=5.0)
    # The heartbeat lets the answers endpoint (possibly in another worker process) tell a live,
    # blocked wait loop apart from a dead one before it considers auto-resuming the job.
    got = hitl.wait_for_answers(
        job_id,
        get_job_fn,
        heartbeat_fn=lambda ts: update_fn(answer_wait_heartbeat_at=ts),
    )
    if not got:
        data = get_job_fn(job_id) or {}
        if hitl.is_terminal(data):
            logger.info(
                "Job %s ended while waiting for answers (status=%s)", job_id, data.get("status")
            )
        else:
            update_fn(
                status="failed",
                phase="completed",
                status_text="Timed out waiting for user answers",
                error="Timed out waiting for user answers",
                waiting_for_answers=False,
            )
        return [], False
    submitted = (get_job_fn(job_id) or {}).get("submitted_answers") or []
    resolved = hitl.answers_to_resolved(submitted, structured)
    update_fn(
        status="running",
        phase="coding",
        status_text="Resuming after user answers",
        waiting_for_answers=False,
        pending_questions=[],
    )
    return resolved, True


def _plan_with_hitl(
    tech_lead: TechLeadAgent,
    plan_input: CodingTeamPlanInput,
    pause_cycle: PauseCycle,
    max_rounds: int = MAX_TECH_LEAD_QUESTION_ROUNDS,
) -> Optional[Dict[str, Any]]:
    """Plan the task graph, pausing for the user whenever the Tech Lead raises an open question.

    Postconditions:
        - Returns the task-graph dict once the Tech Lead emits no open questions, with every
          answered decision folded into ``plan_input.resolved_questions``.
        - Returns ``None`` when a pause ended without answers (terminal/timeout) OR when the Tech
          Lead keeps raising open questions past ``max_rounds`` — in the latter case it fails closed
          rather than building tasks around an undecided question. The caller stops either way.
    """
    for _ in range(max_rounds):
        out = tech_lead.run_plan_to_task_graph(plan_input)
        questions = out.get("open_questions") or []
        if not questions:
            return out
        resolved, ok = pause_cycle(questions, "tech_lead")
        if not ok:
            return None
        plan_input.resolved_questions = list(plan_input.resolved_questions or []) + resolved
        plan_input.open_questions = []
    # Reaching here means the Tech Lead raised open questions on every one of max_rounds rounds.
    # Fail closed — do NOT proceed to build tasks around questions that may still be undecided.
    logger.error(
        "Tech Lead still raising open questions after %d round(s); failing closed", max_rounds
    )
    return None


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
) -> None:
    """
    Run the coding_team pipeline: plan → Task Graph → groom/assign → implement → review → merge.
    Uses in-process job store (coding_team/job_store) for task graph persistence.
    update_job_fn / get_job_fn: if provided (e.g. from software_engineering_team), used for phase/status and cancel check.
    progress_base / progress_span: the slice of the job progress bar this run owns
    (see _coding_progress); a parent pipeline passes its coding-phase band, standalone
    jobs use the full bar.

    ``last_activity_at`` (read by the UI's stall warning) is stamped centrally by the
    job service on every real update — see job_service/db.py — so plain ``_update``
    writes count as activity while the 120s liveness heartbeat does not.
    """
    assert 0 <= progress_base and 0 <= progress_span and progress_base + progress_span <= 100
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
    def _persist_graph() -> None:
        # Persist the snapshot through the SAME store used for the resume read and cancel checks
        # (the injected update_job_fn). On the software-engineering path that is the SE job record;
        # the hardcoded coding_team store targets a record that is never created on that path, so
        # the central job service's UPDATE-WHERE matches no row and the write — hence resume — is
        # silently lost. The standalone coding_team path's default callback writes the same keys to
        # the coding_team record exactly as before.
        snap = graph.snapshot()
        _update(
            task_graph_snapshot=snap["tasks"],
            agent_task_map=snap["agent_task_map"],
            phase=phase,
            status_text=status_text,
            progress=_coding_progress(snap["tasks"], progress_base, progress_span),
        )

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
        tasks_raw = out.get("tasks") or []
        stacks_raw = out.get("stacks") or _DEFAULT_STACK_SPECS
        for t in tasks_raw:
            graph.add_task(
                task_id=t["id"],
                title=t.get("title", t["id"]),
                description=t.get("description", ""),
                dependencies=t.get("dependencies", []),
            )
        # Persist the stacks so a later retry can rebuild the workers without re-planning.
        _update(stack_specs=stacks_raw)
    _persist_graph()

    # Build Senior SWE agents (one per stack)
    stack_specs: List[StackSpec] = []
    for i, s in enumerate(stacks_raw):
        name = s.get("name") or f"stack_{i}"
        tools = s.get("tools_services") or []
        stack_specs.append(StackSpec(name=name, tools_services=tools))
    agent_ids = [s.name or f"agent_{i}" for i, s in enumerate(stack_specs)]
    senior_swes: List[SeniorSWEAgent] = []
    for i, spec in enumerate(stack_specs):
        aid = agent_ids[i]
        llm_swe = llm_getter("coding_team")
        senior_swes.append(SeniorSWEAgent(agent_id=aid, stack_spec=spec, llm=llm_swe))

    phase = "coding"
    status_text = "Assigning and implementing tasks"
    # No progress write here: _persist_graph above already published the band value
    # derived from the graph, which on a resume reflects previously merged tasks —
    # an unconditional base write would regress the bar (e.g. 52 → 10 → 52).
    _update(phase=phase, status_text=status_text, status="running")

    # Run the swarm: coordinator (Tech Lead) + workers (Senior SWEs)
    swarm = CodingTeamSwarm(
        tech_lead=tech_lead,
        workers=senior_swes,
        graph=graph,
        path=path,
        agent_ids=agent_ids,
        llm_getter=llm_getter,
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

    merged_count = graph.count_with_status(TaskStatus.MERGED)
    failed_count = graph.count_with_status(TaskStatus.FAILED)
    # A job with failed tasks must not be presented as a clean success — surface a distinct
    # terminal status so downstream consumers (and the GitHub publish flow) can flag the gap.
    # current_activity=None travels in the terminal write itself so a transient
    # failure of an earlier best-effort clear cannot leave a terminal job serving
    # a stale mid-review activity entry.
    _update(
        status="completed_with_failures" if failed_count else "completed",
        phase="completed",
        status_text=f"Completed: {merged_count} merged, {failed_count} failed",
        progress=100,
        current_activity=None,
    )


class CodingTeamSwarm:
    """Coordinator (Tech Lead) + Workers (Senior SWEs) swarm pattern.

    The coordinator assigns ready tasks to free workers. Each worker implements
    the task, runs quality gates (build, lint, code review), and signals
    completion. The coordinator reviews and merges approved tasks.
    """

    def __init__(
        self,
        tech_lead: TechLeadAgent,
        workers: List[SeniorSWEAgent],
        graph: TaskGraphService,
        path: Path,
        agent_ids: List[str],
        llm_getter: Callable[[str], Any],
    ) -> None:
        self.tech_lead = tech_lead
        self.workers = workers
        self.graph = graph
        self.path = path
        self.agent_ids = agent_ids
        self.llm_getter = llm_getter
        # Bound pause cycle (set in run()) used to escalate a worker-raised decision to the user.
        self.pause_for_questions: Optional[PauseCycle] = None
        # Set True when a pause ended without answers (terminal/timeout); aborts the loop and tells
        # the orchestrator not to overwrite the failure status with "completed".
        self.aborted = False
        self.repo_context = _read_repo_context(path)
        # Repo context only changes when merged work lands new files on the working tree, so cache
        # the merged-task count the context reflects and re-read only when it advances (see run()).
        self._context_merged_count = self._merged_count()

    def _merged_count(self) -> int:
        return self.graph.count_with_status(TaskStatus.MERGED)

    def _find_ready_tasks(self) -> List[Task]:
        return [
            t
            for t in self.graph.get_tasks()
            if t.status == TaskStatus.TO_DO and self.graph._dependencies_satisfied(t.id)
        ]

    def _find_free_agents(self) -> List[str]:
        return [aid for aid in self.agent_ids if self.graph.get_task_for_agent(aid) is None]

    def _assign_tasks(self, ready: List[Task], free_agents: List[str]) -> None:
        """Coordinator decides which tasks go to which workers."""
        if not free_agents or not ready:
            return
        assignments = self.tech_lead.run_assignments(
            agent_ids=self.agent_ids,
            ready_tasks=[
                {"id": t.id, "title": t.title, "assignee": t.assigned_agent_id or "unassigned"}
                for t in ready
            ],
            free_agents=free_agents,
        )
        for a in assignments.get("assignments") or []:
            agent_id = a.get("agent_id")
            task_id = a.get("task_id")
            if agent_id and task_id:
                self.graph.assign_task_to_agent(task_id, agent_id)

    def _implement_and_verify(self, swe: SeniorSWEAgent, update_fn: Callable) -> None:
        """Worker implements its assigned task, then runs quality gate tools."""
        task = self.graph.get_task_for_agent(swe.agent_id)
        if not task:
            return
        # Only (re)implement a task that is actively assigned for work. An IN_REVIEW task is awaiting
        # Tech Lead review — re-running the engineer on it would regenerate code already under review
        # and churn the loop. With un-assignment fixed this is belt-and-suspenders, but it makes the
        # worker's contract explicit and robust against any upstream assignment slip.
        if task.status != TaskStatus.IN_PROGRESS:
            return

        update_fn(status_text=f"Implementing: {task.title}")
        result = swe.run_implement(task, self.path, repo_context=self.repo_context)

        if result.get("status") == "needs_decision":
            # The engineer hit a product/design decision it must not make. Escalate to the user
            # (never decide it here); thread the answer back so the next round implements it.
            self._escalate_decision(task, result, update_fn)
            return

        if result.get("status") == "in_review":
            # Run quality gates as tools
            if not self._run_quality_gates(swe, task, result, update_fn):
                return  # task returned to TODO for revision
            self.graph.update_task(
                task.id,
                feature_branch=result.get("feature_branch"),
                changes_summary=result.get("changes_summary"),
            )
            self.graph.set_task_in_review(task.id)
        else:
            # Any non-review outcome — status="failed" (the LLM call raised) or status="in_progress"
            # (the model set ready_for_review=false / asked for another pass) or any unexpected
            # status — must be bounded. Otherwise the task stays IN_PROGRESS and assigned, its
            # revision_count never advances, and the same full implement call repeats every round to
            # the round cap, after which the task is neither MERGED nor FAILED and the job is reported
            # a clean success despite incomplete work.
            logger.warning(
                "Worker %s task %s did not reach review (status=%s): %s",
                swe.agent_id,
                task.id,
                result.get("status"),
                result.get("error"),
            )
            self._handle_incomplete_implementation(task, result)

    def _handle_incomplete_implementation(self, task: Task, result: Dict[str, Any]) -> None:
        """Bound an implementation that did not reach review so it cannot spin the loop to max_rounds.

        Covers both status="failed" (e.g. the LLM call raised) and status="in_progress" (the model
        set ready_for_review=false). Previously only "failed" was handled and "in_progress" was
        dropped entirely, leaving the task IN_PROGRESS and assigned — so the same call repeated every
        round until the round cap. Count each occurrence against the shared revision cap and, on
        exhaustion, fail the task (and its dependents) terminally with the reason recorded.
        """
        if result.get("status") == "in_progress":
            reason = "Engineer did not mark the work ready for review"
        else:
            reason = f"Implementation failed: {result.get('error') or 'unknown error'}"
        entry = {
            "source": "engineer",
            "reason": reason,
            "requested_changes": [],
        }
        feedback = list(task.revision_feedback or []) + [entry]
        revision_count = task.revision_count + 1
        if revision_count >= MAX_TASK_REVISIONS:
            logger.warning(
                "Task %s did not reach review and exhausted revisions (%d); marking FAILED",
                task.id,
                MAX_TASK_REVISIONS,
            )
            self.graph.update_task(
                task.id,
                status=TaskStatus.FAILED,
                revision_count=revision_count,
                revision_feedback=feedback,
            )
            self._cascade_fail_dependents(task.id)
            return
        # Keep it with the same engineer for another bounded attempt; record the reason.
        self.graph.update_task(
            task.id,
            status=TaskStatus.IN_PROGRESS,
            revision_count=revision_count,
            revision_feedback=feedback,
        )

    def _escalate_decision(self, task: Task, result: Dict[str, Any], update_fn: Callable) -> None:
        """Pause the job for a user decision a worker raised, then thread the answer back to the task.

        The engineer never decides the question itself. The task stays with the same engineer
        (IN_PROGRESS) so it re-implements next round with the user's decision in its feedback. An
        escalation is NOT counted against the revision cap — a late-stage question (a task already
        near the cap) must still get its answer implemented, not discarded. Pathological re-asking
        is bounded by the human (each escalation needs a user answer) and the swarm's round cap. A
        pause that ends without answers (terminal/timeout) aborts the swarm.

        Postconditions:
            - On a successful pause the task is IN_PROGRESS with a ``user_decision`` feedback entry
              and the same engineer, so the answer is implemented next round (revision count
              unchanged). On an unanswered pause ``self.aborted`` is set. With no answer channel the
              task is FAILED (fail closed).
        """
        questions = result.get("open_questions") or []
        if self.pause_for_questions is None:
            # No answer channel wired (should not happen on real paths). Fail closed rather than
            # let the engineer's unanswered decision slip through as silently-decided work.
            logger.error(
                "Worker raised a decision but no pause channel is available; failing task %s",
                task.id,
            )
            self.graph.update_task(
                task.id,
                status=TaskStatus.FAILED,
                revision_feedback=list(task.revision_feedback or [])
                + [
                    {
                        "source": "system",
                        "reason": "engineer needs a product decision but no answer channel is available",
                        "requested_changes": [],
                    }
                ],
            )
            self._cascade_fail_dependents(task.id)
            return
        update_fn(status_text=f"Awaiting user decision: {task.title}")
        resolved, ok = self.pause_for_questions(
            questions, f"engineer:{task.assigned_agent_id or task.id}"
        )
        if not ok:
            self.aborted = True
            return
        feedback = list(task.revision_feedback or []) + [
            {
                "source": "user_decision",
                "reason": _format_decisions(resolved),
                "requested_changes": [],
            }
        ]
        # Bound the total number of decision escalations per task, counted independently of the
        # revision cap (so a task at the revision cap still gets its decision implemented). This is a
        # cumulative per-task ceiling, not a same-question repeat detector: after MAX_TASK_REVISIONS
        # escalations the task is failed rather than pausing a human indefinitely. A task that
        # genuinely needs that many distinct decisions is over-scoped and should be split — at the
        # default (20) this needs 19 prior escalations, so it does not bite well-scoped tasks.
        prior_escalations = sum(
            1
            for e in (task.revision_feedback or [])
            if isinstance(e, dict) and e.get("source") == "user_decision"
        )
        if prior_escalations + 1 >= MAX_TASK_REVISIONS:
            logger.warning(
                "Task %s exceeded %d decision escalations; marking FAILED",
                task.id,
                MAX_TASK_REVISIONS,
            )
            self.graph.update_task(task.id, status=TaskStatus.FAILED, revision_feedback=feedback)
            self._cascade_fail_dependents(task.id)
            return
        self.graph.update_task(
            task.id,
            status=TaskStatus.IN_PROGRESS,
            revision_feedback=feedback,
        )

    def _run_quality_gates(
        self, swe: SeniorSWEAgent, task: Task, result: Dict[str, Any], update_fn: Callable
    ) -> bool:
        """Run build, lint, code review. Returns True if passed, False if returned for revision."""
        try:
            from software_engineering_team.quality_gate_tools import (
                run_build_verification,
                run_code_review,
                run_linting,
            )

            agent_type = swe.stack_spec.name or "backend"

            # Build verification
            update_fn(status_text=f"Build verification: {task.title}")
            build = run_build_verification(self.path, agent_type, task.id)
            if not build.success:
                logger.warning(
                    "[%s] Build failed for task %s: %s", swe.agent_id, task.id, build.error
                )
                return self._return_for_revision(task, [{"type": "build", "error": build.error}])

            # Linting
            update_fn(status_text=f"Linting: {task.title}")
            run_linting(self.path, task.id, llm_getter=self.llm_getter)

            # Code review — bridge the agent's sub-step reports into the job record so
            # the UI shows live review progress instead of a frozen "Code review: ..." line.
            cr_bridge = ActivityBridge(
                update_fn,
                agent="code_review",
                label="Code review",
                task_id=task.id,
                task_title=task.title,
            )
            try:
                cr_bridge("preparing", "starting code review", 0.0)
                review = run_code_review(
                    code=result.get("changes_summary", ""),
                    spec_content="",
                    task_description=task.description or task.title,
                    language="python" if agent_type == "backend" else "typescript",
                    acceptance_criteria=task.acceptance_criteria or [],
                    llm_getter=self.llm_getter,
                    progress_callback=cr_bridge,
                )
            finally:
                # Clear on every exit path so a stale sub-progress bar never lingers
                # into the next gate — a frozen bar masquerading as progress is the
                # exact failure mode this reporting exists to fix.
                cr_bridge.clear()
            if not review.approved:
                logger.info(
                    "[%s] Code review rejected task %s (%d issues); returning for revision",
                    swe.agent_id,
                    task.id,
                    len(review.issues),
                )
                return self._return_for_revision(task, review.issues)

        except ImportError:
            logger.debug("Quality gate tools not available; skipping")
        except Exception as e:
            logger.warning("Quality gate tools error for task %s: %s; proceeding", task.id, e)

        return True

    def _return_for_revision(self, task: Task, feedback: List[Dict[str, Any]]) -> bool:
        """Return a task to TODO for revision. Returns False (task not ready for review)."""
        revision_count = task.revision_count + 1
        if revision_count >= MAX_TASK_REVISIONS:
            logger.warning(
                "Task %s exceeded max revisions (%d); accepting as-is", task.id, MAX_TASK_REVISIONS
            )
            return True  # accept despite issues
        # Append to the accumulated history rather than overwriting it: a task may have prior
        # Tech Lead feedback that must survive a later quality-gate failure, or the next engineer
        # prompt would lose those requirements and the reviewer could reintroduce them.
        self.graph.update_task(
            task.id,
            status=TaskStatus.TO_DO,
            revision_count=revision_count,
            revision_feedback=list(task.revision_feedback or []) + list(feedback),
        )
        # Release the task before the next round (status went to TO_DO above): it must be genuinely
        # unassigned and its agent freed, or it stays mapped to its agent and can be double-assigned.
        self.graph.unassign_task(task.id)
        return False

    def _review_and_merge(self, update_fn: Callable) -> None:
        """Coordinator reviews completed tasks: merge approved ones, send rejected ones back."""
        from software_engineering_team.shared.git_utils import (
            DEVELOPMENT_BRANCH,
            branch_diff,
            merge_branch,
        )

        in_review = [t for t in self.graph.get_tasks() if t.status == TaskStatus.IN_REVIEW]
        for task in in_review:
            # Bridge the Tech Lead's attempt/retry reports into the job record so the
            # UI shows which attempt is running (silent retries look like a hang).
            tl_bridge = ActivityBridge(
                update_fn,
                agent="tech_lead_review",
                label="Tech Lead reviewing",
                task_id=task.id,
                task_title=task.title,
            )
            branch = task.feature_branch or f"feature/{task.id}"
            summary = task.changes_summary or "(no summary recorded)"
            # Everything that can raise — including the diff/evidence prep — sits
            # inside the try so the finally clear runs on every exit path; an
            # activity entry written before the try would survive an exception.
            try:
                tl_bridge("preparing", "collecting branch diff", 0.0)
                diff = branch_diff(self.path, DEVELOPMENT_BRANCH, branch)
                evidence = _build_review_evidence(summary, diff)
                review = self.tech_lead.run_code_review(
                    task_title=task.title,
                    task_description=task.description,
                    acceptance_criteria=task.acceptance_criteria,
                    changes_summary=evidence,
                    progress_callback=tl_bridge,
                )
            finally:
                # Clear on every exit path so a stale sub-progress bar never lingers
                # into the next task's review.
                tl_bridge.clear()
            if review.get("error"):
                # The review itself could not run (e.g. evidence exceeded the model context
                # window). Do NOT route this through the revision loop — re-sending the same
                # failing prompt every round would burn the whole revision budget at max cost.
                # Fail the task once with the diagnostic instead.
                self._fail_task(task, review, "Tech Lead review could not be completed")
            elif review.get("approved"):
                try:
                    ok, _ = merge_branch(self.path, branch, DEVELOPMENT_BRANCH)
                    if ok:
                        self.graph.mark_branch_merged(task.id)
                except Exception as e:
                    logger.warning("Merge failed for %s: %s; marking merged anyway", task.id, e)
                    self.graph.mark_branch_merged(task.id)
            else:
                self._request_revision(task, review)

    def _request_revision(self, task: Task, review: Dict[str, Any]) -> None:
        """Send a Tech-Lead-rejected task back to the SAME engineer for revision.

        Unlike the quality-gate path (_return_for_revision, which demotes to TO_DO and clears the
        assignment), a Tech Lead rejection keeps the task with its current engineer: status goes
        back to IN_PROGRESS so the same SWE re-runs run_implement next round with the reviewer's
        reasons threaded into the prompt. On exhausting MAX_TASK_REVISIONS the task is marked
        FAILED (terminal) rather than merging code the Tech Lead rejected.

        Preconditions:
            - task is currently IN_REVIEW and assigned to an engineer.
        Postconditions:
            - task.status is IN_PROGRESS (revision pending) or FAILED (exhausted); never left
              IN_REVIEW with no state change, so the swarm loop cannot deadlock on it.
        """
        entry = {
            "source": "tech_lead",
            "reason": review.get("reason", ""),
            "requested_changes": review.get("requested_changes") or [],
        }
        feedback = list(task.revision_feedback or []) + [entry]
        revision_count = task.revision_count + 1
        if revision_count >= MAX_TASK_REVISIONS:
            logger.warning(
                "Task %s exceeded max revisions (%d) on Tech Lead review; marking FAILED. Reason: %s",
                task.id,
                MAX_TASK_REVISIONS,
                entry["reason"],
            )
            self.graph.update_task(
                task.id,
                status=TaskStatus.FAILED,
                revision_count=revision_count,
                revision_feedback=feedback,
            )
            self._cascade_fail_dependents(task.id)
            return
        logger.info(
            "Task %s rejected by Tech Lead (revision %d); returning to engineer %s",
            task.id,
            revision_count,
            task.assigned_agent_id,
        )
        # Keep the assignment (do not clear assigned_agent_id / the agent->task mapping) so the
        # same engineer picks it up next round and revises the current work.
        self.graph.update_task(
            task.id,
            status=TaskStatus.IN_PROGRESS,
            revision_count=revision_count,
            revision_feedback=feedback,
        )

    def _fail_task(self, task: Task, review: Dict[str, Any], context: str) -> None:
        """Terminally fail a task (and its dependents) without spinning the revision loop.

        Used when a Tech Lead review cannot be performed (e.g. the review evidence exceeded the
        model context window). Re-routing such a task through the revision loop would re-send the
        same failing prompt every round up to MAX_TASK_REVISIONS at max cost; instead we record
        the diagnostic, mark the task FAILED, and cascade the failure to dependents.

        Postconditions:
            - task.status is FAILED; tasks transitively depending on it are FAILED too.
        """
        entry = {
            "source": "tech_lead",
            "reason": review.get("reason", context),
            "requested_changes": [],
        }
        feedback = list(task.revision_feedback or []) + [entry]
        logger.warning(
            "%s for task %s; marking FAILED. Reason: %s", context, task.id, entry["reason"]
        )
        self.graph.update_task(task.id, status=TaskStatus.FAILED, revision_feedback=feedback)
        self._cascade_fail_dependents(task.id)

    def _cascade_fail_dependents(self, task_id: str) -> None:
        """Propagate a task's FAILED state to every task that can no longer be satisfied.

        A task depending on a FAILED task can never satisfy `_dependencies_satisfied` (which
        requires MERGED deps), so without this it would sit TO_DO forever and keep the swarm loop
        from completing. Delegates to `TaskGraphService.mark_dependents_failed`.
        """
        blocked = self.graph.mark_dependents_failed(task_id)
        if blocked:
            logger.warning("Task %s failure cascaded FAILED to dependents: %s", task_id, blocked)

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
        """
        _update = update_fn or (lambda **kw: None)
        _persist = persist_fn or (lambda: None)
        self.pause_for_questions = pause_for_questions

        for round_num in range(max_rounds):
            if check_cancel and check_cancel():
                _update(status="cancelled", status_text="Cancelled by user")
                return

            # Refresh the repo context when merged work has landed since the last read. The merged
            # count is the right signal here: a task's files become part of the shared/integrated
            # tree only once it merges (work in progress lives on per-worker feature branches), and
            # a dependent task is not assignable until its dependencies are MERGED — so it always
            # sees its prerequisites' code. This avoids a full repo walk on every idle round (e.g.
            # while tasks sit in review or blocked); a one-time snapshot at construction would make a
            # worker blind to earlier merged work and recreate it.
            merged_now = self._merged_count()
            if merged_now != self._context_merged_count:
                self.repo_context = _read_repo_context(self.path)
                self._context_merged_count = merged_now

            # Coordinator: assign ready tasks to free workers
            ready = self._find_ready_tasks()
            free = self._find_free_agents()
            self._assign_tasks(ready, free)
            _persist()

            # Workers: implement + quality gates
            for swe in self.workers:
                self._implement_and_verify(swe, _update)
                if self.aborted:
                    break
            _persist()
            # A worker escalation that ended without answers aborts the loop; the orchestrator sees
            # self.aborted and does not report the job as completed.
            if self.aborted:
                return

            # Coordinator: review and merge
            self._review_and_merge(_update)
            _persist()

            if self._is_complete():
                break
