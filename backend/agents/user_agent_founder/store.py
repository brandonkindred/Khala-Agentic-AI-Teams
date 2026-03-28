"""SQLite-backed store for user agent founder workflow runs and decisions."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'pending',
    se_job_id      TEXT,
    analysis_job_id TEXT,
    spec_content   TEXT,
    repo_path      TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    error          TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    question_id    TEXT NOT NULL,
    question_text  TEXT NOT NULL,
    answer_text    TEXT NOT NULL,
    rationale      TEXT NOT NULL DEFAULT '',
    timestamp      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id);
"""


@dataclass
class StoredRun:
    run_id: str
    status: str
    se_job_id: str | None
    analysis_job_id: str | None
    spec_content: str | None
    repo_path: str | None
    created_at: str
    updated_at: str
    error: str | None


@dataclass
class StoredDecision:
    decision_id: int
    run_id: str
    question_id: str
    question_text: str
    answer_text: str
    rationale: str
    timestamp: str


class FounderRunStore:
    """SQLite-backed store for founder agent workflow runs."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        if db_path is None:
            self._file_path: Optional[str] = None
            self._mem_conn: Optional[sqlite3.Connection] = sqlite3.connect(
                ":memory:", check_same_thread=False
            )
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.executescript(_SCHEMA)
            self._mem_conn.commit()
        else:
            self._file_path = str(db_path)
            self._mem_conn = None
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._init_file_schema()

    def _init_file_schema(self) -> None:
        conn = sqlite3.connect(self._file_path, timeout=15)  # type: ignore[arg-type]
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        if self._mem_conn is not None:
            with self._lock:
                self._mem_conn.row_factory = sqlite3.Row
                yield self._mem_conn
                self._mem_conn.commit()
        else:
            conn = sqlite3.connect(self._file_path, check_same_thread=False, timeout=15)  # type: ignore[arg-type]
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def create_run(self) -> str:
        run_id = str(uuid4())
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._db() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (run_id, "pending", now, now),
            )
        return run_id

    def get_run(self, run_id: str) -> Optional[StoredRun]:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            return StoredRun(
                run_id=row["run_id"],
                status=row["status"],
                se_job_id=row["se_job_id"],
                analysis_job_id=row["analysis_job_id"],
                spec_content=row["spec_content"],
                repo_path=row["repo_path"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                error=row["error"],
            )

    def update_run(self, run_id: str, **kwargs: Any) -> bool:
        if not kwargs:
            return False
        allowed = {"status", "se_job_id", "analysis_job_id", "spec_content", "repo_path", "error"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        fields["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [run_id]
        with self._db() as conn:
            result = conn.execute(f"UPDATE runs SET {set_clause} WHERE run_id = ?", values)
        return result.rowcount > 0

    def add_decision(
        self, run_id: str, question_id: str, question_text: str, answer_text: str, rationale: str
    ) -> int:
        ts = datetime.now(tz=timezone.utc).isoformat()
        with self._db() as conn:
            cursor = conn.execute(
                "INSERT INTO decisions (run_id, question_id, question_text, answer_text, rationale, timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, question_id, question_text, answer_text, rationale, ts),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def get_decisions(self, run_id: str) -> List[StoredDecision]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, run_id, question_id, question_text, answer_text, rationale, timestamp"
                " FROM decisions WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [
            StoredDecision(
                decision_id=r["id"],
                run_id=r["run_id"],
                question_id=r["question_id"],
                question_text=r["question_text"],
                answer_text=r["answer_text"],
                rationale=r["rationale"],
                timestamp=r["timestamp"],
            )
            for r in rows
        ]

    def list_runs(self) -> List[StoredRun]:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        return [
            StoredRun(
                run_id=r["run_id"],
                status=r["status"],
                se_job_id=r["se_job_id"],
                analysis_job_id=r["analysis_job_id"],
                spec_content=r["spec_content"],
                repo_path=r["repo_path"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                error=r["error"],
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_default_store: Optional[FounderRunStore] = None


def get_founder_store() -> FounderRunStore:
    global _default_store
    if _default_store is None:
        from user_agent_founder.db import get_db_path

        _default_store = FounderRunStore(db_path=get_db_path())
    return _default_store
