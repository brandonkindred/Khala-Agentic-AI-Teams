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

# Add software_engineering_team and agents to path so imports resolve.
# software_engineering_team must come first so its modules take precedence over agents/.
_team_dir = Path(__file__).resolve().parent
_agents_dir = _team_dir.parent
for _d in (_team_dir, _agents_dir):
    while str(_d) in sys.path:
        sys.path.remove(str(_d))
sys.path.insert(0, str(_agents_dir))
sys.path.insert(0, str(_team_dir))


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
