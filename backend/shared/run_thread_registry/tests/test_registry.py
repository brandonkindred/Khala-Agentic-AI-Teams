import threading
import time
from concurrent.futures import ThreadPoolExecutor

from shared.run_thread_registry import RunThreadRegistry


def test_register_marks_thread_alive():
    registry = RunThreadRegistry()
    registered = threading.Event()
    stop = threading.Event()

    def _target():
        registry.register("job-1")
        registered.set()
        stop.wait(timeout=5)

    worker = threading.Thread(target=_target)
    worker.start()
    try:
        registered.wait(timeout=5)
        assert registry.is_alive("job-1") is True
    finally:
        stop.set()
        worker.join(timeout=5)
    assert registry.is_alive("job-1") is False  # thread has exited, still registered


def test_clear_removes_registration():
    registry = RunThreadRegistry()
    registry.register("job-1")
    assert registry.is_alive("job-1") is True
    registry.clear("job-1")
    assert registry.is_alive("job-1") is False


def test_is_alive_false_for_unknown_job():
    registry = RunThreadRegistry()
    assert registry.is_alive("never-seen") is False


def test_claim_is_exclusive_until_released():
    registry = RunThreadRegistry()
    assert registry.claim("job-1") is True
    assert registry.claim("job-1") is False
    registry.clear("job-1")
    assert registry.claim("job-1") is True


def test_register_releases_pending_claim():
    registry = RunThreadRegistry()
    assert registry.claim("job-1") is True
    registry.register("job-1")
    assert "job-1" not in registry.starting_jobs
    # A second claim now fails because the thread is alive, not because it's still "starting".
    assert registry.claim("job-1") is False


def test_clear_releases_pending_claim():
    registry = RunThreadRegistry()
    assert registry.claim("job-1") is True
    registry.clear("job-1")
    assert "job-1" not in registry.starting_jobs
    assert registry.claim("job-1") is True


def test_threads_and_starting_jobs_properties_are_live_aliases():
    registry = RunThreadRegistry()
    registry.register("job-1")
    assert "job-1" in registry.threads
    registry.threads.pop("job-1", None)
    assert registry.is_alive("job-1") is False

    assert registry.claim("job-2") is True
    assert "job-2" in registry.starting_jobs
    registry.starting_jobs.discard("job-2")
    assert registry.claim("job-2") is True


def test_concurrent_claim_never_double_wins():
    """N threads racing claim() for the SAME job_id must leave exactly one winner — the lock
    closes the check-then-set window in claim() (check starting_jobs/threads, then mark
    'starting') that would otherwise let two racers both observe "unclaimed" and both succeed.
    """
    registry = RunThreadRegistry()
    n = 20
    barrier = threading.Barrier(n)

    def _claim(_i: int) -> bool:
        barrier.wait(timeout=10)
        return registry.claim("contended-job")

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(_claim, range(n)))

    assert sum(results) == 1
    assert registry.starting_jobs == {"contended-job"}


def test_concurrent_register_clear_across_distinct_jobs_stays_consistent():
    """N threads registering/clearing DISJOINT job_ids concurrently must leave the registry
    consistent — no lost or cross-contaminated entries.
    """
    registry = RunThreadRegistry()
    n = 20
    barrier = threading.Barrier(n)
    stop_events = [threading.Event() for _ in range(n)]

    def _run(i: int) -> None:
        barrier.wait(timeout=10)
        job_id = f"job-{i}"
        registry.register(job_id)
        stop_events[i].wait(timeout=5)
        registry.clear(job_id)

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    # Give every thread a moment to register before releasing any of them.
    time.sleep(0.2)
    for i in range(n):
        assert registry.is_alive(f"job-{i}") is True
    for e in stop_events:
        e.set()
    for t in threads:
        t.join(timeout=5)

    for i in range(n):
        assert registry.is_alive(f"job-{i}") is False
    assert registry.threads == {}
