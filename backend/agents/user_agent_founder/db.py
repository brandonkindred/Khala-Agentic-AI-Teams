"""Database path resolution for user agent founder persistent storage."""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_DIR = Path(__file__).resolve().parent / "data"


def get_db_path() -> str:
    """Return the SQLite database path, creating parent dirs as needed."""
    env = os.environ.get("USER_AGENT_FOUNDER_DB_PATH")
    if env:
        Path(env).parent.mkdir(parents=True, exist_ok=True)
        return env
    path = _DEFAULT_DIR / "user_agent_founder.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)
