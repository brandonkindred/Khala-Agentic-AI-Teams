"""Factory for process-wide shared caches (Redis when configured, else Memory).

Provides ``get_shared_cache(namespace)`` singletons with per-operation fail-open
semantics (Redis outages degrade to miss / local recompute, never raise into
callers) and ``single_flight`` compute deduplication across workers when Redis
is configured. Tests may install a process-wide backend via
``override_shared_cache_backend`` (clears the per-namespace map; callers that
need isolated keyspaces should prefix keys themselves or use distinct
namespaces on the real factory path) and tear down with
``reset_shared_cache_state`` / ``close_shared_cache`` (aliases — both reset
caches, the Redis client, and any test override).
"""

from __future__ import annotations

import ipaddress
import logging
import threading
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse

from shared.cache import config
from shared.cache.interface import SharedCache
from shared.cache.memory import MemoryBackend
from shared.cache.redis_backend import RedisBackend

logger = logging.getLogger(__name__)

# Process-wide singletons. Every read and write of ``_caches``, ``_redis_client``,
# and ``_override_backend`` must hold ``_caches_lock`` (except the best-effort
# ``client.close()`` after the client reference has been swapped out under the lock).
_caches_lock = threading.Lock()
_caches: Dict[str, SharedCache] = {}
_redis_client: Any | None = None
# Test seam: when set, ``get_shared_cache`` returns this backend for every
# namespace (typically a MemoryBackend or a RedisBackend over a fake client).
_override_backend: Optional[SharedCache] = None


def _redact_redis_url(url: str) -> str:
    """Return ``url`` with userinfo password replaced by ``***`` for logs.

    Postconditions:
        - URLs with no password (including ``redis://user@host``) are returned
          unchanged.
        - URLs with a password redact only the password segment to ``***``.
        - IPv6 hosts keep RFC 3986 brackets in the rebuilt netloc
          (``redis://user:***@[::1]:6379/0``).
    """
    try:
        parsed = urlparse(url)
    except Exception:  # pragma: no cover - defensive
        return "<unparseable-redis-url>"
    if parsed.password is None:
        return url
    host = parsed.hostname or ""
    # urlparse strips brackets from IPv6 hostnames; restore them so the
    # redacted URL remains a valid redis-py / RFC 3986 authority.
    if host and ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            pass
        else:
            host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    user = parsed.username or ""
    auth = f"{user}:***@" if user else "***@"
    return urlunparse(parsed._replace(netloc=f"{auth}{host}"))


def _build_redis_client() -> Any | None:
    """Construct a redis-py client, or ``None`` if unavailable.

    Postconditions:
        - Returns a client when ``redis`` is installed and ``config.redis_url()``
          is set. Does **not** ping at construction time — RedisBackend fail-opens
          per operation so a compose cold-start race does not permanently stick
          the process to the memory backend. ImportError returns ``None``.
        - Construction/config errors log at ``error`` (with redacted URL) and
          return ``None`` so callers still fail open to memory.
    """
    url: str | None
    try:
        url = config.redis_url()
    except ValueError as exc:
        logger.error("shared.cache: invalid Redis config (%s); using memory backend", exc)
        return None
    if not url:
        return None
    try:
        import redis  # noqa: PLC0415
    except ImportError:
        logger.warning("shared.cache: redis package not installed; using memory backend")
        return None
    try:
        return redis.Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=config.redis_socket_connect_timeout_s(),
        )
    except Exception:
        logger.error(
            "shared.cache: could not construct Redis client for %s; using memory backend",
            _redact_redis_url(url),
            exc_info=True,
        )
        return None


def _get_or_create_redis_client() -> Any | None:
    """Return the process-wide Redis client, constructing it once under the lock.

    Preconditions:
        - Caller already holds ``_caches_lock``.
    Postconditions:
        - Idempotent: repeated calls return the same client instance (or ``None``
          when construction is unavailable).
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = _build_redis_client()
    return _redis_client


def get_shared_cache(namespace: str) -> SharedCache:
    """Return the process-wide cache for ``namespace``.

    Preconditions:
        - ``namespace`` is a non-empty string (e.g. ``\"cr:chunk:v1\"``).
    Postconditions:
        - Idempotent per process and per namespace: repeated calls with the same
          ``namespace`` return the same backend instance.
        - When Redis is configured and the client can be constructed, returns a
          ``RedisBackend``; otherwise a ``MemoryBackend``. Connectivity is not
          probed at construction (compose cold-start race); per-op fail-open
          covers an unreachable Redis.
        - When a test override is installed via ``override_shared_cache_backend``,
          every namespace returns that override backend instead.
    Raises:
        ValueError: If ``namespace`` is empty.
    """
    if not namespace:
        raise ValueError("namespace must be non-empty")

    with _caches_lock:
        if _override_backend is not None:
            return _override_backend

        existing = _caches.get(namespace)
        if existing is not None:
            return existing

        backend: SharedCache
        if config.is_redis_configured():
            client = _get_or_create_redis_client()
            if client is not None:
                backend = RedisBackend(client, namespace)
            else:
                backend = MemoryBackend()
        else:
            backend = MemoryBackend()

        _caches[namespace] = backend
        return backend


def override_shared_cache_backend(backend: Optional[SharedCache]) -> None:
    """Install (or clear) a test-only backend used by every ``get_shared_cache``.

    Postconditions:
        - When ``backend`` is not ``None``, subsequent ``get_shared_cache``
          calls return it and the per-namespace cache dict is cleared.
          All namespaces share this single backend instance; callers that
          need isolated keyspaces should supply a backend that prefixes or
          partitions keys by namespace (the production ``RedisBackend`` /
          factory path does this automatically when no override is set).
        - When ``None``, restores normal factory behavior.
    """
    global _override_backend
    with _caches_lock:
        _override_backend = backend
        _caches.clear()


def reset_shared_cache_state() -> None:
    """Drop cached backends and the Redis client (tests / process teardown).

    Postconditions:
        - Next ``get_shared_cache`` rebuilds from env. Override is cleared.
        - Any previous Redis client is closed best-effort.
    """
    global _redis_client, _override_backend
    with _caches_lock:
        client = _redis_client
        _caches.clear()
        _redis_client = None
        _override_backend = None
    if client is not None:
        try:
            client.close()
        except Exception:  # pragma: no cover - best-effort close
            logger.debug("shared.cache: error closing Redis client on reset", exc_info=True)


def close_shared_cache() -> None:
    """Alias for :func:`reset_shared_cache_state`.

    Public teardown entry point. Named ``close_shared_cache`` for callers that
    think in "close the Redis client" terms, but the side effects match
    ``reset_shared_cache_state`` exactly: clears per-namespace backends, the
    process Redis client, *and* any test override. Prefer calling
    ``reset_shared_cache_state`` in new code; this alias exists for discoverability.
    """
    reset_shared_cache_state()
