"""Unit tests for the per-conversation phase-cache storage slot added to
``branding_team.api.conversation`` (Story 2c Step 1).

Covers ``_get_or_create_phase_cache`` directly (empty-on-first-use, identity
retention across calls, thread safety) plus one lifecycle test through the
real ``_create_branding_conversation_impl`` / ``_send_branding_conversation_message_impl``
call path, proving the slot is actually seeded and retained across a turn --
not just the helper in isolation. None of this wires the cache into
``orchestrator.run`` yet; that is separate, later work.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import branding_team.api.conversation as conversation
from branding_team.api.models import CreateConversationRequest, SendMessageRequest
from branding_team.models import BrandPhase
from branding_team.shared.phase_output_cache import PhaseOutputCache
from branding_team.tests._memory_stores import install_memory_stores
from branding_team.tests.conftest import make_mission


def test_get_or_create_phase_cache_starts_empty_for_a_fresh_conversation() -> None:
    conversation_id = str(uuid4())
    cache = conversation._get_or_create_phase_cache(conversation_id)
    assert isinstance(cache, PhaseOutputCache)
    assert cache.get(BrandPhase.STRATEGIC_CORE, "any-hash") is None


def test_get_or_create_phase_cache_is_retained_across_calls() -> None:
    """Same conversation_id -> same instance, and mutations are visible later."""
    conversation_id = str(uuid4())
    first = conversation._get_or_create_phase_cache(conversation_id)
    first.put(BrandPhase.STRATEGIC_CORE, "hash-1", MagicMock())

    second = conversation._get_or_create_phase_cache(conversation_id)
    assert second is first
    assert second.get(BrandPhase.STRATEGIC_CORE, "hash-1") is not None


def test_get_or_create_phase_cache_isolates_different_conversations() -> None:
    first_id, second_id = str(uuid4()), str(uuid4())
    first_cache = conversation._get_or_create_phase_cache(first_id)
    second_cache = conversation._get_or_create_phase_cache(second_id)

    assert first_cache is not second_cache
    first_cache.put(BrandPhase.STRATEGIC_CORE, "hash-1", MagicMock())
    assert second_cache.get(BrandPhase.STRATEGIC_CORE, "hash-1") is None


def test_get_or_create_phase_cache_thread_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent first calls for the same new id must not double-construct.

    Mirrors ``tests/test_store_singleton.py::test_get_default_store_thread_safe``:
    the first thread's ``PhaseOutputCache.__init__`` is held open while several
    other threads race to fetch a cache for the same, never-before-seen
    conversation_id. With correct double-checked locking those threads block
    until the first construction finishes, so exactly one instance is ever
    built and every thread observes the same object.
    """
    conversation_id = str(uuid4())
    build_count = 0
    build_count_lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()
    real_init = PhaseOutputCache.__init__

    def _slow_init(self) -> None:
        nonlocal build_count
        with build_count_lock:
            build_count += 1
        started.set()
        assert release.wait(timeout=5), "test setup deadlocked waiting for release"
        real_init(self)

    monkeypatch.setattr(PhaseOutputCache, "__init__", _slow_init)

    results: list[PhaseOutputCache] = []
    results_lock = threading.Lock()

    def _call() -> None:
        cache = conversation._get_or_create_phase_cache(conversation_id)
        with results_lock:
            results.append(cache)

    threads = [threading.Thread(target=_call) for _ in range(8)]
    threads[0].start()
    assert started.wait(timeout=5), "first thread never entered __init__"
    for t in threads[1:]:
        t.start()
    # Give the other threads a chance to reach (and block on)
    # _phase_caches_lock before releasing the first thread to finish.
    time.sleep(0.1)
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 8
    assert len({id(c) for c in results}) == 1
    assert build_count == 1


def test_phase_cache_slot_seeded_on_create_and_retained_through_a_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real create/send-message call path seeds and retains the same slot."""
    install_memory_stores(monkeypatch)
    from branding_team.api import main as main_mod

    create_resp = conversation._create_branding_conversation_impl(CreateConversationRequest())
    conversation_id = create_resp.conversation_id

    assert conversation_id in conversation._phase_caches
    seeded_cache = conversation._phase_caches[conversation_id]
    assert seeded_cache.get(BrandPhase.STRATEGIC_CORE, "any-hash") is None

    mock_agent = MagicMock()
    mock_agent.respond.return_value = (
        "Thanks, noted.",
        make_mission(
            company_name="TestCo",
            company_description="To be discussed.",
            target_audience="TBD",
        ),
        ["Next question?"],
        False,
    )
    monkeypatch.setattr(main_mod, "assistant_agent", mock_agent)

    conversation._send_branding_conversation_message_impl(
        conversation_id, SendMessageRequest(message="Our company is TestCo.")
    )

    assert conversation._phase_caches[conversation_id] is seeded_cache
