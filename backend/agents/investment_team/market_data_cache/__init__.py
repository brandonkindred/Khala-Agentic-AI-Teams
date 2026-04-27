"""Investment team market-data cache (issue #376).

See :mod:`investment_team.market_data_cache.store` for the
``MarketDataCache`` implementation and
:mod:`investment_team.market_data_cache.streaming` for the provider-stream
wrapper.
"""

from __future__ import annotations

from .store import (
    FetchFn,
    MarketDataCache,
    SnapshotMeta,
    compute_dataset_fingerprint,
    get_default_cache,
    reset_default_cache,
)

__all__ = [
    "FetchFn",
    "MarketDataCache",
    "SnapshotMeta",
    "compute_dataset_fingerprint",
    "get_default_cache",
    "reset_default_cache",
]
