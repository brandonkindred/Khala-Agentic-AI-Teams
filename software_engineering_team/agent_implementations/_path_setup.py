"""
Add the software_engineering_team directory to sys.path so imports work when
running scripts from the project root.
"""

import sys
from pathlib import Path

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))
