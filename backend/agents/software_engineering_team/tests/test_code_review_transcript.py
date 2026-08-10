"""Unit tests for code_review_agent.transcript.record_transcript_entry."""

from __future__ import annotations

from llm_service import llm_attribution
from software_engineering_team.code_review_agent import transcript


def test_record_is_noop_without_bound_job_id(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entry",
        lambda job_id, entry: calls.append((job_id, entry)),
    )
    transcript.record_transcript_entry("chunk_review", "a.py", "prompt", "response")
    assert calls == []


def test_record_appends_entry_when_job_id_bound(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entry",
        lambda job_id, entry: calls.append((job_id, entry)),
    )
    with llm_attribution(job_id="job-1"):
        transcript.record_transcript_entry(
            "chunk_review", "a.py", "prompt-text", "response-text", model="m", duration_ms=42.0
        )
    assert len(calls) == 1
    job_id, entry = calls[0]
    assert job_id == "job-1"
    assert entry["stage"] == "chunk_review"
    assert entry["target"] == "a.py"
    assert entry["prompt"] == "prompt-text"
    assert entry["response"] == "response-text"
    assert entry["model"] == "m"
    assert entry["duration_ms"] == 42
    assert entry["started_at"]  # non-empty ISO timestamp


def test_record_never_raises_on_store_failure(monkeypatch) -> None:
    def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "software_engineering_team.review_history_store.append_review_transcript_entry", _boom
    )
    with llm_attribution(job_id="job-1"):
        # Must not raise: persistence failures are logged and swallowed.
        transcript.record_transcript_entry("chunk_review", "a.py", "p", "r")


def test_model_label_falls_back_to_type_name() -> None:
    class _Unresolvable:
        pass

    assert transcript.model_label(_Unresolvable()) == "_Unresolvable"


def test_model_label_reads_model_id_attribute() -> None:
    class _Model:
        model_id = "claude-x"

    assert transcript.model_label(_Model()) == "claude-x"
