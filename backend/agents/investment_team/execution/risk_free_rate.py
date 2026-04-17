"""Risk-free rate resolution for performance metrics.

Resolution order:
1. ``STRATEGY_LAB_RISK_FREE_RATE`` env override (numeric fraction, e.g. ``0.04``).
2. FRED ``DGS3MO`` (3-month T-bill, constant maturity) when ``FRED_API_KEY`` is set.
3. ``RFR_DEFAULT`` hardcoded constant (0.04 = 4% annualized).

All values are returned as a decimal fraction (not percent).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

RFR_DEFAULT = 0.04


def _parse_env_rate() -> Optional[float]:
    raw = os.environ.get("STRATEGY_LAB_RISK_FREE_RATE", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric STRATEGY_LAB_RISK_FREE_RATE=%r", raw)
        return None


def _fetch_fred_dgs3mo(api_key: str, timeout: float = 10.0) -> Optional[float]:
    """Return the latest FRED ``DGS3MO`` observation as a decimal fraction."""
    try:
        import httpx
    except ImportError:
        return None
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": "DGS3MO",
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": "10",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("FRED DGS3MO fetch failed: %s", exc)
        return None

    for obs in data.get("observations", []):
        raw = obs.get("value", ".")
        if raw == "." or raw == "":
            continue
        try:
            return float(raw) / 100.0
        except ValueError:
            continue
    return None


def get_risk_free_rate(*, override: Optional[float] = None) -> float:
    """Resolve the annualized risk-free rate.

    ``override`` short-circuits everything (used by tests and per-run configs).
    """
    if override is not None:
        return float(override)

    env_rate = _parse_env_rate()
    if env_rate is not None:
        return env_rate

    fred_key = os.environ.get("FRED_API_KEY", "").strip()
    if fred_key:
        fred = _fetch_fred_dgs3mo(fred_key)
        if fred is not None:
            return fred

    return RFR_DEFAULT
