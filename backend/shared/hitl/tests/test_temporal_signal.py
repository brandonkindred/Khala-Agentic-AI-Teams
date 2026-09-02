"""Unit tests for shared.hitl.temporal_signal -- the shared ``submit_answers``
Temporal signal handler + buffer/reject/accept state machine.

Drives ``HitlAnswerSignalMixin`` directly as a plain object (no Temporal
server), the same lightweight pattern
``planning_team/tests/test_temporal_answer_signal.py`` and
``software_engineering_team/tests/test_coding_team_temporal_workflow.py`` use
for the sibling implementations this module was extracted from.
"""

from __future__ import annotations

import typing

from temporalio.converter import value_to_type

from shared.hitl.temporal_signal import (
    MAX_BUFFERED_SIGNALS,
    SUBMIT_ANSWERS_SIGNAL,
    HitlAnswerSignalMixin,
)


class _Workflow(HitlAnswerSignalMixin):
    """Minimal stand-in for a real ``@workflow.defn`` class mixing this in."""


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
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": "nope"})

    assert wf._submitted_answers is None


def test_submit_answers_ignores_payload_missing_answers_key() -> None:
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


def test_submit_answers_normalizes_answer_shape_through_schema() -> None:
    """A minimal, schema-valid answer (only question_id) is normalized to the
    full AnswerSubmission field set."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_answers({"resume_token": "tok-1", "answers": [{"question_id": "q1"}]})

    assert wf._submitted_answers == [_answer("q1")]


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
