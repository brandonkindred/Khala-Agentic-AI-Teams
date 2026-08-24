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
        ``delay_seconds`` is the sleep injected into every mocked
        ``DummyLLMClient.chat`` call; ``min_executor_workers``, when given, is
        at least the largest number of LLM calls the benchmarked code path can
        have concurrently in flight; ``on_call``, when given, is invoked
        synchronously on every mocked call before the sleep (e.g. a
        ``threading.Barrier.wait`` a caller uses to prove true concurrency) --
        any exception it raises propagates out of the mocked call.
    Postconditions:
        Yields a ``MockLLMLatencyHarness`` whose ``call_count`` reflects every
        completed mocked call so far, and whose ``executor_injected`` is
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
        executor sized to at least ``min_executor_workers`` workers,
        independent of the runner's real core count.
    """
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
