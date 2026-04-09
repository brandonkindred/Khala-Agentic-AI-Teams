"""Resolve and cache an :class:`AuthorProfile` for runtime prompt injection.

Resolution order:
    1. ``$AUTHOR_PROFILE_PATH``
    2. ``$AGENT_CACHE/author_profile.yaml``
    3. The bundled ``author_profile.example.yaml`` (with a WARN log line)

Set ``AUTHOR_PROFILE_STRICT=true`` to disable the example fallback (raises instead).

Parsed profiles are cached by ``(resolved_path, mtime_ns)`` so repeated calls inside
a single agent run do not re-read or re-validate the YAML.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .model import AuthorProfile

logger = logging.getLogger(__name__)

EXAMPLE_PROFILE_PATH: Path = Path(__file__).resolve().parent / "author_profile.example.yaml"

_ENV_PATH = "AUTHOR_PROFILE_PATH"
_ENV_STRICT = "AUTHOR_PROFILE_STRICT"
_ENV_AGENT_CACHE = "AGENT_CACHE"
_DEFAULT_FILENAME = "author_profile.yaml"


def _strict_mode() -> bool:
    return os.environ.get(_ENV_STRICT, "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_path() -> Optional[Path]:
    """Return the first profile path that exists on disk, or None."""
    env_path = os.environ.get(_ENV_PATH, "").strip()
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.is_file():
            return p
        if _strict_mode():
            raise FileNotFoundError(f"{_ENV_PATH}={p} does not exist and {_ENV_STRICT} is set.")
        logger.warning("%s set to %s but file is missing; falling back.", _ENV_PATH, p)

    cache_root = os.environ.get(_ENV_AGENT_CACHE, "").strip()
    if cache_root:
        p = (Path(cache_root).expanduser() / _DEFAULT_FILENAME).resolve()
        if p.is_file():
            return p

    return None


@lru_cache(maxsize=32)
def _load_cached(path_str: str, mtime_ns: int) -> AuthorProfile:  # noqa: ARG001 — mtime is cache key
    return AuthorProfile.from_yaml_file(path_str)


def load_author_profile(path: Optional[Path | str] = None) -> AuthorProfile:
    """Load and return an :class:`AuthorProfile`.

    Args:
        path: Optional explicit path. When given, env-var resolution is skipped.

    Raises:
        FileNotFoundError: If ``AUTHOR_PROFILE_STRICT`` is set and no profile is found.
    """
    if path is not None:
        resolved: Optional[Path] = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Author profile not found: {resolved}")
    else:
        resolved = _resolve_path()

    if resolved is None:
        if _strict_mode():
            raise FileNotFoundError(
                f"No author profile found. Set {_ENV_PATH} or place "
                f"{_DEFAULT_FILENAME} under ${_ENV_AGENT_CACHE}."
            )
        logger.warning(
            "No author profile configured; using bundled example at %s. Set %s to customize.",
            EXAMPLE_PROFILE_PATH,
            _ENV_PATH,
        )
        resolved = EXAMPLE_PROFILE_PATH

    mtime_ns = resolved.stat().st_mtime_ns
    return _load_cached(str(resolved), mtime_ns)


def clear_cache() -> None:
    """Clear the parsed-profile cache (useful in tests)."""
    _load_cached.cache_clear()
