"""Unit tests for :mod:`agent_team_studio.assistant_kernel.turn_lock`.

Exercises :class:`InMemoryTurnLocks` against a minimal in-memory "record" —
a plain dict with ``history``/``draft`` keys — standing in for what a real
conversation store's row would be, since the module itself is store-agnostic
and only owns the locking/rollback mechanics.
"""

from __future__ import annotations

import threading

import pytest

from agent_team_studio.assistant_kernel.turn_lock import ConversationTurn, InMemoryTurnLocks


def _record_ops(record: dict):
    """Build (read, on_message, on_draft, restore) callables bound to ``record``."""

    def read():
        return list(record["history"]), record["draft"]

    def on_message(role: str, content: str) -> None:
        record["history"].append((role, content))

    def on_draft(draft) -> None:
        record["draft"] = draft

    def restore(history, draft) -> None:
        record["history"] = list(history)
        record["draft"] = draft

    return read, on_message, on_draft, restore


class _Boom(Exception):
    pass


def test_turn_applies_messages_and_draft_on_clean_exit() -> None:
    record = {"history": [], "draft": "initial"}
    read, on_message, on_draft, restore = _record_ops(record)
    locks = InMemoryTurnLocks()

    with locks.turn(
        "conv-1", read=read, on_message=on_message, on_draft=on_draft, restore=restore
    ) as turn:
        assert isinstance(turn, ConversationTurn)
        turn.append_message("user", "hi")
        turn.append_message("assistant", "hello")
        turn.set_draft("updated")

    assert record["history"] == [("user", "hi"), ("assistant", "hello")]
    assert record["draft"] == "updated"


def test_turn_history_snapshots_prior_messages() -> None:
    record = {"history": [("user", "earlier")], "draft": "d0"}
    read, on_message, on_draft, restore = _record_ops(record)
    locks = InMemoryTurnLocks()

    with locks.turn(
        "conv-1", read=read, on_message=on_message, on_draft=on_draft, restore=restore
    ) as turn:
        assert turn.history == [("user", "earlier")]
        assert turn.draft == "d0"


def test_turn_rolls_back_nothing_on_exception_before_any_write() -> None:
    record = {"history": [], "draft": "initial"}
    read, on_message, on_draft, restore = _record_ops(record)
    locks = InMemoryTurnLocks()

    with pytest.raises(_Boom):
        with locks.turn(
            "conv-1", read=read, on_message=on_message, on_draft=on_draft, restore=restore
        ):
            raise _Boom("LLM call failed")

    assert record == {"history": [], "draft": "initial"}


def test_turn_rolls_back_partial_write_on_later_exception() -> None:
    record = {"history": [], "draft": "initial"}
    read, on_message, on_draft, restore = _record_ops(record)
    locks = InMemoryTurnLocks()

    with pytest.raises(_Boom):
        with locks.turn(
            "conv-1", read=read, on_message=on_message, on_draft=on_draft, restore=restore
        ) as turn:
            turn.append_message("user", "hi")
            turn.set_draft("half-updated")
            raise _Boom("save failed after the user message was appended")

    # The partial write is rolled back to the exact pre-turn snapshot.
    assert record == {"history": [], "draft": "initial"}


def test_turn_without_clone_rollback_reflects_inplace_mutation() -> None:
    """Documents the ``clone``-less precondition: mutating ``turn.draft`` in
    place (instead of calling ``set_draft``) corrupts the rollback snapshot,
    since it aliases the same object ``read()`` returned."""
    record = {"history": [], "draft": {"count": 0}}
    read, on_message, on_draft, restore = _record_ops(record)
    locks = InMemoryTurnLocks()

    with pytest.raises(_Boom):
        with locks.turn(
            "conv-1", read=read, on_message=on_message, on_draft=on_draft, restore=restore
        ) as turn:
            turn.draft["count"] = 99
            raise _Boom()

    assert record["draft"] == {"count": 99}


def test_turn_clone_isolates_rollback_from_inplace_mutation() -> None:
    """Passing ``clone`` fixes the scenario above: the rollback snapshot is
    independent of whatever the caller does to ``turn.draft`` afterward."""
    record = {"history": [], "draft": {"count": 0}}
    read, on_message, on_draft, restore = _record_ops(record)
    locks = InMemoryTurnLocks()

    with pytest.raises(_Boom):
        with locks.turn(
            "conv-1",
            read=read,
            on_message=on_message,
            on_draft=on_draft,
            restore=restore,
            clone=dict,
        ) as turn:
            turn.draft["count"] = 99
            raise _Boom()

    assert record["draft"] == {"count": 0}


def test_turn_after_rollback_is_usable_again() -> None:
    record = {"history": [], "draft": "initial"}
    read, on_message, on_draft, restore = _record_ops(record)
    locks = InMemoryTurnLocks()

    with pytest.raises(_Boom):
        with locks.turn(
            "conv-1", read=read, on_message=on_message, on_draft=on_draft, restore=restore
        ) as turn:
            turn.append_message("user", "hi")
            raise _Boom()

    with locks.turn(
        "conv-1", read=read, on_message=on_message, on_draft=on_draft, restore=restore
    ) as turn:
        turn.append_message("user", "retry")
        turn.set_draft("recovered")

    assert record == {"history": [("user", "retry")], "draft": "recovered"}


def test_turn_serializes_concurrent_turns_no_lost_update() -> None:
    """N threads each read-increment-write the same counter draft under the lock.

    Without serialization, concurrent turns would race the read-modify-write
    and lose updates; the lock guarantees the final value equals the thread
    count.
    """
    n = 20
    record = {"history": [], "draft": 0}
    read, on_message, on_draft, restore = _record_ops(record)
    locks = InMemoryTurnLocks()
    barrier = threading.Barrier(n)

    def worker() -> None:
        barrier.wait()
        with locks.turn(
            "conv-1", read=read, on_message=on_message, on_draft=on_draft, restore=restore
        ) as turn:
            current = turn.draft
            # Yield to widen the race window if the lock weren't held.
            threading.Event().wait(0.001)
            turn.set_draft(current + 1)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert record["draft"] == n


def test_different_keys_do_not_contend() -> None:
    """Proves conv-b's turn enters promptly *while conv-a is still held* —
    not merely that it eventually enters. A regression to one shared lock
    for every key would still let conv-b in eventually (once conv-a's
    2s-timeout releases it), so asserting only ``entered_b.is_set()`` after
    the fact wouldn't catch that; the tight 0.5s deadline below, checked
    before conv-a is released, is what actually distinguishes "independent
    per-key locks" from "one lock, got lucky/unlucky on timing"."""
    record_a = {"history": [], "draft": "a0"}
    record_b = {"history": [], "draft": "b0"}
    ops_a = _record_ops(record_a)
    ops_b = _record_ops(record_b)
    locks = InMemoryTurnLocks()

    holding_a = threading.Event()
    release_a = threading.Event()

    def hold_a() -> None:
        with locks.turn(
            "conv-a", read=ops_a[0], on_message=ops_a[1], on_draft=ops_a[2], restore=ops_a[3]
        ):
            holding_a.set()
            release_a.wait(timeout=2)

    a_thread = threading.Thread(target=hold_a)
    a_thread.start()
    try:
        assert holding_a.wait(timeout=2), "conv-a's turn was never entered"

        entered_b = threading.Event()

        def try_b() -> None:
            with locks.turn(
                "conv-b", read=ops_b[0], on_message=ops_b[1], on_draft=ops_b[2], restore=ops_b[3]
            ) as turn:
                entered_b.set()
                turn.set_draft("b1")

        b_thread = threading.Thread(target=try_b)
        b_thread.start()
        try:
            entered_promptly = entered_b.wait(timeout=0.5)
        finally:
            b_thread.join(timeout=2)
    finally:
        release_a.set()
        a_thread.join(timeout=2)

    assert entered_promptly, "conv-b's turn was blocked by conv-a's lock"
    assert record_b["draft"] == "b1"


def test_discard_drops_the_lock_entry() -> None:
    record = {"history": [], "draft": "d0"}
    read, on_message, on_draft, restore = _record_ops(record)
    locks = InMemoryTurnLocks()

    with locks.turn("conv-1", read=read, on_message=on_message, on_draft=on_draft, restore=restore):
        pass
    assert len(locks) == 1

    locks.discard("conv-1")
    assert len(locks) == 0


def test_discard_unknown_key_is_a_no_op() -> None:
    locks = InMemoryTurnLocks()
    locks.discard("does-not-exist")
    assert len(locks) == 0
