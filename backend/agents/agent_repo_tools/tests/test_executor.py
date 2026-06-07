"""Tests for the read-only repo-inspection tool executor.

Cover the contract from the tool spec: listing (with skip set + glob), reading a
file in full, path-traversal rejection (absolute / ``..`` / symlink escape), the
byte-cap error (never a silent truncation), and the env-tunable ceiling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_repo_tools import (
    REPO_INSPECT_TOOL_DEFINITIONS,
    RepoToolContext,
    build_repo_inspect_handlers,
    execute_repo_tool,
)
from agent_repo_tools import executor as executor_mod
from agent_repo_tools.executor import _RepoPathError, _resolve_within_repo

_ENV = "CODING_TEAM_READ_FILE_MAX_BYTES"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A populated workspace: nested source, a doc, an excluded cache dir, an excluded vendor dir."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "src" / "util.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# project\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.cpython-310.pyc").write_text("junk", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "left-pad.js").write_text("module.exports={}", encoding="utf-8")
    return tmp_path


def _ctx(repo: Path) -> RepoToolContext:
    return RepoToolContext(repo)


# --------------------------------------------------------------------------- list_files


def test_list_files_lists_immediate_entries_and_skips_excluded_dirs(repo: Path) -> None:
    out = execute_repo_tool("list_files", {"path": "."}, _ctx(repo))
    assert out["success"] is True
    assert out["path"] == "."
    names = {e["path"]: e["type"] for e in out["entries"]}
    assert names["src"] == "dir"
    assert names["README.md"] == "file"
    # Excluded directories never surface.
    assert "__pycache__" not in names
    assert "node_modules" not in names
    assert out["count"] == len(out["entries"])


def test_list_files_defaults_to_repo_root(repo: Path) -> None:
    out = execute_repo_tool("list_files", {}, _ctx(repo))
    assert out["success"] is True
    assert out["path"] == "."
    assert any(e["path"] == "src" for e in out["entries"])


def test_list_files_glob_recurses(repo: Path) -> None:
    out = execute_repo_tool("list_files", {"glob": "**/*.py"}, _ctx(repo))
    assert out["success"] is True
    paths = sorted(e["path"] for e in out["entries"])
    assert paths == [str(Path("src") / "app.py"), str(Path("src") / "util.py")]


def test_list_files_subdirectory(repo: Path) -> None:
    out = execute_repo_tool("list_files", {"path": "src"}, _ctx(repo))
    assert out["success"] is True
    assert out["path"] == "src"
    assert sorted(e["path"] for e in out["entries"]) == [
        str(Path("src") / "app.py"),
        str(Path("src") / "util.py"),
    ]


def test_list_files_missing_directory(repo: Path) -> None:
    out = execute_repo_tool("list_files", {"path": "nope"}, _ctx(repo))
    assert out == {"success": False, "error": "not_found", "message": "nope"}


def test_list_files_on_a_file_is_rejected(repo: Path) -> None:
    out = execute_repo_tool("list_files", {"path": "README.md"}, _ctx(repo))
    assert out["success"] is False
    assert out["error"] == "not_a_directory"


def test_list_files_skips_symlink_escaping_workspace(
    repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # The escape target lives in its own pytest-managed dir, a sibling of (not under) the workspace.
    outside = tmp_path_factory.mktemp("outside_dir")
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    (repo / "escape").symlink_to(outside, target_is_directory=True)

    out = execute_repo_tool("list_files", {"path": "."}, _ctx(repo))
    assert out["success"] is True
    assert "escape" not in {e["path"] for e in out["entries"]}


def test_list_files_rejects_absolute_glob(repo: Path) -> None:
    # The explicit guard (not Path.glob's version-dependent raise) is what must reject this.
    out = execute_repo_tool("list_files", {"glob": "/etc/*"}, _ctx(repo))
    assert out["success"] is False
    assert out["error"] == "invalid_path"


def test_list_files_rejects_parent_traversal_glob(repo: Path) -> None:
    # `../*` does not raise inside Path.glob, so only the explicit `..` guard rejects it.
    out = execute_repo_tool("list_files", {"glob": "../*"}, _ctx(repo))
    assert out["success"] is False
    assert out["error"] == "invalid_path"


def test_list_files_rejects_malformed_glob(repo: Path) -> None:
    # A pattern that survives the guard but makes Path.glob raise (".") is reported as a
    # bad input, not the opaque catch-all `exception`.
    out = execute_repo_tool("list_files", {"glob": "."}, _ctx(repo))
    assert out["success"] is False
    assert out["error"] == "invalid_path"


# --------------------------------------------------------------------------- read_file


def test_read_file_returns_full_contents(repo: Path) -> None:
    out = execute_repo_tool("read_file", {"path": "src/app.py"}, _ctx(repo))
    assert out["success"] is True
    assert out["content"] == "print('hi')\n"
    assert out["bytes"] == len("print('hi')\n")
    assert out["path"] == str(Path("src") / "app.py")


def test_read_file_missing_path(repo: Path) -> None:
    assert execute_repo_tool("read_file", {"path": "   "}, _ctx(repo))["error"] == "missing_path"
    assert execute_repo_tool("read_file", {}, _ctx(repo))["error"] == "missing_path"


def test_read_file_not_found(repo: Path) -> None:
    out = execute_repo_tool("read_file", {"path": "src/missing.py"}, _ctx(repo))
    assert out == {"success": False, "error": "not_found", "message": "src/missing.py"}


def test_read_file_on_directory_is_rejected(repo: Path) -> None:
    out = execute_repo_tool("read_file", {"path": "src"}, _ctx(repo))
    assert out["success"] is False
    assert out["error"] == "not_a_file"


def test_read_file_oversize_errors_without_content(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "10")
    (repo / "big.txt").write_text("x" * 50, encoding="utf-8")
    out = execute_repo_tool("read_file", {"path": "big.txt"}, _ctx(repo))
    assert out["success"] is False
    assert out["error"] == "file_too_large"
    assert out["size"] == 50
    assert out["limit"] == 10
    assert "content" not in out


def test_read_file_under_ceiling_succeeds(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "100")
    (repo / "small.txt").write_text("abc", encoding="utf-8")
    out = execute_repo_tool("read_file", {"path": "small.txt"}, _ctx(repo))
    assert out["success"] is True
    assert out["content"] == "abc"


def test_read_file_per_call_max_bytes_lowers_limit(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "100")
    (repo / "mid.txt").write_text("x" * 50, encoding="utf-8")
    out = execute_repo_tool("read_file", {"path": "mid.txt", "max_bytes": 10}, _ctx(repo))
    assert out["error"] == "file_too_large"
    assert out["limit"] == 10


def test_read_file_max_bytes_clamped_to_ceiling(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "10")
    (repo / "mid.txt").write_text("x" * 50, encoding="utf-8")
    out = execute_repo_tool("read_file", {"path": "mid.txt", "max_bytes": 1000}, _ctx(repo))
    assert out["error"] == "file_too_large"
    assert out["limit"] == 10  # ceiling wins over the larger per-call request


def test_read_file_boolean_max_bytes_is_ignored(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bool is an int subclass; it must not be treated as a byte budget.
    monkeypatch.setenv(_ENV, "100")
    (repo / "small.txt").write_text("abc", encoding="utf-8")
    out = execute_repo_tool("read_file", {"path": "small.txt", "max_bytes": True}, _ctx(repo))
    assert out["success"] is True
    assert out["content"] == "abc"


def test_read_file_garbage_max_bytes_is_ignored(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "100")
    (repo / "small.txt").write_text("abc", encoding="utf-8")
    out = execute_repo_tool("read_file", {"path": "small.txt", "max_bytes": "lots"}, _ctx(repo))
    assert out["success"] is True


def test_read_file_ceiling_floored_to_one(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "0")  # int_env floors to min_val=1
    (repo / "small.txt").write_text("abc", encoding="utf-8")
    out = execute_repo_tool("read_file", {"path": "small.txt"}, _ctx(repo))
    assert out["error"] == "file_too_large"
    assert out["limit"] == 1


# --------------------------------------------------------------------------- path safety


def test_read_file_rejects_absolute_path(repo: Path) -> None:
    out = execute_repo_tool("read_file", {"path": "/etc/passwd"}, _ctx(repo))
    assert out == {
        "success": False,
        "error": "invalid_path",
        "message": "absolute path not allowed: /etc/passwd",
    }


def test_read_file_rejects_parent_traversal(repo: Path) -> None:
    out = execute_repo_tool("read_file", {"path": "../outside.txt"}, _ctx(repo))
    assert out["success"] is False
    assert out["error"] == "invalid_path"


def test_read_file_rejects_symlink_escape(
    repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside_secret") / "secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    (repo / "link.txt").symlink_to(outside)
    out = execute_repo_tool("read_file", {"path": "link.txt"}, _ctx(repo))
    assert out["success"] is False
    assert out["error"] == "invalid_path"


def test_resolve_within_repo_rejects_empty(repo: Path) -> None:
    with pytest.raises(_RepoPathError):
        _resolve_within_repo(repo, "")
    with pytest.raises(_RepoPathError):
        _resolve_within_repo(repo, None)


def test_resolve_within_repo_accepts_root_and_nested(repo: Path) -> None:
    assert _resolve_within_repo(repo, ".") == repo
    assert _resolve_within_repo(repo, "src/app.py") == repo / "src" / "app.py"


# --------------------------------------------------------------------------- dispatch surface


def test_unknown_tool(repo: Path) -> None:
    out = execute_repo_tool("frobnicate", {}, _ctx(repo))
    assert out == {"success": False, "error": "unknown_tool", "message": "frobnicate"}


def test_model_supplied_repo_path_is_ignored(repo: Path) -> None:
    # A model trying to redirect execution by passing repo_path must be ignored.
    out = execute_repo_tool("list_files", {"path": ".", "repo_path": "/elsewhere"}, _ctx(repo))
    assert out["success"] is True


def test_unexpected_exception_is_caught(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(executor_mod, "_read_file", boom)
    out = execute_repo_tool("read_file", {"path": "src/app.py"}, _ctx(repo))
    assert out == {"success": False, "error": "exception", "message": "kaboom"}


def test_build_repo_inspect_handlers_exposes_both_tools(repo: Path) -> None:
    handlers = build_repo_inspect_handlers(repo)
    assert set(handlers) == {"list_files", "read_file"}
    # Each handler is bound to the workspace and dispatches.
    assert handlers["list_files"]({"path": "."})["success"] is True
    assert handlers["read_file"]({"path": "README.md"})["content"] == "# project\n"


def test_definitions_names_match_handlers(repo: Path) -> None:
    def_names = {d["function"]["name"] for d in REPO_INSPECT_TOOL_DEFINITIONS}
    assert def_names == set(build_repo_inspect_handlers(repo))
