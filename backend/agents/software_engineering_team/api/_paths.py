"""sys.path bootstrap for the SE api package.

Imported by ``api/__init__`` so that bare team-local imports (``spec_parser``,
``orchestrator``) resolve from any api sub-module regardless of import order.

Postconditions:
    - The team root and its ``architect-agents`` dir are on ``sys.path``.
Invariants:
    - Idempotent: inserting an entry already present is a no-op.
"""

from pathlib import Path

from shared_app import bootstrap_syspath

# ``backend/agents`` is already importable in every SE execution context (that is
# how this module and ``shared_app`` are reached), so delegating to the shared
# helper is safe here — unlike coding_team, whose ``_paths`` must bootstrap the
# agents root itself before ``shared_app`` can be imported.
_team_dir = Path(__file__).resolve().parent.parent
bootstrap_syspath(_team_dir, _team_dir / "architect-agents", must_exist=True)
