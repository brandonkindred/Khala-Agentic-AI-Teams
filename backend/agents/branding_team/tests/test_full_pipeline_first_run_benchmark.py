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
*sum* of each phase's critical-path depth. The bounds (``_benchmark_helpers
.MIN_WALL_CLOCK_SECONDS``/``MAX_WALL_CLOCK_SECONDS``) were set by running this
benchmark locally under the mocked 1s latency and adding generous slack for
CI scheduling jitter -- tight enough that a phase's fan-out collapsing back
into a sequential chain (inflating that phase's critical-path depth back to
its full call count) pushes wall-clock past the ceiling, loose enough to
absorb normal jitter on a shared runner.

The expected call count (39, not the issue's stated "37 agents" taken on
faith) is likewise documented on ``_benchmark_helpers.EXPECTED_CALL_COUNT``.
Summing the merge-dict tables in ``orchestrator.py`` (``_PHASE1_NODE_MERGE``
=6, ``_PHASE2_NODE_MERGE``=6, ``_PHASE3_NODE_MERGE``=11,
``_PHASE4_NODE_MERGE``=2+len(CHANNEL_SPECS)=8, ``_PHASE5_NODE_MERGE``=7)
gives 38 distinct fan-out nodes, one call each -- but a run consistently
observes 39 (confirmed stable across repeated local runs).
``BrandComplianceAgent.evaluate()`` runs outside the graph via keyword
matching, not an LLM call, so it isn't the source; the extra call is most
likely one specialist's mocked turn being retried once (e.g. a
structured-output validation retry against the dummy provider's canned
response). Asserting the observed value, rather than a value back-derived to
match "37", is what actually guards against a regression in how many LLM
calls a first run makes.

Uses the shared ``_benchmark_helpers.mock_llm_latency`` harness (also used by
``test_phase2_parallelism_benchmark.py`` and
``test_cached_and_partial_change_benchmark.py``) for dummy-provider forcing,
the ``DummyLLMClient.chat`` latency/call-count injection, and LLM client
cache clearing -- see that helper's docstring for why each piece is needed.
This benchmark also passes ``min_executor_workers`` sized to Phase 4's
nine-way fan-out (the widest single-phase concurrency in the pipeline, see
``graphs/phase4_channel.py``), so the default asyncio executor can never be
undersized on a low-CPU CI runner and falsely serialize a phase's fan-out --
the same false-positive risk ``test_phase2_parallelism_benchmark.py``'s
module docstring describes in detail. The run-and-assert logic itself is
``_benchmark_helpers.run_and_assert_cold_baseline``, shared with
``test_cached_and_partial_change_benchmark.py`` so the two modules can't
silently drift out of sync on what a cold run is expected to look like.

Marked ``@pytest.mark.bench`` per ``backend/conftest.py``'s wall-clock
benchmark convention (skipped by default locally; the ``test-branding`` CI
job un-skips it via its ``-m bench`` marker expression).
"""

from __future__ import annotations

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
        PER_CALL_DELAY_SECONDS,
        min_executor_workers=EXECUTOR_WORKERS,
        executor_thread_name_prefix="full-pipeline-benchmark",
    ) as harness:
        run_and_assert_cold_baseline(
            orchestrator, mission, human_review, PhaseOutputCache(), harness
        )
