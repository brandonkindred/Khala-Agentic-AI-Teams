"""Unit tests for shared_observability.process_health.

These cover the diagnostics that turn a silent worker death into a debuggable
event: defensive env parsing, RSS / cgroup-limit reads, the memory-pressure
evaluation and watchdog tick/loop, the watchdog lifecycle, and the faulthandler
/ uncaught-exception hook installation.
"""

from __future__ import annotations

import logging
import os
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
    # Non-finite parses must fall back to the (finite) default — a nan interval
    # busy-loops the watchdog and inf crashes it; clamp via </> can't catch nan.
    for bad in ("inf", "-inf", "nan", "1e400"):
        monkeypatch.setenv("X_F", bad)
        assert ph._env_float("X_F", 30.0, minimum=1.0) == 30.0


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


@pytest.mark.parametrize("bad", ["garbage", "0", "-5", "inf", "1e400", "-inf", "nan", "  "])
def test_detect_memory_limit_env_bad_value_falls_through(monkeypatch, tmp_path, bad) -> None:
    # Includes 'inf'/'1e400' which make int(float(...)) raise OverflowError — the
    # parse must swallow it (never raise) and fall through to cgroup detection.
    monkeypatch.setenv("TEAM_MEMORY_WATCHDOG_LIMIT_MB", bad)
    monkeypatch.setattr(ph, "_CGROUP_V2_MAX", str(tmp_path / "absent_v2"))
    monkeypatch.setattr(ph, "_CGROUP_V1_LIMIT", str(tmp_path / "absent_v1"))
    assert ph.detect_memory_limit_bytes() is None


def test_positive_int_or_none_never_raises_on_overflow() -> None:
    assert ph._positive_int_or_none("inf") is None
    assert ph._positive_int_or_none("1e400") is None
    assert ph._positive_int_or_none("-inf") is None
    assert ph._positive_int_or_none("512") == 512


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


# ------------------------------------------------------------------ OOM-kill detection


def test_read_oom_kill_count_parses_memory_events(tmp_path) -> None:
    """memory.events is multi-line key/value; extract the oom_kill counter."""
    events = tmp_path / "memory.events"
    events.write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 2\noom_group_kill 0\n")
    assert ph.read_oom_kill_count(str(events)) == 2


def test_read_oom_kill_count_missing_key_or_file(tmp_path) -> None:
    no_key = tmp_path / "memory.events"
    no_key.write_text("low 0\nhigh 0\nmax 0\noom 0\n")  # no oom_kill line
    assert ph.read_oom_kill_count(str(no_key)) is None
    assert ph.read_oom_kill_count(str(tmp_path / "absent")) is None
    garbage = tmp_path / "g"
    garbage.write_text("oom_kill not-a-number\n")
    assert ph.read_oom_kill_count(str(garbage)) is None


def test_read_memory_peak_bytes(tmp_path) -> None:
    peak = tmp_path / "memory.peak"
    peak.write_text(str(730669056))
    assert ph.read_memory_peak_bytes(str(peak)) == 730669056
    assert ph.read_memory_peak_bytes(str(tmp_path / "absent")) is None


def test_oom_check_tick_detects_increment() -> None:
    limit = 4 * 1024 * 1024 * 1024
    # First sample with no prior baseline: adopt the count, no message.
    count, msg = ph._oom_check_tick(
        None, limit_bytes=limit, events_reader=lambda: 2, peak_reader=lambda: 1
    )
    assert count == 2 and msg is None
    # No change: no message.
    count, msg = ph._oom_check_tick(
        2, limit_bytes=limit, events_reader=lambda: 2, peak_reader=lambda: 1
    )
    assert count == 2 and msg is None
    # Increment: a new OOM kill — loud message naming SIGKILL + global-OOM hint.
    count, msg = ph._oom_check_tick(
        2,
        limit_bytes=limit,
        events_reader=lambda: 3,
        peak_reader=lambda: 700 * 1024 * 1024,
    )
    assert count == 3 and msg is not None
    assert "OOM kill" in msg and "SIGKILL" in msg and "global OOM" in msg


def test_oom_check_tick_unreadable_is_noop() -> None:
    count, msg = ph._oom_check_tick(
        5, limit_bytes=100, events_reader=lambda: None, peak_reader=lambda: None
    )
    assert count == 5 and msg is None


def test_watchdog_loop_logs_oom_kill(monkeypatch, caplog) -> None:
    """An oom_kill counter increment between samples logs an ERROR even though the
    worker that died left no traceback — the core visibility fix."""
    stop = threading.Event()
    counts = iter([0, 1])  # baseline 0 (pre-loop), then 1 (in-loop) => increment

    def fake_oom(*_a, **_k):
        try:
            return next(counts)
        except StopIteration:
            return 1

    def fake_usage(*_a, **_k):
        stop.set()  # exit after one iteration
        return 0  # no memory-pressure warning, isolate the OOM-kill path

    monkeypatch.setattr(ph, "read_oom_kill_count", fake_oom)
    monkeypatch.setattr(ph, "read_memory_peak_bytes", lambda *a, **k: 700 * 1024 * 1024)
    monkeypatch.setattr(ph, "read_memory_usage_bytes", fake_usage)
    logger = logging.getLogger("test.ph.oomloop")
    with caplog.at_level(logging.ERROR, logger="test.ph.oomloop"):
        ph._watchdog_loop(
            team="investment_team",
            limit_bytes=4 * 1024 * 1024 * 1024,
            threshold=0.85,
            interval_s=0.01,
            stop_event=stop,
            logger=logger,
        )
    assert any("OOM kill detected" in r.getMessage() for r in caplog.records)


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

    Deterministic: the fake reader sets the stop event during the first tick, so
    the loop runs exactly one body iteration and exits on the next ``wait`` (a
    tiny positive interval is used since the loop now rejects ``interval_s <= 0``).
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
            interval_s=0.01,
            stop_event=stop,
            logger=logger,
        )
    assert any("High memory" in r.getMessage() for r in caplog.records)


def test_watchdog_loop_rejects_nonpositive_interval() -> None:
    """interval_s <= 0 would tight-loop; the loop enforces its precondition."""
    with pytest.raises(ValueError):
        ph._watchdog_loop(
            team="t",
            limit_bytes=100,
            threshold=0.5,
            interval_s=0.0,
            stop_event=threading.Event(),
            logger=logging.getLogger("test.ph.loop_guard"),
        )


def test_watchdog_loop_survives_tick_errors(monkeypatch, caplog) -> None:
    """A reader that raises must not crash the diagnostic thread — it is swallowed."""
    stop = threading.Event()

    def boom(*_a, **_k):
        stop.set()
        raise RuntimeError("proc read failed")

    monkeypatch.setattr(ph, "read_memory_usage_bytes", boom)
    logger = logging.getLogger("test.ph.loop_err")
    # Should not raise out of the loop; the first failure surfaces at WARNING so a
    # persistent watchdog bug isn't hidden at DEBUG forever.
    with caplog.at_level(logging.WARNING, logger="test.ph.loop_err"):
        ph._watchdog_loop(
            team="coding_team",
            limit_bytes=100,
            threshold=0.5,
            interval_s=0.01,
            stop_event=stop,
            logger=logger,
        )
    assert any("memory watchdog tick failed" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------------- watchdog lifecycle


def test_start_memory_watchdog_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("TEAM_MEMORY_WATCHDOG_ENABLED", "false")
    assert ph.start_memory_watchdog("coding_team") is None


def test_start_memory_watchdog_no_limit_and_no_oom_counter_returns_none(monkeypatch) -> None:
    """Nothing to watch (no cgroup limit AND no readable oom_kill counter) → no thread."""
    monkeypatch.setenv("TEAM_MEMORY_WATCHDOG_ENABLED", "true")
    monkeypatch.setattr(ph, "detect_memory_limit_bytes", lambda: None)
    monkeypatch.setattr(ph, "read_oom_kill_count", lambda *a, **k: None)
    assert ph.start_memory_watchdog("coding_team") is None


def test_start_memory_watchdog_oom_only_mode_without_limit(monkeypatch) -> None:
    """An unbounded container (no mem_limit) still gets a watchdog for OOM-kill
    detection when the kernel oom_kill counter is readable — the case that matters
    for host/VM-wide (global) OOM. Regression guard for the silent-OOM gap."""
    monkeypatch.setenv("TEAM_MEMORY_WATCHDOG_ENABLED", "true")
    monkeypatch.setattr(ph, "detect_memory_limit_bytes", lambda: None)
    monkeypatch.setattr(ph, "read_oom_kill_count", lambda *a, **k: 0)  # counter present
    # Long interval so the thread parks on wait() immediately — no timing race.
    wd = ph.start_memory_watchdog("investment_team", interval_s=100.0)
    assert wd is not None and isinstance(wd, ph.Watchdog)
    assert wd.thread.is_alive()
    wd.stop_event.set()
    wd.thread.join(timeout=2)
    assert not wd.thread.is_alive()


def test_oom_check_tick_within_container_cause(monkeypatch) -> None:
    """When the peak is near the container limit, the message attributes the kill
    to the container's own budget — not host/VM-wide (global) OOM."""
    limit = 4 * 1024 * 1024 * 1024
    count, msg = ph._oom_check_tick(
        1,
        limit_bytes=limit,
        events_reader=lambda: 2,
        peak_reader=lambda: int(limit * 0.95),  # 95% of limit → within-container
    )
    assert count == 2 and msg is not None
    assert "exceeded its own memory limit" in msg
    assert "global OOM" not in msg


def test_oom_check_tick_unknown_limit_still_detects(monkeypatch) -> None:
    """With no container limit, an oom_kill increment is still reported, and the
    message flags the limit as unset (pointing at host/VM-wide OOM)."""
    count, msg = ph._oom_check_tick(
        1, limit_bytes=None, events_reader=lambda: 2, peak_reader=lambda: 500 * 1024 * 1024
    )
    assert count == 2 and msg is not None
    assert "unset" in msg and "global OOM" in msg


def test_watchdog_loop_uses_seeded_oom_baseline(monkeypatch, caplog) -> None:
    """initial_oom_count seeds the baseline (no second counter read), so a kill
    between arm time and the first tick is still reported as new."""
    stop = threading.Event()

    def fake_oom(*_a, **_k):
        stop.set()
        return 6  # current count in-loop; baseline comes from initial_oom_count=5

    monkeypatch.setattr(ph, "read_oom_kill_count", fake_oom)
    monkeypatch.setattr(ph, "read_memory_peak_bytes", lambda *a, **k: 400 * 1024 * 1024)
    logger = logging.getLogger("test.ph.seed")
    with caplog.at_level(logging.ERROR, logger="test.ph.seed"):
        ph._watchdog_loop(
            team="investment_team",
            limit_bytes=None,
            threshold=0.85,
            interval_s=0.01,
            stop_event=stop,
            logger=logger,
            initial_oom_count=5,  # had a re-read been used, baseline would be 6 → no alert
        )
    assert any("OOM kill detected" in r.getMessage() for r in caplog.records)


def test_watchdog_loop_oom_only_skips_pressure(monkeypatch, caplog) -> None:
    """limit_bytes=None: the loop must not attempt a pressure evaluation (which
    requires a positive limit) but must still fire OOM detection."""
    stop = threading.Event()
    counts = iter([0, 1])  # baseline 0 (pre-loop), then 1 (in-loop) => increment

    def fake_oom(*_a, **_k):
        try:
            v = next(counts)
        except StopIteration:
            v = 1
        if v == 1:
            stop.set()  # exit after detecting the increment
        return v

    def fake_usage(*_a, **_k):  # must NOT be consulted in OOM-only mode
        raise AssertionError("pressure check ran despite no limit")

    monkeypatch.setattr(ph, "read_oom_kill_count", fake_oom)
    monkeypatch.setattr(ph, "read_memory_peak_bytes", lambda *a, **k: 400 * 1024 * 1024)
    monkeypatch.setattr(ph, "read_memory_usage_bytes", fake_usage)
    logger = logging.getLogger("test.ph.oomonly")
    with caplog.at_level(logging.ERROR, logger="test.ph.oomonly"):
        ph._watchdog_loop(
            team="investment_team",
            limit_bytes=None,
            threshold=0.85,
            interval_s=0.01,
            stop_event=stop,
            logger=logger,
        )
    assert any("OOM kill detected" in r.getMessage() for r in caplog.records)


def test_start_memory_watchdog_returns_watchdog(monkeypatch) -> None:
    monkeypatch.setenv("TEAM_MEMORY_WATCHDOG_ENABLED", "true")
    # Long interval: the thread blocks on wait() immediately and never ticks, so
    # the test exercises only start + clean stop without any timing race.
    wd = ph.start_memory_watchdog(
        "coding_team", limit_bytes=10 * 1024 * 1024, threshold=0.9, interval_s=100.0
    )
    assert wd is not None and isinstance(wd, ph.Watchdog)
    assert wd.thread.is_alive()
    wd.stop_event.set()
    wd.thread.join(timeout=2)
    assert not wd.thread.is_alive()


def test_start_memory_watchdog_requires_team() -> None:
    with pytest.raises(ValueError):
        ph.start_memory_watchdog("")


def test_watchdog_dataclass_is_reexported_from_package() -> None:
    """``Watchdog`` is the public return type of start_memory_watchdog, so it is
    importable from the package without reaching into the private submodule."""
    from shared_observability import Watchdog as ExportedWatchdog

    assert ExportedWatchdog is ph.Watchdog


# ------------------------------------------------------ fault / excepthook diagnostics


@pytest.fixture()
def restore_diag_state():
    """Save/restore process-global hooks the diagnostics installer mutates."""
    saved = (
        sys.excepthook,
        threading.excepthook,
        ph._diagnostics_installed,
        ph._log,
        ph._chain_sys_excepthook,
        ph._chain_thread_excepthook,
    )
    try:
        yield
    finally:
        (
            sys.excepthook,
            threading.excepthook,
            ph._diagnostics_installed,
            ph._log,
            ph._chain_sys_excepthook,
            ph._chain_thread_excepthook,
        ) = saved


def test_install_fault_diagnostics_sets_hooks_and_is_idempotent(restore_diag_state) -> None:
    faulthandler = pytest.importorskip("faulthandler")  # skip if unavailable on the platform

    ph._diagnostics_installed = False
    ph.install_fault_diagnostics(logging.getLogger("test.ph.install"))

    assert sys.excepthook is ph._sys_excepthook
    assert threading.excepthook is ph._thread_excepthook
    assert faulthandler.is_enabled()

    # A second call is a no-op (does not raise; hooks remain installed).
    ph.install_fault_diagnostics()
    assert sys.excepthook is ph._sys_excepthook


def test_install_fault_diagnostics_exports_pythonfaulthandler(
    restore_diag_state, monkeypatch
) -> None:
    """The env var is exported so spawned/forkserver workers arm faulthandler at
    interpreter startup."""
    monkeypatch.delenv("PYTHONFAULTHANDLER", raising=False)
    ph._diagnostics_installed = False
    ph.install_fault_diagnostics(logging.getLogger("test.ph.env"))
    assert os.environ.get("PYTHONFAULTHANDLER") == "1"


def test_install_fault_diagnostics_preserves_operator_pythonfaulthandler(
    restore_diag_state, monkeypatch
) -> None:
    """An operator who set PYTHONFAULTHANDLER=0 (disabled) is not overridden."""
    monkeypatch.setenv("PYTHONFAULTHANDLER", "0")
    ph._diagnostics_installed = False
    ph.install_fault_diagnostics(logging.getLogger("test.ph.env2"))
    assert os.environ.get("PYTHONFAULTHANDLER") == "0"


def test_install_fault_diagnostics_exports_env_even_if_enable_fails(
    restore_diag_state, monkeypatch
) -> None:
    """If faulthandler.enable() raises (e.g. a replaced stderr with no fileno),
    the env var is still exported and the excepthooks are still installed — the
    failure is isolated to this process's faulthandler, not the whole arming."""
    import faulthandler

    monkeypatch.delenv("PYTHONFAULTHANDLER", raising=False)
    monkeypatch.setattr(faulthandler, "is_enabled", lambda: False)

    def _boom() -> None:
        raise RuntimeError("stderr has no fileno")

    monkeypatch.setattr(faulthandler, "enable", _boom)
    ph._diagnostics_installed = False
    ph.install_fault_diagnostics(logging.getLogger("test.ph.env3"))

    assert os.environ.get("PYTHONFAULTHANDLER") == "1"  # set despite enable() failing
    assert sys.excepthook is ph._sys_excepthook  # hooks still installed


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
    ph._chain_sys_excepthook = lambda *a: captured.setdefault("args", a)
    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        exc = sys.exc_info()

    ph._sys_excepthook(*exc)

    assert "args" in captured  # Ctrl-C chains to the previous hook, not logged


def test_sys_excepthook_ignores_systemexit(restore_diag_state, caplog) -> None:
    """SystemExit is an intentional termination, not a fault — it must not be
    logged at CRITICAL (symmetry with the thread hook). In practice the
    interpreter handles main-thread SystemExit before sys.excepthook anyway."""
    ph._log = logging.getLogger("test.ph.sysexit_main")
    ph._chain_sys_excepthook = None
    with caplog.at_level(logging.CRITICAL, logger="test.ph.sysexit_main"):
        ph._sys_excepthook(SystemExit, SystemExit(0), None)
    assert not caplog.records  # not logged


def test_sys_excepthook_chains_to_previous_custom_hook(restore_diag_state, caplog) -> None:
    """A non-default previous hook (e.g. Sentry) is still invoked after logging,
    so installing our diagnostics never disables another error reporter."""
    ph._log = logging.getLogger("test.ph.chain")
    calls = []
    ph._chain_sys_excepthook = lambda *a: calls.append(a)
    try:
        raise ValueError("boom")
    except ValueError:
        exc = sys.exc_info()
    with caplog.at_level(logging.CRITICAL, logger="test.ph.chain"):
        ph._sys_excepthook(*exc)
    assert any("Uncaught exception in main thread" in r.getMessage() for r in caplog.records)
    assert len(calls) == 1  # chained to the previous reporter


def test_sys_excepthook_swallows_raising_chained_hook(restore_diag_state) -> None:
    """A chained hook (e.g. a broken Sentry) that raises must not propagate out of
    our excepthook (which would mask our log / disrupt interpreter teardown)."""
    ph._log = logging.getLogger("test.ph.chain_raise")

    def _boom(*_a):
        raise RuntimeError("sentry down")

    ph._chain_sys_excepthook = _boom
    try:
        raise ValueError("boom")
    except ValueError:
        exc = sys.exc_info()
    ph._sys_excepthook(*exc)  # must not raise


def test_thread_excepthook_chains_to_previous_custom_hook(restore_diag_state) -> None:
    calls = []
    ph._chain_thread_excepthook = lambda args: calls.append(args)
    ph._log = logging.getLogger("test.ph.chain2")
    args = types.SimpleNamespace(
        exc_type=RuntimeError,
        exc_value=RuntimeError("x"),
        exc_traceback=None,
        thread=threading.current_thread(),
    )
    ph._thread_excepthook(args)
    assert len(calls) == 1


def test_thread_excepthook_swallows_raising_chained_hook(restore_diag_state) -> None:
    """A raising chained thread hook must be swallowed, not propagated."""
    ph._log = logging.getLogger("test.ph.chain2_raise")

    def _boom(_args):
        raise RuntimeError("sentry down")

    ph._chain_thread_excepthook = _boom
    args = types.SimpleNamespace(
        exc_type=RuntimeError,
        exc_value=RuntimeError("x"),
        exc_traceback=None,
        thread=threading.current_thread(),
    )
    ph._thread_excepthook(args)  # must not raise


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
