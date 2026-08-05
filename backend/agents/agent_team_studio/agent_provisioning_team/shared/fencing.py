"""Compatibility re-export: fencing-token enforcement moved to ``shared.fencing``.

Strategy Lab's restart-generation fencing needed the identical
resource-mutating-store primitive outside this team, so the implementation
now lives in ``backend/shared/fencing.py``. This module keeps the original
import path (``agent_provisioning_team.shared.fencing``) working for
existing importers without any change on their part.
"""

from __future__ import annotations

from shared.fencing import StaleFencingTokenError, check_fencing_token

__all__ = ["StaleFencingTokenError", "check_fencing_token"]
