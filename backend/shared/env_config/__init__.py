"""Shared typed, defensive environment-variable readers.

Re-exports :func:`env_bool`, :func:`env_int`, and :func:`env_float` (see
``config.py``) — the single implementation of the
``os.environ.get`` + parse + clamp idiom, usable by any team.
"""

from __future__ import annotations

from shared_env_config.config import env_bool, env_float, env_int

__all__ = ["env_bool", "env_int", "env_float"]
