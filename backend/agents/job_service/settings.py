from __future__ import annotations

import os


def get_database_url() -> str:
    url = os.getenv("JOB_SERVICE_DATABASE_URL") or os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("JOB_SERVICE_DATABASE_URL or DATABASE_URL must be set for job_service")
    return url


def get_heartbeat_stale_seconds() -> float:
    raw = os.getenv("JOB_HEARTBEAT_STALE_SECONDS", "300").strip()
    try:
        return float(raw)
    except ValueError:
        return 300.0


def get_api_key() -> str | None:
    key = os.getenv("JOB_SERVICE_API_KEY", "").strip()
    return key or None


def get_stale_check_interval_seconds() -> float:
    raw = os.getenv("JOB_STALE_CHECK_INTERVAL_SECONDS", "30").strip()
    try:
        return float(raw)
    except ValueError:
        return 30.0
