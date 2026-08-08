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
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import httpx

from shared.env import parse_float
from software_engineering_team.models import JobStatus

logger = logging.getLogger(__name__)

WAITING_STATUS = JobStatus.WAITING_FOR_USER.value

# Fallback when an agent omits options entirely (should not happen — prompts require context-specific
# options). Empty list means the UI shows only the always-present "Other (free text)" field, which
# is more appropriate than forcing yes/no onto open-ended questions like "What API fields are needed?"
DEFAULT_CLARIFICATION_OPTIONS: List[Dict[str, Any]] = []

# Option IDs and labels that signal a generic yes/no/not-sure response pattern. Individual options
# matching either set are culled before the minimum-count check so that mixed sets (e.g. "yes"/"no"
# blended with one context-specific option) and variant IDs recognized by their display label
# (e.g. id="opt_yes", label="Yes") are both removed rather than silently accepted.
_GENERIC_OPTION_IDS: frozenset = frozenset({"yes", "no", "not_sure"})
_GENERIC_OPTION_LABELS: frozenset = frozenset(
    {
        "yes",
        "no",
        "not sure",
        "not_sure",
        "not sure / need more info",
        # "other" variants — a non-compliant LLM may emit {id:"choice_other", label:"Other"};
        # filtering by label catches these even when the id is not the reserved "other" string.
        "other",
        "other (specify)",
        "other (please specify)",
        "other (free text)",
    }
)

_DEFAULT_ANSWER_WAIT_TIMEOUT_S = 3600.0
_ANSWER_WAIT_POLL_INTERVAL_S = 5.0
# Public alias: the cadence at which the answer-wait lease (heartbeat) is renewed. Callers that
# must keep the lease fresh outside the wait loop (e.g. during a slow on_pause callback) reuse
# this so the renewal cadence and the wait-loop cadence stay in lockstep, well under the answer
# endpoint's staleness window.
ANSWER_WAIT_POLL_INTERVAL_S = _ANSWER_WAIT_POLL_INTERVAL_S

# The exact set of job-service transport failures that ``JobServiceClient._request`` itself
# classifies as transient and retries before giving up (its ``_RETRY_ANY_METHOD_ERRORS`` +
# ``_RETRY_IDEMPOTENT_ONLY_ERRORS``); kept in lockstep with that classification. The wait loop
# extends the same tolerance — when the client exhausts its own retry budget on a blip (e.g. a
# connection reset, or the brief connect failures during a job-service restart), it re-polls
# instead of crashing a long HITL wait. Everything the client does NOT retry is deliberately left
# to propagate rather than be swallowed and spun to the timeout: permanent transport faults
# (``UnsupportedProtocol``, ``LocalProtocolError``, ``ProxyError``) and HTTP status errors.
# ``ConnectTimeout`` is listed explicitly: it is an ``httpx.TimeoutException``, not a
# ``ConnectError`` subclass (MRO: ConnectTimeout -> TimeoutException -> TransportError).
_TRANSIENT_JOB_READ_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)

# Terminal SUCCESS statuses for a coding-team job: the run finished and the outcome is a success —
# a clean completion, a partial success (some tasks failed), or an already-complete no-op (the work
# was already done, no changes needed). Single source of truth so every consumer (the publish-defer
# gate in api/main.py, the all-terminal set below, the resume guard) agrees on which statuses are
# terminal successes, and a future success status is added in exactly one place.
TERMINAL_SUCCESS_STATUSES = frozenset(
    {
        JobStatus.COMPLETED.value,
        JobStatus.COMPLETED_WITH_FAILURES.value,
        JobStatus.ALREADY_COMPLETE.value,
    }
)

# Job statuses that mean "this job will never resume on its own"; the wait loop must stop polling.
# The terminal SUCCESS statuses plus the two terminal FAILURE statuses: ``already_complete`` is a
# terminal success, so a finished already-complete job must not look resumable to is_terminal()
# consumers (including the /resume endpoint).
_TERMINAL_STATUSES = TERMINAL_SUCCESS_STATUSES | frozenset(
    {JobStatus.FAILED.value, JobStatus.CANCELLED.value}
)


def heartbeat_timestamp() -> str:
    """Current UTC ISO-8601 timestamp for an answer-wait liveness heartbeat.

    Postconditions:
        - Returns a timezone-aware ISO-8601 string parseable by ``datetime.fromisoformat``.
    """
    return datetime.now(timezone.utc).isoformat()


def answer_wait_timeout_s() -> float:
    """Wall-clock cap (seconds) for a single HITL pause.

    Postconditions:
        - Returns a positive float; env ``CODING_TEAM_ANSWER_WAIT_TIMEOUT_S`` overrides the
          default; garbage / non-positive values fall back to the default.
    """
    value = parse_float("CODING_TEAM_ANSWER_WAIT_TIMEOUT_S", _DEFAULT_ANSWER_WAIT_TIMEOUT_S)
    return value if value > 0 else _DEFAULT_ANSWER_WAIT_TIMEOUT_S


def _normalize_options(raw: Any) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    # Dedup by lowercased ID (first occurrence wins) so that: (a) IDs that differ only by
    # whitespace after stripping are collapsed, (b) IDs that differ only by case are collapsed —
    # consistent with the case-insensitive comparison in answers_to_resolved.
    seen_ids: set = set()
    for o in raw or []:
        if isinstance(o, dict) and o.get("id"):
            opt_id = str(o.get("id")).strip()
            if not opt_id:
                logger.warning("Option with whitespace-only id will be dropped")
                continue
            if opt_id.lower() == "other":
                # "other" is the reserved synthetic free-text option added by the UI/API;
                # using it as a structured option id would cause the answer handler to treat
                # any selection of this option as a free-text response. Drop it — the prompt
                # already prohibits it, so this is a defensive guard against a non-compliant LLM.
                logger.warning("Option id 'other' is reserved and will be dropped")
                continue
            norm_key = opt_id.lower()
            if norm_key in seen_ids:
                logger.warning(
                    "Duplicate option id '%s' (case-insensitive) will be dropped", opt_id
                )
                continue
            seen_ids.add(norm_key)
            options.append(
                {
                    "id": opt_id,
                    "label": str(o.get("label") or opt_id).strip(),
                    "is_default": bool(o.get("is_default", False)),
                }
            )
    return options


def _filter_generic_options(
    opts: List[Dict[str, Any]], question_text: str = ""
) -> List[Dict[str, Any]]:
    """Remove options whose ID or label matches the generic yes/no/not-sure/other sets.

    Preconditions:
        - ``opts`` is the output of ``_normalize_options`` (IDs are non-empty stripped strings,
          labels are non-empty stripped strings, no reserved "other" IDs present).
    Postconditions:
        - Returns a sublist of ``opts`` with every option matching ``_GENERIC_OPTION_IDS`` or
          ``_GENERIC_OPTION_LABELS`` removed. If any options are removed, a warning is logged.
          Callers decide the minimum-count policy (``normalize_open_questions`` requires ≥ 2;
          ``convert_to_structured_questions`` keeps whatever survives).
    """
    filtered = [
        o
        for o in opts
        if o["id"].lower() not in _GENERIC_OPTION_IDS
        and o["label"].lower() not in _GENERIC_OPTION_LABELS
    ]
    if len(filtered) != len(opts):
        label = question_text[:60] if question_text else "(unknown)"
        logger.warning(
            "Question '%s': %d generic option(s) removed before acceptance check",
            label,
            len(opts) - len(filtered),
        )
    return filtered


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
          ``question_text``, ``options`` (normalized and generic-option-filtered; empty list when
          none survive so the UI falls back to free-text), ``required=True``, and ``source``.
          Generic yes/no/not-sure/other options are removed via ``_filter_generic_options``;
          unlike ``normalize_open_questions`` no minimum count is enforced here. Empty questions
          are dropped.
    """
    structured: List[Dict[str, Any]] = []
    for idx, q in enumerate(questions or []):
        text = _question_text(q).strip()
        if not text:
            continue
        if isinstance(q, dict):
            qid = str(q.get("id") or f"{source}_{idx}_{uuid.uuid4().hex[:8]}")
            options = _filter_generic_options(_normalize_options(q.get("options")), text)
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
        - Returns dicts each carrying ``question_text`` and ``options`` (always present); empties
          are dropped, and any ``context`` / ``options`` an agent supplied are preserved so
          domain-specific choices round-trip. Options that fail normalization (< 2 survive after
          deduplication and generic-option filtering) are discarded and ``options`` is set to ``[]``
          so callers have a uniform contract — the same fallback as ``convert_to_structured_questions``.
          Deduplication is case-insensitive (consistent with ``answers_to_resolved``). A non-list
          input yields ``[]``.
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
            # Deduplication by case-insensitive ID and the "other" guard are handled inside
            # _normalize_options, so opts is already free of duplicates and reserved IDs here.
            # _filter_generic_options culls individual generic options (by ID and by label) so
            # that mixed sets (e.g. yes/no blended with one context-specific option) and variant
            # IDs detected via their display label (e.g. id="opt_yes", label="Yes") are removed
            # before the minimum-count check rather than silently accepted.
            filtered = _filter_generic_options(opts, text)
            if len(filtered) >= 2:
                entry["options"] = filtered
            else:
                if opts:
                    logger.warning(
                        "Question '%s' has only %d context-specific option(s) after filtering; "
                        "falling back to free-text (options discarded)",
                        text[:60],
                        len(filtered),
                    )
                entry["options"] = []
        else:
            entry["options"] = []
        out.append(entry)
    return out


def normalize_key(text: str) -> str:
    """Whitespace/case-normalized comparison key for arbitrary text.

    Used both to match an open question against a resolved answer (by question text) and to
    de-duplicate fully rendered decision lines (``"question → answer"``, including the answer). It
    normalizes the whole string it is given — it makes no assumption about a ``→`` separator or
    about which part of the text it is keying.

    Preconditions:
        - ``text`` is a string or ``None`` (``None`` is treated as the empty string).
    Postconditions:
        - Returns ``text`` lowercased with leading/trailing whitespace stripped and every internal
          whitespace run collapsed to a single space; ``""`` for ``None``/blank input. Pure: no
          side effects, and inputs equal modulo case and whitespace map to the same key.
    """
    return " ".join((text or "").lower().split())


def unanswered_questions(
    open_questions: List[Any], resolved_questions: Optional[List[Dict[str, Any]]]
) -> List[Any]:
    """Return the open questions NOT covered by a resolved answer (fail-closed).

    Coverage is strictly by normalized question text: an open question is covered only when a
    resolved answer carries a matching ``question_text``. We deliberately do **not** treat N
    text-less answers as covering N open questions by raw count — those answers may belong to a
    different pause batch, and guessing coverage by count would let a genuinely-undecided product
    question reach implementation. The conservative failure mode is therefore to re-ask (pause),
    never to proceed on an unmatched question.

    Preconditions:
        - ``open_questions`` entries are strings or dicts with a question text.
    Postconditions:
        - Returns a sublist of ``open_questions`` (in order); empty iff every open question has a
          resolved answer matching it by text. Never returns a question that has a text-matched
          answer, and never returns ``[]`` while an unmatched open question remains.
    """
    if not open_questions:
        return []
    resolved = resolved_questions or []
    answered_keys = {
        normalize_key(r.get("question_text", ""))
        for r in resolved
        if isinstance(r, dict) and r.get("question_text")
    }
    return [q for q in open_questions if normalize_key(_question_text(q)) not in answered_keys]


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
        selected = (a.get("selected_option_id") or a.get("selected_answer") or "").strip()
        other = a.get("other_text") or ""
        label = ""
        for opt in q.get("options") or []:
            if (opt.get("id") or "").lower() == selected.lower():
                label = opt.get("label") or selected
                break
        if (selected.lower() == "other" or not label) and other:
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


def decision_qa(entry: Dict[str, Any]) -> "tuple[str, str]":
    """Extract the (question, answer) display strings from a resolved/submitted decision record.

    Tolerates the several decision shapes the gate produces — resolved records (``question_text`` +
    ``answer``) and raw submitted answers (``selected_option_id`` / ``other_text``) — so every
    renderer derives the same question→answer text from one fallback chain instead of re-spelling it.

    Postconditions:
        - Returns ``(question, answer)`` as strings (either may be empty when the record lacks it).
    """
    question = str(
        entry.get("question_text") or entry.get("question") or entry.get("question_id") or ""
    )
    answer = str(
        entry.get("answer")
        or entry.get("selected_answer")
        or entry.get("other_text")
        or entry.get("selected_option_id")
        or ""
    )
    return question, answer


def render_decision_line(entry: Dict[str, Any]) -> str:
    """Render one resolved/submitted decision record as a single 'question → answer' line.

    Single source of truth for the per-decision rendering that the orchestrator's
    ``_format_decisions`` / ``_user_decisions_for`` and the Tech Lead's ``_render_resolved_questions``
    each used to spell out independently.

    Preconditions:
        - ``entry`` is a decision-record dict (resolved or raw submitted; see ``decision_qa``).
    Postconditions:
        - Returns ``"{question} → {answer}"`` when a question is present (the answer may be empty —
          a question without an answer yields ``"question → "``), the bare answer when only an
          answer is present, and ``""`` when the record carries neither.
    """
    question, answer = decision_qa(entry)
    return f"{question} → {answer}" if question else answer


def resolved_decision_lines(records: Any) -> List[str]:
    """Render a list of decision records into non-empty 'question → answer' lines, in order.

    Single source of truth for the iterate-and-render loop shared by the orchestrator's
    ``_format_decisions`` / ``_user_decisions_for`` and the Tech Lead's ``_render_resolved_questions``.

    Preconditions:
        - ``records`` is an iterable of decision-record dicts (or None); non-dict members and
          records with no renderable content are skipped.
    Postconditions:
        - Returns one non-empty ``render_decision_line`` string per renderable record, preserving
          input order. De-duplication, bulleting, and joining are left to the caller.
    """
    lines: List[str] = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        line = render_decision_line(r)
        if line:
            lines.append(line)
    return lines


def _format_questions_comment(questions: List[Dict[str, Any]], job_id: str) -> str:
    """Render escalated open questions as a single GitHub issue comment.

    Postconditions:
        - Returns markdown listing each question (with context and selectable option ids when
          present) and how to answer it, so a human can unblock the paused job.
    """
    lines = [
        f"⏸️ Coding team job `{job_id}` is **paused for a decision** and will not proceed until "
        f"these are answered. Submit answers to `POST /run/{job_id}/answers`:",
        "",
    ]
    for i, q in enumerate(questions or [], 1):
        lines.append(f"{i}. **{q.get('question_text', '')}**  _(id: `{q.get('id', '')}`)_")
        if q.get("context"):
            lines.append(f"   - _Why:_ {q['context']}")
        opts = q.get("options") or []
        if opts:
            opt_str = ", ".join(f"`{o.get('id')}` ({o.get('label')})" for o in opts)
            lines.append(f"   - Options: {opt_str} (or `other` with free text)")
    return "\n".join(lines)


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
    heartbeat_fn: Optional[Callable[[str], None]] = None,
) -> bool:
    """Block until the job's ``waiting_for_answers`` flag clears, the job goes terminal, or timeout.

    Preconditions:
        - The caller has already set ``waiting_for_answers=True`` (and the pending questions) on the
          job record, so the loop observes the paused state on its first read.
    Postconditions:
        - Returns True iff ``waiting_for_answers`` became False (answers submitted) before the job
          went terminal or the timeout elapsed; returns False on terminal/timeout. Never proceeds on
          its own — the only True path is an explicit answer submission clearing the flag.
        - When ``heartbeat_fn`` is provided, it is invoked with a current UTC ISO timestamp once per
          poll iteration — both while waiting and when a poll read fails transiently — so other
          processes can distinguish a live (but blocked or read-stalled) wait loop from a dead one.
          Heartbeat failures are swallowed — proving liveness must never break the wait itself.
    """
    timeout = timeout_s if timeout_s is not None else answer_wait_timeout_s()
    start = now()

    def _renew_heartbeat() -> None:
        # Prove this wait loop is still alive so observers can tell a live blocked loop apart
        # from a dead one (e.g. after a process crash). Heartbeat failures are swallowed —
        # proving liveness must never break the wait itself.
        if heartbeat_fn is None:
            return
        try:
            heartbeat_fn(heartbeat_timestamp())
        except Exception:
            logger.debug("answer-wait heartbeat write failed for job %s", job_id, exc_info=True)

    while now() - start < timeout:
        try:
            data = get_job_fn(job_id) or {}
        except _TRANSIENT_JOB_READ_ERRORS:
            # A transient job-service transport failure (e.g. a connection reset that
            # outlived the client's own retry budget) must not kill the wait — treat
            # it like a missed poll: log, keep the liveness heartbeat fresh so observers
            # still see this loop as alive, then back off and re-read. The ``timeout`` bound
            # still caps the total wait, so a sustained outage ends the loop the same way a
            # timeout does. Only the transient transport failures the client itself
            # retries are swallowed (see ``_TRANSIENT_JOB_READ_ERRORS``); permanent
            # transport faults (UnsupportedProtocol, LocalProtocolError), HTTP status
            # errors, and programming bugs (TypeError, etc.) propagate.
            logger.warning(
                "answer-wait job read failed for job %s; retrying after poll interval",
                job_id,
                exc_info=True,
            )
            _renew_heartbeat()
            sleep(poll_interval_s)
            continue
        if not data.get("waiting_for_answers", False):
            return True
        if is_terminal(data):
            return False
        _renew_heartbeat()
        sleep(poll_interval_s)
    logger.warning("Coding team job %s timed out waiting for user answers", job_id)
    return False
