"""
Pytest configuration and fixtures for software_engineering_team tests.

Run from software_engineering_team directory:
  cd software_engineering_team && pytest
"""

import os
import sys
from pathlib import Path

# Disable LLM retries so tests that hit an unavailable LLM fail fast.
os.environ.setdefault("LLM_MAX_RETRIES", "0")
# Disable the slow 429 rate-limit backoff so no test can ever sleep the 300s+
# schedule (this team overrides pytest's rootdir, hiding backend/conftest.py).
os.environ.setdefault("LLM_RATE_LIMIT_MAX_RETRIES", "0")

# Add software_engineering_team and agents to path so imports resolve.
# software_engineering_team must come first so its modules take precedence over agents/.
_team_dir = Path(__file__).resolve().parent
_agents_dir = _team_dir.parent
_backend_dir = _agents_dir.parent
for _d in (_team_dir, _agents_dir, _backend_dir):
    while str(_d) in sys.path:
        sys.path.remove(str(_d))
sys.path.insert(0, str(_agents_dir))
sys.path.insert(0, str(_team_dir))
# backend/ must win over software_engineering_team/ specifically for the
# top-level `shared` package name: software_engineering_team/shared/ is this
# team's own (differently-named-in-intent) compat shim for its own internal
# helpers (git_utils, repo_context_cache, ...), always addressed by callers
# via the fully-qualified `software_engineering_team.shared.*`. Bare
# `import shared` / `from shared.<pkg> import ...` — used throughout this
# team's own rewritten code and by cross-team modules like llm_service — must
# resolve to backend/shared/ instead, or it silently picks up the wrong
# same-named local package and dies on a circular import back into whichever
# module triggered the bare import.
sys.path.insert(0, str(_backend_dir))


def pytest_configure(config):
    """Configure logging so test runs show agent activity when -v or --log-cli-level is used."""
    import logging

    # Default: show INFO logs during tests when -v is passed
    if config.getoption("verbose", 0) > 0:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s %(name)s: %(message)s",
            force=True,
        )
