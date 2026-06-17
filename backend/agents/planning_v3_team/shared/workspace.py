"""Resolve the Planning V3 output workspace from user input.

In Planning V3, ``repo_path`` is an OUTPUT directory — the workflow writes the
generated plan (context doc, PRD, handoff) into it and never reads source code
from it. Users may supply nothing, a git URL, or a client-side path that does
not exist on the backend; all of these resolve to a writable server-side
directory rather than being rejected.

Invariants:
    - Every value returned by ``resolve_workspace`` is an existing, writable
      directory created under ``AGENT_CACHE/planning_v3`` — never anywhere else.
    - Each path segment derived from user text (client name, git repo name, or
      the final component of a supplied filesystem path) is reduced to a single
      sanitized token (see ``_slug``) that contains no path separator and is
      never ``.`` or ``..``. Combined with the server-generated ``job_id`` leaf,
      the resolved workspace is always exactly two levels under the base root.

Confinement:
    Because every branch builds ``<root>/<safe-segment>/<job_id>``, a supplied
    path — absolute, relative, containing ``..``, or carrying non-encodable
    bytes — can never create or write outside the base root: only its final
    component survives, sanitized. The base root itself derives from
    ``AGENT_CACHE`` (default the relative ``.agent_cache``, resolved against the
    process's working directory; deployments set it to an absolute path such as
    ``/data/agents``), matching every other team's cache convention.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# The confinement checks use ``Path.is_relative_to`` (Python >= 3.9). The project
# targets Python 3.10+ (see CLAUDE.md), so this is always available; no fallback
# is needed.


class WorkspaceResolutionError(Exception):
    """Raised when the output workspace cannot be resolved or created.

    Framework-agnostic on purpose: the API layer translates this into an HTTP
    400, so this module never depends on FastAPI (or any web framework) and can
    be reused from a CLI, a worker, or a different framework.
    """


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


def _safe_segment_from_path(path: str) -> str:
    """Return a single confined segment from a supplied filesystem path.

    Preconditions:
        - ``path`` is a non-empty string that is not a git URL.
    Postconditions:
        - Returns ``_slug`` of the path's final ``/``- or ``\\``-delimited
          component (fallback ``workspace``). Only the basename survives, so an
          absolute path, a relative path, or one containing ``..`` collapses to
          a single safe segment and cannot escape the base root.
    """
    tail = re.split(r"[\\/]+", path.strip().rstrip("/\\"))[-1]
    return _slug(tail, fallback="workspace")


def _base_root() -> Path:
    """Return the server-side workspace root.

    Postconditions:
        - Returns ``<AGENT_CACHE or '.agent_cache'>/planning_v3`` as a ``Path``,
          read from the environment on every call (no caching). An unset, empty,
          or all-whitespace ``AGENT_CACHE`` falls back to ``.agent_cache`` (so a
          ``AGENT_CACHE=`` never collapses the root to a bare relative
          ``planning_v3``). A relative root is resolved against the process
          working directory.
    """
    cache_dir = os.environ.get("AGENT_CACHE", "").strip() or ".agent_cache"
    return Path(cache_dir) / "planning_v3"


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
          writable, always located under ``AGENT_CACHE/planning_v3`` (created
          here if absent).
        - Empty/None ``repo_path`` -> ``<root>/<slug(client_name)>/<job_id>``.
        - git-URL ``repo_path`` -> ``<root>/<repo_name>/<job_id>`` (named after
          the repo; no clone is attempted).
        - Filesystem ``repo_path`` -> ``<root>/<safe basename>/<job_id>``; the
          supplied path is confined to its sanitized final component and can
          never write outside the root.
        - Raises ``WorkspaceResolutionError`` for an unsafe ``job_id``, when the
          directory cannot be created (e.g. ``AGENT_CACHE`` points at a
          non-directory), or when the resolved path would escape the base root
          (a pre-existing or race-swapped symlink among the path components,
          checked both before and after ``mkdir``). The API layer maps this to
          HTTP 400.
    """
    # Precondition enforcement (DbC): job_id is server-generated and must be a
    # single safe segment. Enforced with an explicit raise (not ``assert``, which
    # ``python -O`` would strip) so a future caller passing a separator /
    # traversal token fails loud rather than escaping the leaf.
    if not job_id or job_id in (".", "..") or any(c in job_id for c in ("/", "\\", "\x00")):
        raise WorkspaceResolutionError(
            f"job_id must be a single safe path segment, got {job_id!r}"
        )

    base = _base_root()

    if not repo_path or not repo_path.strip():
        candidate = base / _slug(client_name) / job_id
    elif _GIT_URL_RE.match(repo_path.strip()):
        candidate = base / _repo_name_from_git_url(repo_path) / job_id
    else:
        candidate = base / _safe_segment_from_path(repo_path) / job_id

    # Defense-in-depth: ``resolve()`` follows symlinks, so a pre-existing symlink
    # at a path component (a deployment-level concern, not reachable through the
    # sanitized segments) could redirect outside the root. Reject anything that
    # does not land under the resolved base — checked before mkdir so an escaping
    # directory is never created.
    base_resolved = base.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base_resolved):
        raise WorkspaceResolutionError(
            f"Resolved workspace escapes the cache root for repo_path={repo_path!r}"
        )

    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        # OSError covers an unwritable root / a non-directory in the path;
        # ValueError covers non-encodable inputs. Map both to a clean 400.
        raise WorkspaceResolutionError(
            f"Could not create workspace for repo_path={repo_path!r}: {exc}"
        ) from exc

    # TOCTOU re-check: a symlink swapped into a path component between the check
    # above and this mkdir could have redirected the new directory outside the
    # root. Re-resolve the created path and reject if it escaped. (We deliberately
    # do not delete the stray directory — it may resolve to an attacker-chosen
    # location, so removal is left to the operator.)
    final = resolved.resolve()
    if not final.is_relative_to(base_resolved):
        raise WorkspaceResolutionError(
            f"Resolved workspace escaped the cache root after creation for repo_path={repo_path!r}"
        )
    return str(final)
