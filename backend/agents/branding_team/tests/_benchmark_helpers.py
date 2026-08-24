"""Shared mock-LLM-latency harness for branding_team's ``@pytest.mark.bench`` tests.

Every wall-clock benchmark in this suite needs the same three things: force
the dummy provider (``llm_service.config.resolve_provider`` is the sole gate
both ``factory.get_client()`` and ``strands_provider.get_strands_model()``
call, so patching only the ``LLM_PROVIDER`` env var is insufficient when a
provider is Postgres-persisted), inject a fixed sleep + call counter into
``DummyLLMClient.chat``, and clear the LLM client cache around the run so no
cached client from an earlier test or a different provider leaks in. A
benchmark that measures a graph fan-out's wall-clock also needs an oversized
default executor on the event loop ``run_coroutine`` creates: every mocked
``chat()`` call reaches this harness via ``asyncio.to_thread`` (see
``llm_service/strands_adapter.py``'s ``LLMClientModel.stream``), which
offloads onto asyncio's default executor -- lazily created and sized by a
CPU-count heuristic that is itself version-dependent. Without an explicit
oversized executor, a fan-out wider than the runner's real core count can
serialize on a shared CI runner for a reason unrelated to the pipeline code
under test, producing a false-positive wall-clock breach.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from typing import Callable, Iterator, Optional
from unittest.mock import patch

from llm_service import DummyLLMClient, clear_client_cache

# Per-call mocked LLM latency every wall-clock benchmark in this suite uses.
PER_CALL_DELAY_SECONDS = 1.0

# Phase 4 (``brand_experience_principler`` + six ``*_guide`` channel
# specialists + ``brand_architecture_builder`` + ``brand_in_action_illustrator``,
# see ``graphs/phase4_channel.py``) is the widest single-phase fan-out in the
# pipeline at 9 concurrent nodes; headroom above that keeps a benchmark's
# injected executor from ever being the bottleneck.
MAX_PHASE_FAN_OUT = 9
EXECUTOR_WORKERS = MAX_PHASE_FAN_OUT + 4

# Measured locally at a stable ~9.15s across repeated runs: five phases run
# sequentially, each contributing its own internal critical-path depth (see
# ``test_full_pipeline_first_run_benchmark.py``'s module docstring for the
# full reasoning). The floor guards against the mocked latency silently not
# taking effect; the ceiling has generous slack above the observed ~9s but
# still fails well before the ~39s a fully sequential regression would take.
MIN_WALL_CLOCK_SECONDS = 6.0
MAX_WALL_CLOCK_SECONDS = 20.0

# The value below is the count a cold run actually observes, not the "37
# agents" the originating issue stated on faith -- see
# ``test_full_pipeline_first_run_benchmark.py``'s module docstring for the
# full accounting of why this is 39, not 37 or 38.
EXPECTED_CALL_COUNT = 39


class MockLLMLatencyHarness:
    """Mutable state a ``mock_llm_latency`` context manager exposes to its caller.

    Invariants:
        ``call_count`` only ever increases, and only from inside the mocked
        ``DummyLLMClient.chat``; ``executor_injected`` flips permanently to
        ``True`` the first time (if ever) the injected event-loop factory runs.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.executor_injected = False
        self._lock = threading.Lock()

    def _record_call(self) -> None:
        with self._lock:
            self.call_count += 1


@contextmanager
def mock_llm_latency(
    delay_seconds: float,
    *,
    min_executor_workers: Optional[int] = None,
    executor_thread_name_prefix: str = "branding-benchmark",
    on_call: Optional[Callable[[], None]] = None,
) -> Iterator[MockLLMLatencyHarness]:
    """Force the dummy LLM provider and inject fixed per-call latency for one benchmark run.

    Preconditions:
        ``delay_seconds`` must be a non-negative float (seconds to sleep on
        each mocked call). ``min_executor_workers``, when given, must be a
        positive integer at least the largest number of LLM calls the
        benchmarked code path can have concurrently in flight. ``on_call``,
        when given, must be thread-safe: it is invoked synchronously on every
        mocked call before the sleep (e.g. a ``threading.Barrier.wait`` a
        caller uses to prove true concurrency), and because those calls reach
        this harness via ``asyncio.to_thread``, multiple LLM calls -- and so
        multiple concurrent invocations of ``on_call`` -- may be in flight at
        once whenever the benchmarked code path fans out; any exception it
        raises propagates out of the mocked call.
    Postconditions:
        Yields a ``MockLLMLatencyHarness`` whose ``call_count`` reflects every
        invocation of the mocked ``DummyLLMClient.chat`` so far -- incremented
        at entry, before the injected sleep and before the underlying
        ``original_chat`` call returns, so a call that raises still counts as
        an invocation even though it never completed -- and whose ``executor_injected`` is
        ``True`` iff ``min_executor_workers`` was given and the patched
        event-loop factory actually ran at least once. ``llm_service.config
        .resolve_provider`` is patched to return ``"dummy"`` and
        ``DummyLLMClient.chat`` is patched with the latency/counting wrapper
        for the duration of the ``with`` block; the LLM client cache is
        cleared both on entry and on exit (even if the block raises), so
        neither this run's cached clients nor a stale one from an earlier
        test leaks across the boundary. When ``min_executor_workers`` is
        given, ``asyncio.events.new_event_loop`` is also patched so any event
        loop created inside the block gets a ``ThreadPoolExecutor`` default
        executor sized to exactly ``min_executor_workers`` workers,
        independent of the runner's real core count.

    Raises:
        ValueError: if ``delay_seconds`` is negative, or ``min_executor_workers``
            is given and is not a positive integer -- raised immediately, before
            any patching, so a harness-misuse mistake never masquerades as a
            benchmark regression.
    """
    if delay_seconds < 0:
        raise ValueError(f"delay_seconds must be non-negative, got {delay_seconds}")
    if min_executor_workers is not None and min_executor_workers < 1:
        raise ValueError(
            f"min_executor_workers must be a positive integer, got {min_executor_workers}"
        )

    harness = MockLLMLatencyHarness()
    original_chat = DummyLLMClient.chat

    def slow_chat(self: DummyLLMClient, messages, **kwargs):
        harness._record_call()
        if on_call is not None:
            on_call()
        time.sleep(delay_seconds)
        return original_chat(self, messages, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch("llm_service.config.resolve_provider", return_value="dummy"))
        stack.enter_context(patch.object(DummyLLMClient, "chat", slow_chat))

        if min_executor_workers is not None:
            real_new_event_loop = asyncio.events.new_event_loop

            def tracking_new_event_loop() -> asyncio.AbstractEventLoop:
                loop = real_new_event_loop()
                loop.set_default_executor(
                    ThreadPoolExecutor(
                        max_workers=min_executor_workers,
                        thread_name_prefix=executor_thread_name_prefix,
                    )
                )
                harness.executor_injected = True
                return loop

            stack.enter_context(
                patch("asyncio.events.new_event_loop", side_effect=tracking_new_event_loop)
            )

        clear_client_cache()
        try:
            yield harness
        finally:
            clear_client_cache()


def run_and_assert_cold_baseline(
    orchestrator,
    mission,
    human_review,
    phase_cache,
    harness: MockLLMLatencyHarness,
) -> float:
    """Run one cold (or cold-equivalent) pipeline pass and assert it matches
    the shared first-run baseline every benchmark in this suite compares
    against.

    "Cold-equivalent" covers both a genuinely first call against an empty
    cache and a later call whose mission change invalidates every phase's
    cache entry (see ``test_cached_and_partial_change_benchmark.py``'s
    Phase-1-cascade scenario) -- both cases fire the same
    ``EXPECTED_CALL_COUNT`` calls and take the same wall-clock window, so
    they share this one assertion body.

    Preconditions:
        - Must be called from inside a ``mock_llm_latency(...)`` block whose
          ``min_executor_workers`` is at least ``EXECUTOR_WORKERS``.
        - ``orchestrator`` is a ``BrandingTeamOrchestrator``; ``mission`` and
          ``human_review`` are valid arguments to its ``run`` method;
          ``phase_cache`` is the ``PhaseOutputCache`` this run's phases are
          expected to entirely miss against (either empty, or warm only for
          entries this run's input hashes won't match).
    Postconditions:
        - Returns the wall-clock elapsed seconds for this run.
        - Asserts ``harness.executor_injected`` is ``True``, that exactly
          ``EXPECTED_CALL_COUNT`` LLM calls fired during this run (measured
          as the increase in ``harness.call_count`` across the call, not its
          absolute value -- so this is safe to call more than once against
          one shared harness), and that the elapsed time falls within
          ``[MIN_WALL_CLOCK_SECONDS, MAX_WALL_CLOCK_SECONDS]``.
    """
    calls_before = harness.call_count
    start = time.monotonic()
    orchestrator.run(mission, human_review, phase_cache=phase_cache)
    elapsed = time.monotonic() - start

    assert harness.executor_injected, (
        "asyncio.events.new_event_loop was never called -- run_coroutine no longer "
        "creates its loop via asyncio.run's default path, so this benchmark's injected "
        "executor never took effect; the wall-clock result below cannot be trusted "
        "until this coupling is fixed"
    )
    calls_made = harness.call_count - calls_before
    assert calls_made == EXPECTED_CALL_COUNT, (
        f"expected exactly {EXPECTED_CALL_COUNT} LLM calls for this cold (or "
        f"cold-equivalent) run, got {calls_made} -- see "
        "test_full_pipeline_first_run_benchmark.py's module docstring for the full "
        "accounting of this count"
    )
    assert MIN_WALL_CLOCK_SECONDS <= elapsed <= MAX_WALL_CLOCK_SECONDS, (
        f"cold (or cold-equivalent) run took {elapsed:.2f}s, outside the expected "
        f"[{MIN_WALL_CLOCK_SECONDS}, {MAX_WALL_CLOCK_SECONDS}]s window"
    )
    return elapsed
