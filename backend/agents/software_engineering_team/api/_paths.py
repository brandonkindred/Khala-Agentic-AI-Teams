"""sys.path bootstrap for the SE api package.

Imported by ``api/__init__`` so that bare team-local imports (``spec_parser``,
``orchestrator``) resolve from any api sub-module regardless of import order.

Postconditions:
    - The team root and its ``architect-agents`` dir are on ``sys.path``.
Invariants:
    - Idempotent: inserting an entry already present is a no-op.
"""

import sys
from pathlib import Path

# The inserts run only in a fresh process where the team dir is not yet on the
# path; under pytest the test harness has already added it, so the guarded
# branches are not exercised there.
_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))  # pragma: no cover  # process-bootstrap only
_arch_dir = _team_dir / "architect-agents"
if _arch_dir.exists() and str(_arch_dir) not in sys.path:
    sys.path.insert(0, str(_arch_dir))  # pragma: no cover  # process-bootstrap only
