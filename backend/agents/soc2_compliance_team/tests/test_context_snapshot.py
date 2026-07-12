"""Tests for the durable RepoContext snapshot store."""

from __future__ import annotations

import os
import pathlib
import stat
import time

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


# ---------------------------------------------------------------------------
# hardening: owner-only permissions + stale-snapshot purge
#
# The snapshot is a genuine at-rest copy of repository content (repo_loader
# deliberately includes .env-style files so a Security TSC audit can flag
# hardcoded secrets), so it must not be world/group readable, and an orphaned
# snapshot (worker killed before the normal completed/failed cleanup path ran)
# must not persist indefinitely on the shared cache volume.
# ---------------------------------------------------------------------------


def test_save_snapshot_restricts_file_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    context_snapshot.save_snapshot("job-1", _ctx())
    mode = stat.S_IMODE(context_snapshot.snapshot_path("job-1").stat().st_mode)
    assert mode == 0o600


def test_snapshot_dir_restricts_permissions(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    directory = context_snapshot._snapshot_dir()
    mode = stat.S_IMODE(directory.stat().st_mode)
    assert mode == 0o700


def test_save_snapshot_purges_stale_snapshots(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A snapshot older than the TTL (orphaned by a crashed worker) is purged
    the next time any job saves a snapshot."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    context_snapshot.save_snapshot("job-old", _ctx())
    stale_path = context_snapshot.snapshot_path("job-old")
    old_time = time.time() - context_snapshot._STALE_TTL_SECONDS - 60
    os.utime(stale_path, (old_time, old_time))

    context_snapshot.save_snapshot("job-new", _ctx())

    assert not stale_path.exists()
    assert context_snapshot.snapshot_path("job-new").exists()


def test_save_snapshot_keeps_fresh_snapshots(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A snapshot younger than the TTL survives another job's save (it may
    belong to a concurrent in-flight audit)."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    context_snapshot.save_snapshot("job-fresh", _ctx())

    context_snapshot.save_snapshot("job-new", _ctx())

    assert context_snapshot.snapshot_path("job-fresh").exists()


def test_purge_stale_snapshots_swallows_stat_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    context_snapshot.save_snapshot("job-1", _ctx())

    real_stat = pathlib.Path.stat

    def _boom(self, *args, **kwargs):  # noqa: ANN001
        # Only fail stat'ing a *snapshot entry* (glob's internal directory
        # traversal also calls Path.stat and must keep working).
        if self.suffix == ".json":
            raise OSError("stat failed")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", _boom)
    # Must not raise — cleanup is best-effort.
    context_snapshot._purge_stale_snapshots(context_snapshot.snapshot_path("job-1").parent)


def test_purge_stale_snapshots_swallows_glob_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    directory = context_snapshot._snapshot_dir()

    def _boom(self, pattern):  # noqa: ANN001
        raise OSError("glob failed")

    monkeypatch.setattr(pathlib.Path, "glob", _boom)
    # Must not raise — cleanup is best-effort.
    context_snapshot._purge_stale_snapshots(directory)


def test_snapshot_dir_chmod_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))

    def _boom(self, mode):  # noqa: ANN001
        raise OSError("chmod failed")

    monkeypatch.setattr(pathlib.Path, "chmod", _boom)
    # Must not raise — permission hardening is best-effort.
    directory = context_snapshot._snapshot_dir()
    assert directory.is_dir()


def test_save_snapshot_chmod_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    real_chmod = pathlib.Path.chmod

    def _boom(self, mode):  # noqa: ANN001
        # Let the directory's own chmod (inside _snapshot_dir) succeed; only
        # the snapshot *file*'s chmod fails.
        if self.suffix == ".json":
            raise OSError("chmod failed")
        return real_chmod(self, mode)

    monkeypatch.setattr(pathlib.Path, "chmod", _boom)
    # Must not raise — permission hardening is best-effort; the snapshot is
    # still written and readable.
    context_snapshot.save_snapshot("job-1", _ctx())
    assert context_snapshot.load_snapshot("job-1").repo_path == "/repo"
