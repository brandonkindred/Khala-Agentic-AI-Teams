"""Shared utilities for reading repository code and environment helpers.

Consolidates ``_read_repo_code``, ``_truncate_for_context``, and ``_int_env``
that were previously duplicated across backend_agent, orchestrator,
documentation_agent, and frontend_team modules.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# Directories excluded from repo scans (build artifacts, VCS, dependency caches)
REPO_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        ".angular",
    }
)

# Extra interpreter/venv caches excluded by repo-inspection views on top of
# REPO_EXCLUDE_DIRS, plus the combined set those views use. Single-sourced here so
# the active repo-inspection tools (agent_repo_tools) and the passive context
# scanner (coding_team._read_repo_context) cannot drift.
REPO_INSPECT_EXTRA_EXCLUDE_DIRS: frozenset[str] = frozenset({"__pycache__", "venv", ".venv"})
REPO_INSPECT_EXCLUDE_DIRS: frozenset[str] = REPO_EXCLUDE_DIRS | REPO_INSPECT_EXTRA_EXCLUDE_DIRS

# Default extensions per agent domain
BACKEND_EXTENSIONS: List[str] = [".py", ".java"]
FRONTEND_EXTENSIONS: List[str] = [".ts", ".tsx", ".html", ".scss"]
FULL_STACK_EXTENSIONS: List[str] = [
    ".py",
    ".ts",
    ".tsx",
    ".java",
    ".yml",
    ".yaml",
]
DOCUMENTATION_EXTENSIONS: List[str] = [
    ".py",
    ".ts",
    ".tsx",
    ".java",
    ".yml",
    ".yaml",
    ".html",
    ".scss",
]


def read_repo_code(
    repo_path: Path,
    extensions: Optional[List[str]] = None,
    *,
    exclude_dirs: Optional[frozenset[str]] = None,
) -> str:
    """Read source files from *repo_path*, concatenated with path headers.

    Parameters
    ----------
    repo_path:
        Root of the repository to scan.
    extensions:
        File suffixes to include (e.g. ``[".py", ".java"]``).
        Defaults to :data:`FULL_STACK_EXTENSIONS`.
    exclude_dirs:
        Directory names to skip.  Defaults to :data:`REPO_EXCLUDE_DIRS`.
        ``.git`` is *always* excluded regardless of this parameter.
    """
    if extensions is None:
        extensions = FULL_STACK_EXTENSIONS
    if exclude_dirs is None:
        exclude_dirs = REPO_EXCLUDE_DIRS

    always_exclude = exclude_dirs | {".git"}

    parts: List[str] = []
    for f in repo_path.rglob("*"):
        if always_exclude & set(f.parts):
            continue
        if f.is_file() and f.suffix in extensions:
            try:
                parts.append(
                    f"### {f.relative_to(repo_path)} ###\n"
                    f"{f.read_text(encoding='utf-8', errors='replace')}"
                )
            except (OSError, UnicodeDecodeError):
                pass
    return "\n\n".join(parts) if parts else "# No code files found"


# Filename components and suffixes that may hold credentials/secrets. Files
# matching these are excluded from the content sent to the external review model.
_SENSITIVE_NAMES: frozenset[str] = frozenset(
    {
        "credentials",
        "secrets",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".pgpass",
        ".htpasswd",
        ".envrc",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_SENSITIVE_SUFFIXES: frozenset[str] = frozenset(
    {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".asc", ".ppk"}
)

# Cap on bytes read per reviewed file: bounds memory (a multi-GB tracked artifact
# is never loaded whole) and the per-file prompt size. Files larger than this are
# read truncated to the cap.
MAX_REVIEW_FILE_BYTES = 1_000_000


def is_sensitive_path(path: str) -> bool:
    """True when *path* names a likely secret (``.env``/``.env.*``, key, ...).

    Best-effort denylist used to keep secrets out of the content forwarded to the
    external code-review model. The ``.env`` match is anchored (``.env`` exactly
    or an ``.env.<env>`` variant) so a regular source file like ``.environment.py``
    is *not* excluded, while ``.envrc`` (which commonly holds ``export SECRET=``)
    is covered explicitly. Over-inclusion (e.g. ``.env.example``) is acceptable —
    losing review of a template is preferable to leaking a key.
    """
    candidate = Path(path)
    name = candidate.name
    if name in _SENSITIVE_NAMES or name == ".env" or name.startswith(".env."):
        return True
    return candidate.suffix in _SENSITIVE_SUFFIXES


def strip_surrogates(text: str) -> str:
    """Return *text* with any lone surrogates escaped so it is UTF-8/JSON safe.

    Paths read from git via ``surrogateescape`` (for filenames whose bytes are
    invalid in the locale encoding) carry lone surrogates that raise
    ``UnicodeEncodeError`` when later serialized to JSON or encoded to UTF-8 (for
    example in an LLM HTTP request body). Literal backslashes are doubled first so
    they cannot be confused with the ``\\uXXXX`` escapes that ``backslashreplace``
    emits for invalid bytes; the result is therefore *injective* — distinct
    inputs (including a literal ``\\udcff`` filename vs. a 0xFF byte) map to
    distinct text — so two changed filenames never collide to one dict key. Plain
    ASCII text is unchanged.
    """
    return text.replace("\\", "\\\\").encode("utf-8", "backslashreplace").decode("utf-8")


def read_files_as_dict(
    repo_path: Path,
    paths: Iterable[str],
    extensions: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Read *paths* under *repo_path* into a ``{path: content}`` mapping.

    Parameters
    ----------
    repo_path:
        Root the paths are resolved against.
    paths:
        Repo-relative paths to read (e.g. the changed paths from
        ``list_changed_and_deleted``).
    extensions:
        When given, only paths whose suffix is in this list are included.
        ``None`` means no extension filter (so files without a code suffix,
        such as ``Dockerfile`` or ``requirements.txt``, pass through).

    Preconditions:
        - *paths* are repo-relative; the caller has already scoped them.
    Postconditions:
        - Returns a mapping in the iteration order of *paths*, skipping any path
          that is filtered out by *extensions*, escapes *repo_path* (an absolute
          path or one containing ``..``), is binary, or is missing/unreadable.
        - A symlink is represented by its link target text (``# symlink -> ...``)
          and never dereferenced, so the target's unrelated content is not
          mislabeled under the link path and a link pointing outside the repo
          cannot leak content.
        - At most :data:`MAX_REVIEW_FILE_BYTES` are read per file (bounding memory
          for huge artifacts); larger files are truncated to that prefix.
        - Text is decoded as UTF-8 with ``errors="replace"`` (matching
          ``read_repo_code``) so a legacy/non-UTF-8 text file is reviewed rather
          than dropped; binary content (a NUL byte in the read prefix) is omitted
          rather than decoded into gibberish.
        - Result keys (and the symlink-target marker) are run through
          :func:`strip_surrogates`, so a non-UTF-8 filename read via
          surrogateescape cannot crash downstream UTF-8/JSON serialization. The
          file content is still read from the original (surrogate-bearing) path.
        - Never reads a file outside *repo_path*: keys may come from untrusted
          agent output, so containment is enforced before any read.
    """
    repo_root = repo_path.resolve()
    result: Dict[str, str] = {}
    for rel_path in paths:
        candidate = Path(rel_path)
        if extensions is not None and candidate.suffix not in extensions:
            continue
        full_path = repo_root / candidate
        try:
            # Lexical containment first — no symlink following — so an absolute or
            # ``..`` key from untrusted agent output is rejected up front.
            lexical = Path(os.path.normpath(full_path))
            if lexical != repo_root and repo_root not in lexical.parents:
                continue
            # A symlink is reported by its target, never dereferenced (which would
            # mislabel the target's content under the link or escape the repo).
            if full_path.is_symlink():
                result[strip_surrogates(rel_path)] = strip_surrogates(
                    f"# symlink -> {os.readlink(full_path)}\n"
                )
                continue
            # Non-symlink: resolve (following any intra-repo parent links) and
            # re-check containment before reading.
            resolved = full_path.resolve()
            if repo_root not in resolved.parents:
                continue
            # Read at most a bounded prefix so a multi-GB tracked artifact cannot
            # exhaust memory before the NUL check; oversized files are truncated.
            with open(resolved, "rb") as handle:
                data = handle.read(MAX_REVIEW_FILE_BYTES)
            if b"\x00" in data:
                continue  # binary asset: omit rather than review as gibberish
            result[strip_surrogates(rel_path)] = data.decode("utf-8", errors="replace")
        except (OSError, RuntimeError, ValueError):
            continue
    return result


def truncate_for_context(
    text: str, max_chars: int, llm: object = None, content_description: str = "content"
) -> str:
    """Compact *text* with LLM when over budget; pass full text when no LLM available."""
    if not text or len(text) <= max_chars:
        return text or ""
    if llm is not None:
        from llm_service import compact_text

        return compact_text(text, max_chars, llm, content_description)
    return text


def int_env(name: str, default: int, min_val: int = 1) -> int:
    """Read an integer from environment variable *name*, clamped to *min_val*."""
    try:
        return max(min_val, int(os.environ.get(name) or str(default)))
    except ValueError:
        return default
