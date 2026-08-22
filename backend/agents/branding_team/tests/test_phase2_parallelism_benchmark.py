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

Every specialist's mocked LLM call reaches this barrier via
``asyncio.to_thread`` (see ``llm_service/strands_adapter.py``'s
``LLMClientModel.stream``), which offloads onto asyncio's default executor --
lazily created the first time it's needed, sized by a CPU-count heuristic
that is itself version-dependent (``os.cpu_count()`` pre-3.13,
``os.process_cpu_count()`` on 3.13+). Rather than chase that sizing hook
across Python versions, this test installs an explicit oversized default
executor directly on the event loop ``run_coroutine`` creates (by patching
``asyncio.events.new_event_loop``, the primitive ``asyncio.run`` itself calls
to create that loop), guaranteeing enough workers for the barrier regardless
of the runner's real core count or Python version. This is hidden coupling to
``run_coroutine``'s internal use of ``asyncio.run`` -- if it is ever
refactored to drive coroutines a different way (e.g. reusing an existing
loop), this patch would silently stop taking effect and the barrier could
then deadlock on a low-CPU runner for a reason unrelated to Phase 2 itself.
The ``executor_injected`` assertion below exists specifically to catch that:
it fails loudly, distinct from a barrier timeout, if the patched factory is
never invoked.

This also forces the dummy provider and clears the LLM client/Strands model
caches for the run. ``llm_service.config.resolve_provider()`` -- the sole
gate both ``factory.get_client()`` and ``strands_provider.get_strands_model()``
call -- resolves runtime config (Postgres, set via the ``/llm-config`` UI)
*ahead of* the ``LLM_PROVIDER`` env var, so merely setting the env var isn't
enough when a provider is persisted there; patching ``resolve_provider``
itself is the one interception point that holds regardless of env var or
runtime config. Without this, a developer or CI job with a different
provider already configured would silently build real provider clients
instead of ``DummyLLMClient`` -- the ``chat`` patch below would then never be
hit, and this benchmark would either fail on missing credentials or, worse,
send six real LLM requests instead of measuring the mocked latency.

Marked ``@pytest.mark.bench`` per ``backend/conftest.py``'s wall-clock
benchmark convention (sleep-based timing tests stay out of the default unit
run). The dedicated ``test-branding`` CI job un-skips it via its ``-m``
marker expression, so it still runs as a CI regression guard on every PR --
it just doesn't tax the fast default ``pytest`` suite teams run locally.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from branding_team.graphs.phase2_narrative import build_phase2_graph
from branding_team.graphs.shared import serialize_mission
from branding_team.shared.coro_runner import run_coroutine
from branding_team.tests.conftest import make_mission
from llm_service import DummyLLMClient, clear_client_cache

_NUM_SPECIALISTS = 6
_PER_CALL_DELAY_SECONDS = 1.0
# A true fan-out clears the barrier near-instantly (all six threads reach it
# within milliseconds of each other) and the whole run finishes in ~1s, so
# both bounds carry generous slack for scheduling jitter/CPU contention on
# shared CI runners -- while staying far below the ~6s a sequential chain, or
# ~2s a single-edge partial regression, would take.
_BARRIER_TIMEOUT_SECONDS = 10.0
_MAX_WALL_CLOCK_SECONDS = 8.0
_EXECUTOR_WORKERS = _NUM_SPECIALISTS + 2  # headroom above what the barrier needs

# Captured before the test below ever patches ``asyncio.events.new_event_loop``,
# so the replacement can still create a real loop instead of recursing into itself.
_real_new_event_loop = asyncio.events.new_event_loop


def _new_event_loop_with_generous_executor() -> asyncio.AbstractEventLoop:
    """Create a real event loop, pre-sized with a default executor that always
    clears ``_NUM_SPECIALISTS`` workers.

    Postconditions:
        Returns a fresh, unstarted event loop (via the real, pre-patch
        ``asyncio.events.new_event_loop``) whose default executor is a
        ``ThreadPoolExecutor`` with ``_EXECUTOR_WORKERS`` threads --
        independent of ``os.cpu_count()``/``os.process_cpu_count()``, so the
        loop's ``asyncio.to_thread`` offloads (what every mocked ``chat()``
        call rides) can never be capped below what the barrier requires.
        ``asyncio.run``'s own cleanup (``shutdown_default_executor``) still
        shuts this executor down when the loop closes.
    """
    loop = _real_new_event_loop()
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=_EXECUTOR_WORKERS, thread_name_prefix="phase2-benchmark")
    )
    return loop


@pytest.mark.bench
def test_phase2_specialists_execute_in_parallel_under_mocked_llm_latency() -> None:
    """Six specialists x 1s mocked LLM latency must finish in well under the
    sequential baseline.

    Fails if Phase 2 regresses from its zero-edge fan-out back to a
    sequential chain, or if any single edge is (re)introduced between two
    specialists (partial serialization) -- the shared barrier requires all
    six mocked LLM calls to be concurrently in-flight before any completes,
    so even a two-node chain deadlocks the barrier instead of slipping under
    the wall-clock bound. A separate call-count assertion distinguishes "the
    wrong number of LLM calls happened" (e.g. a specialist retries, or the
    graph gains/loses a node) from true serialization, so a failure here
    doesn't get misdiagnosed as the other. Together this proves the
    parallelism epic's latency claim as a durable CI guard.
    """
    barrier = threading.Barrier(_NUM_SPECIALISTS, timeout=_BARRIER_TIMEOUT_SECONDS)
    original_chat = DummyLLMClient.chat
    call_count_lock = threading.Lock()
    call_count = 0

    def slow_chat(self: DummyLLMClient, messages, **kwargs):
        nonlocal call_count
        with call_count_lock:
            call_count += 1
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

    executor_injected = False

    def tracking_new_event_loop() -> asyncio.AbstractEventLoop:
        nonlocal executor_injected
        loop = _new_event_loop_with_generous_executor()
        executor_injected = True
        return loop

    mission = make_mission()
    task = (
        f"Create a comprehensive brand strategy for the following company.\n\n"
        f"Branding Mission:\n{serialize_mission(mission)}"
    )

    with (
        patch("llm_service.config.resolve_provider", return_value="dummy"),
        patch("asyncio.events.new_event_loop", side_effect=tracking_new_event_loop),
        patch.object(DummyLLMClient, "chat", slow_chat),
    ):
        clear_client_cache()
        try:
            start = time.monotonic()
            run_coroutine(build_phase2_graph().invoke_async(task))
            elapsed = time.monotonic() - start
        finally:
            clear_client_cache()

    assert executor_injected, (
        "asyncio.events.new_event_loop was never called -- run_coroutine no longer "
        "creates its loop via asyncio.run's default path, so this benchmark's "
        "injected executor never took effect; the barrier/wall-clock results above "
        "cannot be trusted until this coupling is fixed"
    )
    assert call_count == _NUM_SPECIALISTS, (
        f"expected exactly {_NUM_SPECIALISTS} LLM calls (one per Phase 2 specialist), "
        f"got {call_count} -- a specialist retried, or the graph's specialist count "
        "changed; this is a different failure than serialization, fix the count "
        "mismatch before trusting the wall-clock assertion below"
    )
    assert elapsed <= _MAX_WALL_CLOCK_SECONDS, (
        f"Phase 2 took {elapsed:.2f}s with 6 specialists at "
        f"{_PER_CALL_DELAY_SECONDS}s/call mocked LLM latency -- expected <= "
        f"{_MAX_WALL_CLOCK_SECONDS}s for true parallel fan-out (~6s would mean "
        "specialists are running sequentially)"
    )
