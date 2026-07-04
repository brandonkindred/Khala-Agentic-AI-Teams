"""coding_team API — shared run-thread registry, timing constants, and pure answer/progress helpers.

Monkeypatched collaborators are dereferenced through the ``main`` module object
at call time so ``monkeypatch.setattr(main, ...)`` keeps taking effect after the
split; models are imported directly.

Invariants:
    - ``_active_run_threads`` / ``_starting_run_jobs`` are the one registry pair;
      callers mutate them in place under ``_run_thread_lock`` (never rebind), so
      background threads and the answers/resume routes observe the same maps.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Ensure backend/agents is on path for coding_team and job_service_client
from fastapi import HTTPException

from coding_team.api.models import (
    SubmitAnswersRequest,
)

logger = logging.getLogger(__name__)

# Tracks the orchestrator thread per job so the answers endpoint can tell whether a blocked wait
# loop will pick up answers automatically (thread alive) or the job needs an explicit /resume (the
# thread died, e.g. on a server restart). Mirrors the SE team's _active_orchestrator_threads.
_active_run_threads: Dict[str, threading.Thread] = {}
# Jobs whose orchestrator thread has been claimed but not yet started/registered. The claim closes
# the check-then-spawn race in resume_job: a not-yet-started Thread reports is_alive()==False, so
# without this marker two concurrent /resume calls could both spawn an orchestrator for one job.
_starting_run_jobs: set[str] = set()
_run_thread_lock = threading.Lock()


def _register_run_thread(job_id: str) -> None:
    with _run_thread_lock:
        _active_run_threads[job_id] = threading.current_thread()
        _starting_run_jobs.discard(job_id)


def _clear_run_thread(job_id: str) -> None:
    with _run_thread_lock:
        _active_run_threads.pop(job_id, None)
        _starting_run_jobs.discard(job_id)


def _is_run_thread_alive(job_id: str) -> bool:
    """True if an orchestrator thread for this job is still running (so a blocked wait will resume)."""
    t = _active_run_threads.get(job_id)
    return t is not None and t.is_alive()


def _claim_run_thread(job_id: str) -> bool:
    """Atomically claim the right to start an orchestrator thread for *job_id*.

    Postconditions:
        - Returns True (and marks the job 'starting') iff no thread is running or already being
          started for it; False otherwise. The claim is released by _register_run_thread (once the
          new thread registers) or _clear_run_thread.
    """
    with _run_thread_lock:
        if (
            _active_run_threads.get(job_id) is not None and _active_run_threads[job_id].is_alive()
        ) or (job_id in _starting_run_jobs):
            return False
        _starting_run_jobs.add(job_id)
        return True


def _coerce_progress(value: Any) -> Optional[int]:
    """Coerce a stored progress value to an int in [0, 100], or None.

    Postconditions: garbage (non-numeric) yields None; numeric values are
    clamped so a corrupt record can never render an out-of-range bar.
    """
    try:
        return min(max(int(value), 0), 100)
    except (TypeError, ValueError):
        return None


def _validate_answers(data: Dict[str, Any], request: SubmitAnswersRequest) -> List[Dict[str, Any]]:
    """Validate submitted answers against the job's pending questions; return them as plain dicts.

    Preconditions:
        - ``data`` is the job record; it must be ``waiting_for_answers`` with non-empty
          ``pending_questions``.
    Postconditions:
        - Raises HTTP 400 if the job is not waiting, has no pending questions, any required question
          is unanswered, two answers target the same question, an answer references an unknown
          question, or an 'other' selection carries no text. Otherwise returns the answers as dicts
          ready for ``store_submit_answers``, each
          carrying the ``question_text`` of the pending question it answers (so a later resume can
          match answers to re-asked questions by text).
    """
    if not data.get("waiting_for_answers"):
        raise HTTPException(status_code=400, detail="Job is not waiting for answers.")
    pending = data.get("pending_questions", [])
    if not pending:
        raise HTTPException(status_code=400, detail="No pending questions to answer.")
    # A pending question without an "id" is a corrupted job record (the orchestrator always stamps
    # one), not bad client input — surface it as a controlled 500 instead of a bare KeyError so the
    # failure is attributed to the server and carries a clear message.
    if any("id" not in q for q in pending):
        raise HTTPException(
            status_code=500, detail="Corrupted job record: pending question missing 'id'."
        )
    pending_ids = {q["id"] for q in pending}
    required_ids = {q["id"] for q in pending if q.get("required", True)}
    # Reject duplicate answers for the same question up front: the set below collapses them, so the
    # batch would pass validation while every conflicting entry is still persisted — letting the
    # orchestrator proceed with contradictory decisions for one required question.
    answered_id_list = [a.question_id for a in request.answers]
    seen: set[str] = set()
    dupes: set[str] = set()
    for qid in answered_id_list:
        (dupes if qid in seen else seen).add(qid)
    duplicate_ids = sorted(dupes)
    if duplicate_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Duplicate answers for questions: {', '.join(duplicate_ids)}",
        )
    answered_ids = set(answered_id_list)
    missing = required_ids - answered_ids
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing answers for required questions: {', '.join(sorted(missing))}",
        )
    unknown = answered_ids - pending_ids
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"Unknown question IDs: {', '.join(sorted(unknown))}"
        )
    options_by_qid = {q["id"]: {o.get("id") for o in (q.get("options") or [])} for q in pending}
    for a in request.answers:
        # Whitespace-only free text is not a decision: strip before the emptiness checks so a blank
        # or all-whitespace answer can never be recorded as a (vacuous) decision that 'covers' the
        # open question.
        other_text = (a.other_text or "").strip()
        if a.selected_option_id == "other":
            if not other_text:
                raise HTTPException(
                    status_code=400,
                    detail=f"Question {a.question_id}: 'other' selected but no text provided.",
                )
        elif a.selected_option_id:
            # A non-'other' option id must be one this question actually offered; a bogus id would
            # otherwise be threaded through as the literal user 'decision'.
            if a.selected_option_id not in options_by_qid.get(a.question_id, set()):
                raise HTTPException(
                    status_code=400,
                    detail=f"Question {a.question_id}: unknown option '{a.selected_option_id}'.",
                )
        elif not other_text:
            # Neither an option nor (non-blank) free text: not a decision. Reject it.
            raise HTTPException(
                status_code=400,
                detail=f"Question {a.question_id}: no option selected and no text provided.",
            )
    # Persist the question text alongside each answer: the orchestrator's resume hydration
    # (_hydrate_resolved_from_record) and the HITL coverage check match strictly by question
    # text, so answers stored without it would be discarded — and the question re-asked — on
    # any resume after the original thread died.
    text_by_qid = {q["id"]: q.get("question_text", "") for q in pending}
    return [
        {
            "question_id": a.question_id,
            "question_text": text_by_qid.get(a.question_id, ""),
            "selected_option_id": a.selected_option_id,
            "other_text": a.other_text,
        }
        for a in request.answers
    ]


# A paused orchestrator's wait loop heartbeats every poll (~5s); anything older than this many
# seconds means no live wait loop exists anywhere — including other worker processes, which the
# process-local thread registry cannot see.

_ANSWER_WAIT_HEARTBEAT_STALE_S = 30.0

# Tolerated clock skew between worker hosts: a heartbeat stamped up to this many seconds in the
# future (relative to the checking worker) is still treated as fresh. This covers NTP drift in
# multi-host deployments without blocking resume indefinitely on a far-future/corrupt stamp.
_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S = 10.0

# GitHub returns 422 Unprocessable Entity for validation errors — specifically a
# review comment whose line is off the diff. Only a 422 is recoverable by
# dropping/demoting the offending comment; other statuses signal a real failure.
_HTTP_UNPROCESSABLE = 422

# Body for the extra COMMENT review(s) the bisection path submits after the
# summary has already been posted on its own — so they don't repeat the summary.
_BISECT_CONTINUATION_BODY = "*(continued — additional findings)*"


def _answer_wait_heartbeat_fresh(data: Dict[str, Any]) -> bool:
    """True when a live answer-wait loop (possibly in another worker process) heartbeated recently.

    Preconditions:
        - ``data`` is a job record dict (possibly empty).
    Postconditions:
        - Returns True iff ``answer_wait_heartbeat_at`` parses as an ISO timestamp whose age is in
          ``(-_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S, _ANSWER_WAIT_HEARTBEAT_STALE_S)``. Stamps more
          than ``_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S`` seconds in the future (implausible skew or
          corruption) are NOT fresh — they must never block resume indefinitely. Missing/garbage
          values → False, never raises.
    """
    raw = (data or {}).get("answer_wait_heartbeat_at")
    if not raw:
        return False
    try:
        beat = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - beat).total_seconds()
    return age > -_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S and age < _ANSWER_WAIT_HEARTBEAT_STALE_S
