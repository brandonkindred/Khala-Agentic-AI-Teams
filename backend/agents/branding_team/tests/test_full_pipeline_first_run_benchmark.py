"""Baseline regression guard for a full, uncached branding-pipeline run.

``test_phase2_parallelism_benchmark.py`` proves Phase 2's internal fan-out
stays parallel. This module proves the pipeline-wide baseline that Story
#6959's caching benchmark builds on: a genuinely first-time
``BrandingTeamOrchestrator.run()`` call (a fresh, empty ``PhaseOutputCache``,
so every one of the five phases actually invokes its graph -- no cache hits)
fires every branding-agent LLM call exactly once, and completes within a
wall-clock window calibrated for a 1s-per-call mocked LLM latency.

The window is *not* simple "37 agents x 1s = 37s" sequential math -- Phase 2's
six specialists (and Phase 3's fan-out) already run concurrently within their
own phase (see ``test_phase2_parallelism_benchmark.py``), so a phase's
contribution to total wall-clock is that phase's internal critical-path depth,
not its total call count. The five phases themselves run strictly
sequentially (``_run_phases_with_cache`` invokes them one at a time, later
phases needing earlier phases' outputs as context), so total wall-clock is the
*sum* of each phase's critical-path depth. The bounds below were set by
running this benchmark locally under the mocked 1s latency and adding
generous slack for CI scheduling jitter -- tight enough that a phase's
fan-out collapsing back into a sequential chain (inflating that phase's
critical-path depth back to its full call count) pushes wall-clock past the
ceiling, loose enough to absorb normal jitter on a shared runner.

Uses the shared ``_benchmark_helpers.mock_llm_latency`` harness (also used by
``test_phase2_parallelism_benchmark.py``) for dummy-provider forcing, the
``DummyLLMClient.chat`` latency/call-count injection, and LLM client cache
clearing -- see that helper's docstring for why each piece is needed. This
benchmark also passes ``min_executor_workers`` sized to Phase 4's nine-way
fan-out (the widest single-phase concurrency in the pipeline, see
``graphs/phase4_channel.py``), so the default asyncio executor can never be
undersized on a low-CPU CI runner and falsely serialize a phase's fan-out --
the same false-positive risk ``test_phase2_parallelism_benchmark.py``'s
module docstring describes in detail.

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
# executor from ever being the bottleneck.
_MAX_PHASE_FAN_OUT = 9
_EXECUTOR_WORKERS = _MAX_PHASE_FAN_OUT + 4

# Measured locally at a stable ~9.15s across repeated runs: five phases run
# sequentially, each contributing its own internal critical-path depth (1
# call-depth for phases whose specialists all fan out with no cross-edges,
# more for a phase with a diverge -> converge -> fan-out shape such as Phase
# 3), not their full call count. The floor guards against the mocked latency
# silently not taking effect (e.g. the chat patch missing a call path); the
# ceiling has generous slack above the observed ~9s but still fails well
# before the ~39s a fully sequential regression (every phase's fan-out
# collapsing into a chain) would take.
_MIN_WALL_CLOCK_SECONDS = 6.0
_MAX_WALL_CLOCK_SECONDS = 20.0

# The value below is the count this benchmark actually observes, not the
# issue's stated "37 agents" taken on faith. Summing the merge-dict tables in
# ``orchestrator.py`` (``_PHASE1_NODE_MERGE``=6, ``_PHASE2_NODE_MERGE``=6,
# ``_PHASE3_NODE_MERGE``=11, ``_PHASE4_NODE_MERGE``=2+len(CHANNEL_SPECS)=8,
# ``_PHASE5_NODE_MERGE``=7) gives 38 distinct fan-out nodes, one call each --
# but a run consistently observes 39 (confirmed stable across repeated local
# runs). ``BrandComplianceAgent.evaluate()`` runs outside the graph via
# keyword matching, not an LLM call, so it isn't the source; the extra call
# is most likely one specialist's mocked turn being retried once (e.g. a
# structured-output validation retry against the dummy provider's canned
# response). Asserting the observed value here, rather than a value
# back-derived to match "37", is what actually guards against a regression in
# how many LLM calls a first run makes.
_EXPECTED_CALL_COUNT = 39


@pytest.mark.bench
def test_full_pipeline_first_run_executes_every_agent_within_expected_wall_clock() -> None:
    """A first (uncached) full pipeline run must fire every agent once and
    finish within the expected wall-clock window for 1s-per-call mocked LLM
    latency.

    Uses a fresh ``PhaseOutputCache()`` explicitly (rather than relying on
    ``run()``'s own default) so intent is unambiguous: this is the "no cache
    hits" baseline Story #6959's phase-cache benchmark will compare its
    second (fully cached) run against.
    """
    orchestrator = BrandingTeamOrchestrator()
    mission = make_mission()
    human_review = HumanReview(approved=False)

    with mock_llm_latency(
        _PER_CALL_DELAY_SECONDS,
        min_executor_workers=_EXECUTOR_WORKERS,
        executor_thread_name_prefix="full-pipeline-benchmark",
    ) as harness:
        start = time.monotonic()
        orchestrator.run(mission, human_review, phase_cache=PhaseOutputCache())
        elapsed = time.monotonic() - start

    assert harness.executor_injected, (
        "asyncio.events.new_event_loop was never called -- run_coroutine no longer "
        "creates its loop via asyncio.run's default path, so this benchmark's "
        "injected executor never took effect; the wall-clock result below cannot be "
        "trusted until this coupling is fixed"
    )
    assert harness.call_count == _EXPECTED_CALL_COUNT, (
        f"expected exactly {_EXPECTED_CALL_COUNT} LLM calls across all five phases of a "
        f"first (uncached) run, got {harness.call_count} -- a phase gained/lost a "
        "specialist, or a cache hit skipped a phase that should have run fresh"
    )
    assert elapsed >= _MIN_WALL_CLOCK_SECONDS, (
        f"first run completed in {elapsed:.2f}s, under the {_MIN_WALL_CLOCK_SECONDS}s floor "
        "-- the mocked 1s-per-call LLM latency may not be taking effect (e.g. the chat patch "
        "wasn't hit for every call), so this fast result cannot be trusted as a real timing "
        "measurement"
    )
    assert elapsed <= _MAX_WALL_CLOCK_SECONDS, (
        f"first run took {elapsed:.2f}s, over the {_MAX_WALL_CLOCK_SECONDS}s ceiling -- "
        "indicates a phase's internal fan-out regressed back to a sequential chain, "
        "inflating that phase's contribution to total wall-clock"
    )
