"""Simple in-memory result cache for Deepthought agent results.

Caches agent results keyed by a normalised hash of the focus question so
that identical or near-identical sub-questions across conversations (or
within the same tree) can be served from cache instead of re-running.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time

from deepthought.models import AgentResult

logger = logging.getLogger(__name__)

# Default TTL: 30 minutes.
DEFAULT_TTL_SECONDS = 1800


class _CacheEntry:
    __slots__ = ("result", "expires_at")

    def __init__(self, result: AgentResult, ttl: float) -> None:
        self.result = result
        self.expires_at = time.monotonic() + ttl


class ResultCache:
    """Thread-safe LRU-ish cache mapping question hashes to AgentResults."""

    def __init__(self, max_size: int = 256, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        self._max_size = max_size
        self._ttl = ttl

    @staticmethod
    def _key(question: str) -> str:
        return hashlib.sha256(question.strip().lower().encode()).hexdigest()[:24]

    def get(self, question: str) -> AgentResult | None:
        """Return cached result if present and not expired, else None."""
        key = self._key(question)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.monotonic() > entry.expires_at:
                del self._store[key]
                return None
            logger.info("Cache hit for question: %.80s", question)
            return entry.result

    def put(self, question: str, result: AgentResult) -> None:
        """Store a result.  Evicts oldest entries if at capacity."""
        key = self._key(question)
        with self._lock:
            if len(self._store) >= self._max_size:
                self._evict_expired_or_oldest()
            self._store[key] = _CacheEntry(result, self._ttl)

    def _evict_expired_or_oldest(self) -> None:
        """Remove expired entries; if still at capacity, drop the oldest."""
        now = time.monotonic()
        expired = [k for k, v in self._store.items() if now > v.expires_at]
        for k in expired:
            del self._store[k]
        if len(self._store) >= self._max_size:
            oldest_key = min(self._store, key=lambda k: self._store[k].expires_at)
            del self._store[oldest_key]

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
