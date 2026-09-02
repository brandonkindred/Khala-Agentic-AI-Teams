"""Unit tests for the Planning HITL Temporal primitive: ``submit_planning_answers``
signal + ``wait_condition`` wait mechanism, plus the ``Callable[[list], list]``
answer-callback adapter.

Drives ``PlanningAnswerSignalMixin`` directly as a plain object (no Temporal
server), patching ``temporalio.workflow.wait_condition`` in place -- the same
lightweight pattern
``software_engineering_team/tests/test_coding_team_temporal_workflow.py`` uses
for ``CodingTeamWorkflow``.
"""

from __future__ import annotations

import asyncio

import pytest

from planning_team.temporal.answer_signal import (
    PlanningAnswerPauseSignal,
    PlanningAnswerSignalMixin,
    build_temporal_planning_answer_callback,
)


class _Workflow(PlanningAnswerSignalMixin):
    """Minimal stand-in for a real ``@workflow.defn`` class mixing this in."""


# --------------------------------------------------------------------------
# submit_planning_answers signal validation
# --------------------------------------------------------------------------


def test_submit_answers_sets_state_when_pause_active() -> None:
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_planning_answers({"resume_token": "tok-1", "answers": [{"question_id": "q1"}]})

    assert wf._submitted_answers == [{"question_id": "q1"}]


def test_submit_answers_ignores_non_dict_payload() -> None:
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_planning_answers("not-a-dict")  # type: ignore[arg-type]

    assert wf._submitted_answers is None


def test_submit_answers_ignores_non_list_answers() -> None:
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_planning_answers({"resume_token": "tok-1", "answers": "nope"})

    assert wf._submitted_answers is None


def test_submit_answers_ignores_payload_missing_answers_key() -> None:
    wf = _Workflow()
    wf._active_resume_token = "tok-1"

    wf.submit_planning_answers({"resume_token": "tok-1"})

    assert wf._submitted_answers is None


def test_submit_answers_ignores_mismatched_resume_token() -> None:
    """A submission for a different (stale, or already-resolved) pause must not
    be applied -- token validation defends against a retried/duplicate signal
    resolving the wrong pause."""
    wf = _Workflow()
    wf._active_resume_token = "current-token"

    wf.submit_planning_answers({"resume_token": "stale-token", "answers": [{"question_id": "q1"}]})

    assert wf._submitted_answers is None


def test_submit_answers_ignores_second_submission_for_same_token() -> None:
    """A double-submit (or two clients racing to answer the same pause) must not
    overwrite the first accepted batch -- first submission per token wins."""
    wf = _Workflow()
    wf._active_resume_token = "tok-1"
    first = [{"question_id": "q1", "selected_option_id": "yes"}]
    wf.submit_planning_answers({"resume_token": "tok-1", "answers": first})

    wf.submit_planning_answers(
        {"resume_token": "tok-1", "answers": [{"question_id": "q1", "selected_option_id": "no"}]}
    )

    assert wf._submitted_answers == first


def test_submit_answers_buffers_signal_with_no_active_pause() -> None:
    """A signal arriving before any pause is active is buffered by resume_token,
    not dropped and not applied to _submitted_answers -- wait_for_planning_answers
    is what consumes the buffer once armed."""
    wf = _Workflow()

    wf.submit_planning_answers({"resume_token": "future-tok", "answers": [{"question_id": "q1"}]})

    assert wf._submitted_answers is None
    assert wf._buffered_signals == {"future-tok": [{"question_id": "q1"}]}


def test_submit_answers_drops_early_signal_with_no_usable_resume_token() -> None:
    wf = _Workflow()

    wf.submit_planning_answers({"resume_token": "", "answers": [{"question_id": "q1"}]})
    wf.submit_planning_answers({"answers": [{"question_id": "q1"}]})

    assert wf._buffered_signals == {}


def test_submit_answers_early_buffering_first_submission_per_token_wins() -> None:
    wf = _Workflow()
    first = [{"question_id": "q1"}]

    wf.submit_planning_answers({"resume_token": "tok-1", "answers": first})
    wf.submit_planning_answers({"resume_token": "tok-1", "answers": [{"question_id": "q2"}]})

    assert wf._buffered_signals == {"tok-1": first}


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
    import typing

    from temporalio.converter import value_to_type

    hints = typing.get_type_hints(PlanningAnswerSignalMixin.submit_planning_answers)
    payload_hint = hints["payload"]

    # Must not raise -- a Dict[str, Any] annotation would raise TypeError here.
    assert value_to_type(payload_hint, "not-a-dict") == "not-a-dict"
    assert value_to_type(payload_hint, {"resume_token": "tok-1", "answers": []}) == {
        "resume_token": "tok-1",
        "answers": [],
    }


def test_submit_answers_does_not_buffer_mismatched_token_while_a_pause_is_active() -> None:
    wf = _Workflow()
    wf._active_resume_token = "current-token"

    wf.submit_planning_answers({"resume_token": "other-token", "answers": [{"question_id": "q1"}]})

    assert wf._buffered_signals == {}
    assert wf._submitted_answers is None


# --------------------------------------------------------------------------
# wait_for_planning_answers
# --------------------------------------------------------------------------


def test_wait_for_planning_answers_requires_nonempty_token() -> None:
    wf = _Workflow()

    with pytest.raises(AssertionError):
        asyncio.run(wf.wait_for_planning_answers(""))


def test_wait_for_planning_answers_returns_once_signal_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delivering a token-matching signal wakes wait_condition, and the answers
    it carried are returned -- state is reset to None afterward so a later
    pause round starts clean."""
    wf = _Workflow()

    async def _fake_wait(pred, timeout=None):
        wf.submit_planning_answers({"resume_token": "tok-1", "answers": [{"question_id": "q1"}]})
        assert pred()

    monkeypatch.setattr("temporalio.workflow.wait_condition", _fake_wait)

    answers = asyncio.run(wf.wait_for_planning_answers("tok-1"))

    assert answers == [{"question_id": "q1"}]
    assert wf._submitted_answers is None
    assert wf._active_resume_token is None


def test_wait_for_planning_answers_consumes_buffered_signal_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal buffered before the wait was armed resolves it without a real
    signal round trip -- wait_condition's predicate is already true."""
    wf = _Workflow()
    wf.submit_planning_answers({"resume_token": "tok-1", "answers": [{"question_id": "q1"}]})

    async def _wait_must_not_actually_block(pred, timeout=None):
        assert pred()

    monkeypatch.setattr("temporalio.workflow.wait_condition", _wait_must_not_actually_block)

    answers = asyncio.run(wf.wait_for_planning_answers("tok-1"))

    assert answers == [{"question_id": "q1"}]
    assert wf._buffered_signals == {}


def test_wait_for_planning_answers_discards_non_matching_buffered_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wf = _Workflow()
    wf._buffered_signals = {"other-tok": [{"question_id": "stale"}]}

    async def _fake_wait(pred, timeout=None):
        wf.submit_planning_answers({"resume_token": "tok-1", "answers": [{"question_id": "q1"}]})
        assert pred()

    monkeypatch.setattr("temporalio.workflow.wait_condition", _fake_wait)

    answers = asyncio.run(wf.wait_for_planning_answers("tok-1"))

    assert answers == [{"question_id": "q1"}]
    assert wf._buffered_signals == {}


def test_wait_for_planning_answers_never_resolves_without_a_real_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No signal ever arrives: the wait must never resolve on its own -- this
    proves there is no default/timeout path by polling the real predicate
    (as ``workflow.wait_condition`` would) and asserting it stays unsatisfied
    until an outer ``asyncio.wait_for`` deadline cuts it off."""
    wf = _Workflow()

    async def _polling_wait(pred, timeout=None):
        while not pred():
            await asyncio.sleep(0.01)

    monkeypatch.setattr("temporalio.workflow.wait_condition", _polling_wait)

    async def _run() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(wf.wait_for_planning_answers("tok-1"), timeout=0.1)

    asyncio.run(_run())
    # The wait never delivered an answer, so no answer-shaped state was ever set.
    assert wf._submitted_answers is None


# --------------------------------------------------------------------------
# build_temporal_planning_answer_callback adapter
# --------------------------------------------------------------------------


def test_callback_requires_nonempty_resume_token() -> None:
    with pytest.raises(AssertionError):
        build_temporal_planning_answer_callback("")


def test_callback_raises_pause_signal_when_no_answers_yet() -> None:
    cb = build_temporal_planning_answer_callback("tok-1")
    questions = [{"id": "q1", "options": [{"id": "opt-a", "is_default": True}]}]

    with pytest.raises(PlanningAnswerPauseSignal) as exc_info:
        cb(questions)

    assert exc_info.value.resume_token == "tok-1"
    assert exc_info.value.pending_questions == questions


def test_callback_returns_resolved_answers_filtered_by_question_id() -> None:
    submitted = [
        {"question_id": "q1", "selected_option_id": "opt-a"},
        {"question_id": "q2", "selected_option_id": "opt-b"},
    ]
    cb = build_temporal_planning_answer_callback("tok-1", submitted_answers=submitted)

    result = cb([{"id": "q1"}])

    assert result == [{"question_id": "q1", "selected_option_id": "opt-a"}]


def test_callback_never_fabricates_an_answer_for_an_unmatched_question() -> None:
    """An unmatched question is never given an answer — and never given nothing.

    Answering a batch with `[]` lets Planning proceed unanswered, which is the
    silent auto-answer both modes exist to prevent (thread mode re-pauses on
    every batch). A batch nothing matches is a batch these answers were not
    submitted for, so it pauses again instead.
    """
    cb = build_temporal_planning_answer_callback(
        "tok-1", submitted_answers=[{"question_id": "other", "selected_option_id": "a"}]
    )

    with pytest.raises(PlanningAnswerPauseSignal) as excinfo:
        cb([{"id": "q1"}])

    assert excinfo.value.pending_questions == [{"id": "q1"}]


def test_callback_pauses_again_on_a_batch_from_a_later_round() -> None:
    """Planning re-runs from scratch on resume and can re-identify its questions."""
    submitted = [{"question_id": "q1", "selected_option_id": "opt-a"}]
    cb = build_temporal_planning_answer_callback(
        "tok-1", submitted_answers=submitted, next_resume_token=lambda: "tok-2"
    )

    # The batch these answers belong to still resolves normally.
    assert cb([{"id": "q1"}]) == submitted

    # A batch with entirely different ids pauses, on a FRESH token — a pause
    # round never reuses one (see pause_cycle.mint_resume_token).
    with pytest.raises(PlanningAnswerPauseSignal) as excinfo:
        cb([{"id": "q2"}, {"id": "q3"}])

    assert excinfo.value.resume_token == "tok-2"
    assert excinfo.value.pending_questions == [{"id": "q2"}, {"id": "q3"}]


def test_empty_submitted_answers_resolves_instead_of_pausing_forever() -> None:
    """An explicitly empty submission means "proceed without answers".

    Re-pausing on it would re-ask the same batch on every resume with nothing
    new for the submitter to supply — an unterminating loop, which is strictly
    worse than proceeding on their explicit choice.
    """
    cb = build_temporal_planning_answer_callback(
        "tok-1", submitted_answers=[], next_resume_token=lambda: "tok-2"
    )

    assert cb([{"id": "q1"}]) == []


def test_callback_reuses_its_token_when_no_minter_is_given() -> None:
    """Without a minter a re-pause still happens — sharing the round's token."""
    cb = build_temporal_planning_answer_callback(
        "tok-1", submitted_answers=[{"question_id": "other", "selected_option_id": "a"}]
    )

    with pytest.raises(PlanningAnswerPauseSignal) as excinfo:
        cb([{"id": "q1"}])

    assert excinfo.value.resume_token == "tok-1"


def test_callback_returns_partial_matches_without_pausing() -> None:
    """A partially answered batch is that same batch, not a later round.

    Re-pausing here would re-ask what the user already answered, with nothing
    new for them to add on the second pass.
    """
    submitted = [{"question_id": "q1", "selected_option_id": "opt-a"}]
    cb = build_temporal_planning_answer_callback(
        "tok-1", submitted_answers=submitted, next_resume_token=lambda: "tok-2"
    )

    assert cb([{"id": "q1"}, {"id": "q2"}]) == submitted


def test_callback_answers_an_empty_batch_with_nothing() -> None:
    """No questions asked, nothing to pause for."""
    cb = build_temporal_planning_answer_callback("tok-1", submitted_answers=[])

    assert cb([]) == []


def test_callback_ignores_malformed_question_entries() -> None:
    """A non-dict question entry is not matched against any submitted answer --
    fails closed rather than crashing."""
    submitted = [{"question_id": "q1", "selected_option_id": "opt-a"}]
    cb = build_temporal_planning_answer_callback("tok-1", submitted_answers=submitted)

    result = cb(["not-a-dict", {"id": "q1"}])

    assert result == [{"question_id": "q1", "selected_option_id": "opt-a"}]


def test_callback_skips_answer_with_unhashable_question_id() -> None:
    """A malformed signal could supply a non-str (e.g. unhashable list)
    question_id -- a plain `in question_ids` set-membership test would raise
    TypeError on that. Requiring a str question_id rejects it instead of
    crashing the resumed activity."""
    cb = build_temporal_planning_answer_callback(
        "tok-1",
        submitted_answers=[
            {"question_id": [], "selected_option_id": "opt-a"},
            {"question_id": "q1", "selected_option_id": "opt-b"},
        ],
    )

    result = cb([{"id": "q1"}])

    assert result == [{"question_id": "q1", "selected_option_id": "opt-b"}]


def test_callback_skips_question_with_non_str_id() -> None:
    """A question entry with a non-str (e.g. unhashable list) id must not
    crash building the question_ids set, and must never match anything."""
    cb = build_temporal_planning_answer_callback(
        "tok-1",
        submitted_answers=[{"question_id": "q1", "selected_option_id": "opt-a"}],
    )

    result = cb([{"id": []}, {"id": "q1"}])

    assert result == [{"question_id": "q1", "selected_option_id": "opt-a"}]


def test_callback_skips_malformed_submitted_answer_entries() -> None:
    """A malformed signal can smuggle a non-dict entry into ``submitted_answers``
    (submit_planning_answers only validates ``answers`` is a list, not a
    list-of-dicts) -- the resolved callback must skip it rather than crash
    with AttributeError on ``a.get(...)``, matching this primitive's fail-
    closed contract."""
    cb = build_temporal_planning_answer_callback(
        "tok-1",
        submitted_answers=["bad", {"question_id": "q1", "selected_option_id": "opt-a"}],
    )

    result = cb([{"id": "q1"}])

    assert result == [{"question_id": "q1", "selected_option_id": "opt-a"}]
