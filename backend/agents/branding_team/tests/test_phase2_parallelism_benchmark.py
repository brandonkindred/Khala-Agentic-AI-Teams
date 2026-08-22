"""Regression guard for Phase 2's parallel fan-out latency.

``test_phase2_fan_out_regression.py`` proves the *correctness* half of Phase
2's zero-edge fan-out (no specialist's turn ever carries a sibling's output).
This module proves the *timing* half: with a fixed per-call LLM latency
injected into every specialist's turn, Phase 2's wall-clock time must stay
close to a single call's latency rather than scaling with the specialist
count -- so any accidental reintroduction of sequential edges (or a fan-in)
fails CI on a clear latency bound rather than silently degrading throughput.

A pure wall-clock bound alone is too weak: a *partial* regression (e.g. one
specialist made to depend on another, leaving the other five as true entry
points) would still finish comfortably inside a generous multi-second
window, since the longest chain would only be two 1s calls deep. A
``threading.Barrier`` sized to all six specialists closes that gap: it
requires every specialist's mocked LLM call to be concurrently in-flight
before any of them is allowed to proceed, so *any* serialized edge -- full
chain or a single pair -- times out the barrier and fails the test loudly,
rather than merely being fast enough to slip under a numeric threshold.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from branding_team.graphs.phase2_narrative import build_phase2_graph
from branding_team.graphs.shared import serialize_mission
from branding_team.shared.coro_runner import run_coroutine
from branding_team.tests.conftest import make_mission
from llm_service import DummyLLMClient

_NUM_SPECIALISTS = 6
_PER_CALL_DELAY_SECONDS = 1.0
_BARRIER_TIMEOUT_SECONDS = 2.0  # generous scheduling margin; true fan-out clears near-instantly
_MAX_WALL_CLOCK_SECONDS = 3.0  # well under the ~6s a sequential chain would take


def test_phase2_specialists_execute_in_parallel_under_mocked_llm_latency() -> None:
    """Six specialists x 1s mocked LLM latency must finish in well under 6s.

    Fails if Phase 2 regresses from its zero-edge fan-out back to a
    sequential chain, or if any single edge is (re)introduced between two
    specialists (partial serialization) -- the shared barrier requires all
    six mocked LLM calls to be concurrently in-flight before any completes,
    so even a two-node chain deadlocks the barrier instead of slipping under
    the wall-clock bound. Together this proves the parallelism epic's
    latency claim as a durable CI guard.
    """
    barrier = threading.Barrier(_NUM_SPECIALISTS, timeout=_BARRIER_TIMEOUT_SECONDS)
    original_chat = DummyLLMClient.chat

    def slow_chat(self: DummyLLMClient, messages, **kwargs):
        try:
            barrier.wait()
        except threading.BrokenBarrierError as exc:
            raise RuntimeError(
                f"only some of the {_NUM_SPECIALISTS} Phase 2 specialists reached the "
                f"LLM call within {_BARRIER_TIMEOUT_SECONDS}s of each other -- indicates "
                "a sequential edge (or fan-in) was (re)introduced, serializing the "
                "fan-out this benchmark guards against"
            ) from exc
        time.sleep(_PER_CALL_DELAY_SECONDS)
        return original_chat(self, messages, **kwargs)

    mission = make_mission()
    task = (
        f"Create a comprehensive brand strategy for the following company.\n\n"
        f"Branding Mission:\n{serialize_mission(mission)}"
    )

    with patch.object(DummyLLMClient, "chat", slow_chat):
        start = time.monotonic()
        run_coroutine(build_phase2_graph().invoke_async(task))
        elapsed = time.monotonic() - start

    assert elapsed <= _MAX_WALL_CLOCK_SECONDS, (
        f"Phase 2 took {elapsed:.2f}s with 6 specialists at "
        f"{_PER_CALL_DELAY_SECONDS}s/call mocked LLM latency -- expected <= "
        f"{_MAX_WALL_CLOCK_SECONDS}s for true parallel fan-out (~6s would mean "
        "specialists are running sequentially)"
    )
