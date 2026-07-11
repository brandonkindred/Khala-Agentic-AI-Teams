"""Tests for the durable RepoContext snapshot store."""

from __future__ import annotations

import pathlib

import pytest

from soc2_compliance_team import context_snapshot
from soc2_compliance_team.models import RepoContext


def _ctx() -> RepoContext:
    return RepoContext(
        repo_path="/repo",
        code_summary="print('hi')",
        readme_content="# Title",
        file_list=["main.py"],
        tech_stack_hint="Python",
    )


def test_save_and_load_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    handle = context_snapshot.save_snapshot("job-1", _ctx())
    assert handle.endswith("job-1.json")
    assert str(tmp_path) in handle

    loaded = context_snapshot.load_snapshot("job-1")
    assert isinstance(loaded, RepoContext)
    assert loaded.code_summary == "print('hi')"
    assert loaded.file_list == ["main.py"]


def test_delete_removes_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    context_snapshot.save_snapshot("job-1", _ctx())
    assert context_snapshot.snapshot_path("job-1").exists()

    context_snapshot.delete_snapshot("job-1")
    assert not context_snapshot.snapshot_path("job-1").exists()


def test_delete_missing_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    # No snapshot saved — delete must not raise.
    context_snapshot.delete_snapshot("job-absent")


def test_delete_swallows_unlink_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    context_snapshot.save_snapshot("job-1", _ctx())

    def _boom(self, missing_ok=False):  # noqa: ANN001
        raise OSError("disk error")

    monkeypatch.setattr(pathlib.Path, "unlink", _boom)
    # Best-effort — an unlink error is logged, never propagated.
    context_snapshot.delete_snapshot("job-1")


def test_load_missing_raises(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        context_snapshot.load_snapshot("job-absent")


def test_snapshot_path_uses_agent_cache(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    p = context_snapshot.snapshot_path("job-x")
    assert str(tmp_path) in str(p)
    assert "soc2_compliance_team" in str(p)
    assert p.name == "job-x.json"


def test_snapshot_path_falls_back_to_tempdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    p = context_snapshot.snapshot_path("job-y")
    assert p.name == "job-y.json"


def test_snapshot_path_requires_job_id(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    with pytest.raises(AssertionError):
        context_snapshot.snapshot_path("")
