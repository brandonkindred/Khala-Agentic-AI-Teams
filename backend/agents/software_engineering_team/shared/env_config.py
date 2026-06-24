"""SE-local alias for the shared env-config readers.

The implementation now lives in the top-level :mod:`shared_env_config` package
so any team can use it; this module re-exports it to keep the existing
``software_engineering_team.shared.env_config`` import path stable.
"""

from __future__ import annotations

from shared_env_config import env_bool, env_float, env_int

__all__ = ["env_bool", "env_int", "env_float"]
