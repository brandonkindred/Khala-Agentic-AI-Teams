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
import logging

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
        "tok-1",
        submitted_answers=submitted,
        next_resume_token=lambda: "tok-2",
    )

    # The batch these answers belong to still resolves normally.
    assert cb([{"id": "q1"}]) == submitted

    # A batch with entirely different ids pauses, on a FRESH token — a pause
    # round never reuses one (see pause_cycle.mint_resume_token).
    with pytest.raises(PlanningAnswerPauseSignal) as excinfo:
        cb([{"id": "q2"}, {"id": "q3"}])

    assert excinfo.value.resume_token == "tok-2"
    assert excinfo.value.pending_questions == [{"id": "q2"}, {"id": "q3"}]


def test_empty_submitted_answers_pauses_rather_than_submitting_nothing() -> None:
    """An empty answer set cannot resume the sub-job it is meant to resume.

    The product-analysis answers route rejects any batch missing a required
    question, and ``convert_to_pending_questions`` marks every question required.
    So "return []" is not "proceed without answers" -- it is a submission the
    route drops on the floor, leaving the sub-job waiting until it times out.
    Pausing at least puts the question back to a human; the caller's round budget
    is what stops it repeating.
    """
    cb = build_temporal_planning_answer_callback(
        "tok-1",
        submitted_answers=[],
        next_resume_token=lambda: "tok-2",
    )

    with pytest.raises(PlanningAnswerPauseSignal) as exc:
        cb([{"id": "q1"}])
    assert exc.value.resume_token == "tok-2"


def test_a_fully_answered_batch_resolves_while_a_new_one_pauses() -> None:
    """Answers accumulate across rounds, so a later round's callback holds
    earlier rounds' answers. A batch they fully cover resolves; a batch carrying
    anything they do not still pauses."""
    cb = build_temporal_planning_answer_callback(
        "tok-2",
        submitted_answers=[{"question_id": "q1", "selected_option_id": "a"}],
        next_resume_token=lambda: "tok-3",
    )

    assert cb([{"id": "q1"}]) == [{"question_id": "q1", "selected_option_id": "a"}]
    with pytest.raises(PlanningAnswerPauseSignal):
        cb([{"id": "q3"}])


def test_a_new_question_alongside_an_answered_one_still_pauses() -> None:
    """One stale match must not carry brand-new questions through unanswered."""
    cb = build_temporal_planning_answer_callback(
        "tok-2",
        submitted_answers=[{"question_id": "q1", "selected_option_id": "a"}],
        next_resume_token=lambda: "tok-3",
    )

    with pytest.raises(PlanningAnswerPauseSignal) as excinfo:
        cb([{"id": "q1"}, {"id": "q4"}, {"id": "q5"}])

    assert excinfo.value.resume_token == "tok-3"
    assert excinfo.value.pending_questions == [{"id": "q1"}, {"id": "q4"}, {"id": "q5"}]


def test_callback_reuses_its_token_when_no_minter_is_given() -> None:
    """Without a minter a re-pause still happens — sharing the round's token."""
    cb = build_temporal_planning_answer_callback(
        "tok-1", submitted_answers=[{"question_id": "other", "selected_option_id": "a"}]
    )

    with pytest.raises(PlanningAnswerPauseSignal) as excinfo:
        cb([{"id": "q1"}])

    assert excinfo.value.resume_token == "tok-1"


def test_callback_pauses_on_a_partially_answered_batch() -> None:
    """A partial set is indistinguishable from silence at the route: it is
    rejected for the missing required question and the sub-job keeps waiting.
    Returning it would look like progress and produce a hang."""
    cb = build_temporal_planning_answer_callback(
        "tok-1",
        submitted_answers=[{"question_id": "q1", "selected_option_id": "opt-a"}],
        next_resume_token=lambda: "tok-2",
    )

    with pytest.raises(PlanningAnswerPauseSignal):
        cb([{"id": "q1"}, {"id": "q2"}])


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


def test_callback_defaults_unanswered_questions_when_repause_is_forbidden() -> None:
    """``allow_repause=False`` is the caller's escape from a pause loop that is
    not guaranteed to converge -- Planning question ids are LLM-minted, so a
    replay can mint fresh ones for questions already answered. The final round
    must hand back a set the answers route will accept, which means defaulting
    what nobody answered rather than submitting a short set that gets rejected.
    """
    cb = build_temporal_planning_answer_callback(
        "tok-1",
        submitted_answers=[{"question_id": "q1", "selected_option_id": "opt-a"}],
        next_resume_token=lambda: "tok-2",
        allow_repause=False,
    )

    result = cb(
        [
            {"id": "q1"},
            {
                "id": "q-drifted",
                "options": [
                    {"id": "opt-x", "is_default": False},
                    {"id": "opt-y", "is_default": True},
                ],
            },
        ]
    )

    assert result == [
        {"question_id": "q1", "selected_option_id": "opt-a"},
        {"question_id": "q-drifted", "selected_option_id": "opt-y", "other_text": None},
    ]


def test_default_answer_falls_back_to_highest_confidence_then_to_none() -> None:
    """Not every pending question carries an ``is_default`` option, and one with
    no options at all carries only a free-text placeholder -- neither may crash
    the final round or emit an option id the route never saw.

    With no ``is_default``, the pick is the highest-confidence option, NOT the
    first: that is the policy ``user_communication.get_default_option`` applies
    on thread mode's auto-answer path, and falling back to list order here would
    default the same question differently depending on which runtime mode spent
    its pause budget.
    """
    cb = build_temporal_planning_answer_callback("tok-1", submitted_answers=[], allow_repause=False)

    result = cb(
        [
            {
                "id": "q-no-default",
                "options": [
                    {"id": "opt-low", "confidence": 0.2},
                    {"id": "opt-high", "confidence": 0.9},
                ],
            },
            {"id": "q-no-options", "options": []},
            {"id": "q-malformed-options", "options": ["not-a-dict", {"id": 7}]},
        ]
    )

    assert [a["selected_option_id"] for a in result] == ["opt-high", None, None]


def test_default_answer_matches_get_default_option_on_the_same_question() -> None:
    """Pin the parity the docstring claims, against the real thread-mode helper.

    The two consume different shapes -- ``OpenQuestion`` there, the wire dicts
    ``convert_to_pending_questions`` emits here -- so nothing but a test stops
    them drifting apart again.
    """
    from software_engineering_team.product_requirements_analysis_agent.models import (
        OpenQuestion,
        QuestionOption,
    )
    from software_engineering_team.product_requirements_analysis_agent.user_communication import (
        convert_to_pending_questions,
        get_default_option,
    )

    for options in (
        [
            QuestionOption(id="a", label="A", is_default=False, confidence=0.2),
            QuestionOption(id="b", label="B", is_default=False, confidence=0.9),
        ],
        [
            QuestionOption(id="a", label="A", is_default=True, confidence=0.1),
            QuestionOption(id="b", label="B", is_default=False, confidence=0.9),
        ],
        [
            QuestionOption(id="a", label="A", is_default=False, confidence=0.5),
            QuestionOption(id="b", label="B", is_default=False, confidence=0.5),
        ],
    ):
        question = OpenQuestion(id="q1", question_text="Which?", options=options)
        pending = convert_to_pending_questions([question])
        cb = build_temporal_planning_answer_callback(
            "tok-1", submitted_answers=[], allow_repause=False
        )

        thread_mode = get_default_option(question)
        temporal_mode = cb(pending)[0]

        assert thread_mode is not None
        assert temporal_mode["selected_option_id"] == thread_mode.id


def test_default_answer_ignores_a_bool_confidence() -> None:
    """``bool`` is an ``int`` subclass, so a malformed ``confidence: True`` would
    otherwise score 1.0 and outrank every real option."""
    cb = build_temporal_planning_answer_callback("tok-1", submitted_answers=[], allow_repause=False)

    result = cb(
        [
            {
                "id": "q1",
                "options": [
                    {"id": "opt-bogus", "confidence": True},
                    {"id": "opt-real", "confidence": 0.4},
                ],
            }
        ]
    )

    assert result[0]["selected_option_id"] == "opt-real"


def test_callback_warns_when_defaulting_an_unanswered_question(caplog) -> None:
    """Choosing an answer nobody gave is the silent auto-answer this callback
    otherwise exists to prevent -- when the budget forces it, it must not be
    silent.

    ``caplog.at_level`` rather than a hand-attached handler: it forces the level
    for the block, so an ambient ``logging.disable`` or a raised level on the
    ``planning_team`` hierarchy cannot silently suppress the record and turn this
    into an unexplained failure.
    """
    cb = build_temporal_planning_answer_callback("tok-1", submitted_answers=[], allow_repause=False)

    with caplog.at_level(logging.WARNING, logger="planning_team.temporal.answer_signal"):
        assert cb([{"id": "q-never-shown"}]) == [
            {"question_id": "q-never-shown", "selected_option_id": None, "other_text": None}
        ]

    assert any(
        rec.levelno == logging.WARNING and "q-never-shown" in rec.getMessage()
        for rec in caplog.records
    )


def test_callback_still_pauses_on_an_unanswered_batch_when_repause_allowed() -> None:
    """The escape hatch is opt-in: the default keeps the pause behaviour intact."""
    cb = build_temporal_planning_answer_callback(
        "tok-1",
        submitted_answers=[],
        next_resume_token=lambda: "tok-2",
        allow_repause=True,
    )

    with pytest.raises(PlanningAnswerPauseSignal):
        cb([{"id": "q-never-shown"}])


@pytest.mark.parametrize("bad_value", [None, 1, 0, "yes", [True]])
def test_callback_rejects_non_bool_allow_repause(bad_value: object) -> None:
    """``allow_repause`` decides whether Phase 2 can terminate; anything but a
    bool must fail loudly at the boundary rather than silently picking a branch.

    Parametrized over truthy AND falsy non-bools, not just ``None``: a guard
    that regressed to ``if allow_repause is None: raise`` plus plain truthiness
    would still reject ``None`` while letting ``1`` or ``"yes"`` select a
    termination branch the caller never asked for.
    """
    with pytest.raises(AssertionError, match="allow_repause"):
        build_temporal_planning_answer_callback(
            "tok-1",
            submitted_answers=[],
            allow_repause=bad_value,  # type: ignore[arg-type]
        )


def test_callback_rejects_a_non_callable_next_resume_token() -> None:
    """A token string passed where the minting callable belongs must fail at
    construction. Left unchecked it stays silent until a batch actually
    re-pauses, then surfaces as ``TypeError: 'str' object is not callable`` from
    inside a resumed activity."""
    with pytest.raises(AssertionError, match="next_resume_token"):
        build_temporal_planning_answer_callback(
            "tok-1",
            submitted_answers=[],
            next_resume_token="tok-2",  # type: ignore[arg-type]
        )


def test_default_answer_ignores_a_nan_confidence() -> None:
    """NaN passes the numeric check but loses every ``>`` comparison, so ``max``
    keeps a LEADING NaN option over later, higher-confidence ones — a malformed
    option outranking well-formed ones, which the postcondition rules out.

    Order matters here: the NaN option is first, which is the only arrangement
    that exposes the bug.
    """
    cb = build_temporal_planning_answer_callback("tok-1", submitted_answers=[], allow_repause=False)

    result = cb(
        [
            {
                "id": "q1",
                "options": [
                    {"id": "opt-nan", "confidence": float("nan")},
                    {"id": "opt-real", "confidence": 0.4},
                ],
            }
        ]
    )

    assert result[0]["selected_option_id"] == "opt-real"


def test_default_answer_ignores_an_infinite_confidence() -> None:
    """``inf`` is finite-checked for the same reason: it would outrank every real
    option rather than being treated as the malformed value it is."""
    cb = build_temporal_planning_answer_callback("tok-1", submitted_answers=[], allow_repause=False)

    result = cb(
        [
            {
                "id": "q1",
                "options": [
                    {"id": "opt-inf", "confidence": float("inf")},
                    {"id": "opt-real", "confidence": 0.4},
                ],
            }
        ]
    )

    assert result[0]["selected_option_id"] == "opt-real"


def test_default_answer_ignores_an_out_of_range_int_confidence() -> None:
    """``float()`` raises ``OverflowError`` on an int beyond ~1e308, and
    ``json.loads`` parses integer tokens into unbounded ints — so the same
    LLM-parsed source the other exclusions guard against can supply one.

    Letting it raise would crash the resumed activity on the final round, which
    is precisely the path that must default rather than fail.
    """
    cb = build_temporal_planning_answer_callback("tok-1", submitted_answers=[], allow_repause=False)

    result = cb(
        [
            {
                "id": "q1",
                "options": [
                    {"id": "opt-huge", "confidence": 10**400},
                    {"id": "opt-real", "confidence": 0.4},
                ],
            }
        ]
    )

    assert result[0]["selected_option_id"] == "opt-real"


def test_callback_returns_at_most_one_answer_per_question() -> None:
    """A signal's ``answers`` is validated as a list, not a list of DISTINCT
    answers, so the same question_id can arrive twice.

    Nothing downstream catches it: the product-analysis route compares sets of
    ids, so a duplicate satisfies both its required-coverage and unknown-id
    checks, and the batch is then stored verbatim -- leaving which answer
    actually applies decided by iteration order. First entry wins here.
    """
    cb = build_temporal_planning_answer_callback(
        "job-dup:tok1",
        submitted_answers=[
            {"question_id": "q1", "selected_option_id": "first"},
            {"question_id": "q1", "selected_option_id": "second"},
        ],
    )

    answers = cb([{"id": "q1", "options": [{"id": "first"}, {"id": "second"}]}])

    assert answers == [{"question_id": "q1", "selected_option_id": "first"}]
