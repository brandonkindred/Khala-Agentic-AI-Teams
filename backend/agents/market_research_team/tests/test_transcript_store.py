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
