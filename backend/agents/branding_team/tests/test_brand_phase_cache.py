"""Unit tests for the per-brand phase-cache registry in ``branding_team.api.main``.

Covers ``_get_brand_cache`` directly (empty-on-first-use, identity retention
across calls, distinct handles per brand, thread safety) plus a test proving
``_run_branding_core`` threads the exact per-brand cache into
``orchestrator.run`` when ``brand_id`` is set. Mirrors
``tests/test_conversation_phase_cache.py``, whose ``_get_or_create_phase_cache``
this registry is a per-brand counterpart to.
"""

from __future__ import annotations

import threading
import time
from uuid import uuid4

import pytest

from branding_team.api import main as api_main
from branding_team.models import BrandPhase, StrategicCoreOutput
from branding_team.shared import job_store
from branding_team.shared.phase_output_cache import PhaseOutputCache


def test_get_brand_cache_starts_empty_for_a_fresh_brand() -> None:
    brand_id = str(uuid4())
    cache = api_main._get_brand_cache(brand_id)
    assert isinstance(cache, PhaseOutputCache)
    assert cache.get(BrandPhase.STRATEGIC_CORE, "any-hash") is None


def test_get_brand_cache_is_retained_across_calls() -> None:
    """Same brand_id -> same instance, and mutations are visible later."""
    brand_id = str(uuid4())
    output = StrategicCoreOutput(brand_purpose="Ship calm software")
    first = api_main._get_brand_cache(brand_id)
    first.put(BrandPhase.STRATEGIC_CORE, "hash-1", output)

    second = api_main._get_brand_cache(brand_id)
    assert second is first
    assert second.get(BrandPhase.STRATEGIC_CORE, "hash-1") == output


def test_get_brand_cache_returns_distinct_handles_per_brand() -> None:
    first_id, second_id = str(uuid4()), str(uuid4())
    first_cache = api_main._get_brand_cache(first_id)
    second_cache = api_main._get_brand_cache(second_id)

    assert first_cache is not second_cache


def test_get_brand_cache_thread_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent first calls for the same new brand_id must not double-construct."""
    brand_id = str(uuid4())
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
        cache = api_main._get_brand_cache(brand_id)
        with results_lock:
            results.append(cache)

    threads = [threading.Thread(target=_call) for _ in range(8)]
    threads[0].start()
    assert started.wait(timeout=5), "first thread never entered __init__"
    for t in threads[1:]:
        t.start()
    time.sleep(0.1)
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 8
    assert len({id(c) for c in results}) == 1
    assert build_count == 1


def test_run_branding_core_threads_the_brands_phase_cache_into_orchestrator_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a brand_id is known, the exact per-brand cache reaches
    ``orchestrator.run``, not a copy or a fresh instance."""
    monkeypatch.setattr(job_store, "update_job_if_not_cancelled", lambda *a, **kw: True)

    brand_id = str(uuid4())
    expected_cache = api_main._get_brand_cache(brand_id)

    captured: dict = {}

    class _Result:
        def model_dump(self):
            return {}

    class _Orchestrator:
        def run(self, **kwargs):
            captured.update(kwargs)
            return _Result()

    monkeypatch.setattr(api_main, "orchestrator", _Orchestrator())

    api_main._run_branding_core(
        job_id="job-cache",
        mission=object(),
        human_review=object(),
        brand_checks=[],
        client_id="client-1",
        brand_id=brand_id,
        include_market_research=False,
        include_design_assets=False,
        target_phase=None,
    )

    assert captured["phase_cache"] is expected_cache


def test_run_branding_core_uses_a_fresh_cache_when_brand_id_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No brand_id -> orchestrator.run still gets a (fresh, empty) PhaseOutputCache."""
    monkeypatch.setattr(job_store, "update_job_if_not_cancelled", lambda *a, **kw: True)

    captured: dict = {}

    class _Result:
        def model_dump(self):
            return {}

    class _Orchestrator:
        def run(self, **kwargs):
            captured.update(kwargs)
            return _Result()

    monkeypatch.setattr(api_main, "orchestrator", _Orchestrator())

    api_main._run_branding_core(
        job_id="job-no-brand",
        mission=object(),
        human_review=object(),
        brand_checks=[],
        client_id=None,
        brand_id=None,
        include_market_research=False,
        include_design_assets=False,
        target_phase=None,
    )

    assert isinstance(captured["phase_cache"], PhaseOutputCache)
