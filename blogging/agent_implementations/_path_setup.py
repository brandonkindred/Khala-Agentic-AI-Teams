"""
Add the blogging directory to sys.path so imports work when running scripts
from the project root (e.g. python blogging/agent_implementations/run_foo.py).
"""

import sys
from pathlib import Path

_blogging_dir = Path(__file__).resolve().parent.parent
if str(_blogging_dir) not in sys.path:
    sys.path.insert(0, str(_blogging_dir))
