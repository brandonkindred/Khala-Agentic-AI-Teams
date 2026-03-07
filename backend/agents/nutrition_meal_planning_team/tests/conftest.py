"""Pytest config for nutrition_meal_planning_team. Ensures agents and backend are on path."""

import sys
from pathlib import Path

_agents_dir = Path(__file__).resolve().parent.parent.parent
_backend_dir = _agents_dir.parent
for _d in (_backend_dir, _agents_dir):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
