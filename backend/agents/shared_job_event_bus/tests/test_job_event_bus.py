"""Unit tests for the shared per-job event-bus algorithm (DB-free)."""

from __future__ import annotations

from shared_job_event_bus import (
    BusState,
    Subscription,
    cleanup_job,
    publish,
    reap_once,
    subscribe,
    unsubscribe,
)


def test_subscribe_tracks_in_both_maps() -> None:
    state = BusState()
    sub = subscribe(state, "j1")
    assert isinstance(sub, Subscription)
    assert state.subscribers["j1"] == [sub]
    assert "j1" in state.job_created_at  # invariant: present in both maps


def test_publish_delivers_payload_and_wakes() -> None:
    state = BusState()
    sub = subscribe(state, "j1")
    publish(state, "j1", {"phase": "x", "n": 2}, event_type="progress")
    assert len(sub.events) == 1
    event = sub.events[0]
    assert event["type"] == "progress"
    assert event["phase"] == "x" and event["n"] == 2
    assert "ts" in event
    assert sub.notify.is_set()


def test_publish_without_type_and_without_subscribers() -> None:
    state = BusState()
    publish(state, "nobody", {"a": 1})  # no subscribers → no-op, no raise
    sub = subscribe(state, "j2")
    publish(state, "j2", {"a": 1})
    assert "type" not in sub.events[0]


def test_publish_bus_fields_are_authoritative() -> None:
    # A caller-supplied "ts"/"type" must not override the bus's own values.
    state = BusState()
    sub = subscribe(state, "j3")
    publish(
        state,
        "j3",
        {"ts": "CALLER-TS", "type": "caller-type", "phase": "x"},
        event_type="progress",
    )
    event = sub.events[0]
    assert event["type"] == "progress"  # event_type wins over caller "type"
    assert event["ts"] != "CALLER-TS"  # bus timestamp wins over caller "ts"
    assert event["phase"] == "x"  # other caller fields preserved


def test_unsubscribe_drops_empty_job_from_both_maps() -> None:
    state = BusState()
    sub = subscribe(state, "j3")
    unsubscribe(state, "j3", sub)
    assert "j3" not in state.subscribers
    assert "j3" not in state.job_created_at
    # unknown job / unknown sub are silent
    unsubscribe(state, "missing", Subscription())
    unsubscribe(state, "j3", Subscription())


def test_unsubscribe_unknown_sub_keeps_others() -> None:
    # Removing a subscription that is not in a non-empty list hits the
    # ValueError branch and leaves the registered subscriber intact.
    state = BusState()
    real = subscribe(state, "j3b")
    unsubscribe(state, "j3b", Subscription())  # bogus → silent
    assert real in state.subscribers["j3b"]


def test_cleanup_job_wakes_all_and_drops() -> None:
    state = BusState()
    a = subscribe(state, "j4")
    b = subscribe(state, "j4")
    cleanup_job(state, "j4")
    assert "j4" not in state.subscribers and "j4" not in state.job_created_at
    assert a.notify.is_set() and b.notify.is_set()
    cleanup_job(state, "unknown")  # no-op


def test_reap_evicts_idle_subscription() -> None:
    state = BusState()
    sub = subscribe(state, "j5")
    sub.last_activity -= 1e9  # ancient
    jobs, subs = reap_once(state, ttl_seconds=3600, max_jobs=1024)
    assert (jobs, subs) == (1, 1)
    assert "j5" not in state.subscribers


def test_reap_keeps_active_and_enforces_job_cap() -> None:
    state = BusState()
    subscribe(state, "a")
    subscribe(state, "b")
    subscribe(state, "c")
    # All active → TTL pass keeps them; cap pass drops oldest until <= max_jobs.
    jobs, subs = reap_once(state, ttl_seconds=3600, max_jobs=2)
    assert len(state.subscribers) == 2
    assert jobs == 1 and subs == 1
    # Oldest ("a") evicted first.
    assert "a" not in state.subscribers


def test_reap_marks_evicted_subscriptions_closed() -> None:
    # Both eviction passes must flag the detached subscription as closed (and
    # wake it) so a streaming consumer can end its stream instead of hanging.
    state = BusState()
    idle = subscribe(state, "idle")
    idle.last_activity -= 1e9  # ancient → TTL pass
    capped = subscribe(state, "capped")  # newest, but cap=1 with idle evicted...
    reap_once(state, ttl_seconds=3600, max_jobs=1)
    assert idle.closed is True and idle.notify.is_set()
    # "capped" survives here (idle freed the only slot); force the cap pass.
    reap_once(state, ttl_seconds=3600, max_jobs=0)
    assert capped.closed is True and capped.notify.is_set()


def test_reap_logs_when_evicting(caplog) -> None:
    import logging

    state = BusState()
    subscribe(state, "x")
    state.subscribers["x"][0].last_activity -= 1e9
    with caplog.at_level(logging.INFO):
        reap_once(state, ttl_seconds=1, max_jobs=10, logger=logging.getLogger(__name__), label="t")
    assert any("reaper: evicted" in r.message for r in caplog.records)


def test_reap_noop_returns_zeros() -> None:
    state = BusState()
    subscribe(state, "y")
    assert reap_once(state, ttl_seconds=3600, max_jobs=10) == (0, 0)


def test_touch_refreshes_last_activity() -> None:
    sub = Subscription()
    before = sub.last_activity
    sub.last_activity = before - 100.0
    sub.touch()
    assert sub.last_activity > before - 100.0


def test_buses_are_independent() -> None:
    a = BusState()
    b = BusState()
    subscribe(a, "shared-id")
    assert "shared-id" in a.subscribers
    assert "shared-id" not in b.subscribers


def test_reap_empty_cap_breaks_cleanly() -> None:
    # max_jobs=0 with no creation entries must not loop forever.
    state = BusState()
    assert reap_once(state, ttl_seconds=1, max_jobs=0) == (0, 0)


def test_reap_cap_guards_against_map_desync() -> None:
    # Defensive: if the creation map is somehow emptied while subscribers
    # remain (an invariant violation), the cap loop must break, not spin.
    state = BusState()
    subscribe(state, "live")
    state.job_created_at.clear()  # force desync
    jobs, subs = reap_once(state, ttl_seconds=3600, max_jobs=0)
    assert (jobs, subs) == (0, 0)
    assert "live" in state.subscribers  # untouched — the guard broke the loop
