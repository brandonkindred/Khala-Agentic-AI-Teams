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
   cascade directly (every phase's LLM calls fire again, via the same
   ``_benchmark_helpers.run_and_assert_cold_baseline`` the cold-run baseline
   test uses) rather than asserting a partial hit that the current cache
   design cannot produce.

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
        PER_CALL_DELAY_SECONDS,
        min_executor_workers=EXECUTOR_WORKERS,
        executor_thread_name_prefix="partial-change-benchmark",
    ) as harness:
        run_and_assert_cold_baseline(orchestrator, original_mission, human_review, cache, harness)
        # The assertion below is intentionally the same shape as the cold run
        # above (via run_and_assert_cold_baseline): a Phase-1-relevant
        # mission change is expected to behave exactly like a cold run,
        # since phase_input_hash hashes the whole mission per phase (see
        # module docstring) and so misses on every one of the five phases,
        # not just Phase 1.
        run_and_assert_cold_baseline(orchestrator, changed_mission, human_review, cache, harness)
