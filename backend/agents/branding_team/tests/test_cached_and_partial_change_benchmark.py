"""Cache-hit and selective-invalidation regression guard for
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
   reusing the same cache. ``shared/memoization.py``'s ``phase_input_hash``
   now accepts a per-phase ``mission_fields`` allowlist (wired into
   ``orchestrator.py``'s ``_PHASE_SPEC`` from the mission-field dependency
   analysis), so a change to a field outside a phase's own allowlist no
   longer touches that phase's input hash. ``company_description`` is on
   STRATEGIC_CORE's allowlist and no other phase's, so only STRATEGIC_CORE's
   cache entry misses; every downstream phase's cache key still folds in
   STRATEGIC_CORE's *output*, and since the dummy provider's structured
   output is a deterministic function of the request (not the specific
   mission text), STRATEGIC_CORE's recomputed output is identical to what
   was cached, so no downstream phase's cache entry is invalidated either.
   This test asserts exactly that: only STRATEGIC_CORE's six calls (five
   specialists + one compositor, see ``graphs/phase1_strategic_core.py``)
   fire again, not the full ``EXPECTED_CALL_COUNT``.

Uses the shared ``_benchmark_helpers`` module (also used by
``test_phase2_parallelism_benchmark.py`` and
``test_full_pipeline_first_run_benchmark.py``) for dummy-provider forcing,
the ``DummyLLMClient.chat`` latency/call-count injection, LLM client cache
clearing, an oversized default executor sized to Phase 4's nine-way fan-out,
and the cold-run constants/assertion body -- see that module's docstrings for
why each piece is needed. All three runs in each test share one harness
instance, so ``harness.call_count`` accumulates across them;
``run_and_assert_cold_baseline`` and this module's own assertions compare
against the call count observed just before each run, not zero.

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
from branding_team.tests._benchmark_helpers import (
    EXECUTOR_WORKERS,
    PER_CALL_DELAY_SECONDS,
    mock_llm_latency,
    run_and_assert_cold_baseline,
)
from branding_team.tests.conftest import make_mission

# Task #6978's literal AC for the second (fully cached) run. A cache hit does
# no sleeping at all, so this ceiling has generous headroom above the ~0s a
# healthy cache hit actually takes while still failing well below the cold
# run's ~9s observed baseline if the cache silently stops taking effect.
_CACHED_RUN_MAX_WALL_CLOCK_SECONDS = 7.0

# STRATEGIC_CORE's five specialists (one call each, run concurrently) plus
# its one compositor (a sixth call, sequential after the fan-out) -- see
# ``graphs/phase1_strategic_core.py``. This is the only phase a
# STRATEGIC_CORE-allowlisted mission-field change invalidates.
_STRATEGIC_CORE_CALL_COUNT = 6

# STRATEGIC_CORE's critical-path depth under 1s-per-call mocked latency: one
# delay for the five-way fan-out (they run concurrently) plus one more for
# the compositor that depends on all five. Bounds carry generous slack for
# CI scheduling jitter while still failing well below the ~9s full cold-run
# baseline if this scenario silently stops being phase-1-only.
_PHASE_ONE_ONLY_MIN_WALL_CLOCK_SECONDS = 2 * PER_CALL_DELAY_SECONDS * 0.5
_PHASE_ONE_ONLY_MAX_WALL_CLOCK_SECONDS = 6.0


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
        PER_CALL_DELAY_SECONDS,
        min_executor_workers=EXECUTOR_WORKERS,
        executor_thread_name_prefix="cached-run-benchmark",
    ) as harness:
        run_and_assert_cold_baseline(orchestrator, mission, human_review, cache, harness)

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
def test_phase_one_relevant_mission_change_reinvokes_only_strategic_core() -> None:
    """A mission change to a field on STRATEGIC_CORE's ``mission_fields``
    allowlist (``company_description``) and no other phase's, reusing the
    same warm cache, must miss only STRATEGIC_CORE's cache entry -- and,
    because the dummy provider's structured output for a given request is
    deterministic regardless of the mission text, STRATEGIC_CORE's
    recomputed output matches what every downstream phase's cache key
    already expects, so no downstream phase is invalidated either. This is
    the epic's whole point: firing STRATEGIC_CORE's six calls again, not the
    full pipeline's ``EXPECTED_CALL_COUNT``, and finishing in roughly
    STRATEGIC_CORE's own critical-path depth rather than the full cold
    run's wall-clock."""
    orchestrator = BrandingTeamOrchestrator()
    original_mission = make_mission()
    changed_mission = make_mission(
        company_description="A completely different value proposition for the same company"
    )
    human_review = HumanReview(approved=False)
    cache = PhaseOutputCache()

    with mock_llm_latency(
        PER_CALL_DELAY_SECONDS,
        min_executor_workers=EXECUTOR_WORKERS,
        executor_thread_name_prefix="partial-change-benchmark",
    ) as harness:
        run_and_assert_cold_baseline(orchestrator, original_mission, human_review, cache, harness)

        calls_before = harness.call_count
        start = time.monotonic()
        orchestrator.run(changed_mission, human_review, phase_cache=cache)
        elapsed = time.monotonic() - start

    calls_made = harness.call_count - calls_before
    assert calls_made == _STRATEGIC_CORE_CALL_COUNT, (
        f"expected exactly {_STRATEGIC_CORE_CALL_COUNT} LLM calls (STRATEGIC_CORE only) for a "
        f"STRATEGIC_CORE-allowlisted-only mission change, got {calls_made} -- either the "
        "mission_fields allowlist stopped scoping this phase's cache key, or STRATEGIC_CORE's "
        "recompute stopped matching what downstream phases' cache keys expect"
    )
    assert (
        _PHASE_ONE_ONLY_MIN_WALL_CLOCK_SECONDS <= elapsed <= _PHASE_ONE_ONLY_MAX_WALL_CLOCK_SECONDS
    ), (
        f"phase-1-only recompute took {elapsed:.2f}s, outside the expected "
        f"[{_PHASE_ONE_ONLY_MIN_WALL_CLOCK_SECONDS}, {_PHASE_ONE_ONLY_MAX_WALL_CLOCK_SECONDS}]s window"
    )
