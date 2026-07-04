"""Leaf date helpers shared across the investment team (no intra-package imports).

Kept dependency-free (stdlib only) so both the low-level TradingView MCP client and the
higher-level ``market_data_service`` can import it *downward* without forming an import
cycle — the reason the epoch→calendar-day conversion lives here rather than on either
consumer.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

# Epoch magnitude at/above which a timestamp is milliseconds rather than seconds
# (13-digit ms ≈ year 2001+; 10-digit seconds stay well below this).
_EPOCH_MS_THRESHOLD = 1_000_000_000_000


def epoch_to_utc_date(epoch_seconds: float) -> Optional[str]:
    """Convert an epoch time in **seconds** to a ``YYYY-MM-DD`` UTC calendar day.

    Single source of truth for epoch→calendar-day conversion shared by the CoinGecko
    provider and the TradingView MCP client. UTC is explicit (not ``date.fromtimestamp``,
    which uses the process-local timezone) so ticks near midnight bucket onto the same day
    on every host / CI runner — the determinism the forward-fill relies on.

    Preconditions: ``epoch_seconds`` is a POSIX timestamp in seconds (callers convert
        milliseconds themselves).
    Postconditions: returns the UTC date as ``YYYY-MM-DD``, or ``None`` when the value is
        non-finite (NaN/Inf) or out of the representable range — so the caller drops the
        row rather than raising.
    """
    if not math.isfinite(epoch_seconds):
        return None
    try:
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None
