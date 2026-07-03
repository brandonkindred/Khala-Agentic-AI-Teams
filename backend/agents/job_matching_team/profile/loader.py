"""Resolve and cache a :class:`JobSeekerProfile` for the matching pipeline.

Resolution order:
    1. The career section of the central user profile
       (``user_profiles.profile_json["career"]`` via
       :mod:`job_matching_team.profile.career_store`)
    2. ``$JOB_SEEKER_PROFILE_PATH``
    3. ``$AGENT_CACHE/job_seeker_profile.yaml``
    4. The bundled ``job_seeker_profile.example.yaml`` (with a WARN log line)

The YAML chain (2-4) is the offline/legacy fallback; edits made through the
API always land in the career section. Set ``JOB_SEEKER_PROFILE_STRICT=true``
to disable the example fallback (raises instead). Parsed YAML profiles are
cached by ``(resolved_path, mtime_ns)`` so repeated calls within a run do not
re-read or re-validate the file.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .model import JobSeekerProfile

logger = logging.getLogger(__name__)

EXAMPLE_PROFILE_PATH: Path = Path(__file__).resolve().parent / "job_seeker_profile.example.yaml"

_ENV_PATH = "JOB_SEEKER_PROFILE_PATH"
_ENV_STRICT = "JOB_SEEKER_PROFILE_STRICT"
_ENV_AGENT_CACHE = "AGENT_CACHE"
_DEFAULT_FILENAME = "job_seeker_profile.yaml"


def _strict_mode() -> bool:
    return os.environ.get(_ENV_STRICT, "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_path() -> Optional[Path]:
    """Return the first profile path that exists on disk, or None.

    Raises:
        FileNotFoundError: If ``JOB_SEEKER_PROFILE_PATH`` is set, missing, and
            strict mode is enabled.
    """
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
def _load_cached(path_str: str, mtime_ns: int) -> JobSeekerProfile:  # noqa: ARG001 — mtime is cache key
    return JobSeekerProfile.from_yaml_file(path_str)


def load_job_seeker_profile(path: Optional[Path | str] = None) -> JobSeekerProfile:
    """Load and return a :class:`JobSeekerProfile`.

    Args:
        path: Optional explicit path. When given, both the career-section and
            env-var resolution are skipped.

    Postconditions:
        * Returns a validated profile. The career section of the central user
          profile wins when present; otherwise the YAML chain applies. When no
          file is found and strict mode is off, the bundled example is used
          (with a WARN log line).

    Raises:
        FileNotFoundError: If an explicit ``path`` is missing, or if strict mode
            is set and no profile can be resolved.
    """
    if path is not None:
        resolved: Optional[Path] = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Job seeker profile not found: {resolved}")
    else:
        # Prefer the career section of the central user profile; ``None`` means
        # absent or operationally unavailable, either way the YAML chain applies.
        from .career_store import load_career_profile

        career = load_career_profile()
        if career is not None:
            return career
        resolved = _resolve_path()

    if resolved is None:
        if _strict_mode():
            raise FileNotFoundError(
                f"No job seeker profile found. Set {_ENV_PATH} or place "
                f"{_DEFAULT_FILENAME} under ${_ENV_AGENT_CACHE}."
            )
        logger.warning(
            "No job seeker profile configured; using bundled example at %s. Set %s to customize.",
            EXAMPLE_PROFILE_PATH,
            _ENV_PATH,
        )
        resolved = EXAMPLE_PROFILE_PATH

    mtime_ns = resolved.stat().st_mtime_ns
    return _load_cached(str(resolved), mtime_ns)


def clear_cache() -> None:
    """Clear the parsed-profile cache (useful in tests)."""
    _load_cached.cache_clear()
