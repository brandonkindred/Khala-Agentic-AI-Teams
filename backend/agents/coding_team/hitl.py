"""
Human-in-the-loop (HITL) gate helpers for the coding team.

The coding team must never let an agent silently decide an open product, design,
policy, or safety question. These helpers implement a deterministic, fail-closed
gate: when open questions exist and are not yet answered by the user, the job is
paused (status ``waiting_for_user``, flag ``waiting_for_answers`` True) and the
questions are surfaced; execution resumes only once the user submits answers.

The pause flag (``waiting_for_answers``), pending-questions field
(``pending_questions``) and answer field (``submitted_answers``) deliberately
match the software_engineering_team job-record contract, so the SE answers
endpoint (POST /run-team/{job_id}/answers) resumes a coding-team pause
transparently on the SE-driven path, while the coding-team's own answers
endpoint serves the standalone and GitHub-issue paths.

Invariants:
    - The decision to proceed is made by these deterministic checks over job
      data, never by an LLM judging whether it may proceed.
    - On any ambiguity (an open question that cannot be matched to an answer),
      the gate reports the question as unanswered (fail closed → pause), never
      proceeds.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

WAITING_STATUS = "waiting_for_user"

# Mirrors software_engineering_team.orchestrator.DEFAULT_CLARIFICATION_OPTIONS so both teams
# present an identical answer UI; the UI always offers an "other" free-text option on top.
DEFAULT_CLARIFICATION_OPTIONS: List[Dict[str, Any]] = [
    {"id": "yes", "label": "Yes"},
    {"id": "no", "label": "No"},
    {"id": "not_sure", "label": "Not sure / Need more info"},
]

_DEFAULT_ANSWER_WAIT_TIMEOUT_S = 3600.0
_ANSWER_WAIT_POLL_INTERVAL_S = 5.0

# Job statuses that mean "this job will never resume on its own"; the wait loop must stop polling.
_TERMINAL_STATUSES = frozenset({"failed", "cancelled", "completed", "completed_with_failures"})


def answer_wait_timeout_s() -> float:
    """Wall-clock cap (seconds) for a single HITL pause.

    Postconditions:
        - Returns a positive float; env ``CODING_TEAM_ANSWER_WAIT_TIMEOUT_S`` overrides the
          default; garbage / non-positive values fall back to the default.
    """
    raw = os.getenv("CODING_TEAM_ANSWER_WAIT_TIMEOUT_S", "")
    try:
        val = float(raw)
        return val if val > 0 else _DEFAULT_ANSWER_WAIT_TIMEOUT_S
    except (TypeError, ValueError):
        return _DEFAULT_ANSWER_WAIT_TIMEOUT_S


def _normalize_options(raw: Any) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    for o in raw or []:
        if isinstance(o, dict) and o.get("id"):
            options.append(
                {
                    "id": str(o["id"]),
                    "label": str(o.get("label") or o["id"]),
                    "is_default": bool(o.get("is_default", False)),
                }
            )
    return options


def _question_text(q: Any) -> str:
    """Extract the human-readable question text from a string or partially-structured dict."""
    if isinstance(q, dict):
        return str(q.get("question_text") or q.get("text") or q.get("question") or "")
    return str(q)


def convert_to_structured_questions(
    questions: List[Any], source: str = "coding_team"
) -> List[Dict[str, Any]]:
    """Normalize free-text or partially-structured questions into structured pending questions.

    Preconditions:
        - ``questions`` is a list whose entries are either non-empty strings or dicts carrying a
          question text (``question_text`` / ``text`` / ``question``).
    Postconditions:
        - Returns one dict per non-empty input question, each with a stable unique ``id``,
          ``question_text``, ``options`` (the question's own options if provided, else the default
          yes/no/not-sure set), ``required=True``, and ``source``. A question that already carries
          an ``id`` and domain-specific ``options`` round-trips unchanged. Empty questions are
          dropped.
    """
    structured: List[Dict[str, Any]] = []
    for idx, q in enumerate(questions or []):
        text = _question_text(q).strip()
        if not text:
            continue
        if isinstance(q, dict):
            qid = str(q.get("id") or f"{source}_{idx}_{uuid.uuid4().hex[:8]}")
            options = _normalize_options(q.get("options")) or list(DEFAULT_CLARIFICATION_OPTIONS)
            context = q.get("context")
        else:
            qid = f"{source}_{idx}_{uuid.uuid4().hex[:8]}"
            options = list(DEFAULT_CLARIFICATION_OPTIONS)
            context = None
        structured.append(
            {
                "id": qid,
                "question_text": text,
                "context": context,
                "options": options,
                "required": True,
                "source": source,
            }
        )
    return structured


def normalize_open_questions(raw: Any) -> List[Dict[str, Any]]:
    """Normalize raw LLM ``open_questions`` output into a clean list of question dicts.

    Preconditions:
        - ``raw`` is a list of strings or dicts (or None).
    Postconditions:
        - Returns dicts each carrying at least ``question_text``; empties are dropped, and any
          ``context`` / ``options`` an agent supplied are preserved so domain-specific choices
          round-trip. A non-list input yields ``[]``.
    """
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for q in raw:
        text = _question_text(q).strip()
        if not text:
            continue
        entry: Dict[str, Any] = {"question_text": text}
        if isinstance(q, dict):
            if q.get("context"):
                entry["context"] = str(q["context"])
            opts = _normalize_options(q.get("options"))
            if opts:
                entry["options"] = opts
        out.append(entry)
    return out


def question_key(text: str) -> str:
    """Whitespace/case-normalized key used to match an open question against a resolved answer."""
    return " ".join((text or "").lower().split())


def unanswered_questions(
    open_questions: List[Any], resolved_questions: Optional[List[Dict[str, Any]]]
) -> List[Any]:
    """Return the open questions NOT covered by a resolved answer (fail-closed).

    Coverage is by normalized question text when resolved answers carry it. When resolved answers
    exist but none carry text (a legacy answer shape that cannot be matched), an open question is
    treated as covered only if there are at least as many resolved answers as open questions (count
    coverage). With no resolved answers, every open question is unanswered.

    Preconditions:
        - ``open_questions`` entries are strings or dicts with a question text.
    Postconditions:
        - Returns a sublist of ``open_questions`` (in order); empty iff every open question is
          covered. Never returns a question that has a matching answer.
    """
    if not open_questions:
        return []
    resolved = resolved_questions or []
    answered_keys = {
        question_key(r.get("question_text", ""))
        for r in resolved
        if isinstance(r, dict) and r.get("question_text")
    }
    unmatched = [q for q in open_questions if question_key(_question_text(q)) not in answered_keys]
    if not unmatched:
        return []
    # Some open questions did not match by text. If the resolved answers carry no text at all
    # (legacy shape) but there are at least as many of them as open questions, treat them as
    # covered; otherwise fail closed and report the unmatched questions.
    if not answered_keys and resolved and len(resolved) >= len(open_questions):
        return []
    return unmatched


def answers_to_resolved(
    submitted_answers: Optional[List[Dict[str, Any]]],
    pending_questions: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Map raw submitted answers to resolved-question records carrying question text + chosen answer.

    Preconditions:
        - ``submitted_answers`` entries are dicts with at least ``question_id`` and a selected
          option id (or ``other_text``). ``pending_questions`` is the structured set that was
          surfaced (used to resolve the option label and the question text).
    Postconditions:
        - Returns one record per answer that belongs to the surfaced batch (matched by
          ``question_id`` against ``pending_questions``), each with ``question_id``,
          ``question_text``, a human-readable ``answer`` string, and the raw ``selected_option_id``
          / ``other_text``. Answers for other batches (``submitted_answers`` accumulates across
          pauses) are ignored. The records' ``question_text`` lets a later ``unanswered_questions``
          call match them by text.
    """
    by_id = {q.get("id"): q for q in (pending_questions or []) if isinstance(q, dict)}
    resolved: List[Dict[str, Any]] = []
    for a in submitted_answers or []:
        if not isinstance(a, dict):
            continue
        qid = a.get("question_id") or a.get("id")
        if qid not in by_id:
            # An answer from a different pause batch (submitted_answers accumulates); skip it.
            continue
        q = by_id.get(qid) or {}
        selected = a.get("selected_option_id") or a.get("selected_answer") or ""
        other = a.get("other_text") or ""
        label = ""
        for opt in q.get("options") or []:
            if opt.get("id") == selected:
                label = opt.get("label") or selected
                break
        if (selected == "other" or not label) and other:
            answer_text = other
        else:
            answer_text = label or selected
        resolved.append(
            {
                "question_id": qid,
                "question_text": q.get("question_text", ""),
                "answer": answer_text,
                "selected_option_id": selected,
                "other_text": other,
            }
        )
    return resolved


def is_terminal(job_data: Dict[str, Any]) -> bool:
    """True when a job will not resume on its own (terminal status or cancellation requested)."""
    if not job_data:
        return False
    if job_data.get("cancel_requested") or job_data.get("status") == "cancelled":
        return True
    return job_data.get("status") in _TERMINAL_STATUSES


def wait_for_answers(
    job_id: str,
    get_job_fn: Callable[[str], Optional[Dict[str, Any]]],
    *,
    timeout_s: Optional[float] = None,
    poll_interval_s: float = _ANSWER_WAIT_POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> bool:
    """Block until the job's ``waiting_for_answers`` flag clears, the job goes terminal, or timeout.

    Preconditions:
        - The caller has already set ``waiting_for_answers=True`` (and the pending questions) on the
          job record, so the loop observes the paused state on its first read.
    Postconditions:
        - Returns True iff ``waiting_for_answers`` became False (answers submitted) before the job
          went terminal or the timeout elapsed; returns False on terminal/timeout. Never proceeds on
          its own — the only True path is an explicit answer submission clearing the flag.
    """
    timeout = timeout_s if timeout_s is not None else answer_wait_timeout_s()
    start = now()
    while now() - start < timeout:
        data = get_job_fn(job_id) or {}
        if not data.get("waiting_for_answers", False):
            return True
        if is_terminal(data):
            return False
        sleep(poll_interval_s)
    logger.warning("Coding team job %s timed out waiting for user answers", job_id)
    return False
