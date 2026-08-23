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

Only ``llm_service.config.resolve_provider`` and ``DummyLLMClient.chat`` are
patched (see that module's docstring for why patching the env var alone is
insufficient when a provider is Postgres-persisted); ``clear_client_cache()``
is called before and after so no cached client from an earlier test or a
different provider leaks in.

Marked ``@pytest.mark.bench`` per ``backend/conftest.py``'s wall-clock
benchmark convention (skipped by default locally; the ``test-branding`` CI
job un-skips it via its ``-m bench`` marker expression).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from branding_team.models import HumanReview
from branding_team.orchestrator import BrandingTeamOrchestrator
from branding_team.shared.phase_output_cache import PhaseOutputCache
from branding_team.tests.conftest import make_mission
from llm_service import DummyLLMClient, clear_client_cache

_PER_CALL_DELAY_SECONDS = 1.0

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
    original_chat = DummyLLMClient.chat
    call_count_lock = threading.Lock()
    call_count = 0

    def slow_chat(self: DummyLLMClient, messages, **kwargs):
        nonlocal call_count
        with call_count_lock:
            call_count += 1
        time.sleep(_PER_CALL_DELAY_SECONDS)
        return original_chat(self, messages, **kwargs)

    orchestrator = BrandingTeamOrchestrator()
    mission = make_mission()
    human_review = HumanReview(approved=False)

    with (
        patch("llm_service.config.resolve_provider", return_value="dummy"),
        patch.object(DummyLLMClient, "chat", slow_chat),
    ):
        clear_client_cache()
        try:
            start = time.monotonic()
            orchestrator.run(mission, human_review, phase_cache=PhaseOutputCache())
            elapsed = time.monotonic() - start
        finally:
            clear_client_cache()

    assert call_count == _EXPECTED_CALL_COUNT, (
        f"expected exactly {_EXPECTED_CALL_COUNT} LLM calls across all five phases of a "
        f"first (uncached) run, got {call_count} -- a phase gained/lost a specialist, or "
        "a cache hit skipped a phase that should have run fresh"
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
