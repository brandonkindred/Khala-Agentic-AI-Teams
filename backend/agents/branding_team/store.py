"""In-memory store for clients and brands with versioning."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from .models import (
    Brand,
    BrandingMission,
    BrandStatus,
    BrandVersionSummary,
    Client,
    TeamOutput,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BrandingStore:
    """Thread-safe in-memory store for clients and brands."""

    def __init__(self) -> None:
        self._clients: dict[str, Client] = {}
        self._brands: dict[str, Brand] = {}
        self._client_to_brands: dict[str, list[str]] = {}  # client_id -> [brand_id, ...]
        self._lock = threading.Lock()

    def get_client(self, client_id: str) -> Optional[Client]:
        with self._lock:
            return self._clients.get(client_id)

    def list_clients(self) -> List[Client]:
        with self._lock:
            return list(self._clients.values())

    def create_client(self, name: str, contact_info: Optional[str] = None, notes: Optional[str] = None) -> Client:
        with self._lock:
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
            self._clients[client_id] = client
            self._client_to_brands[client_id] = []
            return client

    def get_brand(self, client_id: str, brand_id: str) -> Optional[Brand]:
        with self._lock:
            brand = self._brands.get(brand_id)
            if brand is None or brand.client_id != client_id:
                return None
            return brand

    def list_brands_for_client(self, client_id: str) -> List[Brand]:
        with self._lock:
            if client_id not in self._client_to_brands:
                return []
            brand_ids = self._client_to_brands[client_id]
            return [self._brands[bid] for bid in brand_ids if bid in self._brands]

    def create_brand(
        self,
        client_id: str,
        mission: BrandingMission,
        name: Optional[str] = None,
    ) -> Optional[Brand]:
        with self._lock:
            if client_id not in self._clients:
                return None
            brand_id = f"brand_{uuid4().hex[:12]}"
            now = _now()
            brand_name = name or mission.company_name
            brand = Brand(
                id=brand_id,
                client_id=client_id,
                name=brand_name,
                status=BrandStatus.draft,
                mission=mission,
                latest_output=None,
                version=0,
                history=[],
                created_at=now,
                updated_at=now,
            )
            self._brands[brand_id] = brand
            self._client_to_brands[client_id].append(brand_id)
            return brand

    def update_brand(
        self,
        client_id: str,
        brand_id: str,
        mission: Optional[BrandingMission] = None,
        status: Optional[BrandStatus] = None,
        name: Optional[str] = None,
    ) -> Optional[Brand]:
        with self._lock:
            brand = self._brands.get(brand_id)
            if brand is None or brand.client_id != client_id:
                return None
            updates: dict = {"updated_at": _now()}
            if mission is not None:
                updates["mission"] = mission
            if status is not None:
                updates["status"] = status
            if name is not None:
                updates["name"] = name
            updated = brand.model_copy(update=updates)
            self._brands[brand_id] = updated
            return updated

    def append_brand_version(
        self,
        client_id: str,
        brand_id: str,
        output: TeamOutput,
    ) -> Optional[Brand]:
        with self._lock:
            brand = self._brands.get(brand_id)
            if brand is None or brand.client_id != client_id:
                return None
            now = _now()
            new_version = brand.version + 1
            history_entry = BrandVersionSummary(
                version=new_version,
                created_at=now,
                status=output.status.value,
            )
            new_history = list(brand.history) + [history_entry]
            updated = brand.model_copy(
                update={
                    "latest_output": output,
                    "version": new_version,
                    "history": new_history,
                    "updated_at": now,
                }
            )
            self._brands[brand_id] = updated
            return updated


# Singleton for use in API and orchestrator
_default_store: Optional[BrandingStore] = None


def get_default_store() -> BrandingStore:
    global _default_store
    if _default_store is None:
        _default_store = BrandingStore()
    return _default_store
