"""Regression guard for Phase 2's parallel fan-out latency.

``test_phase2_fan_out_regression.py`` proves the *correctness* half of Phase
2's zero-edge fan-out (no specialist's turn ever carries a sibling's output).
This module proves the *timing* half: with a fixed per-call LLM latency
injected into every specialist's turn, Phase 2's wall-clock time must stay
close to a single call's latency rather than scaling with the specialist
count -- so any accidental reintroduction of sequential edges (or a fan-in)
fails CI on a clear latency bound rather than silently degrading throughput.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from branding_team.graphs.phase2_narrative import build_phase2_graph
from branding_team.graphs.shared import serialize_mission
from branding_team.shared.coro_runner import run_coroutine
from branding_team.tests.conftest import make_mission
from llm_service import DummyLLMClient

_PER_CALL_DELAY_SECONDS = 1.0
_MAX_WALL_CLOCK_SECONDS = 3.0  # well under the ~6s a sequential chain would take


def test_phase2_specialists_execute_in_parallel_under_mocked_llm_latency() -> None:
    """Six specialists x 1s mocked LLM latency must finish in well under 6s.

    Fails if Phase 2 regresses from its zero-edge fan-out back to a
    sequential chain (or gains any edge that serializes specialist turns),
    proving the parallelism epic's latency claim as a durable CI guard.
    """
    original_chat = DummyLLMClient.chat

    def slow_chat(self: DummyLLMClient, messages, **kwargs):
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
