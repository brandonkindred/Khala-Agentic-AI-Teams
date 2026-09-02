"""Temporal-durable answer-callback primitive for Planning clarification questions.

Provides the signal + ``wait_condition`` mechanism a Temporal workflow needs to
pause durably (surviving worker restarts) until a human answers a Planning
clarification question, plus an adapter that presents Planning's existing
``answer_callback: Callable[[list], list]`` contract (the same shape
``software_engineering_team.orchestrator._build_planning_answer_callback``
already satisfies for thread mode) without Planning's own code needing to
know it is running under Temporal.

Modeled directly on the coding team's ``submit_answers`` signal
(``software_engineering_team/temporal/coding_team_workflow.py``) and its
``_ActivityPauseSignal`` activity-side pause exception
(``software_engineering_team/pause_cycle.py``). Full contract/rationale in
``system_design/planning_hitl_temporal_contract.md``.

This module deliberately stops short of wiring into a concrete workflow or
activity (``planning_team/temporal/activities.py``) — that is separate,
follow-on work. ``PlanningAnswerSignalMixin`` is a plain mixin any
``@workflow.defn`` class can inherit; nothing here assumes which workflow
class will use it.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from temporalio import workflow

from planning_team.exceptions import PlanningAnswerPauseSignal

__all__ = [
    "SUBMIT_PLANNING_ANSWERS_SIGNAL",
    "PlanningAnswerPauseSignal",
    "build_temporal_planning_answer_callback",
    "PlanningAnswerSignalMixin",
]

# Wire shape fixed by system_design/planning_hitl_temporal_contract.md.
SUBMIT_PLANNING_ANSWERS_SIGNAL = "submit_planning_answers"

# PlanningAnswerPauseSignal itself now lives in planning_team.exceptions (no Temporal
# dependency), so planning_team.orchestrator can catch it without importing this
# subpackage. Re-exported here (imported above) since this module raises it and is
# where existing callers already look for it.


def build_temporal_planning_answer_callback(
    resume_token: str,
    submitted_answers: Optional[List[Dict[str, Any]]] = None,
    next_resume_token: Optional[Callable[[], str]] = None,
    asked_question_ids: Optional[List[str]] = None,
) -> Callable[[list], list]:
    """Build a ``Callable[[list], list]`` satisfying Planning's ``answer_callback``
    contract (``planning_team.orchestrator.resolve_pra_answers``), backed by the
    durable signal-wait mechanism instead of thread-mode's blocking poll loop.

    Preconditions:
        - ``resume_token`` is a non-empty str uniquely identifying this pause
          round (the same token a workflow will arm via
          ``PlanningAnswerSignalMixin.wait_for_planning_answers``).
        - ``submitted_answers``, when not ``None``, is the exact list already
          resolved for this ``resume_token`` (e.g. via a validated
          ``submit_planning_answers`` signal) — dicts shaped
          ``{"question_id": ..., "selected_option_id": ...}``, matching what
          thread-mode's ``_build_planning_answer_callback`` already returns.
        - ``next_resume_token``, when given, mints a FRESH token per call (see
          ``pause_cycle.mint_resume_token``: a token is unique per pause round
          and never reused), for the case below where a resumed run has to
          pause again. Omitted, a re-pause reuses ``resume_token``, which is
          still preferable to answering a batch silently but leaves the two
          rounds sharing one token.
        - ``asked_question_ids``, when given, is every question id already
          PRESENTED to a submitter across this run's pause rounds (the
          workflow accumulates it from each pause's ``pending_questions``).
          Omitted, it is empty, so any non-empty batch pauses -- correct for
          the first round, which is the only round a caller that does not
          track it can be in.
    Postconditions:
        - Returns a callable ``cb(questions) -> list``.
        - When ``submitted_answers`` is ``None``: calling ``cb`` never returns —
          it raises ``PlanningAnswerPauseSignal(resume_token, questions)``,
          carrying the exact ``questions`` passed in verbatim as
          ``pending_questions`` for a caller to persist/relay.
        - When ``submitted_answers`` is provided: calling ``cb`` returns the
          subset of ``submitted_answers`` whose ``question_id`` matches one of
          ``questions``' ``id`` values, preserving ``submitted_answers``'
          order. Never fabricates an answer for a question with no matching
          entry, and never returns a default — a question with no matching
          submitted answer is simply absent from the result.
        - Exception: a batch containing any question NOT in
          ``asked_question_ids`` raises ``PlanningAnswerPauseSignal`` rather
          than answering it. Such a question has never been put to a
          submitter, and letting Planning proceed on it is the silent
          auto-answer both modes exist to prevent.
        - A batch every question of which has already been asked resolves with
          whatever the submitter gave for it — including ``[]``. That is the
          difference between "declined" and "never seen", and it is what makes
          this terminate: an empty or partial submission proceeds on the
          submitter's choice rather than being re-asked on every replay. Such a
          batch is not the one these answers were submitted for (Planning
          re-runs from scratch on resume, and its questions are LLM-driven, so
          a re-run can re-identify them or open a further round), and
          answering it with nothing is exactly the silent auto-answer both
          modes exist to prevent — thread mode re-pauses on every batch. A
          batch where SOME answer matches is treated as that same batch,
          partially answered, and returns the matches: re-pausing there would
          re-ask questions the user has already answered, with nothing new to
          add on the second pass. A non-dict entry
          in ``submitted_answers`` (a malformed signal's ``answers`` list is
          validated as a list, not as a list-of-dicts) is skipped rather than
          raising — fails closed instead of an ``AttributeError`` surfacing
          from a resumed activity. Matching requires both ``id``/``question_id``
          to be ``str`` (the codebase's own convention for these fields,
          e.g. ``resolve_pra_answers``) rather than merely hashable — a
          malformed signal could otherwise supply an unhashable
          ``question_id`` (e.g. a list) and crash the set-membership test
          instead of simply never matching.
    """
    assert isinstance(resume_token, str) and resume_token, (
        "build_temporal_planning_answer_callback requires a non-empty resume_token"
    )

    if submitted_answers is None:

        def _pause_cb(questions: list) -> list:
            raise PlanningAnswerPauseSignal(resume_token, list(questions))

        return _pause_cb

    resolved = list(submitted_answers)
    already_asked = {qid for qid in (asked_question_ids or []) if isinstance(qid, str)}

    def _resolved_cb(questions: list) -> list:
        question_ids = {
            q.get("id") for q in questions if isinstance(q, dict) and isinstance(q.get("id"), str)
        }
        # Only well-formed entries can be "unasked": a malformed one (non-dict,
        # or an id that is not a str) carries no question a submitter could
        # ever answer, so pausing on it would re-ask something unanswerable on
        # every replay. It is skipped here and never matches below either --
        # failing closed on the answer, not on the loop.
        unasked = [
            q
            for q in questions
            if isinstance(q, dict) and isinstance(q.get("id"), str) and q["id"] not in already_asked
        ]
        if unasked:
            # At least one question here has never been put to anyone. Planning
            # replays from scratch on resume and its questions are LLM-driven,
            # so a re-run can open a further clarification round or
            # re-identify what it asks. Answering such a batch with whatever
            # happens to match would let Planning proceed on a question nobody
            # has seen — the silent auto-answer both modes exist to prevent.
            # Pause instead, exactly as the first round did.
            #
            # Deciding this on what was ASKED, not on what was answered, is
            # what keeps it terminating: a batch the submitter has already seen
            # resolves with whatever they gave (including nothing), so an empty
            # or partial submission proceeds on their choice instead of being
            # re-asked forever.
            raise PlanningAnswerPauseSignal(
                next_resume_token() if next_resume_token is not None else resume_token,
                list(questions),
            )
        return [
            a
            for a in resolved
            if isinstance(a, dict)
            and isinstance(a.get("question_id"), str)
            and a.get("question_id") in question_ids
        ]

    return _resolved_cb


class PlanningAnswerSignalMixin:
    """Mixin giving a Temporal workflow class durable pause/resume capability for
    Planning clarification questions, via the ``submit_planning_answers`` signal.

    Invariants:
        - ``self._active_resume_token`` is non-None only while this workflow is
          waiting on a pause it has armed (between
          ``wait_for_planning_answers`` being called for a token and that same
          call returning) — so ``submit_planning_answers`` can tell a fresh
          submission for the CURRENT pause apart from a stale one for an
          already-resolved pause.
        - ``self._submitted_answers`` is non-None only in the narrow window
          between a validated ``submit_planning_answers`` signal being
          delivered and ``wait_for_planning_answers`` consuming it (which
          resets it to ``None`` before returning) — so a stale answer batch
          from one pause round can never be mistaken for a fresh one in the
          next.
        - ``self._buffered_signals`` holds at most one early-arrived answer
          batch per not-yet-armed ``resume_token``. The moment
          ``wait_for_planning_answers`` arms a token it applies the matching
          buffered entry (if any) and clears the entire dict, so stale keys
          cannot accumulate across pause rounds in durable workflow state.
    """

    def __init__(self) -> None:
        super().__init__()
        self._active_resume_token: Optional[str] = None
        self._submitted_answers: Optional[List[Dict[str, Any]]] = None
        self._buffered_signals: Dict[str, List[Dict[str, Any]]] = {}

    @workflow.signal(name=SUBMIT_PLANNING_ANSWERS_SIGNAL)
    def submit_planning_answers(self, payload: Any) -> None:
        """Deliver a human answer batch for the current (or next) pause.

        Preconditions:
            - None enforced — ``payload`` arrives from outside the workflow, so
              this handler validates its shape defensively rather than trusting
              a precondition an external, unvalidated signal cannot guarantee.
              A well-formed payload is a dict shaped
              ``{"resume_token": str, "answers": list}``, per
              ``system_design/planning_hitl_temporal_contract.md``. The
              parameter is typed ``Any``, not ``Dict[str, Any]``, deliberately:
              Temporal's data converter type-checks a signal argument against
              its annotation *before* the handler body runs, so a ``Dict``
              annotation would raise ``TypeError`` for a non-dict payload
              during argument conversion — never reaching the ``isinstance``
              guard below — and an unhandled exception here fails the
              workflow task and, since Temporal replays history, would fail
              identically on every future replay, permanently stranding the
              workflow.
        Postconditions:
            - Any payload that is not a dict, or a dict without a list
              ``"answers"`` value, is ignored (returns without side effects).
            - When no pause is currently active
              (``self._active_resume_token is None``), a well-formed payload is
              treated as an early arrival for a pause not yet armed: a
              non-empty string ``resume_token`` is buffered in
              ``self._buffered_signals``, keyed by that token (first
              submission per token wins — an already-buffered token is left
              alone). A payload with no usable ``resume_token`` while no pause
              is active has nothing to key a buffer entry on and is dropped.
            - Otherwise, validates ``payload.get("resume_token")`` against
              ``self._active_resume_token``: a mismatch is ignored, not
              applied; once a batch is accepted for the current token, a
              second matching-token signal (a double-submit, or two clients
              racing) is ignored too — first submission per token wins. Only a
              token-matching first submission with a list ``"answers"`` sets
              ``self._submitted_answers`` to that list, satisfying a
              ``wait_condition`` predicate of
              ``self._submitted_answers is not None``.
        """
        if not isinstance(payload, dict):
            return
        answers = payload.get("answers")
        if not isinstance(answers, list):
            return
        resume_token = payload.get("resume_token")
        if self._active_resume_token is None:
            if isinstance(resume_token, str) and resume_token:
                self._buffered_signals.setdefault(resume_token, answers)
            return
        if resume_token != self._active_resume_token:
            return
        if self._submitted_answers is not None:
            return
        self._submitted_answers = answers

    async def wait_for_planning_answers(self, resume_token: str) -> List[Dict[str, Any]]:
        """Durably suspend this workflow until a matching ``submit_planning_answers``
        signal is delivered, then return the answers.

        Preconditions:
            - ``resume_token`` is a non-empty str identifying the pause round
              (must match what a caller persisted/relayed alongside a
              ``PlanningAnswerPauseSignal``'s ``resume_token``).
            - Only called from within ``@workflow.defn`` code (uses
              ``workflow.wait_condition``, which is only valid there).
        Postconditions:
            - Applies any signal already buffered for ``resume_token`` (a
              signal that arrived before this call armed the wait) and clears
              ``self._buffered_signals`` entirely — no stale buffered entry for
              a different token can leak into a later pause round.
            - Suspends (durably — this ``await`` survives a worker restart)
              until ``self._submitted_answers is not None``, i.e. until a
              validated, token-matching ``submit_planning_answers`` signal
              lands. There is no timeout and no default path: this method
              never returns without a real signal.
            - Returns the delivered answers list and resets
              ``self._active_resume_token``/``self._submitted_answers`` to
              ``None`` before returning, so a later pause round starts clean.
        """
        assert isinstance(resume_token, str) and resume_token, (
            "wait_for_planning_answers requires a non-empty resume_token"
        )
        self._active_resume_token = resume_token
        self._submitted_answers = self._buffered_signals.pop(resume_token, None)
        self._buffered_signals.clear()
        await workflow.wait_condition(lambda: self._submitted_answers is not None)
        answers = self._submitted_answers
        self._submitted_answers = None
        self._active_resume_token = None
        assert answers is not None
        return answers
