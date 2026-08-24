"""Cache-hit and cascading-invalidation regression guard for
``BrandingTeamOrchestrator.run()``, closing the loop Story #6959 opened.

``test_full_pipeline_first_run_benchmark.py`` proves the uncached ("first
run") baseline. This module proves the two scenarios that baseline exists to
be compared against:

1. A second run with an identical mission, reusing the same
   ``PhaseOutputCache`` -- every one of the five phases should hit, firing
   zero additional LLM calls and completing in a small fraction of the first
   run's wall-clock (Task #6978's AC: "second run ... ≤ 7s").
2. A third run whose mission differs only in a Phase-1-relevant field
   (``company_description``, which feeds ``StrategicCoreOutput``), still
   reusing the same cache -- Task #6978 frames this as "~Phase 1 time +
   cascaded downstream re-execution", not a partial-cache-hit. That phrasing
   is deliberate, not a simplification: ``shared/memoization.py``'s
   ``phase_input_hash`` hashes the *entire* serialized mission for every
   phase (see that module's docstring -- there is no per-phase mission-field
   subsetting anywhere in this codebase), so a change to *any* mission field
   changes every phase's input hash, not just the phase(s) that field
   semantically belongs to. A "Phase-1-only" mission change therefore misses
   on all five phases, exactly like a cold run -- this test asserts that
   cascade directly (every phase's LLM calls fire again) rather than
   asserting a partial hit that the current cache design cannot produce.

Uses the shared ``_benchmark_helpers.mock_llm_latency`` harness (also used by
``test_phase2_parallelism_benchmark.py`` and
``test_full_pipeline_first_run_benchmark.py``) for dummy-provider forcing,
the ``DummyLLMClient.chat`` latency/call-count injection, LLM client cache
clearing, and an oversized default executor sized to Phase 4's nine-way
fan-out -- see that helper's docstring for why each piece is needed. All
three runs in this test share one harness instance, so ``harness.call_count``
accumulates across them; each run's assertions compare against the call
count observed just before that run, not zero.

Marked ``@pytest.mark.bench`` per ``backend/conftest.py``'s wall-clock
benchmark convention (skipped by default locally; the ``test-branding`` CI
job un-skips it via its ``-m bench`` marker expression).
"""

from __future__ import annotations

import time

import pytest

from branding_team.models import HumanReview
from branding_team.orchestrator import BrandingTeamOrchestrator
from branding_team.shared.phase_output_cache import PhaseOutputCache
from branding_team.tests._benchmark_helpers import mock_llm_latency
from branding_team.tests.conftest import make_mission

_PER_CALL_DELAY_SECONDS = 1.0

# Phase 4 (``brand_experience_principler`` + six ``*_guide`` channel
# specialists + ``brand_architecture_builder`` + ``brand_in_action_illustrator``,
# see ``graphs/phase4_channel.py``) is the widest single-phase fan-out in the
# pipeline at 9 concurrent nodes; headroom above that keeps the injected
# executor from ever being the bottleneck. Mirrors
# ``test_full_pipeline_first_run_benchmark.py``.
_MAX_PHASE_FAN_OUT = 9
_EXECUTOR_WORKERS = _MAX_PHASE_FAN_OUT + 4

# Same bounds and derivation as ``test_full_pipeline_first_run_benchmark.py``:
# measured locally at a stable ~9.15s across repeated runs for a genuinely
# uncached run under 1s-per-call mocked latency. Reused here for both the
# cold run (#1) and the Phase-1-change run (#3), since #3's cascading
# invalidation makes it behave identically to a cold run.
_MIN_WALL_CLOCK_SECONDS = 6.0
_MAX_WALL_CLOCK_SECONDS = 20.0

# Observed, not derived from the issue's "37 agents" -- see
# ``test_full_pipeline_first_run_benchmark.py``'s module docstring for the
# full accounting of why this is 39, not 37 or 38.
_EXPECTED_CALL_COUNT = 39

# Task #6978's literal AC for the second (fully cached) run. A cache hit does
# no sleeping at all, so this ceiling has generous headroom above the ~0s a
# healthy cache hit actually takes while still failing well below the cold
# run's ~9s observed baseline if the cache silently stops taking effect.
_CACHED_RUN_MAX_WALL_CLOCK_SECONDS = 7.0


@pytest.mark.bench
def test_second_identical_run_hits_cache_and_stays_under_seven_seconds() -> None:
    """A second run with a byte-identical mission and the same cache
    instance must fire zero additional LLM calls and finish well under the
    first (cold) run's wall-clock, honoring Task #6978's "≤ 7s" AC."""
    orchestrator = BrandingTeamOrchestrator()
    mission = make_mission()
    human_review = HumanReview(approved=False)
    cache = PhaseOutputCache()

    with mock_llm_latency(
        _PER_CALL_DELAY_SECONDS,
        min_executor_workers=_EXECUTOR_WORKERS,
        executor_thread_name_prefix="cached-run-benchmark",
    ) as harness:
        start = time.monotonic()
        orchestrator.run(mission, human_review, phase_cache=cache)
        cold_elapsed = time.monotonic() - start

        assert harness.executor_injected, (
            "asyncio.events.new_event_loop was never called -- run_coroutine no longer "
            "creates its loop via asyncio.run's default path, so this benchmark's injected "
            "executor never took effect; the wall-clock results below cannot be trusted "
            "until this coupling is fixed"
        )
        assert harness.call_count == _EXPECTED_CALL_COUNT, (
            f"expected exactly {_EXPECTED_CALL_COUNT} LLM calls on the cold first run, got "
            f"{harness.call_count} -- see test_full_pipeline_first_run_benchmark.py for the "
            "full accounting of this count"
        )
        assert _MIN_WALL_CLOCK_SECONDS <= cold_elapsed <= _MAX_WALL_CLOCK_SECONDS, (
            f"cold first run took {cold_elapsed:.2f}s, outside the expected "
            f"[{_MIN_WALL_CLOCK_SECONDS}, {_MAX_WALL_CLOCK_SECONDS}]s window -- this run is "
            "this test's own baseline for the cached run below, so it must itself look like "
            "a genuine uncached run before that comparison means anything"
        )

        call_count_before_cached_run = harness.call_count
        start = time.monotonic()
        orchestrator.run(mission, human_review, phase_cache=cache)
        cached_elapsed = time.monotonic() - start

    assert harness.call_count == call_count_before_cached_run, (
        f"second run with an identical mission fired {harness.call_count - call_count_before_cached_run} "
        "additional LLM call(s) -- every one of the five phases should have hit the shared "
        "PhaseOutputCache and skipped invoking its agents entirely"
    )
    assert cached_elapsed <= _CACHED_RUN_MAX_WALL_CLOCK_SECONDS, (
        f"second (fully cached) run took {cached_elapsed:.2f}s, over the "
        f"{_CACHED_RUN_MAX_WALL_CLOCK_SECONDS}s ceiling from Task #6978's AC -- a healthy "
        "all-cache-hit run does no LLM-call sleeping at all, so this indicates the cache "
        "stopped taking effect"
    )


@pytest.mark.bench
def test_phase_one_relevant_mission_change_cascades_to_every_phase() -> None:
    """A mission change to a Phase-1-relevant field, reusing the same warm
    cache, must miss on every phase (not just Phase 1) -- because
    ``phase_input_hash`` hashes the entire mission for every phase -- firing
    the full agent count again and taking roughly the cold-run's wall-clock,
    not a partial-hit fraction of it."""
    orchestrator = BrandingTeamOrchestrator()
    original_mission = make_mission()
    changed_mission = make_mission(
        company_description="A completely different value proposition for the same company"
    )
    human_review = HumanReview(approved=False)
    cache = PhaseOutputCache()

    with mock_llm_latency(
        _PER_CALL_DELAY_SECONDS,
        min_executor_workers=_EXECUTOR_WORKERS,
        executor_thread_name_prefix="partial-change-benchmark",
    ) as harness:
        orchestrator.run(original_mission, human_review, phase_cache=cache)
        assert harness.executor_injected, (
            "asyncio.events.new_event_loop was never called -- run_coroutine no longer "
            "creates its loop via asyncio.run's default path, so this benchmark's injected "
            "executor never took effect; the wall-clock results below cannot be trusted "
            "until this coupling is fixed"
        )
        assert harness.call_count == _EXPECTED_CALL_COUNT, (
            f"expected exactly {_EXPECTED_CALL_COUNT} LLM calls on the cold first run, got "
            f"{harness.call_count} -- see test_full_pipeline_first_run_benchmark.py for the "
            "full accounting of this count"
        )

        call_count_before_changed_run = harness.call_count
        start = time.monotonic()
        orchestrator.run(changed_mission, human_review, phase_cache=cache)
        changed_elapsed = time.monotonic() - start

    calls_from_changed_run = harness.call_count - call_count_before_changed_run
    assert calls_from_changed_run == _EXPECTED_CALL_COUNT, (
        f"expected the Phase-1-relevant mission change to cascade to every phase, firing all "
        f"{_EXPECTED_CALL_COUNT} LLM calls again, but only {calls_from_changed_run} fired -- "
        "phase_input_hash hashes the entire mission for every phase (see "
        "shared/memoization.py's docstring), so a changed mission field should miss on all "
        "five phases, not just Phase 1"
    )
    assert _MIN_WALL_CLOCK_SECONDS <= changed_elapsed <= _MAX_WALL_CLOCK_SECONDS, (
        f"Phase-1-relevant-change run took {changed_elapsed:.2f}s, outside the expected "
        f"[{_MIN_WALL_CLOCK_SECONDS}, {_MAX_WALL_CLOCK_SECONDS}]s window -- since every phase "
        "should miss and recompute, this run's wall-clock should look like a cold run's, "
        "same as test_full_pipeline_first_run_benchmark.py's baseline"
    )
