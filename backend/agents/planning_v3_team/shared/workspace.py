"""Resolve the Planning V3 output workspace from user input.

In Planning V3, ``repo_path`` is an OUTPUT directory — the workflow writes the
generated plan (context doc, PRD, handoff) into it and never reads source code
from it. Users may supply nothing, a git URL, or a client-side path that does
not exist on the backend; all of these must resolve to a writable server-side
directory rather than being rejected.

Invariants:
    - Every value returned by ``resolve_workspace`` is an existing, writable
      directory (created on demand) under either the caller-supplied path or
      ``AGENT_CACHE/planning_v3/``.
    - Directory names derived from user text (client name, git repo name) are
      single, sanitized path segments — they can never contain a path separator
      or ``..`` and therefore cannot escape the base root.

Trust boundary:
    When the caller supplies an explicit filesystem path it is used verbatim
    (``expanduser`` only), so the workspace can land anywhere the backend
    process can write. This is intentional — ``repo_path`` is a caller-chosen
    output folder, the same trust level as a plain ``mkdir`` on a supplied
    path, and the endpoint sits behind the authenticated security gateway.
    Empty and git-URL inputs are always confined under ``AGENT_CACHE`` via the
    sanitized segments above. Deployments that want to forbid arbitrary
    absolute paths should constrain the resolved path to an allowed root at
    the call site rather than weakening the sanitization here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import HTTPException

# Discriminates a git URL (git@host:..., ssh://..., https?://..., git://...)
# from a filesystem path such as ``/Users/...`` or ``C:\...`` (which have no
# leading URL scheme and therefore fall through to the filesystem branch).
_GIT_URL_RE = re.compile(
    r"^(?:git@[^:]+:|ssh://[^/]+/|https?://[^/]+/|git://[^/]+/)", re.IGNORECASE
)

# Any run of characters outside this safe set collapses to a single '-'.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

_SLUG_MAX_LEN = 80


def _slug(value: str | None, *, fallback: str = "session") -> str:
    """Sanitize arbitrary text into a single safe path segment.

    Preconditions:
        - ``value`` is None or arbitrary user text; ``fallback`` is a known-safe
          non-empty segment.
    Postconditions:
        - Returns a non-empty string containing only ``[A-Za-z0-9._-]``, at most
          ``_SLUG_MAX_LEN`` chars, with no path separators and never ``.`` or
          ``..`` (so it cannot traverse out of a parent directory). When the
          sanitized result would be empty or a traversal token, ``fallback`` is
          returned.
    """
    cleaned = _UNSAFE_RE.sub("-", (value or "").strip()).strip("-._")
    cleaned = cleaned[:_SLUG_MAX_LEN].strip("-._")
    if not cleaned or cleaned in (".", ".."):
        return fallback
    return cleaned


def _repo_name_from_git_url(url: str) -> str:
    """Return the sanitized repository name from a git URL.

    Preconditions:
        - ``url`` matches ``_GIT_URL_RE``.
    Postconditions:
        - Returns the final ``/``- or ``:``-delimited segment with any query
          string/fragment and a trailing ``.git`` removed, sanitized via
          ``_slug`` (fallback ``repo``).
    """
    # Drop any ``?query`` / ``#fragment`` before isolating the repo name so a
    # URL like ``https://host/org/repo.git?ref=main`` yields ``repo``.
    cleaned = url.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
    tail = re.split(r"[/:]", cleaned)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return _slug(tail, fallback="repo")


def _base_root() -> Path:
    """Return the server-side workspace root.

    Postconditions:
        - Returns ``<AGENT_CACHE or '.agent_cache'>/planning_v3`` as a ``Path``,
          read from the environment on every call (no caching).
    """
    return Path(os.environ.get("AGENT_CACHE", ".agent_cache")) / "planning_v3"


def resolve_workspace(
    repo_path: str | None,
    client_name: str | None,
    job_id: str,
) -> str:
    """Resolve a writable Planning V3 output directory from request input.

    Preconditions:
        - ``job_id`` is a non-empty, filesystem-safe identifier (the caller
          passes a server-generated UUID4 string).
        - ``repo_path`` is None, "", a git URL, or a filesystem path.
        - ``client_name`` is None or arbitrary user text.

    Postconditions:
        - Returns an absolute ``str`` path to a directory that exists and is
          writable (created here if absent) and is not a regular file.
        - Empty/None ``repo_path`` -> ``<root>/<slug(client_name)>/<job_id>``.
        - git-URL ``repo_path`` -> ``<root>/<repo_name>/<job_id>`` (a server
          workspace named after the repo; no clone is attempted).
        - Filesystem ``repo_path`` -> ``Path(repo_path).expanduser()`` used as
          given.
        - Raises ``HTTPException(400)`` only when the resolved path exists and is
          a regular file, or when the directory cannot be created.
    """
    base = _base_root()

    if not repo_path or not repo_path.strip():
        candidate = base / _slug(client_name) / job_id
    elif _GIT_URL_RE.match(repo_path.strip()):
        candidate = base / _repo_name_from_git_url(repo_path) / job_id
    else:
        candidate = Path(repo_path).expanduser()

    if candidate.exists() and not candidate.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Workspace path {candidate} exists but is not a directory (input: {repo_path!r})",
        )
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        # ValueError covers non-encodable inputs (e.g. an embedded NUL byte),
        # which os.mkdir raises rather than OSError; map both to a clean 400.
        raise HTTPException(
            status_code=400,
            detail=f"Could not create workspace for repo_path={repo_path!r}: {exc}",
        ) from exc
    return str(candidate.resolve())
