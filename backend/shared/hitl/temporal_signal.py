"""Shared Temporal signal-handler primitive for durable HITL answer delivery.

Provides the receiving half of the durable human-in-the-loop mechanism: a
``@workflow.signal``-decorated ``submit_answers`` handler plus the buffer/
reject/accept state machine backing it, as a composable mixin any
``@workflow.defn`` class can inherit. This is the signal name and payload
envelope the coding team's ``CodingTeamWorkflow`` already uses in production
(``software_engineering_team/temporal/coding_team_workflow.py``), extracted
here so ``PlanningWorkflow`` can register the identical contract without a
third bespoke copy of its own.

Not yet a full convergence: ``CodingTeamWorkflow`` still carries its own
inline copy of this same state machine, and
``planning_team.temporal.answer_signal.PlanningAnswerSignalMixin`` (signal
name ``submit_planning_answers``, predating this contract, still wired into
``RunTeamWorkflowV2``) remains live and untouched. Migrating
``CodingTeamWorkflow`` onto this mixin (with a ``workflow.patched`` gate
protecting its pre-existing history) and reconciling
``PlanningAnswerSignalMixin`` are deliberately deferred, not implicitly
completed by this module's existence — both are tracked as follow-up work.

Deliberately excludes any ``wait_condition``-based wait/resume logic — this
module only registers and validates; a workflow durably pausing on
``self._submitted_answers`` is separate, follow-on work. A signal handler
must never raise (Temporal replays workflow history, so an unhandled
exception here would fail identically forever), so every validation failure
here is a silent, side-effect-free rejection: the payload is dropped and the
workflow's paused state is left exactly as it was.

Preconditions:
    - ``backend/agents`` and ``backend`` are on ``sys.path`` (the ``shared_*``
      convention).
Postconditions:
    - Importing has no side effects beyond class/function definition; no I/O,
      no workflow execution.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import ValidationError
from temporalio import workflow

from shared.hitl.models import AnswerSubmission

__all__ = [
    "SUBMIT_ANSWERS_SIGNAL",
    "MAX_BUFFERED_SIGNALS",
    "HitlAnswerSignalMixin",
]

#: Reused verbatim from ``CodingTeamWorkflow.submit_answers`` — signal names are
#: scoped per workflow type, so there is no collision risk in sharing the name
#: across ``CodingTeamWorkflow``/``PlanningWorkflow``, and a single name keeps one
#: shared vocabulary for any future workflow that hosts both kinds of gate.
SUBMIT_ANSWERS_SIGNAL = "submit_answers"

#: Upper bound on distinct not-yet-armed ``resume_token``s ``_buffered_signals``
#: retains. An unbounded buffer lets an adversarial or misbehaving sender grow
#: durable workflow state without limit merely by sending signals with fresh,
#: never-armed tokens. Small and fixed: a workflow only ever has one pause
#: armed (or about to be armed) at a time, so more than a handful of
#: early-arrived, still-unclaimed batches is already anomalous.
MAX_BUFFERED_SIGNALS = 8


def _validate_answer_batch(raw: Any) -> Optional[List[Dict[str, Any]]]:
    """Validate a signal payload's ``answers`` value against ``AnswerSubmission``.

    Preconditions:
        - None — ``raw`` is untrusted, signal-delivered data of arbitrary shape.
    Postconditions:
        - Returns ``None`` (never raises, for any input) if ``raw`` is not a
          non-empty list, or any element is not a dict with all-``str`` keys,
          or any element fails ``AnswerSubmission`` validation — the whole
          batch is rejected on a single bad entry rather than silently
          dropping just that entry, so a resume can never proceed with a
          partially-validated answer set. An empty list is rejected too:
          there is no content to apply, and accepting it would let a caller
          mistake "submitted, vacuously" for "not yet submitted" if it ever
          tests ``_submitted_answers`` for truthiness instead of ``is not
          None``.
        - Otherwise returns a new, non-empty list of plain dicts, one per
          input element, each normalized through ``AnswerSubmission`` (so
          every dict carries the schema's full field set, e.g. an omitted
          ``other_text`` becomes an explicit ``None``).
    """
    if not isinstance(raw, list) or not raw:
        return None
    validated: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            return None
        try:
            answer = AnswerSubmission(**item)
        except ValidationError:
            return None
        validated.append(answer.model_dump())
    return validated


class HitlAnswerSignalMixin:
    """Mixin giving a Temporal workflow class the ``submit_answers`` signal and
    its backing buffer/reject/accept state machine.

    Invariants:
        - ``self._active_resume_token`` is non-``None`` only while a subclass
          has armed a pause for that token (arming/consuming is this mixin's
          caller's responsibility — not provided here, see module docstring)
          — so ``submit_answers`` can tell a fresh submission for the CURRENT
          pause apart from a stale one for an already-resolved pause.
        - ``self._submitted_answers`` is non-``None`` only in the narrow
          window between a validated, token-matching ``submit_answers``
          signal being delivered and a caller consuming it — so a stale
          answer batch from one pause round can never be mistaken for a
          fresh one in the next.
        - ``self._buffered_signals`` holds at most one early-arrived, validated
          answer batch per not-yet-armed ``resume_token``, and never more than
          ``MAX_BUFFERED_SIGNALS`` distinct tokens at once — the oldest entry
          (by arrival order) is evicted before a new one is buffered past the
          cap, so durable workflow state cannot grow without bound.

    Requirements on adopters:
        - A workflow class using this mixin must ensure
          ``HitlAnswerSignalMixin.__init__`` runs — define no ``__init__`` of
          its own, or chain ``super().__init__()`` if it does — so the buffer
          state attributes exist before any signal is delivered. Skipping
          this makes the first delivered signal raise ``AttributeError``
          inside the handler, the permanent-strand failure mode this module
          exists to prevent.
        - A step-2 consumer waiting on this state (e.g. a
          ``workflow.wait_condition`` predicate) MUST test
          ``self._submitted_answers is not None``, never truthiness: that is
          the only reliable "has a signal landed" test. It happens to be
          moot today only because ``submit_answers`` never stores an empty
          list (an empty ``"answers"`` batch is rejected as malformed, see
          :func:`_validate_answer_batch`) — but ``is not None`` remains the
          contractually correct test, not an accident of today's validation
          choice.
    """

    def __init__(self) -> None:
        super().__init__()
        self._active_resume_token: Optional[str] = None
        self._submitted_answers: Optional[List[Dict[str, Any]]] = None
        self._buffered_signals: Dict[str, List[Dict[str, Any]]] = {}

    @workflow.signal(name=SUBMIT_ANSWERS_SIGNAL)
    def submit_answers(self, payload: Any) -> None:
        """Deliver a human answer batch for the current (or a not-yet-armed) pause.

        Preconditions:
            - None enforced — ``payload`` arrives from outside the workflow, so
              this handler validates its shape defensively rather than trusting
              a precondition an external, unvalidated signal cannot guarantee.
              A well-formed payload is a dict shaped
              ``{"resume_token": str, "answers": list}``, each ``answers``
              element ``AnswerSubmission``-shaped. The parameter is typed
              ``Any``, not ``Dict[str, Any]``, deliberately: Temporal's data
              converter type-checks a signal argument against its annotation
              *before* the handler body runs, so a ``Dict`` annotation would
              raise ``TypeError`` for a non-dict wire payload during argument
              conversion — never reaching the checks below — and an unhandled
              exception here fails the workflow task and, since Temporal
              replays history, would fail identically on every future replay,
              permanently stranding the workflow.
        Postconditions:
            - A payload that is not a dict, or whose ``"answers"`` value fails
              :func:`_validate_answer_batch` (missing, not a list, or any
              element malformed), is ignored: returns without any side
              effect, leaving the workflow's paused state exactly as it was
              (fails closed rather than resuming with partial content).
            - When no pause is currently active
              (``self._active_resume_token is None``), a well-formed payload is
              treated as an early arrival for a pause not yet armed: a
              non-empty string ``resume_token`` is buffered in
              ``self._buffered_signals``, keyed by that token (first
              submission per token wins — an already-buffered token is left
              alone; buffering past ``MAX_BUFFERED_SIGNALS`` evicts the oldest
              entry first). A payload with no usable ``resume_token`` while no
              pause is active has nothing to key a buffer entry on and is
              dropped.
            - Otherwise, validates ``payload.get("resume_token")`` against
              ``self._active_resume_token``: a mismatch is ignored, not
              applied — this is the out-of-order rejection: a signal that
              arrives for a pause that is not the one currently pending is
              never applied to it. Once a batch is accepted for the current
              token, a second matching-token signal (a double-submit, or two
              clients racing) is ignored too — first submission per token
              wins. Only a token-matching first submission with a valid
              ``"answers"`` batch sets ``self._submitted_answers``.
        """
        if not isinstance(payload, dict):
            return
        answers = _validate_answer_batch(payload.get("answers"))
        if answers is None:
            return
        resume_token = payload.get("resume_token")
        if self._active_resume_token is None:
            if isinstance(resume_token, str) and resume_token:
                if resume_token not in self._buffered_signals and len(self._buffered_signals) >= MAX_BUFFERED_SIGNALS:
                    oldest_token = next(iter(self._buffered_signals))
                    del self._buffered_signals[oldest_token]
                self._buffered_signals.setdefault(resume_token, answers)
            return
        if resume_token != self._active_resume_token:
            return
        if self._submitted_answers is not None:
            return
        self._submitted_answers = answers
