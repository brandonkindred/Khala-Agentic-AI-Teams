"""SQLite-backed store for clients and brands with versioning.

When instantiated with no arguments (``BrandingStore()``), uses an isolated
in-memory SQLite database — each instance gets its own fresh DB, which keeps
unit tests isolated without any special setup.

The production singleton (``get_default_store()``) passes a file path so all
worker processes share the same on-disk database via SQLite WAL mode.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional
from uuid import uuid4

from .models import (
    Brand,
    BrandingMission,
    BrandStatus,
    BrandVersionSummary,
    Client,
    TeamOutput,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id   TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brands (
    id        TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    data      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_brands_client ON brands(client_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BrandingStore:
    """SQLite-backed store for clients and brands."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        if db_path is None:
            # Per-instance in-memory DB — isolated for unit tests.
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

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    def get_client(self, client_id: str) -> Optional[Client]:
        with self._db() as conn:
            row = conn.execute("SELECT data FROM clients WHERE id = ?", (client_id,)).fetchone()
        if row is None:
            return None
        return Client.model_validate_json(row[0])

    def list_clients(self) -> List[Client]:
        with self._db() as conn:
            rows = conn.execute("SELECT data FROM clients").fetchall()
        return [Client.model_validate_json(r[0]) for r in rows]

    def create_client(
        self,
        name: str,
        contact_info: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Client:
        client_id = f"client_{uuid4().hex[:12]}"
        now = _now()
        client = Client(
            id=client_id,
            name=name,
            created_at=now,
            updated_at=now,
            contact_info=contact_info,
            notes=notes,
        )
        with self._db() as conn:
            conn.execute(
                "INSERT INTO clients (id, data) VALUES (?, ?)",
                (client_id, client.model_dump_json()),
            )
        return client

    # ------------------------------------------------------------------
    # Brands
    # ------------------------------------------------------------------

    def get_brand(self, client_id: str, brand_id: str) -> Optional[Brand]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT data FROM brands WHERE id = ? AND client_id = ?",
                (brand_id, client_id),
            ).fetchone()
        if row is None:
            return None
        return Brand.model_validate_json(row[0])

    def list_brands_for_client(self, client_id: str) -> List[Brand]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT data FROM brands WHERE client_id = ?", (client_id,)
            ).fetchall()
        return [Brand.model_validate_json(r[0]) for r in rows]

    def create_brand(
        self,
        client_id: str,
        mission: BrandingMission,
        name: Optional[str] = None,
    ) -> Optional[Brand]:
        with self._db() as conn:
            if conn.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone() is None:
                return None
            brand_id = f"brand_{uuid4().hex[:12]}"
            now = _now()
            brand = Brand(
                id=brand_id,
                client_id=client_id,
                name=name or mission.company_name,
                status=BrandStatus.draft,
                mission=mission,
                latest_output=None,
                version=0,
                history=[],
                created_at=now,
                updated_at=now,
            )
            conn.execute(
                "INSERT INTO brands (id, client_id, data) VALUES (?, ?, ?)",
                (brand_id, client_id, brand.model_dump_json()),
            )
        return brand

    def update_brand(
        self,
        client_id: str,
        brand_id: str,
        mission: Optional[BrandingMission] = None,
        status: Optional[BrandStatus] = None,
        name: Optional[str] = None,
    ) -> Optional[Brand]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT data FROM brands WHERE id = ? AND client_id = ?",
                (brand_id, client_id),
            ).fetchone()
            if row is None:
                return None
            brand = Brand.model_validate_json(row[0])
            updates: dict = {"updated_at": _now()}
            if mission is not None:
                updates["mission"] = mission
            if status is not None:
                updates["status"] = status
            if name is not None:
                updates["name"] = name
            updated = brand.model_copy(update=updates)
            conn.execute(
                "UPDATE brands SET data = ? WHERE id = ? AND client_id = ?",
                (updated.model_dump_json(), brand_id, client_id),
            )
        return updated

    def append_brand_version(
        self,
        client_id: str,
        brand_id: str,
        output: TeamOutput,
    ) -> Optional[Brand]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT data FROM brands WHERE id = ? AND client_id = ?",
                (brand_id, client_id),
            ).fetchone()
            if row is None:
                return None
            brand = Brand.model_validate_json(row[0])
            now = _now()
            new_version = brand.version + 1
            history_entry = BrandVersionSummary(
                version=new_version,
                created_at=now,
                status=output.status.value,
            )
            updated = brand.model_copy(
                update={
                    "latest_output": output,
                    "current_phase": output.current_phase,
                    "version": new_version,
                    "history": list(brand.history) + [history_entry],
                    "updated_at": now,
                }
            )
            conn.execute(
                "UPDATE brands SET data = ? WHERE id = ? AND client_id = ?",
                (updated.model_dump_json(), brand_id, client_id),
            )
        return updated


# ---------------------------------------------------------------------------
# Singleton — file-backed for production, shared across all worker processes.
# ---------------------------------------------------------------------------

_default_store: Optional[BrandingStore] = None


def get_default_store() -> BrandingStore:
    global _default_store
    if _default_store is None:
        from .db import get_db_path

        _default_store = BrandingStore(db_path=get_db_path())
    return _default_store
