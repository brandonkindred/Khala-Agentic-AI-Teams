"""Dispatch read-only repo-inspection tool calls, sandboxed to the job workspace."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

from software_engineering_team.shared.repo_utils import REPO_INSPECT_EXCLUDE_DIRS, int_env

from .context import RepoToolContext
from .definitions import REPO_INSPECT_TOOL_DEFINITIONS

logger = logging.getLogger(__name__)

# Directories never surfaced by inspection. Single-sourced with the passive repo-context scanner
# (coding_team._read_repo_context) so both views of the repo agree.
_INSPECT_EXCLUDE_DIRS: frozenset[str] = REPO_INSPECT_EXCLUDE_DIRS

# ``Path.glob`` follows symlinked directories when expanding ``**`` on Python < 3.13, so a symlink
# cycle in the workspace could make recursion run away. 3.13+ defaults to not recursing symlinks
# and exposes ``recurse_symlinks``; pass it where supported. (On older runtimes the per-entry
# containment filter in ``_list_files`` still drops any escaping result, and a cycle surfaces as a
# bounded OSError rather than unbounded work.)
_GLOB_KWARGS: dict[str, Any] = {"recurse_symlinks": False} if sys.version_info >= (3, 13) else {}

_READ_FILE_MAX_BYTES_ENV = "CODING_TEAM_READ_FILE_MAX_BYTES"
_READ_FILE_MAX_BYTES_DEFAULT = 65536


class _RepoPathError(ValueError):
    """Raised when a model-supplied path is absent, absolute, or escapes the workspace."""


def _strip_model_repo_path(args: dict[str, Any]) -> dict[str, Any]:
    """Ignore repo_path if the model sends it; execution always uses RepoToolContext."""
    return {k: v for k, v in args.items() if k != "repo_path"}


def _has_unsafe_parts(p: Path) -> bool:
    """True if a model-supplied relative path or glob is absolute or contains a ``..`` segment.

    Single source for the "must not point outside the workspace" predicate shared by path
    resolution and glob validation, so the two checks cannot drift.
    """
    return p.is_absolute() or ".." in p.parts


def _resolve_within_repo(repo: Path, rel: str) -> Path:
    """Resolve a model-supplied relative path and confirm it stays inside the workspace.

    Preconditions:
        - ``repo`` is an absolute, resolved directory path (RepoToolContext guarantees this).
        - ``rel`` is the raw path string from the tool arguments.
    Postconditions:
        - Returns the fully resolved path, guaranteed to equal ``repo`` or live under it.
        - Raises ``_RepoPathError`` for empty, absolute, ``..``-containing, or escaping paths
          (including symlink escapes, because resolution happens before the containment check).
    """
    rel_str = "" if rel is None else str(rel).strip()
    if not rel_str:
        raise _RepoPathError("empty path not allowed")
    p = Path(rel_str)
    if _has_unsafe_parts(p):
        reason = "absolute path not allowed" if p.is_absolute() else "path escapes repo"
        raise _RepoPathError(f"{reason}: {rel_str}")
    candidate = (repo / p).resolve()
    if not candidate.is_relative_to(repo):
        raise _RepoPathError(f"path escapes repo: {rel_str}")
    return candidate


def _is_excluded(rel_parts: tuple[str, ...]) -> bool:
    """True if any path segment names an excluded (build/VCS/cache) directory."""
    return any(part in _INSPECT_EXCLUDE_DIRS for part in rel_parts)


def _list_files(repo: Path, args: dict[str, Any]) -> dict[str, Any]:
    requested = str(args.get("path") or ".")
    base = _resolve_within_repo(repo, requested)
    if not base.exists():
        return {"success": False, "error": "not_found", "message": requested}
    if not base.is_dir():
        return {"success": False, "error": "not_a_directory", "message": requested}

    glob = args.get("glob")
    if glob:
        glob_str = str(glob)
        # Validate the glob like a path: the model must not glob outside the workspace.
        if _has_unsafe_parts(Path(glob_str)):
            raise _RepoPathError(f"glob escapes repo (absolute or '..'): {glob_str}")
        # A malformed pattern (e.g. ".") makes Path.glob raise; report it as a bad input
        # rather than letting it fall through to the opaque catch-all error.
        try:
            raw = sorted(base.glob(glob_str, **_GLOB_KWARGS))
        except (ValueError, IndexError, NotImplementedError) as e:
            raise _RepoPathError(f"invalid glob: {glob_str}") from e
    else:
        raw = sorted(base.iterdir())

    entries: List[dict[str, str]] = []
    for entry in raw:
        rel = entry.relative_to(repo)
        if _is_excluded(rel.parts):
            continue
        # Skip entries that escape the workspace via a symlink (read-only safety).
        if not entry.resolve().is_relative_to(repo):
            continue
        entries.append({"path": str(rel), "type": "dir" if entry.is_dir() else "file"})

    return {
        "success": True,
        "path": str(base.relative_to(repo)),
        "entries": entries,
        "count": len(entries),
    }


def _read_file(repo: Path, args: dict[str, Any]) -> dict[str, Any]:
    raw_path = args.get("path")
    # `path` is required for read_file (unlike list_files, which defaults to the root), so an
    # absent/blank value gets the distinct `missing_path` code rather than the `invalid_path`
    # that _resolve_within_repo would raise for the same empty string.
    if raw_path is None or not str(raw_path).strip():
        return {"success": False, "error": "missing_path", "message": "path is required"}
    target = _resolve_within_repo(repo, str(raw_path))
    if not target.exists():
        return {"success": False, "error": "not_found", "message": str(raw_path)}
    if not target.is_file():
        return {"success": False, "error": "not_a_file", "message": str(raw_path)}

    ceiling = int_env(_READ_FILE_MAX_BYTES_ENV, _READ_FILE_MAX_BYTES_DEFAULT, min_val=1)
    requested = args.get("max_bytes")
    effective = ceiling
    if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0:
        effective = min(requested, ceiling)

    size = target.stat().st_size
    if size > effective:
        return {
            "success": False,
            "error": "file_too_large",
            "size": size,
            "limit": effective,
            "message": (
                f"file is {size} bytes, over the {effective}-byte limit; read a more specific "
                f"path or raise {_READ_FILE_MAX_BYTES_ENV}"
            ),
        }

    content = target.read_bytes().decode("utf-8", errors="replace")
    return {
        "success": True,
        "path": str(target.relative_to(repo)),
        "bytes": size,
        "content": content,
    }


def execute_repo_tool(name: str, arguments: dict[str, Any], ctx: RepoToolContext) -> dict[str, Any]:
    """Run a single repo-inspection tool by OpenAI function name.

    Preconditions:
        - ``name`` is one of the names in ``REPO_INSPECT_TOOL_DEFINITIONS``.
        - ``ctx`` binds the workspace root the tool is confined to.
    Postconditions:
        - Returns a JSON-serializable dict; on any failure (bad path, missing target,
          oversize file, unknown tool) ``success`` is ``False`` with an ``error`` code.
          A model-supplied path can never read or list outside ``ctx.repo_path``.
    """
    args = _strip_model_repo_path(dict(arguments or {}))
    repo = ctx.repo_path
    try:
        if name == "list_files":
            return _list_files(repo, args)
        if name == "read_file":
            return _read_file(repo, args)
        return {"success": False, "error": "unknown_tool", "message": name}
    except _RepoPathError as e:
        return {"success": False, "error": "invalid_path", "message": str(e)}
    except Exception as e:
        logger.warning("execute_repo_tool %s failed: %s", name, e)
        return {"success": False, "error": "exception", "message": str(e)}


def build_repo_inspect_handlers(
    repo_path: str | Path,
) -> Dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    """Build the ``{tool_name: handler}`` map bound to one workspace.

    Preconditions:
        - ``repo_path`` is the job workspace root.
    Postconditions:
        - Returns exactly one handler per definition in ``REPO_INSPECT_TOOL_DEFINITIONS``;
          each handler takes the parsed-arguments dict and returns a JSON-serializable dict.
    """
    ctx = RepoToolContext(Path(repo_path))

    def _wrap(tool_name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def _run(args: dict[str, Any]) -> dict[str, Any]:
            return execute_repo_tool(tool_name, args, ctx)

        return _run

    return {
        fn["function"]["name"]: _wrap(fn["function"]["name"])
        for fn in REPO_INSPECT_TOOL_DEFINITIONS
    }
