"""Public surface for ``shared.cache``.

Typical usage::

    from shared.cache import get_shared_cache

    cache = get_shared_cache("cr:chunk")
"""

from shared.cache.build_id import cache_build_id, with_cache_build_id
from shared.cache.factory import (
    close_shared_cache,
    get_shared_cache,
    override_shared_cache_backend,
    reset_shared_cache_state,
)
from shared.cache.interface import SharedCache
from shared.cache.memory import MemoryBackend
from shared.cache.redis_backend import RedisBackend

__all__ = [
    "MemoryBackend",
    "RedisBackend",
    "SharedCache",
    "cache_build_id",
    "close_shared_cache",
    "get_shared_cache",
    "override_shared_cache_backend",
    "reset_shared_cache_state",
    "with_cache_build_id",
]
