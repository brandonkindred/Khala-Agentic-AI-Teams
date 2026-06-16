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
    _safe_segment_from_path,
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


def test_explicit_path_confined_to_basename(cache):
    # An explicit filesystem path is reduced to its sanitized basename under
    # the root — it is never used verbatim.
    out = Path(resolve_workspace("/Users/brandon/Home-maintenance-tracker", "ignored", "jx"))
    assert out == (cache / "planning_v3" / "Home-maintenance-tracker" / "jx").resolve()
    assert out.is_dir()


def test_traversal_path_confined_under_root(cache):
    # '..' segments cannot escape: only the final component survives.
    root = (cache / "planning_v3").resolve()
    out = Path(resolve_workspace("../../outside", None, "jt"))
    assert out.is_relative_to(root)
    assert out == (root / "outside" / "jt").resolve()


def test_bare_dotdot_path_falls_back(cache):
    root = (cache / "planning_v3").resolve()
    out = Path(resolve_workspace("..", None, "jd"))
    assert out == (root / "workspace" / "jd").resolve()
    assert out.is_relative_to(root)


def test_unwritable_root_raises_400(tmp_path, monkeypatch):
    # AGENT_CACHE pointing at a regular file makes the root uncreatable.
    blocker = tmp_path / "cachefile"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("AGENT_CACHE", str(blocker))
    with pytest.raises(HTTPException) as exc:
        resolve_workspace("", None, "jm")
    assert exc.value.status_code == 400


def test_empty_agent_cache_env_falls_back_to_default(tmp_path, monkeypatch):
    # A set-but-empty/whitespace AGENT_CACHE must not collapse the root to a bare
    # relative 'planning_v3'; it falls back to the '.agent_cache' default. Run
    # from a temp cwd so the relative default materializes there, not the repo.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_CACHE", "")
    out = Path(resolve_workspace("", "Acme", "jE"))
    assert out == (tmp_path / ".agent_cache" / "planning_v3" / "Acme" / "jE").resolve()
    assert out.is_dir()


def test_existing_file_at_segment_path_raises_400(cache):
    # Acceptance criterion: when a regular file already occupies the spot where
    # the workspace would be created, the resolver returns a clean 400. Here the
    # user-derived segment ('collide') is a file, so mkdir(parents=True) of
    # <root>/collide/<job_id> hits a non-directory -> HTTPException(400).
    root = cache / "planning_v3"
    root.mkdir(parents=True, exist_ok=True)
    (root / "collide").write_text("x", encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        resolve_workspace("/some/client/collide", None, "jf")
    assert exc.value.status_code == 400


def test_symlink_segment_escaping_root_raises_400(cache):
    # Defense-in-depth: a pre-existing symlink at the user-derived segment that
    # points outside the root must be rejected (not followed), since resolve()
    # would otherwise escape AGENT_CACHE/planning_v3.
    base = cache / "planning_v3"
    base.mkdir(parents=True, exist_ok=True)
    outside = cache / "outside"
    outside.mkdir()
    (base / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException) as exc:
        resolve_workspace("escape", None, "js")
    assert exc.value.status_code == 400
    # The escaping directory must not have been created.
    assert not (outside / "js").exists()


def test_slug_rejects_traversal_and_separators():
    assert _slug("../../etc") not in ("..", ".", "")
    assert "/" not in _slug("a/b/c")
    assert _slug("..") == "session"
    assert _slug("") == "session"
    assert _slug(None, fallback="repo") == "repo"


def test_repo_name_strips_git_suffix():
    assert _repo_name_from_git_url("git@host:org/My.Repo.git") == "My.Repo"
    assert _repo_name_from_git_url("https://host/org/plain/") == "plain"


def test_safe_segment_from_path():
    assert _safe_segment_from_path("/a/b/c") == "c"
    assert _safe_segment_from_path("relrepo") == "relrepo"
    assert _safe_segment_from_path("/a/b/c/") == "c"
    assert _safe_segment_from_path("C:\\Users\\proj") == "proj"
    assert _safe_segment_from_path("..") == "workspace"
    assert _safe_segment_from_path("/") == "workspace"


def test_repo_name_strips_query_and_fragment():
    assert _repo_name_from_git_url("https://host/org/repo.git?ref=main") == "repo"
    assert _repo_name_from_git_url("https://host/org/repo#readme") == "repo"


def test_git_url_with_query_maps_to_clean_repo_name(cache):
    out = resolve_workspace("https://github.com/owner/repo.git?ref=main", None, "jq")
    assert Path(out) == (cache / "planning_v3" / "repo" / "jq").resolve()


def test_nul_byte_repo_path_sanitized_and_confined(cache):
    # The NUL byte is stripped by the slug, so the path is confined (not a 500).
    root = (cache / "planning_v3").resolve()
    out = Path(resolve_workspace("/tmp/a\x00b", None, "jn"))
    assert "\x00" not in str(out)
    assert out.is_relative_to(root)
    assert out.is_dir()
