"""Unit tests for shared_observability.process_health.

These cover the diagnostics that turn a silent worker death into a debuggable
event: defensive env parsing, RSS / cgroup-limit reads, the memory-pressure
evaluation and watchdog tick/loop, the watchdog lifecycle, and the faulthandler
/ uncaught-exception hook installation.
"""

from __future__ import annotations

import logging
import sys
import threading
import types

import pytest

from shared_observability import process_health as ph

# --------------------------------------------------------------------- env parsing


def test_env_bool_default_blank_and_truthy(monkeypatch) -> None:
    monkeypatch.delenv("X_BOOL", raising=False)
    assert ph._env_bool("X_BOOL", True) is True
    assert ph._env_bool("X_BOOL", False) is False
    monkeypatch.setenv("X_BOOL", "   ")
    assert ph._env_bool("X_BOOL", True) is True  # blank → default
    for val in ("1", "true", "YES", "On"):
        monkeypatch.setenv("X_BOOL", val)
        assert ph._env_bool("X_BOOL", False) is True
    for val in ("0", "false", "nope"):
        monkeypatch.setenv("X_BOOL", val)
        assert ph._env_bool("X_BOOL", True) is False


def test_env_float_default_garbage_and_clamp(monkeypatch) -> None:
    monkeypatch.delenv("X_F", raising=False)
    assert ph._env_float("X_F", 30.0) == 30.0
    monkeypatch.setenv("X_F", "garbage")
    assert ph._env_float("X_F", 30.0) == 30.0  # unparseable → default
    monkeypatch.setenv("X_F", "0.5")
    assert ph._env_float("X_F", 30.0) == 0.5
    monkeypatch.setenv("X_F", "0.001")
    assert ph._env_float("X_F", 0.85, minimum=0.1) == 0.1  # clamped to floor
    monkeypatch.setenv("X_F", "5")
    assert ph._env_float("X_F", 0.85, maximum=0.99) == 0.99  # clamped to ceiling


# --------------------------------------------------------------------- memory reads


def test_read_rss_bytes_parses_vmrss(tmp_path) -> None:
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmRSS:\t  2048 kB\nVmSize:\t 9999 kB\n")
    assert ph.read_rss_bytes(str(status)) == 2048 * 1024


def test_read_rss_bytes_missing_file_and_no_vmrss(tmp_path) -> None:
    assert ph.read_rss_bytes(str(tmp_path / "nope")) is None
    no_rss = tmp_path / "status2"
    no_rss.write_text("Name:\tpython\nVmSize:\t 10 kB\n")
    assert ph.read_rss_bytes(str(no_rss)) is None


def test_read_memory_usage_prefers_cgroup_v2_current(monkeypatch, tmp_path) -> None:
    cur = tmp_path / "memory.current"
    cur.write_text(str(300 * 1024 * 1024))
    monkeypatch.setattr(ph, "_CGROUP_V2_CURRENT", str(cur))
    monkeypatch.setattr(ph, "_CGROUP_V1_USAGE", str(tmp_path / "absent_v1"))
    # RSS fallback must NOT be consulted when a cgroup counter is present.
    monkeypatch.setattr(ph, "read_rss_bytes", lambda *a, **k: 1)
    assert ph.read_memory_usage_bytes() == 300 * 1024 * 1024


def test_read_memory_usage_reads_cgroup_v1_usage(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ph, "_CGROUP_V2_CURRENT", str(tmp_path / "absent_v2"))
    v1 = tmp_path / "usage_in_bytes"
    v1.write_text(str(64 * 1024 * 1024))
    monkeypatch.setattr(ph, "_CGROUP_V1_USAGE", str(v1))
    assert ph.read_memory_usage_bytes() == 64 * 1024 * 1024


def test_read_memory_usage_falls_back_to_rss(monkeypatch, tmp_path) -> None:
    """Off-cgroup (no usage counters): fall back to this process's RSS."""
    monkeypatch.setattr(ph, "_CGROUP_V2_CURRENT", str(tmp_path / "absent_v2"))
    monkeypatch.setattr(ph, "_CGROUP_V1_USAGE", str(tmp_path / "absent_v1"))
    monkeypatch.setattr(ph, "read_rss_bytes", lambda *a, **k: 7 * 1024 * 1024)
    assert ph.read_memory_usage_bytes() == 7 * 1024 * 1024


def test_read_memory_usage_all_sources_unavailable_is_none(monkeypatch, tmp_path) -> None:
    """When neither a cgroup counter nor /proc RSS is readable, return None so the
    watchdog tick treats the sample as 'unknown' rather than crashing."""
    monkeypatch.setattr(ph, "_CGROUP_V2_CURRENT", str(tmp_path / "absent_v2"))
    monkeypatch.setattr(ph, "_CGROUP_V1_USAGE", str(tmp_path / "absent_v1"))
    monkeypatch.setattr(ph, "read_rss_bytes", lambda *a, **k: None)
    assert ph.read_memory_usage_bytes() is None


def test_read_int_file_variants(tmp_path) -> None:
    good = tmp_path / "g"
    good.write_text("12345\n")
    assert ph._read_int_file(str(good)) == 12345
    mx = tmp_path / "m"
    mx.write_text("max\n")
    assert ph._read_int_file(str(mx)) is None  # cgroup v2 'no limit'
    bad = tmp_path / "b"
    bad.write_text("not-a-number\n")
    assert ph._read_int_file(str(bad)) is None
    assert ph._read_int_file(str(tmp_path / "absent")) is None


def test_detect_memory_limit_env_override(monkeypatch) -> None:
    monkeypatch.setenv("TEAM_MEMORY_WATCHDOG_LIMIT_MB", "512")
    assert ph.detect_memory_limit_bytes() == 512 * 1024 * 1024


def test_detect_memory_limit_env_garbage_falls_through(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TEAM_MEMORY_WATCHDOG_LIMIT_MB", "garbage")
    monkeypatch.setattr(ph, "_CGROUP_V2_MAX", str(tmp_path / "absent_v2"))
    monkeypatch.setattr(ph, "_CGROUP_V1_LIMIT", str(tmp_path / "absent_v1"))
    assert ph.detect_memory_limit_bytes() is None


def test_detect_memory_limit_reads_cgroup_v2(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TEAM_MEMORY_WATCHDOG_LIMIT_MB", raising=False)
    v2 = tmp_path / "memory.max"
    v2.write_text(str(256 * 1024 * 1024))
    monkeypatch.setattr(ph, "_CGROUP_V2_MAX", str(v2))
    monkeypatch.setattr(ph, "_CGROUP_V1_LIMIT", str(tmp_path / "absent_v1"))
    assert ph.detect_memory_limit_bytes() == 256 * 1024 * 1024


def test_detect_memory_limit_reads_cgroup_v1(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TEAM_MEMORY_WATCHDOG_LIMIT_MB", raising=False)
    monkeypatch.setattr(ph, "_CGROUP_V2_MAX", str(tmp_path / "absent_v2"))
    v1 = tmp_path / "limit"
    v1.write_text(str(128 * 1024 * 1024))
    monkeypatch.setattr(ph, "_CGROUP_V1_LIMIT", str(v1))
    assert ph.detect_memory_limit_bytes() == 128 * 1024 * 1024


def test_detect_memory_limit_ignores_unlimited_sentinels(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TEAM_MEMORY_WATCHDOG_LIMIT_MB", raising=False)
    v2 = tmp_path / "memory.max"
    v2.write_text("max")  # cgroup v2 'no limit'
    v1 = tmp_path / "limit"
    v1.write_text(str(ph._UNLIMITED_BYTES + 1))  # cgroup v1 huge sentinel
    monkeypatch.setattr(ph, "_CGROUP_V2_MAX", str(v2))
    monkeypatch.setattr(ph, "_CGROUP_V1_LIMIT", str(v1))
    assert ph.detect_memory_limit_bytes() is None


# --------------------------------------------------------------- pressure / tick / loop


def test_evaluate_memory_pressure_threshold() -> None:
    assert ph.evaluate_memory_pressure(85, 100, 0.85) is True
    assert ph.evaluate_memory_pressure(84, 100, 0.85) is False
    assert ph.evaluate_memory_pressure(100, 100, 0.85) is True


@pytest.mark.parametrize(
    "rss,limit,thr",
    [(-1, 100, 0.5), (10, 0, 0.5), (10, 100, 0.0), (10, 100, 1.5)],
)
def test_evaluate_memory_pressure_precondition_violations(rss, limit, thr) -> None:
    # Raises (not asserts) so the guard holds even under `python -O`.
    with pytest.raises(ValueError):
        ph.evaluate_memory_pressure(rss, limit, thr)


def test_watchdog_tick_warns_once_then_recovers() -> None:
    limit = 100
    warned, msg = ph._watchdog_tick(
        limit_bytes=limit, threshold=0.85, warned=False, usage_reader=lambda: 90
    )
    assert warned is True and msg is not None and "OOM" in msg  # first crossing warns
    warned, msg = ph._watchdog_tick(
        limit_bytes=limit, threshold=0.85, warned=True, usage_reader=lambda: 95
    )
    assert warned is True and msg is None  # sustained pressure does not repeat
    warned, msg = ph._watchdog_tick(
        limit_bytes=limit, threshold=0.85, warned=True, usage_reader=lambda: 10
    )
    assert warned is False and msg is None  # recovery re-arms the warning


def test_watchdog_tick_no_usage_is_noop() -> None:
    warned, msg = ph._watchdog_tick(
        limit_bytes=100, threshold=0.85, warned=False, usage_reader=lambda: None
    )
    assert warned is False and msg is None


def test_watchdog_loop_logs_once_then_exits_on_stop(monkeypatch, caplog) -> None:
    """One pressured sample logs a WARNING; the loop then exits when stop is set.

    Deterministic: the fake RSS reader sets the stop event during the first tick,
    so the loop runs exactly one body iteration and exits on the next ``wait``.
    """
    stop = threading.Event()

    def fake_usage(*_a, **_k):
        stop.set()  # request shutdown so the loop exits after this iteration
        return 1024 * 1024 * 1024

    monkeypatch.setattr(ph, "read_memory_usage_bytes", fake_usage)
    logger = logging.getLogger("test.ph.loop")
    with caplog.at_level(logging.WARNING, logger="test.ph.loop"):
        ph._watchdog_loop(
            team="coding_team",
            limit_bytes=1024 * 1024 * 1024,
            threshold=0.5,
            interval_s=0.0,
            stop_event=stop,
            logger=logger,
        )
    assert any("High memory" in r.getMessage() for r in caplog.records)


def test_watchdog_loop_survives_tick_errors(monkeypatch, caplog) -> None:
    """A reader that raises must not crash the diagnostic thread — it is swallowed."""
    stop = threading.Event()

    def boom(*_a, **_k):
        stop.set()
        raise RuntimeError("proc read failed")

    monkeypatch.setattr(ph, "read_memory_usage_bytes", boom)
    logger = logging.getLogger("test.ph.loop_err")
    # Should not raise out of the loop.
    ph._watchdog_loop(
        team="coding_team",
        limit_bytes=100,
        threshold=0.5,
        interval_s=0.0,
        stop_event=stop,
        logger=logger,
    )


# ----------------------------------------------------------------- watchdog lifecycle


def test_start_memory_watchdog_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("TEAM_MEMORY_WATCHDOG_ENABLED", "false")
    assert ph.start_memory_watchdog("coding_team") is None


def test_start_memory_watchdog_no_limit_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("TEAM_MEMORY_WATCHDOG_ENABLED", "true")
    monkeypatch.setattr(ph, "detect_memory_limit_bytes", lambda: None)
    assert ph.start_memory_watchdog("coding_team") is None


def test_start_memory_watchdog_returns_stoppable_thread(monkeypatch) -> None:
    monkeypatch.setenv("TEAM_MEMORY_WATCHDOG_ENABLED", "true")
    # Long interval: the thread blocks on wait() immediately and never ticks, so
    # the test exercises only start + clean stop without any timing race.
    thread = ph.start_memory_watchdog(
        "coding_team", limit_bytes=10 * 1024 * 1024, threshold=0.9, interval_s=100.0
    )
    assert thread is not None and thread.is_alive()
    assert hasattr(thread, "stop_event")
    thread.stop_event.set()  # type: ignore[attr-defined]
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_start_memory_watchdog_requires_team() -> None:
    with pytest.raises(AssertionError):
        ph.start_memory_watchdog("")


# ------------------------------------------------------ fault / excepthook diagnostics


@pytest.fixture()
def restore_diag_state():
    """Save/restore process-global hooks the diagnostics installer mutates."""
    saved = (
        sys.excepthook,
        threading.excepthook,
        ph._diagnostics_installed,
        ph._log,
        ph._original_sys_excepthook,
    )
    try:
        yield
    finally:
        (
            sys.excepthook,
            threading.excepthook,
            ph._diagnostics_installed,
            ph._log,
            ph._original_sys_excepthook,
        ) = saved


def test_install_fault_diagnostics_sets_hooks_and_is_idempotent(restore_diag_state) -> None:
    import faulthandler

    ph._diagnostics_installed = False
    ph.install_fault_diagnostics(logging.getLogger("test.ph.install"))

    assert sys.excepthook is ph._sys_excepthook
    assert threading.excepthook is ph._thread_excepthook
    assert faulthandler.is_enabled()

    # A second call is a no-op (does not raise; hooks remain installed).
    ph.install_fault_diagnostics()
    assert sys.excepthook is ph._sys_excepthook


def test_sys_excepthook_logs_uncaught_with_traceback(restore_diag_state, caplog) -> None:
    ph._log = logging.getLogger("test.ph.sysexc")
    try:
        raise ValueError("boom in main")
    except ValueError:
        exc = sys.exc_info()

    with caplog.at_level(logging.CRITICAL, logger="test.ph.sysexc"):
        ph._sys_excepthook(*exc)

    assert any("Uncaught exception in main thread" in r.getMessage() for r in caplog.records)
    assert any(r.exc_info for r in caplog.records)


def test_sys_excepthook_delegates_keyboardinterrupt(restore_diag_state) -> None:
    captured = {}
    ph._original_sys_excepthook = lambda *a: captured.setdefault("args", a)
    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        exc = sys.exc_info()

    ph._sys_excepthook(*exc)

    assert "args" in captured  # Ctrl-C delegates to the original hook, not logged


def test_thread_excepthook_logs_uncaught(restore_diag_state, caplog) -> None:
    ph._log = logging.getLogger("test.ph.thread")
    try:
        raise RuntimeError("boom in thread")
    except RuntimeError:
        exc_type, exc_value, exc_tb = sys.exc_info()
    args = types.SimpleNamespace(
        exc_type=exc_type,
        exc_value=exc_value,
        exc_traceback=exc_tb,
        thread=threading.current_thread(),
    )

    with caplog.at_level(logging.CRITICAL, logger="test.ph.thread"):
        ph._thread_excepthook(args)

    assert any("Uncaught exception in thread" in r.getMessage() for r in caplog.records)
    assert any(r.exc_info for r in caplog.records)


def test_thread_excepthook_ignores_systemexit(restore_diag_state, caplog) -> None:
    ph._log = logging.getLogger("test.ph.sysexit")
    args = types.SimpleNamespace(
        exc_type=SystemExit,
        exc_value=SystemExit(),
        exc_traceback=None,
        thread=threading.current_thread(),
    )
    with caplog.at_level(logging.CRITICAL, logger="test.ph.sysexit"):
        ph._thread_excepthook(args)

    assert not caplog.records  # a thread's SystemExit is a normal exit, not an error
