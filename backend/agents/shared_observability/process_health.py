"""Process-health diagnostics shared by every Khala team microservice.

Turns "the worker just died with no traceback" into something debuggable:

* :func:`install_fault_diagnostics` arms :mod:`faulthandler` (so a native
  fault — SIGSEGV / SIGABRT / SIGBUS / SIGFPE / SIGILL — dumps a Python stack to
  stderr instead of vanishing) and routes uncaught exceptions from the main
  thread *and* worker threads through :mod:`logging` with a full traceback.
* :func:`start_memory_watchdog` runs a tiny daemon thread that samples the
  container's cgroup memory usage (falling back to this process's RSS when not
  in a cgroup) and logs a WARNING as it approaches the cgroup / env memory
  budget, so the last line before an OOM-kill names the cause — the kernel's
  SIGKILL itself is uncatchable and otherwise leaves no trace at all. The same
  thread polls the cgroup ``oom_kill`` counter (``memory.events``) and logs an
  ERROR whenever it rises, turning an already-happened silent kill into an
  explicit, durable log line — and, since it reports ``memory.peak`` alongside,
  distinguishing a container-limit OOM from host/VM-wide (global) OOM.

Everything here is best-effort and import-safe: a missing ``/proc``, missing
cgroup files, or a platform without :mod:`faulthandler` degrade to no-ops and
never raise, so arming diagnostics can never stop a service from booting.

Environment variables (all parse defensively — garbage falls back to the
documented default, out-of-range values clamp to the floor/ceiling):

* ``TEAM_MEMORY_WATCHDOG_ENABLED`` — master switch (default ``true``).
* ``TEAM_MEMORY_WATCHDOG_LIMIT_MB`` — override the detected memory budget, in MB.
* ``TEAM_MEMORY_WATCHDOG_THRESHOLD`` — warn fraction (default 0.85); an env value
  is clamped to ``[0.1, 0.99]`` (never 1.0 — warning at the limit is too late).
* ``TEAM_MEMORY_WATCHDOG_INTERVAL_S`` — sample interval in seconds (default 30).
"""

from __future__ import annotations

import logging
import math
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

# cgroup files live at fixed paths; expose them as module constants so tests can
# point them at temp files without monkeypatching ``open``.
_CGROUP_V2_MAX = "/sys/fs/cgroup/memory.max"
_CGROUP_V1_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
# Current usage counters — the value the kernel OOM killer actually watches.
_CGROUP_V2_CURRENT = "/sys/fs/cgroup/memory.current"
_CGROUP_V1_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
# Post-mortem OOM record: memory.events carries an ``oom_kill`` counter that the
# kernel bumps whenever it SIGKILLs a process in this cgroup — the only durable
# trace of an otherwise-silent worker death. memory.peak is the cgroup's usage
# high-water mark; a peak far below memory.max with a rising oom_kill means the
# kill came from *host/VM-wide* pressure (global OOM), not this container's limit.
_CGROUP_V2_EVENTS = "/sys/fs/cgroup/memory.events"
_CGROUP_V2_PEAK = "/sys/fs/cgroup/memory.peak"
_PROC_STATUS = "/proc/self/status"

# Values at/above this are the kernel's "no limit" sentinels (cgroup v1 reports
# something near ``PAGE_COUNTER_MAX``; v2 reports the literal string ``max``).
_UNLIMITED_BYTES = 1 << 62

_DEFAULT_INTERVAL_S = 30.0
_DEFAULT_THRESHOLD = 0.85

_log: Optional[logging.Logger] = None
_diagnostics_installed = False
# Hooks present before we install ours, captured at install time so our hooks
# can chain to other reporters (e.g. Sentry) instead of silently disabling them.
_chain_sys_excepthook: Any = None
_chain_thread_excepthook: Any = None


def _get_logger() -> logging.Logger:
    """Return the logger diagnostics should write to (set by install, else default).

    Preconditions:
        - None.
    Postconditions:
        - Returns the install-supplied logger when one was set, else the module
          logger. Never None.
    """
    return _log if _log is not None else logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defensive env parsing
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean from env var *name*, defensively.

    Preconditions:
        - None.
    Postconditions:
        - Returns True for "1"/"true"/"yes"/"on" (case-insensitive), *default*
          when the var is unset/blank, and False otherwise. Never raises.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(
    name: str,
    default: float,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    """Parse a float from env var *name*, defensively, then clamp.

    Preconditions:
        - ``minimum <= maximum`` when both are provided; *default* is finite.
    Postconditions:
        - Returns a finite float clamped to ``[minimum, maximum]``; falls back to
          *default* when the var is unset/blank/non-numeric or parses to a
          non-finite value (``inf``/``nan``). Never raises.
    """
    raw = os.environ.get(name)
    value = default
    if raw is not None and raw.strip():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = default
    # Reject inf/nan: a non-finite interval busy-loops or crashes the watchdog,
    # and clamp comparisons (</>) are always False for nan so they can't catch it.
    if not math.isfinite(value):
        value = default
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def _positive_int_or_none(raw: Optional[str]) -> Optional[int]:
    """Parse *raw* into a positive int, defensively.

    Shared by the env-override parse paths so "string → positive int" lives in
    one place instead of being hand-rolled per call site.

    Preconditions:
        - None.
    Postconditions:
        - Returns a positive int, or None when *raw* is absent/blank/non-numeric
          or not strictly positive. Never raises.
    """
    if not raw or not raw.strip():
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(float("inf"))/int(float("1e400")) — must stay caught
        # so this helper honours its "Never raises" postcondition.
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# Memory accounting
# ---------------------------------------------------------------------------


def _mb(num_bytes: int) -> int:
    """Render a byte count as whole megabytes for human-readable log lines.

    Preconditions:
        - ``num_bytes >= 0``.
    Postconditions:
        - Returns ``num_bytes // (1024 * 1024)`` (floor megabytes).
    """
    return num_bytes // (1024 * 1024)


def _read_int_file(path: str) -> Optional[int]:
    """Read a single integer from *path*.

    Preconditions:
        - None.
    Postconditions:
        - Returns the parsed int, or None when the file is absent/unreadable,
          empty, the literal ``max``, or non-numeric. Never raises.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def read_rss_bytes(status_path: str = _PROC_STATUS) -> Optional[int]:
    """Return the process resident set size (RSS) in bytes, or None if unavailable.

    Preconditions:
        - ``status_path`` points at a ``/proc/<pid>/status``-formatted file.
    Postconditions:
        - Returns a non-negative byte count parsed from the ``VmRSS:`` line, or
          None when the file is missing/unreadable or has no ``VmRSS:`` entry.
          Never raises.
    """
    try:
        with open(status_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            # /proc reports VmRSS in kB.
                            return int(parts[1]) * 1024
                        except (TypeError, ValueError):
                            return None
    except OSError:
        return None
    return None


def read_memory_usage_bytes() -> Optional[int]:
    """Return current memory usage in bytes — container-wide, else this process's RSS.

    The kernel OOM killer fires on the *cgroup's* total usage (every process in
    the container) crossing ``memory.max``, so the watchdog must compare that
    same number against the budget — not a single worker's RSS, which under
    ``workers=2`` would stay well under the shared limit and never warn. Reads
    cgroup v2 ``memory.current`` → v1 ``memory.usage_in_bytes`` → per-process RSS
    (the last for local/non-cgroup runs).

    Postconditions:
        - Returns a non-negative byte count, or None when neither a cgroup usage
          counter nor ``/proc`` RSS is readable. Never raises.
    """
    for path in (_CGROUP_V2_CURRENT, _CGROUP_V1_USAGE):
        value = _read_int_file(path)
        if value is not None and value >= 0:
            return value
    return read_rss_bytes()


def read_oom_kill_count(events_path: str = _CGROUP_V2_EVENTS) -> Optional[int]:
    """Return the cgroup ``oom_kill`` counter, or None when unavailable.

    ``memory.events`` is a multi-line ``key value`` file (so :func:`_read_int_file`
    can't parse it); this extracts the ``oom_kill`` line. The counter is the
    kernel's authoritative, durable record that it SIGKILLed a process in this
    cgroup — it survives the dead worker, so polling it is the one reliable way to
    surface an otherwise-silent OOM death.

    Preconditions:
        - None.
    Postconditions:
        - Returns a non-negative int parsed from the ``oom_kill`` line, or None
          when the file is absent/unreadable (e.g. cgroup v1) or has no numeric
          ``oom_kill`` entry. Never raises.
    """
    try:
        with open(events_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "oom_kill":
            try:
                return int(parts[1])
            except (TypeError, ValueError):
                return None
    return None


def read_memory_peak_bytes(peak_path: str = _CGROUP_V2_PEAK) -> Optional[int]:
    """Return the cgroup memory usage high-water mark in bytes, or None.

    Preconditions:
        - None.
    Postconditions:
        - Returns a non-negative byte count from ``memory.peak``, or None when the
          file is absent/unreadable/non-numeric (e.g. cgroup v1). Never raises.
    """
    value = _read_int_file(peak_path)
    return value if value is not None and value >= 0 else None


def detect_memory_limit_bytes() -> Optional[int]:
    """Return the process memory budget in bytes, or None when there is no limit.

    Resolution order: ``TEAM_MEMORY_WATCHDOG_LIMIT_MB`` env override → cgroup v2
    ``memory.max`` → cgroup v1 ``memory.limit_in_bytes``. Kernel "unlimited"
    sentinels are treated as no limit.

    Preconditions:
        - None.
    Postconditions:
        - Returns a positive byte count, or None. Never raises.
    """
    mb = _positive_int_or_none(os.environ.get("TEAM_MEMORY_WATCHDOG_LIMIT_MB"))
    if mb is not None:
        return mb * 1024 * 1024

    for path in (_CGROUP_V2_MAX, _CGROUP_V1_LIMIT):
        value = _read_int_file(path)
        if value is not None and 0 < value < _UNLIMITED_BYTES:
            return value
    return None


def evaluate_memory_pressure(rss_bytes: int, limit_bytes: int, threshold: float) -> bool:
    """Return True when usage has reached ``threshold`` of the memory limit.

    Preconditions (enforced with ``raise`` rather than ``assert`` so they hold
    even under ``python -O``, since this is a module-public function):
        - ``rss_bytes >= 0``.
        - ``limit_bytes > 0``.
        - ``0 < threshold <= 1``.
    Postconditions:
        - Returns ``rss_bytes >= limit_bytes * threshold``; no side effects.
    """
    if rss_bytes < 0:
        raise ValueError("rss_bytes must be non-negative")
    if limit_bytes <= 0:
        raise ValueError("limit_bytes must be positive")
    if not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1]")
    return rss_bytes >= limit_bytes * threshold


def _watchdog_tick(
    *,
    limit_bytes: int,
    threshold: float,
    warned: bool,
    usage_reader: Optional[Callable[[], Optional[int]]] = None,
) -> tuple[bool, Optional[str]]:
    """Evaluate one watchdog sample.

    Returns ``(new_warned, message_or_None)``. A WARNING message is produced only
    on the transition *into* the pressured state, so a sustained high-memory
    period logs once rather than every interval; recovery re-arms the warning.

    Preconditions:
        - ``limit_bytes > 0`` and ``0 < threshold <= 1``.
    Postconditions:
        - ``message`` is non-None iff this sample crossed into pressure while the
          previous state was un-warned.
    """
    # Resolve at call time (not as a default arg) so the module-level
    # ``read_memory_usage_bytes`` can be monkeypatched in tests.
    reader = usage_reader if usage_reader is not None else read_memory_usage_bytes
    usage = reader()
    if usage is None:
        return warned, None
    pressured = evaluate_memory_pressure(usage, limit_bytes, threshold)
    if pressured and not warned:
        pct = usage / limit_bytes * 100.0
        message = (
            f"High memory: {_mb(usage)}MB / {_mb(limit_bytes)}MB "
            f"({pct:.0f}%, warn at {threshold * 100:.0f}%) — approaching the "
            f"container memory limit; an OOM kill (SIGKILL, no traceback) is imminent"
        )
        return True, message
    if not pressured and warned:
        return False, None
    return warned, None


def _oom_check_tick(
    last_count: Optional[int],
    *,
    limit_bytes: Optional[int] = None,
    events_reader: Optional[Callable[[], Optional[int]]] = None,
    peak_reader: Optional[Callable[[], Optional[int]]] = None,
) -> tuple[Optional[int], Optional[str]]:
    """Evaluate one OOM-kill-counter sample.

    Returns ``(new_count, message_or_None)``. A message is produced only when the
    counter has *risen* since ``last_count`` — i.e. the kernel SIGKILLed a process
    in this cgroup since the previous sample. The first observation (``last_count``
    is None) just adopts the baseline silently, so pre-existing kills from before
    the watchdog started are not re-reported as new.

    ``limit_bytes`` is optional: a container with no ``mem_limit`` has no cgroup
    budget yet can still be killed by host/VM-wide (global) OOM, so OOM detection
    must work without one — the limit only enriches the log line.

    Postconditions:
        - ``message`` is non-None iff ``last_count is not None and current > last_count``.
          On an unreadable counter the previous ``last_count`` is preserved and no
          message is emitted. Never raises (readers are best-effort).
    """
    reader = events_reader if events_reader is not None else read_oom_kill_count
    current = reader()
    if current is None:
        return last_count, None
    if last_count is not None and current > last_count:
        peak_fn = peak_reader if peak_reader is not None else read_memory_peak_bytes
        peak = peak_fn()
        peak_str = f"{_mb(peak)}MB" if peak else "unknown"
        limit_str = f"{_mb(limit_bytes)}MB" if limit_bytes else "unset (no container limit)"
        message = (
            f"OOM kill detected: cgroup oom_kill counter rose to {current} "
            f"(was {last_count}) — the kernel SIGKILLed a process in this container "
            f"with no traceback (a uvicorn worker simply vanishes and is restarted). "
            f"container peak={peak_str} / limit={limit_str}. If peak stayed well "
            f"below the limit (or the limit is unset), the host/VM ran out of memory "
            f"(global OOM) — raise Docker/VM memory or reduce the running stack "
            f"rather than this container's limit."
        )
        return current, message
    return current, None


def _watchdog_loop(
    *,
    team: str,
    limit_bytes: Optional[int],
    threshold: float,
    interval_s: float,
    stop_event: threading.Event,
    logger: logging.Logger,
    initial_oom_count: Optional[int] = None,
) -> None:
    """Sample memory on *interval_s* until *stop_event* is set, logging pressure.

    When ``limit_bytes`` is None there is no budget to compare against, so the
    pressure-threshold warning is skipped and the loop runs in OOM-detection-only
    mode (the cgroup ``oom_kill`` counter still fires on a host/VM-wide kill of an
    unbounded container).

    ``initial_oom_count`` seeds the OOM-kill baseline from the value already read
    at arm time, so a kill occurring between arming and the first tick is still
    reported (and the counter file isn't read twice). Falls back to reading once
    when not supplied (e.g. direct unit-test calls).

    Preconditions (enforced):
        - ``interval_s > 0`` — a non-positive interval would tight-loop this
          thread (``start_memory_watchdog`` already floors it at 1.0).
        - ``0 < threshold <= 1``; ``limit_bytes`` is a positive byte count or None.
    Postconditions:
        - Emits a WARNING via *logger* once per transition into memory pressure
          (only when ``limit_bytes`` is set), and an ERROR when the cgroup
          ``oom_kill`` counter rises (a worker was SIGKILLed since the last
          sample); returns only after *stop_event* is set. A failing sample is
          swallowed (a diagnostic thread must never crash the worker).
    """
    if interval_s <= 0:
        raise ValueError("interval_s must be > 0")
    warned = False
    tick_error_logged = False
    # Seed the OOM-kill baseline from the arm-time read so a kill between arming
    # and the first tick isn't folded silently into a fresh baseline.
    last_oom = initial_oom_count if initial_oom_count is not None else read_oom_kill_count()
    while not stop_event.wait(interval_s):
        try:
            if limit_bytes:
                warned, message = _watchdog_tick(
                    limit_bytes=limit_bytes, threshold=threshold, warned=warned
                )
                if message:
                    logger.warning("[%s] %s", team, message)
            last_oom, oom_message = _oom_check_tick(last_oom, limit_bytes=limit_bytes)
            if oom_message:
                logger.error("[%s] %s", team, oom_message)
        except Exception:  # noqa: BLE001 — a diagnostic thread must never crash the worker
            # Surface the *first* failure at WARNING so a persistent bug (e.g. in
            # the pressure/oom check) isn't hidden at DEBUG forever; subsequent
            # failures stay at DEBUG to avoid flooding the logs every interval.
            if not tick_error_logged:
                logger.warning("[%s] memory watchdog tick failed", team, exc_info=True)
                tick_error_logged = True
            else:
                logger.debug("memory watchdog tick failed", exc_info=True)


@dataclass
class Watchdog:
    """A running memory watchdog: its daemon ``thread`` and the ``stop_event``
    that shuts it down cleanly. Returned by :func:`start_memory_watchdog`."""

    thread: threading.Thread
    stop_event: threading.Event


def start_memory_watchdog(
    team: str,
    *,
    logger: Optional[logging.Logger] = None,
    interval_s: Optional[float] = None,
    threshold: Optional[float] = None,
    limit_bytes: Optional[int] = None,
) -> Optional[Watchdog]:
    """Start a daemon thread that warns as memory usage approaches the budget.

    The thread is the only early signal of an impending OOM kill: the kernel's
    SIGKILL cannot be caught, so without this the worker simply vanishes. The
    WARNING it emits becomes the last (and explanatory) log line before death.

    Preconditions:
        - ``team`` is a non-empty identifier used in log lines.
    Postconditions:
        - Returns a :class:`Watchdog` (its daemon thread + stop event), or None
          when disabled via env or when neither a memory limit nor a readable
          cgroup ``oom_kill`` counter is available (nothing to watch). Never
          raises (other than the ``team`` precondition). When a limit is present
          the thread emits pressure warnings + OOM detection; with only the
          counter it runs in OOM-detection-only mode.
    """
    if not team:
        raise ValueError("team must be a non-empty identifier")
    log = logger or _get_logger()

    if not _env_bool("TEAM_MEMORY_WATCHDOG_ENABLED", True):
        log.debug("memory watchdog disabled via TEAM_MEMORY_WATCHDOG_ENABLED")
        return None

    if limit_bytes is None:
        limit_bytes = detect_memory_limit_bytes()
    has_limit = bool(limit_bytes and limit_bytes > 0)
    # Even with no cgroup memory limit (an unbounded container), keep watching when
    # the kernel's oom_kill counter is readable: such a container can still be
    # SIGKILLed by host/VM-wide (global) OOM, and that counter is the only trace.
    # Read it once here and reuse as the startup baseline below (one file read).
    prior_oom = read_oom_kill_count()
    oom_available = prior_oom is not None
    if not has_limit and not oom_available:
        log.debug(
            "memory watchdog: no memory limit and no oom_kill counter; not starting for %s",
            team,
        )
        return None
    if not has_limit:
        limit_bytes = None  # OOM-detection-only mode (no pressure threshold)

    if interval_s is None:
        interval_s = _env_float("TEAM_MEMORY_WATCHDOG_INTERVAL_S", _DEFAULT_INTERVAL_S, minimum=1.0)
    if threshold is None:
        threshold = _env_float(
            "TEAM_MEMORY_WATCHDOG_THRESHOLD", _DEFAULT_THRESHOLD, minimum=0.1, maximum=0.99
        )

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_watchdog_loop,
        kwargs={
            "team": team,
            "limit_bytes": limit_bytes,
            "threshold": threshold,
            "interval_s": interval_s,
            "stop_event": stop_event,
            "logger": log,
            # Seed from the count read above so a kill between here and the first
            # tick is reported and the counter file isn't read a second time.
            "initial_oom_count": prior_oom,
        },
        name=f"mem-watchdog-{team}",
        daemon=True,
    )
    thread.start()
    if limit_bytes:
        log.info(
            "Memory watchdog armed for %s: warn at %.0f%% of %dMB (every %.0fs)",
            team,
            threshold * 100,
            _mb(limit_bytes),
            interval_s,
        )
    else:
        log.info(
            "Memory watchdog armed for %s: OOM-kill detection only — no container "
            "memory limit set, so only host/VM-wide kills are watched (every %.0fs)",
            team,
            interval_s,
        )
    # Surface the kernel's OOM-kill record + usage high-water mark at arm time.
    # A non-zero prior count immediately tells an operator the container has
    # already been OOM-killed (the symptom is otherwise silent), and a peak far
    # below the limit points at host/VM-wide pressure rather than this limit.
    # ``prior_oom`` was already read above (reused here to avoid a second read).
    peak = read_memory_peak_bytes()
    if prior_oom is not None or peak is not None:
        log.info(
            "Memory watchdog baseline for %s: prior oom_kills=%s, peak=%s of %s",
            team,
            prior_oom if prior_oom is not None else "n/a",
            f"{_mb(peak)}MB" if peak is not None else "n/a",
            f"{_mb(limit_bytes)}MB" if limit_bytes else "no limit",
        )
        if prior_oom:
            log.warning(
                "[%s] cgroup has %d prior OOM kill(s) since container start — a "
                "worker was SIGKILLed (no traceback). Check host/VM memory if the "
                "peak above is well under the limit.",
                team,
                prior_oom,
            )
    return Watchdog(thread=thread, stop_event=stop_event)


# ---------------------------------------------------------------------------
# Fault / uncaught-exception diagnostics
# ---------------------------------------------------------------------------


def _sys_excepthook(exc_type, exc_value, exc_tb) -> None:
    """Log an uncaught main-thread exception, then chain to any prior hook.

    Preconditions:
        - Installed by :func:`install_fault_diagnostics`, which captures any
          pre-existing hook in ``_chain_sys_excepthook``.
    Postconditions:
        - Logs at CRITICAL with the traceback, except for KeyboardInterrupt and
          SystemExit (intentional terminations, not faults), then invokes the
          previously-installed custom hook if one exists, else the stdlib default
          for KeyboardInterrupt. Never raises.
    """
    # KeyboardInterrupt/SystemExit are intentional terminations, not faults —
    # don't log them at CRITICAL. (In practice the interpreter handles a
    # main-thread SystemExit before it reaches sys.excepthook; the guard mirrors
    # _thread_excepthook and covers a direct call.)
    if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        _get_logger().critical(
            "Uncaught exception in main thread; process is terminating",
            exc_info=(exc_type, exc_value, exc_tb),
        )
    # Chain to a previously-installed custom hook (e.g. Sentry) so we don't
    # silently disable other reporters. Skip the stdlib default to avoid a
    # duplicate stderr traceback (we already logged it) — except for
    # KeyboardInterrupt, where the default performs normal Ctrl-C handling.
    prev = _chain_sys_excepthook
    target = None
    if prev is not None and prev is not _sys_excepthook and prev is not sys.__excepthook__:
        target = prev
    elif issubclass(exc_type, KeyboardInterrupt):
        target = sys.__excepthook__
    if target is not None:
        try:
            target(exc_type, exc_value, exc_tb)
        except Exception:  # noqa: BLE001 — a failing downstream hook must not mask our log
            _get_logger().debug("chained sys excepthook raised", exc_info=True)


def _thread_excepthook(args) -> None:
    """Log an uncaught non-main-thread exception, then chain to any prior hook.

    Preconditions:
        - ``args`` is a ``threading.ExceptHookArgs`` (exc_type / exc_value /
          exc_traceback / thread).
    Postconditions:
        - Logs at CRITICAL with the traceback (except SystemExit, a normal thread
          exit), then invokes a previously-installed custom thread hook if one
          exists. Never raises.
    """
    if args.exc_type is not SystemExit:  # SystemExit is a normal thread exit
        thread_name = getattr(args.thread, "name", "?")
        _get_logger().critical(
            "Uncaught exception in thread %r",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
    prev = _chain_thread_excepthook
    default = getattr(threading, "__excepthook__", None)
    if prev is not None and prev is not _thread_excepthook and prev is not default:
        try:
            prev(args)  # preserve another reporter (e.g. Sentry)
        except Exception:  # noqa: BLE001 — a failing downstream hook must not mask our log
            _get_logger().debug("chained thread excepthook raised", exc_info=True)


def install_fault_diagnostics(logger: Optional[logging.Logger] = None) -> None:
    """Arm faulthandler and uncaught-exception logging for this process.

    Idempotent within a process. After this call:
      * a native fatal signal dumps a Python stack to stderr (faulthandler), and
      * an uncaught exception in any thread is logged with a full traceback
        instead of a bare stderr dump that container log scrapers often miss.

    It deliberately does **not** install SIGTERM/SIGINT handlers, leaving
    uvicorn's graceful-shutdown handling untouched. Any hooks already installed
    (e.g. Sentry) are captured and chained to, so this never disables another
    reporter.

    Postconditions:
        - ``sys.excepthook`` and ``threading.excepthook`` route through the
          module logger and then chain to whatever hook was previously
          installed; faulthandler is enabled when available. Never raises.
    """
    global _diagnostics_installed, _log, _chain_sys_excepthook, _chain_thread_excepthook
    if logger is not None:
        _log = logger
    if _diagnostics_installed:
        return
    _diagnostics_installed = True

    # Export PYTHONFAULTHANDLER first and independently of enable(): a spawned /
    # forkserver worker reads it at interpreter startup (with a fresh stderr), so
    # it must be set even if enable() can't attach to *this* process's stderr
    # (e.g. stderr was replaced and has no real fileno, as under some test
    # runners). Don't override an operator who set it to "0".
    if "PYTHONFAULTHANDLER" not in os.environ:
        os.environ["PYTHONFAULTHANDLER"] = "1"
    try:
        import faulthandler

        if not faulthandler.is_enabled():
            faulthandler.enable()
    except Exception:  # noqa: BLE001 — diagnostics must never block startup
        _get_logger().debug("faulthandler.enable() failed for this process", exc_info=True)

    # Capture the hooks already in place (e.g. Sentry) so ours chain to them.
    if sys.excepthook is not _sys_excepthook:
        _chain_sys_excepthook = sys.excepthook
    sys.excepthook = _sys_excepthook
    try:
        if threading.excepthook is not _thread_excepthook:
            _chain_thread_excepthook = threading.excepthook
        threading.excepthook = _thread_excepthook  # Python 3.8+
    except Exception:  # noqa: BLE001
        _get_logger().debug("threading.excepthook not settable", exc_info=True)

    _get_logger().info("Fault diagnostics armed: faulthandler + uncaught-exception logging")
