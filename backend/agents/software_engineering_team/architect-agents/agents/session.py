"""Session management for Enterprise Architect orchestrator."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from strands.session.file_session_manager import FileSessionManager
from strands.session.s3_session_manager import S3SessionManager


def get_session_manager(session_id: str | None = None) -> Any | None:
    """Create a session manager for the orchestrator.

    Uses S3SessionManager when ARCHITECT_SESSION_BUCKET is set (production).
    Uses FileSessionManager when unset (local dev), storing in sessions/ directory.

    Args:
        session_id: Optional session ID. If None, a new UUID is generated for
            each engagement.

    Returns:
        SessionManager instance, or None if session persistence is disabled
        (ARCHITECT_SESSION_DISABLED=1).
    """
    if os.environ.get("ARCHITECT_SESSION_DISABLED") == "1":
        return None

    sid = session_id or str(uuid.uuid4())
    bucket = os.environ.get("ARCHITECT_SESSION_BUCKET")
    if bucket:
        return S3SessionManager(
            session_id=sid,
            bucket=bucket,
            prefix=os.environ.get("ARCHITECT_SESSION_PREFIX", "sessions/"),
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
    # Local dev: use FileSessionManager
    storage_dir = os.environ.get("ARCHITECT_SESSION_DIR")
    if not storage_dir:
        base = Path(__file__).resolve().parent.parent
        storage_dir = str(base / "sessions")
    elif not Path(storage_dir).is_absolute():
        base = Path(__file__).resolve().parent.parent
        storage_dir = str(base / storage_dir)
    return FileSessionManager(session_id=sid, storage_dir=storage_dir)
