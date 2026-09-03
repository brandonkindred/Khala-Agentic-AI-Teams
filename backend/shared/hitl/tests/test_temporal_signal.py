"""Unit tests for shared.hitl.temporal_signal -- the shared ``submit_answers``
Temporal signal handler + buffer/reject/accept state machine.

Drives ``HitlAnswerSignalMixin`` directly as a plain object (no Temporal
server), the same lightweight pattern
``planning_team/tests/test_temporal_answer_signal.py`` and
``software_engineering_team/tests/test_coding_team_temporal_workflow.py`` use
for the sibling implementations this module was extracted from.

**This is the canonical suite for the mixin's standalone behavioral
contract.** ``software_engineering_team/tests/test_shared_infra_gap_coverage.py``
mirrors these same cases (see that module's docstring) purely because it's
the only one of the two CI actually collects today -- update THIS suite
first when the mixin's contract changes, then mirror the change there.
"""

from __future__ import annotations

import logging
import typing

import pytest
from temporalio.converter import value_to_type

import shared.hitl.temporal_signal as temporal_signal_module
from shared.hitl.temporal_signal import (
    _OWNED_STATE_ATTRS,
    MAX_BUFFERED_SIGNALS,
    SUBMIT_ANSWERS_SIGNAL,
    HitlAnswerSignalMixin,
)


class _Workflow(HitlAnswerSignalMixin):
    """Minimal stand-in for a real ``@workflow.defn`` class mixing this in."""


class _PriorMixinOwningTheSameAttribute:
    """Stand-in for a sibling signal mixin (e.g. PlanningAnswerSignalMixin) that
    also chains super().__init__() and owns one of the same attribute names."""

    def __init__(self) -> None:
        super().__init__()
        self._active_resume_token = None


def test_init_raises_if_a_prior_mixin_already_owns_the_same_state() -> None:
    """Composing HitlAnswerSignalMixin with another mixin that owns the same
    private attribute names (forbidden per the module docstring) must fail
    loudly at construction time -- silently overwriting the sibling's state
    would alias its signal contract onto this one instead."""

    class _Both(HitlAnswerSignalMixin, _PriorMixinOwningTheSameAttribute):
        pass

    with pytest.raises(TypeError, match="_active_resume_token"):
        _Both()


def test_init_assigns_exactly_the_owned_state_attrs() -> None:
    """Pins the guarantee _OWNED_STATE_ATTRS exists for: the attributes __init__
    actually assigns must exactly match the tuple the composition guard checks,
    so a future attribute added to one but not the other can't silently escape
    the cross-mixin conflict check."""
    wf = _Workflow()

    assert set(wf.__dict__.keys()) == set(_OWNED_STATE_ATTRS)


def _answer(question_id: str = "q1", **overrides) -> dict:
    payload = {"question_id": question_id, "selected_option_id": None, "other_text": None}
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# signal name
# --------------------------------------------------------------------------


def test_signal_name_is_submit_answers() -> None:
    """Reused verbatim from CodingTeamWorkflow -- SPEC-024 mandates the same
    name, not a Planning-specific one."""
    assert SUBMIT_ANSWERS_SIGNAL == "submit_answers"


# --------------------------------------------------------------------------
# submit_answers -- malformed payload rejection (fails closed)
# --------------------------------------------------------------------------


def test_submit_answers_ignores_non_dict_payload() -> None:
    """A non-dict payload (str, list, None, ...) must be dropped, never applied
    or buffered -- fail closed on any shape the validator cannot interpret."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers("not-a-dict")

    assert wf._submitted_answers is None


def test_submit_answers_tolerates_zero_argument_delivery() -> None:
    """Temporal invokes a signal handler as handler.fn(*decoded_args) -- a
    zero-arg delivery (e.g. an empty-args signal, or a forwarding shim that
    drops an empty payload) must bind the ``payload: Any = None`` default and
    fall through to the non-dict rejection rather than raising TypeError for
    a missing required argument, which would permanently strand the workflow
    on replay."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers()

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {}


def test_submit_answers_ignores_non_list_answers() -> None:
    """An 'answers' value that is not a list must reject the batch rather than
    be iterated or coerced."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": "nope"})

    assert wf._submitted_answers is None


def test_submit_answers_ignores_payload_missing_answers_key() -> None:
    """A payload without the 'answers' key has nothing to validate -- drop it
    rather than treat absence as an empty, acceptable batch."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1"})

    assert wf._submitted_answers is None


def test_submit_answers_rejects_whole_batch_on_one_malformed_answer_entry() -> None:
    """A malformed payload accepted as an answer would resume the workflow with
    fabricated content -- one bad entry must reject the entire batch, not just
    be skipped, so a resume can never proceed with a partially-validated set."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers(
        {
            "resume_token": "tok-1",
            "answers": [_answer("q1"), {"selected_option_id": "missing-question-id"}],
        }
    )

    assert wf._submitted_answers is None


def test_submit_answers_rejects_non_dict_answer_entry() -> None:
    """A non-dict entry inside 'answers' cannot be unpacked into
    AnswerSubmission -- reject the whole batch before any entry is applied."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": ["not-a-dict"]})

    assert wf._submitted_answers is None


def test_submit_answers_rejects_malformed_batch_with_no_active_pause() -> None:
    """Payload validation runs before the buffering branch: a malformed batch
    arriving while no pause is active must be dropped, not buffered as
    garbage a later wait_for_planning_answers-style consumer would apply."""
    wf = _Workflow()

    wf.submit_answers({"resume_token": "future-tok", "answers": [{"selected_option_id": "no-question-id"}]})

    assert wf._buffered_signals == {}
    assert wf._submitted_answers is None


def test_submit_answers_rejects_answer_entry_with_non_string_keys() -> None:
    """A dict answer entry with a non-str key would raise TypeError from
    AnswerSubmission(**item) if unpacked directly -- must be rejected before
    that, not let the exception escape the handler."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": [{1: "x", "question_id": "q1"}]})

    assert wf._submitted_answers is None


def test_submit_answers_rejects_answer_entry_with_unrecognized_key() -> None:
    """An unrecognized key (e.g. a misspelled field name) must reject the
    whole batch rather than silently succeed with the typo'd content
    dropped -- pydantic's default model_dump() discards unknown fields,
    which would otherwise let a sender's typo pass as a "successful"
    submission missing its actual content."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": [{"question_id": "q1", "other_txt": "typo"}]})

    assert wf._submitted_answers is None


def test_submit_answers_rejects_empty_answers_list() -> None:
    """An empty batch has no content to apply -- accepting it would let a
    caller mistake 'submitted, vacuously' for 'not yet submitted' if it ever
    tests _submitted_answers for truthiness."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": []})

    assert wf._submitted_answers is None


# --------------------------------------------------------------------------
# submit_answers -- accept path
# --------------------------------------------------------------------------


def test_submit_answers_sets_state_when_pause_active() -> None:
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q1", selected_option_id="yes")]})

    assert wf._submitted_answers == [_answer("q1", selected_option_id="yes")]
    assert wf._buffered_signals == {}


def test_submit_answers_normalizes_answer_shape_through_schema() -> None:
    """A minimal, schema-valid answer (only question_id) is normalized to the
    full AnswerSubmission field set."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": [{"question_id": "q1"}]})

    assert wf._submitted_answers == [_answer("q1")]
    assert wf._buffered_signals == {}


# --------------------------------------------------------------------------
# submit_answers -- out-of-order rejection
# --------------------------------------------------------------------------


def test_submit_answers_ignores_mismatched_resume_token() -> None:
    """A submission for a pause that is not the one currently pending must not
    be applied to it -- token validation defends against a retried/duplicate
    signal resolving the wrong pause."""
    wf = _Workflow()
    wf._active_resume_token = "current-token"

    wf.submit_answers({"resume_token": "stale-token", "answers": [_answer("q1")]})

    assert wf._submitted_answers is None


def test_submit_answers_does_not_buffer_mismatched_token_while_a_pause_is_active() -> None:
    wf = _Workflow()
    wf._active_resume_token = "current-token"

    wf.submit_answers({"resume_token": "other-token", "answers": [_answer("q1")]})

    assert wf._buffered_signals == {}
    assert wf._submitted_answers is None


def test_submit_answers_ignores_second_submission_for_same_token() -> None:
    """A double-submit (or two clients racing to answer the same pause) must not
    overwrite the first accepted batch -- first submission per token wins."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"
    first = [_answer("q1", selected_option_id="yes")]
    wf.submit_answers({"resume_token": "tok-1", "answers": first})

    wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q1", selected_option_id="no")]})

    assert wf._submitted_answers == first
    assert wf._buffered_signals == {}


# --------------------------------------------------------------------------
# submit_answers -- early-arrival buffering
# --------------------------------------------------------------------------


def test_submit_answers_buffers_signal_with_no_active_pause() -> None:
    wf = _Workflow()

    wf.submit_answers({"resume_token": "future-tok", "answers": [_answer("q1")]})

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {"future-tok": [_answer("q1")]}


def test_submit_answers_drops_early_signal_with_no_usable_resume_token() -> None:
    wf = _Workflow()

    wf.submit_answers({"resume_token": "", "answers": [_answer("q1")]})
    wf.submit_answers({"answers": [_answer("q1")]})

    assert wf._buffered_signals == {}


def test_submit_answers_early_buffering_first_submission_per_token_wins() -> None:
    wf = _Workflow()
    first = [_answer("q1")]

    wf.submit_answers({"resume_token": "tok-1", "answers": first})
    wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q2")]})

    assert wf._buffered_signals == {"tok-1": first}


def test_submit_answers_buffer_evicts_oldest_token_past_cap() -> None:
    """Durable workflow state cannot grow without bound: buffering past
    MAX_BUFFERED_SIGNALS distinct tokens evicts the oldest one first."""
    wf = _Workflow()

    for i in range(MAX_BUFFERED_SIGNALS):
        wf.submit_answers({"resume_token": f"tok-{i}", "answers": [_answer("q1")]})
    assert len(wf._buffered_signals) == MAX_BUFFERED_SIGNALS
    assert "tok-0" in wf._buffered_signals

    wf.submit_answers({"resume_token": "tok-overflow", "answers": [_answer("q1")]})

    assert len(wf._buffered_signals) == MAX_BUFFERED_SIGNALS
    assert "tok-0" not in wf._buffered_signals
    assert "tok-overflow" in wf._buffered_signals
    assert "tok-1" in wf._buffered_signals


def test_submit_answers_buffering_an_already_present_token_does_not_evict() -> None:
    """Re-signaling an already-buffered token is a no-op (first-writer-wins) and
    must not itself count toward/trigger cap eviction."""
    wf = _Workflow()
    for i in range(MAX_BUFFERED_SIGNALS):
        wf.submit_answers({"resume_token": f"tok-{i}", "answers": [_answer("q1")]})

    wf.submit_answers({"resume_token": "tok-0", "answers": [_answer("q2")]})

    assert len(wf._buffered_signals) == MAX_BUFFERED_SIGNALS
    assert wf._buffered_signals["tok-0"] == [_answer("q1")]


def test_submit_answers_does_not_raise_if_max_buffered_signals_is_ever_zero(monkeypatch) -> None:
    """Defensive: if MAX_BUFFERED_SIGNALS were ever 0 (or negative), the eviction
    guard's len(...) >= MAX_BUFFERED_SIGNALS check would be true against an
    empty buffer -- next(iter(self._buffered_signals)) must not then raise
    StopIteration, which would violate the handler's never-raise contract.
    Unreachable with today's positive constant; pins the hardening."""
    monkeypatch.setattr(temporal_signal_module, "MAX_BUFFERED_SIGNALS", 0)
    wf = _Workflow()

    wf.submit_answers({"resume_token": "tok-1", "answers": [_answer("q1")]})

    assert wf._buffered_signals == {"tok-1": [_answer("q1")]}


# --------------------------------------------------------------------------
# replay-safety: payload: Any annotation
# --------------------------------------------------------------------------


def test_submit_answers_payload_annotation_survives_temporal_type_conversion() -> None:
    """The ``payload`` parameter must stay annotated ``Any`` -- Temporal's data
    converter type-checks a signal argument against its annotation *before*
    the handler body runs. A ``Dict``-shaped annotation would make
    ``value_to_type`` raise ``TypeError`` for a non-dict wire payload (e.g. a
    bare string), which fails the workflow task outright and, since Temporal
    replays history, would fail identically on every future replay --
    permanently stranding the workflow, defeating this handler's own
    isinstance-based fail-closed design. This drives the real Temporal
    converter (not a fake) against the handler's live type hint to prove a
    non-dict payload converts cleanly instead of raising."""
    hints = typing.get_type_hints(HitlAnswerSignalMixin.submit_answers)
    payload_hint = hints["payload"]

    # Must not raise -- a Dict[str, Any] annotation would raise TypeError here.
    assert value_to_type(payload_hint, "not-a-dict") == "not-a-dict"
    assert value_to_type(payload_hint, {"resume_token": "tok-1", "answers": []}) == {
        "resume_token": "tok-1",
        "answers": [],
    }


# --------------------------------------------------------------------------
# _log_signal_diagnostic -- in-workflow branch
# --------------------------------------------------------------------------


class _FakeWorkflowLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, tuple]] = []

    def warning(self, msg: str, *args) -> None:
        self.warnings.append((msg, args))


def test_log_signal_diagnostic_logs_via_workflow_logger_inside_a_workflow(monkeypatch) -> None:
    """The in-workflow branch of _log_signal_diagnostic (guarded by
    workflow.in_workflow()) is only reachable inside a real Temporal workflow
    sandbox -- monkeypatch workflow.in_workflow/workflow.logger to exercise it
    without one, proving the operator diagnostic trail this module's
    postconditions promise is actually emitted, not just documented."""
    fake_logger = _FakeWorkflowLogger()
    monkeypatch.setattr(temporal_signal_module.workflow, "in_workflow", lambda: True)
    monkeypatch.setattr(temporal_signal_module.workflow, "logger", fake_logger)

    temporal_signal_module._log_signal_diagnostic("submit_answers rejected: %r", "reason")

    assert fake_logger.warnings == [("submit_answers rejected: %r", ("reason",))]


def test_log_signal_diagnostic_is_a_silent_no_op_outside_a_workflow(monkeypatch, caplog) -> None:
    """The documented contract is a no-op (not a stdlib-logging fallback) when
    workflow.in_workflow() is False -- e.g. every other test in this suite,
    which drives HitlAnswerSignalMixin as a bare object with no Temporal
    context. Asserts both that the workflow logger is never touched AND that
    nothing lands on the stdlib logging chain either, pinning "no-op" rather
    than "logs somewhere else" as the actual behavior."""
    fake_logger = _FakeWorkflowLogger()
    monkeypatch.setattr(temporal_signal_module.workflow, "in_workflow", lambda: False)
    monkeypatch.setattr(temporal_signal_module.workflow, "logger", fake_logger)

    with caplog.at_level(logging.DEBUG):
        temporal_signal_module._log_signal_diagnostic("submit_answers rejected: %r", "reason")

    assert fake_logger.warnings == []
    assert not caplog.records
