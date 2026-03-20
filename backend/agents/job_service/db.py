from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator

import psycopg
from psycopg.rows import dict_row

from job_service.settings import get_database_url

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    team TEXT NOT NULL,
    job_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    last_heartbeat_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    PRIMARY KEY (team, job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_heartbeat ON jobs (status, last_heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_jobs_team_status ON jobs (team, status);
"""


def ensure_schema() -> None:
    with psycopg.connect(get_database_url()) as conn:
        conn.execute(SCHEMA_SQL)
        conn.commit()


@contextmanager
def connection() -> Generator[psycopg.Connection, None, None]:
    conn = psycopg.connect(get_database_url(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
