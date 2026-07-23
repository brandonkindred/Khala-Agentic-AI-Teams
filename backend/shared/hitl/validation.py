"""Answer-submission validation for pending HITL questions.

The reconciled, strictest union of the rule sets both teams historically carried:
coding_team's ``_validate_answers`` already implemented every rule here; SE's
inline route validation was missing the corrupted-record guard and the
duplicate-answer rejection. This is the single owner of that logic.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import HTTPException

from shared.hitl.models import SubmitAnswersRequest


def validate_answers(data: Dict[str, Any], request: SubmitAnswersRequest) -> List[Dict[str, Any]]:
    """Validate submitted answers against a job's pending questions; return them as plain dicts.

    Preconditions:
        - ``data`` is the job record; a valid submission requires it to be
          ``waiting_for_answers`` with a non-empty, well-formed ``pending_questions``
          list (each entry a dict carrying an ``id``).
        - ``request`` is the parsed :class:`SubmitAnswersRequest`.
    Postconditions:
        - Raises ``HTTPException`` 400 if the job is not waiting, has no pending
          questions, any required question is unanswered, two answers target the
          same question, an answer references an unknown question, or an answer is
          not a decision ('other' selected without non-blank text, a non-'other'
          option the question never offered, or neither an option nor text).
        - Raises ``HTTPException`` **500** if a pending question is missing its
          ``id`` — that is a corrupted server-side record, not bad client input,
          so it is surfaced as a controlled server error rather than a bare
          ``KeyError``.
        - Otherwise returns the answers as dicts ready for the job store, each
          carrying the ``question_text`` of the pending question it answers (so a
          later resume can match answers to re-asked questions by text). Each dict
          is ``{question_id, question_text, selected_option_id, other_text}`` with
          ``other_text`` returned raw (un-stripped).
    """
    if not data.get("waiting_for_answers"):
        raise HTTPException(status_code=400, detail="Job is not waiting for answers.")
    pending = data.get("pending_questions", [])
    if not pending:
        raise HTTPException(status_code=400, detail="No pending questions to answer.")
    # A pending question that is not a dict, or is a dict without an "id", is a corrupted job record
    # (the orchestrator always stamps a dict with an id), not bad client input — surface it as a
    # controlled 500 instead of a bare TypeError/KeyError so the failure is attributed to the server
    # and carries a clear message. The isinstance guard must come first: `"id" not in q` on a
    # non-dict (str/int/None) would itself raise before the id check ran.
    if any(not isinstance(q, dict) or "id" not in q for q in pending):
        raise HTTPException(status_code=500, detail="Corrupted job record: pending question missing 'id'.")
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
        raise HTTPException(status_code=400, detail=f"Unknown question IDs: {', '.join(sorted(unknown))}")
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
    # Persist the question text alongside each answer: a team's resume hydration and HITL coverage
    # check match strictly by question text, so answers stored without it would be discarded — and
    # the question re-asked — on any resume after the original thread died.
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
