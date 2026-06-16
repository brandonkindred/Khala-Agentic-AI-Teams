"""Unit tests for the Planning V3 workspace resolver.

These are job-service-free: they exercise resolve_workspace and its helpers,
asserting that any input (empty, git URL, client path) yields a writable
directory and that only a file-collision / mkdir failure raises 400.
"""

import sys
from pathlib import Path

import pytest

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from fastapi import HTTPException  # noqa: E402

from planning_v3_team.shared.workspace import (  # noqa: E402
    _repo_name_from_git_url,
    _slug,
    resolve_workspace,
)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    return tmp_path


def test_empty_repo_path_uses_client_slug_and_job_id(cache):
    out = resolve_workspace("", "Acme Corp", "job-123")
    p = Path(out)
    assert p.is_dir()
    assert p == (cache / "planning_v3" / "Acme-Corp" / "job-123").resolve()


def test_none_repo_path_and_none_client_falls_back_to_session(cache):
    out = resolve_workspace(None, None, "job-9")
    p = Path(out)
    assert p.is_dir()
    assert p == (cache / "planning_v3" / "session" / "job-9").resolve()


def test_git_ssh_url_maps_to_repo_name(cache):
    out = resolve_workspace(
        "git@github.com:brandonkindred/Home-maintenance-tracker.git", None, "j1"
    )
    p = Path(out)
    assert p.is_dir()
    assert p == (cache / "planning_v3" / "Home-maintenance-tracker" / "j1").resolve()


def test_git_https_url_without_suffix(cache):
    out = resolve_workspace("https://github.com/owner/repo", None, "j2")
    assert Path(out) == (cache / "planning_v3" / "repo" / "j2").resolve()


def test_client_local_path_used_as_is(cache, tmp_path):
    target = tmp_path / "sub" / "out"
    out = resolve_workspace(str(target), "ignored", "jx")
    assert Path(out) == target.resolve()
    assert Path(out).is_dir()


def test_existing_file_collision_raises_400(cache, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        resolve_workspace(str(f), None, "jf")
    assert exc.value.status_code == 400


def test_mkdir_failure_raises_400(cache, tmp_path):
    # A path whose parent is a file cannot be created as a directory.
    f = tmp_path / "blocker"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        resolve_workspace(str(f / "child"), None, "jm")
    assert exc.value.status_code == 400


def test_slug_rejects_traversal_and_separators():
    assert _slug("../../etc") not in ("..", ".", "")
    assert "/" not in _slug("a/b/c")
    assert _slug("..") == "session"
    assert _slug("") == "session"
    assert _slug(None, fallback="repo") == "repo"


def test_repo_name_strips_git_suffix():
    assert _repo_name_from_git_url("git@host:org/My.Repo.git") == "My.Repo"
    assert _repo_name_from_git_url("https://host/org/plain/") == "plain"


def test_repo_name_strips_query_and_fragment():
    assert _repo_name_from_git_url("https://host/org/repo.git?ref=main") == "repo"
    assert _repo_name_from_git_url("https://host/org/repo#readme") == "repo"


def test_git_url_with_query_maps_to_clean_repo_name(cache):
    out = resolve_workspace("https://github.com/owner/repo.git?ref=main", None, "jq")
    assert Path(out) == (cache / "planning_v3" / "repo" / "jq").resolve()


def test_nul_byte_repo_path_raises_400(cache):
    with pytest.raises(HTTPException) as exc:
        resolve_workspace("/tmp/a\x00b", None, "jn")
    assert exc.value.status_code == 400
