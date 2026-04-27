"""Filesystem-path resolution for the market-data cache.

Resolution order (mirrors ``backend/agents/blogging/shared/run_pipeline_job.py``):

1. ``INVESTMENT_MARKET_DATA_CACHE_ROOT`` — explicit operator override.
2. ``AGENT_CACHE`` — the canonical cross-team cache root; this module
   namespaces under ``investment_team/market_data``.
3. Tempdir fallback — ephemeral; logs a one-shot WARNING because the
   cache loses its reproducibility guarantee across restarts.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_TEMPDIR_FALLBACK_WARNED = False


def cache_root() -> Path:
    """Return the directory under which Parquet snapshots are written."""
    custom = os.environ.get("INVESTMENT_MARKET_DATA_CACHE_ROOT", "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    agent_cache = os.environ.get("AGENT_CACHE", "").strip()
    if agent_cache:
        return Path(agent_cache).expanduser().resolve() / "investment_team" / "market_data"

    fallback = Path(tempfile.gettempdir()) / "investment_market_data"
    global _TEMPDIR_FALLBACK_WARNED
    if not _TEMPDIR_FALLBACK_WARNED:
        logger.warning(
            "Neither INVESTMENT_MARKET_DATA_CACHE_ROOT nor AGENT_CACHE is set; "
            "market-data cache writing to %s, which is NOT persistent across "
            "process/container restarts. Backtests will lose reproducibility.",
            fallback,
        )
        _TEMPDIR_FALLBACK_WARNED = True
    return fallback


def snapshot_path(
    *,
    asset_class: str,
    symbol: str,
    frequency: str,
    provider: str,
    fetch_date: str,
) -> Path:
    """Build the canonical Parquet path for a snapshot.

    ``fetch_date`` is the UTC date the snapshot was first written
    (``YYYY-MM-DD``); a snapshot is immutable once written.  The path
    template is verbatim from issue #376.
    """
    return cache_root() / asset_class / symbol / frequency / provider / f"{fetch_date}.parquet"


__all__ = ["cache_root", "snapshot_path"]
