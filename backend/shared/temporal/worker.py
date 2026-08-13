"""Shared Temporal worker startup.

Every team used to hand-roll this: create a ThreadPoolExecutor, connect the
client, build a ``Worker``, run it in a daemon thread. ``start_team_worker``
replaces all that boilerplate — a team just passes its workflows/activities
list.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Optional

from shared.temporal.client import (
    connect_temporal_client,
    get_default_task_queue,
    get_temporal_loop,
    is_temporal_enabled,
    set_temporal_client,
    set_temporal_loop,
)

logger = logging.getLogger(__name__)

_worker_threads: dict[str, threading.Thread] = {}
_worker_ready: dict[str, threading.Event] = {}
_activity_executors: dict[str, ThreadPoolExecutor] = {}
_workers: dict[str, Any] = {}
_worker_loops: dict[str, asyncio.AbstractEventLoop] = {}


def is_team_worker_alive(team: str) -> bool:
    """Return whether ``team`` has a live Temporal worker daemon thread.

    Preconditions:
        - ``team`` is a non-empty team key previously passed to
          :func:`start_team_worker` (unknown teams simply report False).

    Postconditions:
        - Returns True iff a registered thread for ``team`` exists and
          ``is_alive()`` is True.
    """
    thread = _worker_threads.get(team)
    return thread is not None and thread.is_alive()


def is_team_worker_ready(team: str) -> bool:
    """Return whether ``team``'s Temporal worker is connected and polling.

    ``start_team_worker`` returns True as soon as the daemon thread is spawned;
    connect happens asynchronously. This is the non-blocking counterpart of
    :func:`wait_for_team_worker_ready`: False while the thread exists but has
    not finished connecting, and False if the thread has exited.

    Preconditions:
        - ``team`` is a non-empty team key (unknown teams report False).
    Postconditions:
        - Returns True iff a live worker thread is registered for ``team`` and
          that team's ready event is set. Never raises. Never blocks.
    """
    event = _worker_ready.get(team)
    return is_team_worker_alive(team) and event is not None and event.is_set()


def wait_for_team_worker_ready(team: str, timeout_s: float | None = None) -> None:
    """Block until ``team``'s worker has connected (ready event set).

    ``start_team_worker`` returns True as soon as the daemon thread is
    spawned; connect happens asynchronously. This wait is the public signal
    that the worker finished connecting and is about to run.

    Preconditions:
        - ``start_team_worker(team, ...)`` has already been called successfully
          for this process (a ready ``Event`` is registered for ``team``).
        - ``timeout_s`` is ``None`` (use ``CLIENT_READY_TIMEOUT_S``) or >= 0.

    Postconditions:
        - Returns once the team's ready event is set and the worker thread is
          still alive.
        - Raises ``RuntimeError`` if the worker was never started, the thread
          exits before becoming ready, or the timeout elapses first.
    """
    from shared.temporal.runner import CLIENT_READY_TIMEOUT_S

    if timeout_s is None:
        timeout_s = CLIENT_READY_TIMEOUT_S
    event = _worker_ready.get(team)
    if event is None:
        raise RuntimeError(f"{team} Temporal worker was not started; refusing to serve without a worker")
    if event.wait(timeout=timeout_s):
        if not is_team_worker_alive(team):
            raise RuntimeError(f"{team} Temporal worker thread exited after start; refusing to serve without a worker")
        return
    if not is_team_worker_alive(team):
        raise RuntimeError(f"{team} Temporal worker thread exited after start; refusing to serve without a worker")
    raise RuntimeError(f"{team} Temporal worker thread never became ready; refusing to serve without a worker")


def _build_workflow_runner() -> Any:
    """Build a SandboxedWorkflowRunner that passes through pydantic, boto3/strands, and httpx.

    Without ``pydantic``/``pydantic_core`` passthrough, schema generation for
    models that reference ``datetime.datetime`` (e.g. ``Optional[datetime]``
    fields) fails inside the Temporal workflow sandbox: pydantic-core compares
    types by identity and the sandboxed reimport of pydantic ends up with a
    different ``datetime.datetime`` reference than pydantic-core's compiled
    one, raising ``PydanticSchemaGenerationError``.

    ``strands``/``boto3``/``botocore``/``urllib3``/``httpx`` need the same
    treatment for a different reason: registering any team's workflow class
    requires Python to first import its ancestor packages, and several teams'
    top-level ``__init__.py`` eagerly imports agent/orchestrator code that
    imports ``strands`` and/or ``llm_service`` at module scope (e.g.
    ``market_research_team``, ``branding_team``, ``sales_team``). ``strands``
    unconditionally imports its Bedrock model provider
    (``strands.models.bedrock``), which does a top-level ``import boto3``
    regardless of which LLM provider is actually configured; botocore does
    thread-lock and dynamic-class-generation work at import time that is not
    safe to replay in the sandbox's isolated module namespace, surfacing as an
    import failure inside ``botocore.compat``/``urllib3``. Separately,
    ``llm_service.clients.ollama`` imports ``httpx`` at module scope, and
    ``httpx._models`` defines ``class _CookieCompatRequest(urllib.request.Request)``
    at import time — accessing ``__mro_entries__`` on the sandbox-restricted
    ``urllib.request.Request`` raises ``RestrictedWorkflowAccessError``. None of
    these packages are used by workflow ``run()`` bodies in this repo (only by
    code that executes inside activities), so passing them through sacrifices
    no real determinism checking.
    """
    from temporalio.worker.workflow_sandbox import (
        SandboxedWorkflowRunner,
        SandboxRestrictions,
    )

    restrictions = SandboxRestrictions.default.with_passthrough_modules(
        "pydantic",
        "pydantic_core",
        "strands",
        "boto3",
        "botocore",
        "urllib3",
        "httpx",
        # numpy and pandas use C extension modules that can only be loaded once
        # per process. If any module in the workflow's transitive import graph
        # (e.g. market_regime.py, indicators.py) imports them at the top level,
        # the Temporal sandbox's re-import during workflow replay triggers
        # "cannot load module more than once per process". Passing them through
        # makes the sandbox share the already-loaded instances rather than
        # attempting a second load. This is safe because workflow run() bodies
        # in this repo never call numpy/pandas directly — those calls live
        # exclusively in activity code.
        "numpy",
        "pandas",
        # StrategyLabCycleWorkflow.run() calls dto.convergence_tracker_from_wire,
        # which imports quality_gates.ConvergenceTracker; quality_gates/__init__.py
        # eagerly imports every quality gate (including backtest_anomaly.py,
        # spec_readiness.py), and those import investment_team.market_data_service
        # for a type/helper reference. market_data_service reads
        # ALPHA_VANTAGE_API_KEY via `os.environ.get(...)` at module scope, which
        # the sandbox's re-import forbids (`RestrictedWorkflowAccessError`).
        # Passing the module through is safe on the same grounds as numpy/pandas
        # above: workflow run() bodies never call into market_data_service
        # themselves, only reach it as a side effect of this import chain.
        "investment_team.market_data_service",
        # Same import chain, same failure class: quality_gates/__init__.py's
        # eager quality-gate imports reach predicate_conformance.py, which
        # imports StrategyLabBudgetConfig from budget_config.py, which imports
        # llm_service.config for resolve_timeout(). llm_service/config.py does
        # ``_warned_lock = threading.Lock()`` at module scope, which the
        # sandbox's re-import forbids (`RestrictedWorkflowAccessError` on
        # ``threading.Lock.__call__``). Passing budget_config through is safe
        # on the same grounds as market_data_service above: workflow run()
        # bodies never call StrategyLabBudgetConfig themselves, only reach it
        # as a side effect of this import chain.
        "investment_team.strategy_lab.budget_config",
    )
    return SandboxedWorkflowRunner(restrictions=restrictions)


async def _run_worker_async(
    team: str,
    task_queue: str,
    workflows: Iterable[Any],
    activities: Iterable[Any],
    max_concurrent_activities: int,
) -> None:
    from temporalio.worker import Worker

    client = await connect_temporal_client()
    if client is None:
        return
    # First team to connect owns the shared client/loop slots.
    set_temporal_client(client)
    set_temporal_loop(asyncio.get_running_loop())

    executor = _activity_executors.setdefault(
        team,
        ThreadPoolExecutor(
            max_workers=max_concurrent_activities,
            thread_name_prefix=f"{team}-temporal-activity",
        ),
    )
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=list(workflows),
        activities=list(activities),
        activity_executor=executor,
        max_concurrent_activities=max_concurrent_activities,
        workflow_runner=_build_workflow_runner(),
    )
    _workers[team] = worker
    _worker_loops[team] = asyncio.get_running_loop()
    try:
        ready = _worker_ready.get(team)
        if ready is not None:
            ready.set()
        logger.info("Temporal worker starting: team=%s task_queue=%s", team, task_queue)
        await worker.run()
    finally:
        _workers.pop(team, None)
        _worker_loops.pop(team, None)


def start_team_worker(
    team: str,
    workflows: Iterable[Any],
    activities: Iterable[Any],
    task_queue: Optional[str] = None,
    max_concurrent_activities: int = 4,
) -> bool:
    """Start a Temporal worker for a team in a daemon thread.

    Returns True if a worker thread is running (or already running),
    False when Temporal is disabled.
    """
    if not is_temporal_enabled():
        logger.info("Temporal disabled; skipping worker for team=%s", team)
        return False
    existing = _worker_threads.get(team)
    if existing is not None and existing.is_alive():
        return True

    queue = task_queue or get_default_task_queue()
    ready = threading.Event()
    _worker_ready[team] = ready

    def _target() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_worker_async(team, queue, workflows, activities, max_concurrent_activities))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Temporal worker failed for team=%s: %s", team, e)
        finally:
            ready.clear()
            # _run_worker_async populates the shared client/loop slots with THIS
            # loop before running. Now that the loop is about to close, release
            # those slots so a later start_workflow_sync waits for a live worker
            # (or fails clearly) instead of submitting to a closed loop and
            # raising "Event loop is closed". Guard on identity so we never
            # clobber a different worker that has since taken ownership.
            if get_temporal_loop() is loop:
                set_temporal_loop(None)
                set_temporal_client(None)
            loop.close()

    thread = threading.Thread(target=_target, name=f"{team}-temporal-worker", daemon=True)
    thread.start()
    _worker_threads[team] = thread
    logger.info("Temporal worker thread started for team=%s queue=%s", team, queue)
    return True


def stop_team_worker(team: str, *, timeout_s: float = 5.0) -> None:
    """Stop one in-process Temporal worker and join its daemon thread.

    Preconditions:
        - ``team`` is a non-empty team key (unknown teams are a no-op).
        - ``timeout_s`` >= 0.

    Postconditions:
        - ``Worker.shutdown()`` has been requested on that team's loop when a
          live worker exists; the daemon thread has been joined for at most
          ``timeout_s`` seconds. Registry entries for the team are cleared.
          Never raises.
    """
    assert team, "team must be a non-empty team key"
    assert timeout_s >= 0, "timeout_s must be non-negative"
    live_worker = _workers.get(team)
    loop = _worker_loops.get(team)
    thread = _worker_threads.get(team)
    if live_worker is not None and loop is not None and not loop.is_closed() and loop.is_running():
        try:
            fut = asyncio.run_coroutine_threadsafe(live_worker.shutdown(), loop)
            fut.result(timeout=timeout_s)
        except Exception:
            logger.warning("Temporal worker.shutdown failed for team=%s", team, exc_info=True)
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout_s)
        if thread.is_alive():
            logger.warning("Temporal worker thread did not exit for team=%s", team)
    _worker_threads.pop(team, None)
    _workers.pop(team, None)
    _worker_loops.pop(team, None)
    _worker_ready.pop(team, None)
    executor = _activity_executors.pop(team, None)
    if executor is not None:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # pragma: no cover - ThreadPoolExecutor.shutdown does not raise
            logger.debug("activity executor shutdown failed for team=%s", team, exc_info=True)


def stop_all_team_workers(*, timeout_s: float = 5.0) -> None:
    """Stop every in-process Temporal worker started via :func:`start_team_worker`.

    Used on Unified API graceful shutdown so LLM-producing activities finish
    (or are cancelled by ``Worker.shutdown``) before the usage flusher
    unregisters and Postgres closes.

    Preconditions:
        - ``timeout_s`` >= 0 (applied per team).

    Postconditions:
        - Every registered team has been passed to :func:`stop_team_worker`.
          Never raises.
    """
    assert timeout_s >= 0, "timeout_s must be non-negative"
    teams = list(dict.fromkeys([*_worker_threads, *_workers]))
    for team in teams:
        try:
            stop_team_worker(team, timeout_s=timeout_s)
        except Exception:  # pragma: no cover - stop_team_worker never raises; defensive
            logger.warning("stop_team_worker failed for team=%s", team, exc_info=True)
