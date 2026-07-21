"""
Add the blogging directory to sys.path so imports work when running scripts
from the project root (e.g. python blogging/agent_implementations/run_foo.py).

Also ensure ``backend/`` is on ``sys.path`` (and ahead of this directory) so
``import shared`` resolves to the platform package at ``backend/shared/``, not
``blogging/shared``.
"""

import sys
from pathlib import Path

_blogging_dir = Path(__file__).resolve().parent.parent
_backend_root = _blogging_dir.parent.parent  # agents/blogging → agents → backend
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))
if str(_blogging_dir) not in sys.path:
    sys.path.append(str(_blogging_dir))
