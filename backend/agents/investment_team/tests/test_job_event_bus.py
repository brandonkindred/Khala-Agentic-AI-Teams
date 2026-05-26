"""Tests for ``api.job_event_bus`` (per-job SSE event bus).

The module exposes ``subscribe``/``unsubscribe``/``publish``/``cleanup_job``.
Tests verify subscription lifecycle, multi-subscriber broadcast,
publish-without-subscribers (no-op), and the ``cleanup_job`` notify
behaviour.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_subscribers() -> None:
    """Clear module-level subscriber state between tests."""
    from investment_team.api import job_event_bus

    job_event_bus._subscribers.clear()
    yield
    job_event_bus._subscribers.clear()


def test_subscribe_returns_handle_with_empty_event_queue() -> None:
    from investment_team.api.job_event_bus import subscribe

    sub = subscribe("job-1")
    assert sub.events == sub.events  # deque comparison
    assert len(sub.events) == 0
    assert sub.notify.is_set() is False


def test_publish_delivers_event_to_subscriber() -> None:
    from investment_team.api.job_event_bus import publish, subscribe

    sub = subscribe("job-2")
    publish("job-2", {"phase": "ideation", "n": 1}, event_type="progress")

    assert len(sub.events) == 1
    event = sub.events[0]
    assert event["type"] == "progress"
    assert event["phase"] == "ideation"
    assert event["n"] == 1
    # Timestamp must be added by the bus.
    assert "ts" in event
    # The notify event should fire so blocked consumers wake up.
    assert sub.notify.is_set()


def test_publish_without_subscribers_is_noop() -> None:
    from investment_team.api.job_event_bus import publish

    # Should NOT raise even when no one is subscribed.
    publish("nobody", {"phase": "ideation"}, event_type="progress")


def test_publish_without_event_type_omits_type_field() -> None:
    from investment_team.api.job_event_bus import publish, subscribe

    sub = subscribe("job-3")
    publish("job-3", {"phase": "idle"})

    event = sub.events[0]
    assert "type" not in event
    assert event["phase"] == "idle"
    assert "ts" in event


def test_publish_broadcasts_to_all_subscribers() -> None:
    from investment_team.api.job_event_bus import publish, subscribe

    sub_a = subscribe("job-4")
    sub_b = subscribe("job-4")
    publish("job-4", {"phase": "validation"}, event_type="progress")

    assert len(sub_a.events) == 1
    assert len(sub_b.events) == 1
    assert sub_a.notify.is_set()
    assert sub_b.notify.is_set()


def test_unsubscribe_removes_subscription_and_cleans_empty_lists() -> None:
    from investment_team.api import job_event_bus
    from investment_team.api.job_event_bus import publish, subscribe, unsubscribe

    sub = subscribe("job-5")
    assert "job-5" in job_event_bus._subscribers

    unsubscribe("job-5", sub)
    # Subscriber list was emptied → the key was deleted entirely.
    assert "job-5" not in job_event_bus._subscribers

    # Publishing now is a no-op (no leftover subscribers).
    publish("job-5", {"phase": "noop"})


def test_unsubscribe_unknown_subscription_is_silent() -> None:
    """``unsubscribe`` for a subscription that was never registered is a no-op."""
    from investment_team.api.job_event_bus import Subscription, subscribe, unsubscribe

    sub_a = subscribe("job-6")
    bogus = Subscription()
    # Should not raise (the ValueError branch).
    unsubscribe("job-6", bogus)
    # The original subscription is still present.
    from investment_team.api import job_event_bus

    assert sub_a in job_event_bus._subscribers["job-6"]


def test_unsubscribe_unknown_job_is_silent() -> None:
    from investment_team.api.job_event_bus import Subscription, unsubscribe

    # Should not raise for an unknown job id.
    unsubscribe("never-existed", Subscription())


def test_cleanup_job_wakes_and_drops_all_subscribers() -> None:
    from investment_team.api import job_event_bus
    from investment_team.api.job_event_bus import cleanup_job, subscribe

    sub_a = subscribe("job-7")
    sub_b = subscribe("job-7")

    cleanup_job("job-7")
    assert "job-7" not in job_event_bus._subscribers
    # Both subscribers should have their notify event set so blocked
    # consumers exit cleanly.
    assert sub_a.notify.is_set()
    assert sub_b.notify.is_set()


def test_cleanup_job_unknown_id_is_noop() -> None:
    from investment_team.api.job_event_bus import cleanup_job

    cleanup_job("never-existed")  # no exception
