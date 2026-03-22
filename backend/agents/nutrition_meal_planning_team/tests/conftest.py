"""Pytest config for nutrition_meal_planning_team. Ensures agents and backend are on path."""

import os
import sys
from pathlib import Path

_agents_dir = Path(__file__).resolve().parent.parent.parent
_backend_dir = _agents_dir.parent
for _d in (_backend_dir, _agents_dir):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

# Disable LLM retries so tests that hit an unavailable LLM fail fast and fall
# through to structural fallback paths rather than waiting minutes.
os.environ.setdefault("LLM_MAX_RETRIES", "0")
