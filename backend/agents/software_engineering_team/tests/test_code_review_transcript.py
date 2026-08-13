"""Unit tests for the batched code-review transcript flusher (``transcript``).

Mirrors ``test_trace_flusher.py``: the flusher moves ``code_review_transcripts``
writes off the LLM call path — ``record_transcript_entry`` builds an entry dict
(pure Python, no I/O) and appends it to a bounded deque; a background heartbeat
drains the deque in one batched write per job_id. These tests pin buffer/
overflow semantics, batching-by-job_id, and zero DB I/O on enqueue.
"""

from __future__ import annotations

import threading

import pytest

from llm_service import llm_attribution
from software_engineering_team.code_review_agent import transcript


@pytest.fixture(autouse=True)
def _reset_transcript(monkeypatch):
    """Start each test with an empty buffer, no heartbeat, and Postgres enabled
    (so record_transcript_entry exercises the enqueue path by default)."""
    monkeypatch.setattr(transcript, "is_postgres_enabled", lambda: True)
    transcript._reset_for_test()
    yield
    transcript._reset_for_test()


def test_record_does_no_db_io_on_enqueue(monkeypatch) -> None:
    """record_transcript_entry never touches Postgres — enqueuing is pure Python."""

    def _boom(*_a, **_kw):
        pytest.fail("record_transcript_entry must not write to the store on enqueue")

    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entries",
        _boom,
    )
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry("chunk_review", "a.py", "prompt", "response")
    assert transcript._buffer_size() == 1


def test_record_is_noop_without_bound_job_id() -> None:
    transcript.record_transcript_entry("chunk_review", "a.py", "prompt", "response")
    assert transcript._buffer_size() == 0


def test_record_is_noop_when_postgres_disabled(monkeypatch) -> None:
    monkeypatch.setattr(transcript, "is_postgres_enabled", lambda: False)
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry("chunk_review", "a.py", "prompt", "response")
    assert transcript._buffer_size() == 0


def test_record_builds_entry_fields(monkeypatch) -> None:
    captured: list = []

    def _write(job_id, entries):
        captured.append((job_id, entries))
        return True

    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entries",
        _write,
    )
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry(
            "chunk_review", "a.py", "prompt-text", "response-text", model="m", duration_ms=42.0
        )
    transcript.drain()

    assert len(captured) == 1
    job_id, entries = captured[0]
    assert job_id == "job-1"
    assert len(entries) == 1
    entry = entries[0]
    assert entry["stage"] == "chunk_review"
    assert entry["target"] == "a.py"
    assert entry["prompt"] == "prompt-text"
    assert entry["response"] == "response-text"
    assert entry["model"] == "m"
    assert entry["duration_ms"] == 42
    assert entry["started_at"]  # non-empty ISO timestamp


def test_record_prepends_system_prompt_when_supplied(monkeypatch) -> None:
    """The recorded prompt includes the system prompt when the caller supplies
    one -- otherwise the transcript would omit the instruction layer that
    actually governed the model's behavior for that call."""
    captured: list = []

    def _write(job_id, entries):
        captured.append((job_id, entries))
        return True

    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entries",
        _write,
    )
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry(
            "chunk_review", "a.py", "user-text", "resp", system_prompt="be a reviewer"
        )
    transcript.drain()

    entry = captured[0][1][0]
    assert "be a reviewer" in entry["prompt"]
    assert "user-text" in entry["prompt"]
    assert entry["prompt"].index("be a reviewer") < entry["prompt"].index("user-text")


def test_record_leaves_prompt_unchanged_without_system_prompt(monkeypatch) -> None:
    captured: list = []

    def _write(job_id, entries):
        captured.append((job_id, entries))
        return True

    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entries",
        _write,
    )
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry("chunk_review", "a.py", "user-text", "resp")
    transcript.drain()

    entry = captured[0][1][0]
    assert entry["prompt"] == "user-text"


def test_overflow_warning_throttled_to_once_per_burst(monkeypatch, caplog) -> None:
    monkeypatch.setenv("CODE_REVIEW_TRANSCRIPT_BUFFER_MAX", "2")
    caplog.set_level("WARNING", logger="software_engineering_team.code_review_agent.transcript")
    with llm_attribution(job_id="job-1"):
        for i in range(5):
            transcript.record_transcript_entry("chunk_review", f"f{i}.py", "p", "r")

    warnings = [r for r in caplog.records if "dropping oldest" in r.message]
    assert len(warnings) == 1
    assert transcript._buffer_size() == 2

    caplog.clear()
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry("chunk_review", "f9.py", "p", "r")
    warnings2 = [r for r in caplog.records if "dropping oldest" in r.message]
    assert len(warnings2) == 0


def test_note_overflow_requires_buffer_lock() -> None:
    """_note_overflow mutates the overflow throttle; calling it without
    ``_buffer_lock`` is a caller bug and must fail the precondition."""
    with pytest.raises(RuntimeError, match="_buffer_lock"):
        transcript._note_overflow(1)


def test_drain_batches_entries_per_job(monkeypatch) -> None:
    """Entries for two different jobs flush as two separate batched calls."""
    captured: dict = {}

    def _write(job_id, entries):
        captured.setdefault(job_id, []).extend(entries)
        return True

    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entries",
        _write,
    )
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry("chunk_review", "a.py", "p1", "r1")
        transcript.record_transcript_entry("synthesis", "", "p2", "r2")
    with llm_attribution(job_id="job-2"):
        transcript.record_transcript_entry("chunk_review", "b.py", "p3", "r3")

    n = transcript.drain()

    assert n == 3
    assert [e["stage"] for e in captured["job-1"]] == ["chunk_review", "synthesis"]
    assert [e["stage"] for e in captured["job-2"]] == ["chunk_review"]
    assert transcript._buffer_size() == 0


def test_drain_requeues_on_write_failure(monkeypatch, caplog) -> None:
    """A failed write is requeued, not discarded, so the next drain retries it."""
    caplog.set_level("WARNING", logger="software_engineering_team.code_review_agent.transcript")
    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entries",
        lambda job_id, entries: False,
    )
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry("chunk_review", "a.py", "p", "r")
    n = transcript.drain()
    assert n == 0
    assert any("failed to flush" in r.message for r in caplog.records)
    # Requeued, not dropped — the entry is still buffered for the next drain.
    assert transcript._buffer_size() == 1


def test_drain_requeues_on_write_exception(monkeypatch, caplog) -> None:
    """A write that raises (rather than returning False) is also requeued."""
    caplog.set_level("WARNING", logger="software_engineering_team.code_review_agent.transcript")
    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entries",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pg down")),
    )
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry("chunk_review", "a.py", "p", "r")
    n = transcript.drain()
    assert n == 0
    assert any("failed to flush" in r.message for r in caplog.records)
    assert transcript._buffer_size() == 1


def test_drain_requeue_then_succeeds_on_retry(monkeypatch) -> None:
    """The requeued entry is included in — and cleared by — the next successful drain."""
    calls: list = []

    def _write(job_id, entries):
        calls.append(list(entries))
        return len(calls) > 1  # first call fails, second (the retry) succeeds

    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entries",
        _write,
    )
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry("chunk_review", "a.py", "p", "r")
    assert transcript.drain() == 0
    assert transcript._buffer_size() == 1

    assert transcript.drain() == 1
    assert transcript._buffer_size() == 0
    assert len(calls) == 2
    assert calls[0] == calls[1]  # same entry retried verbatim


def test_requeue_drops_oldest_on_overflow(monkeypatch, caplog) -> None:
    """Requeuing past the buffer cap drops the oldest entries, like _enqueue."""
    monkeypatch.setenv("CODE_REVIEW_TRANSCRIPT_BUFFER_MAX", "1")
    caplog.set_level("WARNING", logger="software_engineering_team.code_review_agent.transcript")
    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entries",
        lambda job_id, entries: False,
    )
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry("chunk_review", "a.py", "p1", "r1")
        transcript.record_transcript_entry("chunk_review", "b.py", "p2", "r2")
    # The cap already dropped one entry on enqueue; only one is buffered to drain.
    assert transcript._buffer_size() == 1
    transcript.drain()
    # The failed write is requeued but still bounded by the cap.
    assert transcript._buffer_size() == 1


def test_drain_empty_buffer_is_zero() -> None:
    assert transcript.drain() == 0


def test_overlapping_drains_wait_for_in_flight_persist(monkeypatch) -> None:
    """A terminal drain() must not return while another drain has snapshotted
    the buffer but not yet finished writing — otherwise the UI's one-shot
    fetch after CodeReviewAgent.run's finally can miss that batch.
    """
    first_entered = threading.Event()
    release_first = threading.Event()
    first_finished = threading.Event()
    second_finished = threading.Event()

    def _write(job_id, entries):
        first_entered.set()
        assert release_first.wait(timeout=2.0)
        return True

    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entries",
        _write,
    )
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry("chunk_review", "a.py", "p", "r")

    def _first() -> None:
        transcript.drain()
        first_finished.set()

    def _second() -> None:
        transcript.drain()
        second_finished.set()

    t1 = threading.Thread(target=_first)
    t1.start()
    assert first_entered.wait(timeout=2.0)
    t2 = threading.Thread(target=_second)
    t2.start()
    # Second drain must not complete while the first persist is in flight.
    assert not second_finished.wait(timeout=0.3)
    release_first.set()
    assert second_finished.wait(timeout=2.0)
    assert first_finished.is_set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert not t1.is_alive() and not t2.is_alive()


def test_record_never_raises_when_enqueue_fails(monkeypatch, caplog) -> None:
    """record_transcript_entry's contract is never-raises — a buffer failure
    must not propagate into the review pipeline."""
    caplog.set_level("WARNING", logger="software_engineering_team.code_review_agent.transcript")

    def _boom(*_a, **_kw):
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(transcript, "_enqueue", _boom)
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry("chunk_review", "a.py", "p", "r")
    assert any("failed to buffer" in r.message for r in caplog.records)


def test_register_starts_heartbeat_idempotently() -> None:
    started: list = []
    real_start = transcript.BackgroundHeartbeat.start

    def fake_start(self):
        started.append(self)
        return real_start(self)

    try:
        transcript.BackgroundHeartbeat.start = fake_start
        transcript.register_transcript_flusher()
        transcript.register_transcript_flusher()  # idempotent — second is a no-op
        assert len(started) == 1
    finally:
        transcript.BackgroundHeartbeat.start = real_start


def test_unregister_stops_heartbeat() -> None:
    transcript.register_transcript_flusher()
    assert transcript._registered is True
    transcript.unregister()
    assert transcript._registered is False


def test_unregister_when_not_registered_is_noop() -> None:
    transcript.unregister()  # must not raise
    assert transcript._registered is False


def test_shutdown_drains_after_unregistering(monkeypatch) -> None:
    order: list = []
    monkeypatch.setattr(transcript, "unregister", lambda: order.append("unregister"))
    monkeypatch.setattr(transcript, "drain", lambda: order.append("drain") or 0)

    transcript.shutdown()

    assert order == ["unregister", "drain"]


def test_model_label_falls_back_to_type_name() -> None:
    class _Unresolvable:
        pass

    assert transcript.model_label(_Unresolvable()) == "_Unresolvable"


def test_model_label_reads_model_id_attribute() -> None:
    class _Model:
        model_id = "claude-x"

    assert transcript.model_label(_Model()) == "claude-x"


def test_model_label_reads_config_dict() -> None:
    class _Model:
        config = {"model_name": "claude-y"}

    assert transcript.model_label(_Model()) == "claude-y"


def test_unflushed_entries_returns_requeued_batch(monkeypatch) -> None:
    """After a failed drain the batch is still buffered; unflushed_entries
    exposes it so a one-shot transcript GET is not empty while the heartbeat
    retries."""
    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entries",
        lambda *a, **k: False,
    )
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry("chunk_review", "a.py", "p", "r")
    assert transcript.drain() == 0
    extra = transcript.unflushed_entries("job-1")
    assert len(extra) == 1
    assert extra[0]["target"] == "a.py"
    assert transcript.unflushed_entries("other-job") == []
    assert transcript.merge_unflushed("job-1", []) == extra
    assert transcript.merge_unflushed("job-1", [{"stage": "durable"}])[0]["stage"] == "durable"
