"""Human-in-the-loop pause/resume cycle for the coding-team Tech Lead planning gate.

Extracted from ``coding_team/orchestrator.py`` (issue: decompose the orchestrator
god-file into named collaborators) — pure structural move, no behavior change.
Builds on the stateless HITL primitives in ``coding_team/hitl.py``.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Callable, Dict, List, Literal, Optional

from software_engineering_team import hitl
from software_engineering_team.models import CodingTeamPlanInput
from software_engineering_team.tech_lead_agent import TechLeadAgent

logger = logging.getLogger(__name__)

# Cap on the Tech-Lead clarify→answer→re-plan loop. Each round is one pause for the user plus one
# re-plan; bounding it stops a model that keeps asking from looping forever. On exhaustion the
# orchestrator fails closed rather than building tasks around an undecided question.
MAX_TECH_LEAD_QUESTION_ROUNDS = 5

# Type alias for the bound pause cycle: given questions + a source label, surface them to the user,
# block until answered, and return (resolved_answers, ok). ok=False means the job went terminal or
# timed out while waiting (the cycle has already set the failure status) and the caller must stop.
# In pause_strategy="return" mode (see _run_pause_cycle), a call through this same signature never
# returns at all -- it raises _ActivityPauseSignal instead. Callers bound to this type only ever see
# that behavior change if the underlying job actually runs pause_strategy="return"; every existing
# caller passes exactly (questions, source) and needs no change either way.
PauseCycle = Callable[[List[Any], str], "tuple[List[Dict[str, Any]], bool]"]


class _ActivityPauseSignal(Exception):
    """Internal control-flow signal: a HITL gate paused while ``pause_strategy="return"``.

    Carries the exact discriminated-result payload ``run_coding_team_orchestrator`` needs
    to return to a Temporal-activity caller, letting a pause deep inside ``_plan_with_hitl``'s
    loop or ``swarm_implementation._escalate_decision`` unwind back to that function's own
    return statement without every intermediate frame needing to learn a new return-value
    shape (``PauseCycle``'s ``(resolved, ok)`` contract stays completely unchanged).

    Invariants:
        - Never crosses ``run_coding_team_orchestrator``'s own boundary — caught there and
          translated into a return value, never propagated to ``run_orchestrator_wired`` or
          the Temporal activity itself.
    """

    def __init__(
        self,
        *,
        resume_token: str,
        pause_kind: str,
        pause_context: Optional[Dict[str, Any]],
        pending_questions: List[Dict[str, Any]],
    ) -> None:
        self.resume_token = resume_token
        self.pause_kind = pause_kind
        self.pause_context = pause_context
        self.pending_questions = pending_questions
        super().__init__(f"paused: resume_token={resume_token} pause_kind={pause_kind}")

    @property
    def payload(self) -> Dict[str, Any]:
        """The discriminated-result fields this signal carries (excluding ``outcome``/``job_id``,
        which the catcher in ``run_coding_team_orchestrator`` adds)."""
        return {
            "resume_token": self.resume_token,
            "pause_kind": self.pause_kind,
            "pause_context": self.pause_context,
            "pending_questions": self.pending_questions,
        }


def mint_resume_token(job_id: str) -> str:
    """Mint a fresh ``resume_token``, unique per pause round.

    Preconditions:
        - ``job_id`` is non-empty.
    Postconditions:
        - Returns ``f"{job_id}:{uuid4().hex[:12]}"`` — unique per call, never reused across
          pause rounds even for the same ``job_id``. Callers must persist the returned value
          atomically with the rest of the pause envelope and must not call this again for the
          same pause round (see ``_run_pause_cycle``: minted only for a genuinely new pause,
          re-emitted unchanged on a pre-work activity retry — see
          ``_check_pending_pause_reentry``).
    """
    return f"{job_id}:{uuid.uuid4().hex[:12]}"


def _pause_kind_for_source(source: str) -> str:
    """Map a ``_run_pause_cycle`` ``source`` label to the contract doc's ``pause_kind``.

    Preconditions:
        - ``source`` is one of this codebase's three recognized labels: ``"plan_input"``
          (entry gate), ``"tech_lead"`` (Tech-Lead clarify loop), or a string starting with
          ``"engineer:"`` (worker escalation — see ``swarm_implementation._escalate_decision``).
    Postconditions:
        - Returns ``"entry"`` | ``"tech_lead_clarify"`` | ``"worker_escalation"``. Raises
          ``ValueError`` on an unrecognized source — fails closed rather than silently
          mislabeling a pause kind the workflow-side consumer doesn't expect.
    """
    if source == "plan_input":
        return "entry"
    if source == "tech_lead":
        return "tech_lead_clarify"
    if source.startswith("engineer:"):
        return "worker_escalation"
    raise ValueError(f"Unrecognized pause source: {source!r}")


def _pause_context_for_source(source: str, pause_kind: str) -> Optional[Dict[str, Any]]:
    """Derive ``pause_context`` from ``source`` for a worker-escalation pause; ``None`` otherwise.

    Preconditions:
        - ``pause_kind`` is ``_pause_kind_for_source(source)``'s result for this same ``source``.
    Postconditions:
        - For ``"worker_escalation"``, returns ``{"task_ids": [<id>]}`` where ``<id>`` is the
          task id embedded in ``source`` (``f"engineer:{task.id}"`` — the swarm's own
          convention). ``None`` for every other ``pause_kind``, per the contract's
          ``pause_context`` shape (set only for worker escalation; entry/tech-lead-clarify
          carry no per-task context).
    """
    if pause_kind != "worker_escalation":
        return None
    return {"task_ids": [source[len("engineer:") :]]}


def _check_pending_pause_reentry(
    job_data: Dict[str, Any],
    acknowledged_resume_token: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Classify a ``pause_strategy="return"`` (re-)entry against a persisted, unresolved pause.

    Preconditions:
        - ``job_data`` is the job record read at the top of this invocation (may be ``{}``).
        - Only meaningful under ``pause_strategy="return"``; a block-mode caller must not call
          this — block-mode pauses never persist ``resume_token``.
    Postconditions:
        - Returns ``None`` when ``job_data`` carries no persisted, unresolved pause
          (``waiting_for_answers`` falsy, or no ``resume_token`` on the record) — the caller
          should proceed normally (fresh run, or a genuinely resolved-and-continuing run).
        - Returns ``{"consume": True, "resume_token", "pause_kind", "pause_context",
          "pending_questions"}`` when ``acknowledged_resume_token`` matches the persisted
          ``resume_token`` by exact equality — a genuine resume; the caller must atomically
          clear the pause envelope and continue (the already-appended ``submitted_answers``
          are picked up by the existing ``_hydrate_resolved_from_record`` call).
        - Returns ``{"consume": False, ...same fields...}`` when a persisted, unresolved pause
          exists but ``acknowledged_resume_token`` is missing or does not match — a pre-work
          activity retry; the caller must re-emit this exact payload as
          ``{"outcome": "paused", ...}`` without re-running any work.
    """
    if not job_data.get("waiting_for_answers"):
        return None
    persisted_token = job_data.get("resume_token")
    if not persisted_token:
        return None
    return {
        "consume": acknowledged_resume_token == persisted_token,
        "resume_token": persisted_token,
        "pause_kind": job_data.get("pause_kind"),
        "pause_context": job_data.get("pause_context"),
        "pending_questions": job_data.get("pending_questions") or [],
    }


def _format_decisions(resolved: List[Dict[str, Any]]) -> str:
    """Render resolved decisions as a 'question → answer' block for an engineer's revision feedback.

    Postconditions:
        - Returns "" when ``resolved`` carries no renderable decision (so the function is safe for
          any caller and never emits a preamble with no decisions under it); otherwise returns the
          preamble followed by one ``- question → answer`` bullet per decision.
    """
    body = "\n".join(f"- {ln}" for ln in hitl.resolved_decision_lines(resolved))
    if not body:
        return ""
    return (
        "The user answered the open question(s) you raised. Implement these decisions exactly; "
        "do not ask again:\n" + body
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


def _last_activity_kw(pinned_activity_at: Optional[str]) -> Dict[str, Any]:
    """Build the ``last_activity_at`` kwarg for a heartbeat update, omitting it when unknown.

    Thread 12: only forward ``last_activity_at`` when we have a concrete value; passing None
    would overwrite the field with null on every heartbeat, breaking the contract that
    last_activity_at reflects real work, not liveness pings.
    """
    return {"last_activity_at": pinned_activity_at} if pinned_activity_at is not None else {}


def _pin_last_activity_at(
    job_id: str, get_job_fn: Callable[[str], Optional[Dict[str, Any]]]
) -> Optional[str]:
    """Read the job's ``last_activity_at`` right after the pause-publish update, for pinning.

    The answer-wait heartbeats are liveness pings, NOT real orchestrator activity, but they route
    through the normal update path which stamps last_activity_at on every write. Pin it to its
    just-published value and pass it back on each heartbeat so a job that waits hours for a user
    doesn't look continuously active (the API contract is that last_activity_at excludes
    heartbeats, and stall/age indicators depend on it). Nothing real happens while waiting, so the
    value is stable for the whole pause.

    Thread 10: wrap the post-pause get_job so a transient store error doesn't crash the
    orchestrator after the pause flag is already written (which would leave waiting_for_answers
    True while the thread handler marks the job failed). On failure, return None — heartbeats
    simply won't pin last_activity_at (see ``_last_activity_kw``).
    """
    try:
        return (get_job_fn(job_id) or {}).get("last_activity_at")
    except Exception:
        logger.debug(
            "Failed to read pinned_activity_at after pause update for job %s; "
            "answer-wait heartbeats will not pin last_activity_at.",
            job_id,
            exc_info=True,
        )
        return None


def _run_on_pause_with_lease_renewal(
    job_id: str,
    structured: List[Dict[str, Any]],
    on_pause: Callable[[List[Dict[str, Any]]], None],
    update_fn: Callable[..., None],
    pinned_activity_at: Optional[str],
) -> None:
    """Invoke ``on_pause`` while a background thread renews the answer-wait lease.

    on_pause can post a GitHub comment whose client uses ~30s timeouts with retries, which can
    exceed the answer endpoint's heartbeat-staleness window. Periodic wait-loop heartbeats don't
    start until on_pause returns, so without renewal the single initial heartbeat could go stale
    mid-callback and let another worker conclude the orchestrator died and spawn a second run.
    Keep renewing the lease in the background for the callback's full duration.
    """
    _renew_stop = threading.Event()
    _pinned_kw = _last_activity_kw(pinned_activity_at)

    def _renew_lease() -> None:
        while not _renew_stop.wait(hitl.ANSWER_WAIT_POLL_INTERVAL_S):
            try:
                update_fn(
                    answer_wait_heartbeat_at=hitl.heartbeat_timestamp(),
                    **_pinned_kw,
                )
            except Exception:  # noqa: BLE001 — renewal must never abort the pause
                logger.debug(
                    "answer-wait lease renewal during on_pause failed for job %s",
                    job_id,
                    exc_info=True,
                )

    _renew_thread = threading.Thread(target=_renew_lease, name=f"pause-lease-{job_id}", daemon=True)
    # Thread 14: a system resource exhaustion may prevent the thread from starting; swallow
    # that error and continue without the renewer (the initial heartbeat from the pause-publish
    # update will cover short on_pause callbacks; a long callback may let the lease go stale).
    _renew_started = False
    try:
        _renew_thread.start()
        _renew_started = True
    except RuntimeError:
        logger.warning(
            "Could not start pause-lease renewal thread for job %s; "
            "on_pause callback runs without background heartbeat renewal.",
            job_id,
            exc_info=True,
        )
    try:
        on_pause(structured)
    except Exception as e:  # noqa: BLE001 — surfacing the pause must never abort the job
        logger.warning("on_pause callback failed for job %s: %s", job_id, e)
    finally:
        _renew_stop.set()
        if _renew_started:
            _renew_thread.join(timeout=5.0)


def _wait_and_collect_answers(
    job_id: str,
    structured: List[Dict[str, Any]],
    *,
    get_job_fn: Callable[[str], Optional[Dict[str, Any]]],
    update_fn: Callable[..., None],
    pinned_activity_at: Optional[str],
) -> "tuple[List[Dict[str, Any]], bool]":
    """Block until answered/terminal/timeout, then resolve answers or fail out.

    The heartbeat lets the answers endpoint (possibly in another worker process) tell a live,
    blocked wait loop apart from a dead one before it considers auto-resuming the job.

    Postconditions:
        - On answers: returns ``(resolved, True)`` and the job is back to ``running``.
        - On timeout: sets the job ``failed`` and returns ``([], False)``.
        - On the job going terminal while waiting (e.g. cancelled): leaves the status as-is and
          returns ``([], False)``.
    """
    _pinned_kw = _last_activity_kw(pinned_activity_at)
    got = hitl.wait_for_answers(
        job_id,
        get_job_fn,
        heartbeat_fn=lambda ts: update_fn(answer_wait_heartbeat_at=ts, **_pinned_kw),
    )
    if not got:
        # Thread 11: wrap the terminal-check read; a store error here means we cannot distinguish
        # a timed-out wait from a cancellation, so default to the safer "mark failed" path.
        try:
            data = get_job_fn(job_id) or {}
        except Exception:
            logger.debug(
                "Failed to read job status after wait timeout for job %s", job_id, exc_info=True
            )
            data = {}
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
    # Thread 11: wrap the submitted-answers read; if it fails after answers were received, log and
    # continue with an empty list so the job is cleared from the paused state (the update below
    # clears waiting_for_answers) rather than crashing with the pause flag still set.
    try:
        submitted = (get_job_fn(job_id) or {}).get("submitted_answers") or []
    except Exception:
        logger.warning(
            "Failed to read submitted_answers for job %s; proceeding with empty answers.",
            job_id,
            exc_info=True,
        )
        submitted = []
    resolved = hitl.answers_to_resolved(submitted, structured)
    update_fn(
        status="running",
        phase="coding",
        status_text="Resuming after user answers",
        waiting_for_answers=False,
        pending_questions=[],
    )
    return resolved, True


def _run_pause_cycle(
    job_id: str,
    questions: List[Any],
    source: str,
    *,
    get_job_fn: Callable[[str], Optional[Dict[str, Any]]],
    update_fn: Callable[..., None],
    on_pause: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    pause_strategy: Literal["block", "return"] = "block",
) -> "tuple[List[Dict[str, Any]], bool]":
    """Surface open questions, pause the job, then either block until answered or return promptly.

    This is the single deterministic gate the whole coding team funnels decisions through. It sets
    the job ``waiting_for_user`` (flag ``waiting_for_answers``) and records the structured questions
    in the SAME atomic update either way.

    Preconditions:
        - ``pause_strategy`` is ``"block"`` or ``"return"``; violated by raising ``ValueError`` (this
          is the sole enforcement point for every caller, including ones that bypass the type
          annotation — e.g. tests calling this function directly rather than through
          ``run_coding_team_orchestrator``, which validates its own ``pause_strategy`` parameter
          before ever reaching here, but cannot enforce what a lower-level direct caller passes).
    Postconditions:
        - Returns ``([], True)`` immediately when there is nothing to ask (both modes, unchanged).
        - ``pause_strategy="block"`` (unchanged from before ``pause_strategy`` existed): optionally
          invokes ``on_pause`` (e.g. to post a GitHub issue comment), then blocks until the answer
          endpoint clears the flag. On answers: returns ``(resolved, True)`` and the job is back to
          ``running``. On timeout: sets the job ``failed`` and returns ``([], False)``. On the job
          going terminal while waiting (e.g. cancelled): leaves the status as-is and returns
          ``([], False)``. Never fabricates or defaults an answer. Never calls
          ``mint_resume_token``/writes ``resume_token``.
        - ``pause_strategy="return"``: additionally writes a freshly minted ``resume_token`` and the
          ``pause_kind``/``pause_context`` derived from ``source`` in that SAME atomic update, then
          raises ``_ActivityPauseSignal`` carrying that payload — never calls ``on_pause`` or
          ``hitl.wait_for_answers`` in this mode; this call never returns normally.
    """
    if pause_strategy not in ("block", "return"):
        raise ValueError(f"pause_strategy must be 'block' or 'return', got {pause_strategy!r}")
    structured = hitl.convert_to_structured_questions(questions, source=source)
    if not structured:
        return [], True
    # Record the wait-loop lease (the first heartbeat) ATOMICALLY with the pause flag: the same
    # update publishes ``waiting_for_answers=True`` and a fresh ``answer_wait_heartbeat_at``, so a
    # concurrent answer that lands on another worker can never observe the pause without also
    # observing a live heartbeat. Doing the heartbeat after on_pause (which may post a slow GitHub
    # comment) or only on the first wait_for_answers tick would leave a window where another worker
    # sees the flag but no heartbeat, declares the orchestrator dead, and double-drives the job.
    update_kwargs: Dict[str, Any] = dict(
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
    resume_token: Optional[str] = None
    pause_kind: Optional[str] = None
    pause_context: Optional[Dict[str, Any]] = None
    if pause_strategy == "return":
        pause_kind = _pause_kind_for_source(source)
        pause_context = _pause_context_for_source(source, pause_kind)
        resume_token = mint_resume_token(job_id)
        # resume_token/pause_kind/pause_context are written ONLY in "return" mode: their presence
        # on the job record is exactly what POST /run/{job_id}/answers uses to decide whether a
        # submission must signal a Temporal workflow instead of relying on a blocked thread — a
        # block-mode job must never carry a resume_token, or that route would try to signal a
        # workflow that does not exist.
        update_kwargs.update(
            resume_token=resume_token, pause_kind=pause_kind, pause_context=pause_context
        )
    update_fn(**update_kwargs)
    if pause_strategy == "return":
        raise _ActivityPauseSignal(
            resume_token=resume_token,
            pause_kind=pause_kind,
            pause_context=pause_context,
            pending_questions=structured,
        )
    pinned_activity_at = _pin_last_activity_at(job_id, get_job_fn)
    if on_pause is not None:
        _run_on_pause_with_lease_renewal(
            job_id, structured, on_pause, update_fn, pinned_activity_at
        )
    return _wait_and_collect_answers(
        job_id,
        structured,
        get_job_fn=get_job_fn,
        update_fn=update_fn,
        pinned_activity_at=pinned_activity_at,
    )


def _plan_with_hitl(
    tech_lead: TechLeadAgent,
    plan_input: CodingTeamPlanInput,
    pause_cycle: PauseCycle,
    max_rounds: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Plan the task graph, pausing for the user whenever the Tech Lead raises an open question.

    Postconditions:
        - Returns the task-graph dict once the Tech Lead emits no open questions, with every
          answered decision folded into ``plan_input.resolved_questions``.
        - Returns ``None`` when a pause ended without answers (terminal/timeout) OR when the Tech
          Lead keeps raising open questions past ``max_rounds`` — in the latter case it fails closed
          rather than building tasks around an undecided question. The caller stops either way.
    """
    if max_rounds is None:
        # Late-bound (not a literal default) so a test/caller patch of
        # coding_team.orchestrator.MAX_TECH_LEAD_QUESTION_ROUNDS is honored: a plain default
        # expression would bind the constant's value once at module-import time and never see
        # a later monkeypatch of the orchestrator's re-exported name.
        from software_engineering_team import coding_team_orchestrator as _orch  # noqa: PLC0415

        max_rounds = _orch.MAX_TECH_LEAD_QUESTION_ROUNDS
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
