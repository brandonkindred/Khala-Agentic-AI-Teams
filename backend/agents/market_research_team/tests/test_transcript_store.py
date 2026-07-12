"""Unit tests for the per-job transcript store.

``AGENT_CACHE`` is pointed at a per-test temp dir by the autouse
``_isolated_transcript_store`` fixture in ``conftest.py``.
"""

from __future__ import annotations

import pytest

from market_research_team.shared import transcript_store as ts


def test_save_returns_bodyless_refs_and_load_round_trips() -> None:
    loaded = [("inline_transcript_1", "body one"), ("a.txt", "body two")]

    refs = ts.save_transcripts("job-1", loaded)

    # Refs carry only index + source — no transcript body crosses the boundary.
    assert refs == [
        {"index": 0, "source": "inline_transcript_1"},
        {"index": 1, "source": "a.txt"},
    ]
    assert ts.load_transcript("job-1", 0) == ("inline_transcript_1", "body one")
    assert ts.load_transcript("job-1", 1) == ("a.txt", "body two")


def test_save_empty_creates_no_refs() -> None:
    assert ts.save_transcripts("job-empty", []) == []


def test_save_is_idempotent_across_retries() -> None:
    ts.save_transcripts("job-2", [("s", "first")])
    ts.save_transcripts("job-2", [("s", "second")])  # re-run overwrites
    assert ts.load_transcript("job-2", 0) == ("s", "second")


def test_clear_removes_transcripts() -> None:
    ts.save_transcripts("job-3", [("s", "body")])
    ts.clear_transcripts("job-3")
    with pytest.raises(FileNotFoundError):
        ts.load_transcript("job-3", 0)


def test_clear_missing_job_is_a_noop() -> None:
    # Never raises even when the job dir was never created.
    ts.clear_transcripts("job-never-existed")


def test_sweep_orphaned_no_base_dir_returns_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path / "never-created"))
    assert ts.sweep_orphaned(lambda job_id: True) == 0


def test_sweep_orphaned_clears_only_inactive_jobs() -> None:
    ts.save_transcripts("job-active", [("s", "body")])
    ts.save_transcripts("job-inactive", [("s", "body")])

    cleared = ts.sweep_orphaned(lambda job_id: job_id == "job-active")

    assert cleared == 1
    assert ts.load_transcript("job-active", 0) == ("s", "body")
    with pytest.raises(FileNotFoundError):
        ts.load_transcript("job-inactive", 0)


def test_sweep_orphaned_treats_status_check_error_as_inactive() -> None:
    ts.save_transcripts("job-error", [("s", "body")])

    def _boom(job_id: str) -> bool:
        raise RuntimeError("job store down")

    cleared = ts.sweep_orphaned(_boom)

    assert cleared == 1
    with pytest.raises(FileNotFoundError):
        ts.load_transcript("job-error", 0)


def test_base_dir_warns_once_when_agent_cache_unset(monkeypatch, caplog) -> None:
    """Regression: the fallback-tempdir path must log exactly once per process,
    not once per call, mirroring blogging's one-shot warning convention."""
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    monkeypatch.setattr(ts, "_tempfile_fallback_warned", False)

    with caplog.at_level("WARNING", logger=ts.logger.name):
        ts._base_dir()
        ts._base_dir()

    warnings = [r for r in caplog.records if "AGENT_CACHE is not set" in r.message]
    assert len(warnings) == 1
