"""Environment resolution for the shared Redis-backed cache layer.

Enablement is gated on ``REDIS_URL`` or ``REDIS_HOST`` (either is enough). When
neither is set the factory falls back to an in-process memory backend so local
dev and unit tests keep today's single-process LRU behavior without a Redis
dependency.

Env vars:

    REDIS_URL              Full Redis URL (e.g. ``redis://redis:6379/0``). Wins
                           over host/port/password when set.
    REDIS_HOST             Redis hostname (default unset → layer disabled unless
                           ``REDIS_URL`` is set). Bare hostname or bare IPv6
                           literal without a zone ID; do not embed a port (use
                           ``REDIS_PORT``) or scoped addresses (use ``REDIS_URL``).
    REDIS_PORT             Redis port (default ``6379``).
    REDIS_PASSWORD         Optional Redis password.
    REDIS_DB               Logical database index (default ``0``).
    REDIS_CACHE_TTL_S      TTL for cached value keys (default ``86400`` = 24h).
    REDIS_LOCK_TTL_S       TTL for single-flight leader locks (default ``3600``).
                           Sized for long code-review computes so the lock is
                           unlikely to expire mid-flight and allow duplicate
                           leaders.
    REDIS_WAITER_POLL_S    Waiter poll interval seconds (default ``0.05``).
    REDIS_WAITER_TIMEOUT_S Max seconds a waiter polls before recomputing
                           (default equals the *effective* ``REDIS_LOCK_TTL_S``
                           when unset, so waiters wait at least as long as a
                           customized lock TTL).
    REDIS_RESULT_TTL_S     TTL for short-lived single-flight result/error
                           markers published for waiters (default equals the
                           *effective* ``REDIS_LOCK_TTL_S`` when unset). The
                           Redis backend still caps this at 60s so abandoned
                           markers do not linger past leadership.
    REDIS_SOCKET_CONNECT_TIMEOUT_S
                           redis-py ``socket_connect_timeout`` seconds
                           (default ``1.0``).
    REDIS_KEY_PREFIX       Optional prefix for all Redis keys (default
                           ``khala``). A blank value falls back to the default
                           prefix. Must not contain ``:`` (the backend appends
                           ``:{namespace}:`` itself).
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import quote

from shared.env import parse_float, parse_int

_DEFAULT_PORT = 6379
_DEFAULT_DB = 0
_DEFAULT_CACHE_TTL_S = 86400
_DEFAULT_KEY_PREFIX = "khala"
# Lock TTL sized for long code-review computes. Waiter timeout defaults to the
# same value so a waiter outlives the lock and does not race into a duplicate
# compute while the original leader is still running.
_DEFAULT_LOCK_TTL_S = 3600
_DEFAULT_WAITER_POLL_S = 0.05
_DEFAULT_SOCKET_CONNECT_TIMEOUT_S = 1.0
_PORT_IN_HOST_ERROR = (
    "REDIS_HOST must not include a port; set REDIS_PORT separately "
    "or use REDIS_URL (got {host!r})"
)


def is_redis_configured() -> bool:
    """True when a Redis endpoint is configured for the shared cache.

    Postconditions:
        - Returns ``True`` iff ``REDIS_URL`` or ``REDIS_HOST`` is a non-blank
          string. Unset means the factory uses the in-process memory backend.
    """
    return bool(os.getenv("REDIS_URL", "").strip() or os.getenv("REDIS_HOST", "").strip())


def key_prefix() -> str:
    """Global Redis key prefix (default ``khala``). Blank env falls back to default.

    Postconditions:
        - Returns a non-empty string with no ``:`` (the backend appends
          ``:{namespace}:`` itself). Blank/whitespace ``REDIS_KEY_PREFIX``
          falls back to ``khala``.
        - Raises ``ValueError`` when the configured prefix contains ``:``.
    """
    raw = os.getenv("REDIS_KEY_PREFIX", _DEFAULT_KEY_PREFIX).strip()
    prefix = raw or _DEFAULT_KEY_PREFIX
    if ":" in prefix:
        raise ValueError(f"REDIS_KEY_PREFIX must not contain ':' (got {prefix!r})")
    return prefix


def redis_url() -> str | None:
    """Return a Redis URL when configured, else ``None``.

    Postconditions:
        - When ``REDIS_URL`` is set to a non-blank value, returns it stripped.
        - When ``REDIS_URL`` is blank/unset and ``REDIS_HOST`` is set, builds
          ``redis://[:password@]host:port/db``. Bare IPv6 hosts are bracketed
          (``redis://[::1]:6379/0``) so redis-py can parse them. Scoped /
          zone-ID addresses (e.g. ``fe80::1%eth0``) and other non-trivial
          forms must use ``REDIS_URL`` instead — ``REDIS_HOST`` is for a bare
          hostname or bare IPv6 literal without a zone ID, and must not embed
          a port (set ``REDIS_PORT`` separately).
        - When neither ``REDIS_URL`` nor ``REDIS_HOST`` is set (or both are
          blank/whitespace), returns ``None``.
        - Raises ``ValueError`` when ``REDIS_HOST`` embeds a port — either
          ``host:port`` or a bracketed ``[ipv6]:port`` form.
    """
    explicit = os.getenv("REDIS_URL", "").strip()
    if explicit:
        return explicit
    host = os.getenv("REDIS_HOST", "").strip()
    if not host:
        return None
    # Bracket bare IPv6 literals only (``ipaddress.IPv6Address``), never
    # ``host:port`` or other colon-bearing strings that are not IPv6.
    if host.startswith("["):
        closing = host.find("]")
        if closing == -1:
            raise ValueError(
                f"REDIS_HOST has unclosed bracket (got {host!r}); "
                "use a bare IPv6 literal, a properly bracketed IPv6 address, "
                "or REDIS_URL"
            )
        if ":" in host[closing + 1 :]:
            raise ValueError(_PORT_IN_HOST_ERROR.format(host=host))
        if "%" in host:
            raise ValueError(
                "REDIS_HOST must not include a zone ID; use REDIS_URL "
                f"(got {host!r})"
            )
        inner = host[1:closing]
        try:
            ipaddress.IPv6Address(inner)
        except ValueError as exc:
            raise ValueError(
                f"REDIS_HOST bracketed value is not a valid IPv6 address: {host!r}"
            ) from exc
    else:
        if "%" in host:
            raise ValueError(
                "REDIS_HOST must not include a zone ID; use REDIS_URL "
                f"(got {host!r})"
            )
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            if ":" in host:
                raise ValueError(_PORT_IN_HOST_ERROR.format(host=host)) from None
        else:
            host = f"[{host}]"
    port = parse_int("REDIS_PORT", _DEFAULT_PORT, minimum=1, maximum=65535)
    db = parse_int("REDIS_DB", _DEFAULT_DB, minimum=0)
    password = os.getenv("REDIS_PASSWORD") or ""
    auth = f":{quote(password, safe='')}@" if password else ""
    return f"redis://{auth}{host}:{port}/{db}"


def cache_ttl_s() -> int:
    """TTL (seconds) applied to Redis value keys. Floor 1."""
    return parse_int("REDIS_CACHE_TTL_S", _DEFAULT_CACHE_TTL_S, minimum=1)


def lock_ttl_s() -> int:
    """TTL (seconds) for single-flight leader locks. Floor 1."""
    return parse_int("REDIS_LOCK_TTL_S", _DEFAULT_LOCK_TTL_S, minimum=1)


def waiter_poll_s() -> float:
    """Seconds between waiter polls for a leader's result. Floor 0.01."""
    return parse_float("REDIS_WAITER_POLL_S", _DEFAULT_WAITER_POLL_S, minimum=0.01)


def waiter_timeout_s() -> float:
    """Max seconds a waiter polls before giving up and recomputing. Floor 1.

    When ``REDIS_WAITER_TIMEOUT_S`` is unset/blank, defaults to the *current*
    ``lock_ttl_s()`` so waiters wait at least as long as a customized lock TTL.

    The default tracking lock TTL (typically 3600s for long code-review
    computes) is intentional: capping the waiter below the lock TTL would make
    waiters recompute while the leader still holds the lock, defeating
    single-flight. Operators who need a shorter waiter bound must also shrink
    ``REDIS_LOCK_TTL_S`` (or set both knobs explicitly).
    """
    raw = os.getenv("REDIS_WAITER_TIMEOUT_S")
    if raw is None or not str(raw).strip():
        return float(lock_ttl_s())
    return parse_float("REDIS_WAITER_TIMEOUT_S", float(lock_ttl_s()), minimum=1.0)


def result_ttl_s() -> int:
    """TTL (seconds) for short-lived single-flight result/error markers. Floor 1.

    When ``REDIS_RESULT_TTL_S`` is unset/blank, defaults to the *current*
    ``lock_ttl_s()``. ``RedisBackend`` still applies its own hard cap so
    abandoned markers cannot outlive leadership by more than that ceiling.
    """
    raw = os.getenv("REDIS_RESULT_TTL_S")
    if raw is None or not str(raw).strip():
        return lock_ttl_s()
    return parse_int("REDIS_RESULT_TTL_S", lock_ttl_s(), minimum=1)


def redis_socket_connect_timeout_s() -> float:
    """redis-py socket connect timeout (seconds). Floor 0.1."""
    return parse_float(
        "REDIS_SOCKET_CONNECT_TIMEOUT_S",
        _DEFAULT_SOCKET_CONNECT_TIMEOUT_S,
        minimum=0.1,
    )
