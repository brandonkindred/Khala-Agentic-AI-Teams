"""sys.path bootstrap for the coding_team api package.

Imported by ``api/__init__`` so ``backend/agents`` is importable from any api
sub-module regardless of import order.

Postconditions:
    - ``backend/agents`` (the repo's agents root) is on ``sys.path``.
Invariants:
    - Idempotent.
"""

import sys
from pathlib import Path

_agents_root = Path(__file__).resolve().parent.parent.parent
if str(_agents_root) not in sys.path:
    sys.path.insert(0, str(_agents_root))  # pragma: no cover  # process-bootstrap only
