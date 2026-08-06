"""Redis-backed shared cache with fail-open semantics and NX single-flight.

Values live under ``{prefix}{namespace}:{key}`` (default prefix ``khala:``;
override via ``REDIS_KEY_PREFIX``). A ZSET ``{prefix}{namespace}:__lru``
tracks last-access scores so ``max_entries`` can trim the oldest keys on
write. Single-flight uses a short-lived NX lock plus a result key that
waiters poll.
"""

from __future__ import annotations

import importlib
import json
import logging
import time
import uuid
from typing import Any, Callable, List, Optional, Tuple

from . import config as cache_config

logger = logging.getLogger(__name__)

_RESULT_SUFFIX = ":__sf_result"
_LOCK_SUFFIX = ":__sf_lock"
# Envelopes for the short-lived single-flight result key. Payload bytes are
# never stored bare so a legitimate compute() result cannot collide with an
# error marker (and vice versa). Legacy bare payloads (no envelope) are still
# accepted by waiters for rolling-upgrade compatibility.
_RESULT_PREFIX = b"\x00OK\x00"
_ERROR_PREFIX = b"\x00ERR\x00"
_LRU_ZSET = "__lru"
# Short-lived single-flight publish TTL. Caps how long waiters can observe a
# non-durable result/error marker; durable values use ``cache_ttl_s()``. Kept
# below typical lock TTL so abandoned markers do not linger after leadership
# ends, while still covering slow waiter poll loops.
_MAX_RESULT_TTL_S = 60
_CLEAR_DELETE_BATCH = 500
_TRIM_BATCH = 100
try:
    from redis.exceptions import RedisError as _RedisError
except ImportError:  # pragma: no cover - redis optional at import time
    _REDIS_OP_ERRORS: tuple[type[BaseException], ...] = (
        OSError,
        ConnectionError,
        TimeoutError,
    )
else:
    _REDIS_OP_ERRORS = (_RedisError, OSError, ConnectionError, TimeoutError)
# Compare-and-delete so a leader whose lock TTL expired cannot unlock a newer
# leader's token. Lock TTL must still exceed typical compute duration (see
# ``REDIS_LOCK_TTL_S``); ownership tokens do not renew leases.
_RELEASE_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end
"""
# Acquire the NX lock and clear any stale single-flight result in one atomic
# step so a waiter cannot observe a prior flight's result under the new lock.
_ACQUIRE_LOCK_LUA = """
local acquired = redis.call("set", KEYS[1], ARGV[1], "nx", "ex", tonumber(ARGV[2]))
if not acquired then
  return 0
end
redis.call("del", KEYS[2])
return 1
"""

# Modules whose exception classes may be reconstructed for waiters. Keeps Redis
# error markers from becoming an arbitrary import gadget if the store is
# writable by a third party. Prefixes are intentionally narrow (no blanket
# ``agents.``) and reconstruction is further limited to ``Exception`` subclasses
# (never ``SystemExit`` / ``KeyboardInterrupt`` / ``GeneratorExit``).
_ALLOWED_EXC_MODULE_PREFIXES = (
    "builtins",
    "shared.",
    "code_review_agent.",
    "llm_service.",
    "software_engineering_team.",
)


class _LeaderComputeError(RuntimeError):
    """Fallback when a leader error marker cannot be reconstructed."""


class RedisBackend:
    """Shared Redis cache for one namespace.

    Preconditions:
        - ``client`` is a redis-py client (or compatible fake) exposing
          ``get``/``set``/``delete``/``scan_iter``/``zadd``/``zcard``/
          ``zrange``/``zrem`` with ``nx``/``ex`` kwargs on ``set``.
    Invariants:
        - ``get`` / ``set`` / ``delete`` / ``clear`` catch Redis/client exceptions
          and degrade to miss / no-op — never raise into callers for outages.
        - ``single_flight`` fails open on Redis coordination failures (local
          recompute). Exceptions raised by ``compute`` still propagate to the
          leader. Waiters that observe a published error marker re-raise the
          reconstructed exception only when it is an ``Exception`` subclass
          from an allow-listed module; ``SystemExit`` / ``KeyboardInterrupt`` /
          ``GeneratorExit`` and corrupt or non-allow-listed markers fail open
          to a miss (recompute) rather than terminating the worker or raising
          ``_LeaderComputeError`` into the caller.
        - Successful ``get`` synchronously touches the LRU ZSET (``zadd``) so
          eviction order stays accurate under shared multi-worker load. That is
          an intentional write-on-read trade-off: approximate/async touches would
          under-protect hot keys during concurrent capacity pressure.
    """

    def __init__(
        self,
        client: Any,
        namespace: str,
        *,
        key_prefix: str | None = None,
        cache_ttl_s: int | None = None,
        lock_ttl_s: int | None = None,
        result_ttl_s: int | None = None,
        waiter_poll_s: float | None = None,
        waiter_timeout_s: float | None = None,
    ) -> None:
        if not namespace:
            raise ValueError("namespace must be non-empty")
        self._client = client
        self._namespace = namespace
        prefix_root = key_prefix if key_prefix is not None else cache_config.key_prefix()
        if not prefix_root or ":" in prefix_root:
            raise ValueError("key_prefix must be a non-empty string without ':'")
        self._prefix = f"{prefix_root}:{namespace}:"
        # Snapshot tunables at construction so backends can be tested / configured
        # independently of process-global env reads after init. TTL values are
        # cast with ``int()`` (Redis ``ex`` requires whole seconds; fractional
        # inputs truncate toward zero). Waiter timings use ``float()``.
        self._cache_ttl_s = int(cache_ttl_s if cache_ttl_s is not None else cache_config.cache_ttl_s())
        self._lock_ttl_s = int(lock_ttl_s if lock_ttl_s is not None else cache_config.lock_ttl_s())
        self._result_ttl_cfg_s = int(
            result_ttl_s if result_ttl_s is not None else cache_config.result_ttl_s()
        )
        self._waiter_poll_s = float(
            waiter_poll_s if waiter_poll_s is not None else cache_config.waiter_poll_s()
        )
        self._waiter_timeout_s = float(
            waiter_timeout_s if waiter_timeout_s is not None else cache_config.waiter_timeout_s()
        )
        if self._cache_ttl_s <= 0:
            raise ValueError("cache_ttl_s must be positive")
        if self._lock_ttl_s <= 0:
            raise ValueError("lock_ttl_s must be positive")
        if self._result_ttl_cfg_s <= 0:
            raise ValueError("result_ttl_s must be positive")
        if self._waiter_poll_s < 0:
            raise ValueError("waiter_poll_s must be non-negative")
        if self._waiter_timeout_s < 0:
            raise ValueError("waiter_timeout_s must be non-negative")

    def _value_key(self, key: str) -> str:
        self._require_logical_key(key)
        return f"{self._prefix}{key}"

    def _lock_key(self, key: str) -> str:
        self._require_logical_key(key)
        return f"{self._prefix}{key}{_LOCK_SUFFIX}"

    def _result_key(self, key: str) -> str:
        self._require_logical_key(key)
        return f"{self._prefix}{key}{_RESULT_SUFFIX}"

    def _lru_key(self) -> str:
        return f"{self._prefix}{_LRU_ZSET}"

    def _result_ttl_s(self) -> int:
        return min(_MAX_RESULT_TTL_S, self._result_ttl_cfg_s)

    @staticmethod
    def _require_logical_key(key: str) -> None:
        """Reject non-str / empty / colon-containing logical keys (namespace invariant).

        Preconditions:
            - Callers pass the logical key (not a fully-qualified Redis key).
        Postconditions:
            - Raises ``TypeError`` when ``key`` is not a ``str``.
            - Raises ``ValueError`` when ``key`` is empty or contains ``:``
              (empty would collide with the namespace prefix; ``:`` would break
              namespace ownership / clear scoping).
        """
        if not isinstance(key, str):
            raise TypeError("key must be str")
        if not key:
            raise ValueError("key must be non-empty")
        if ":" in key:
            raise ValueError("cache logical keys must not contain ':'")

    @staticmethod
    def _as_bytes(raw: Any) -> Optional[bytes]:
        """Convert a redis-py string response to bytes, preserving ``None``.

        Non-string responses (typically ``bytes`` under ``decode_responses=False``)
        are returned as-is; callers are expected to receive bytes-like values.
        """
        if raw is None:
            return None
        if isinstance(raw, str):
            return raw.encode("utf-8")
        return raw

    def _belongs_to_namespace(self, redis_key: str) -> bool:
        """True when ``redis_key`` is owned by this namespace (not a nested one).

        Logical keys must not contain ``:`` (enforced by ``_require_logical_key``
        on write/read paths); coordination suffixes (``:__sf_lock`` /
        ``:__sf_result``) and the ``__lru`` ZSET are allowed. Nested namespaces
        like ``foo:bar`` under prefix ``khala:foo:`` are excluded so ``clear()``
        cannot wipe another namespace's keys.
        """
        if not redis_key.startswith(self._prefix):
            return False
        rest = redis_key[len(self._prefix) :]
        if rest == _LRU_ZSET:
            return True
        if rest.endswith(_LOCK_SUFFIX):
            body = rest[: -len(_LOCK_SUFFIX)]
            return bool(body) and ":" not in body
        if rest.endswith(_RESULT_SUFFIX):
            body = rest[: -len(_RESULT_SUFFIX)]
            return bool(body) and ":" not in body
        return bool(rest) and ":" not in rest

    def _release_lock(self, lock_key: str, token: bytes) -> None:
        """Delete ``lock_key`` only when its value still equals ``token``.

        Best-effort / fail-open: Redis errors are logged and swallowed so unlock
        never raises into the leader path.
        """
        try:
            self._client.eval(_RELEASE_LOCK_LUA, 1, lock_key, token)
        except _REDIS_OP_ERRORS:  # pragma: no cover - best-effort unlock
            logger.debug("shared.cache redis lock release failed for %s", lock_key, exc_info=True)

    @staticmethod
    def _scan_key_as_str(redis_key: Any) -> str:
        """Decode a ``scan_iter`` key for ownership checks (bytes → str)."""
        if isinstance(redis_key, (bytes, bytearray)):
            return redis_key.decode("utf-8")
        return str(redis_key)

    def get(self, key: str) -> Optional[bytes]:
        """Return the cached value for ``key``, or ``None`` on miss / Redis failure.

        Preconditions:
            - ``key`` is a ``str`` with no ``:`` (enforced by ``_require_logical_key``).
        Postconditions:
            - Returns the stored bytes on hit; touches the LRU ZSET best-effort
              (write-on-read) only after a successful value hit so eviction order
              stays accurate under shared load.
            - Returns ``None`` on miss, Redis/client outages, or an unexpected
              value encoding failure from the client (fail-open). On a value
              miss, best-effort removes any stale LRU ZSET member left behind
              after the value key's TTL expired.
            - Still raises ``TypeError`` / ``ValueError`` for invalid keys
              (before any Redis call).
        """
        self._require_logical_key(key)
        try:
            try:
                raw = self._as_bytes(self._client.get(self._value_key(key)))
            except (UnicodeEncodeError, TypeError):
                logger.warning(
                    "shared.cache redis get got unusable value for %s; treating as miss",
                    key,
                    exc_info=True,
                )
                return None
            if raw is None:
                try:
                    self._client.zrem(self._lru_key(), key)
                except _REDIS_OP_ERRORS:  # pragma: no cover - best-effort stale cleanup
                    pass
                return None
            try:
                # Wall-clock scores: multi-worker Redis needs a clock comparable
                # across processes (``time.monotonic`` is per-process).
                self._client.zadd(self._lru_key(), {key: time.time()})
            except _REDIS_OP_ERRORS:  # pragma: no cover - best-effort touch
                pass
            return raw
        except _REDIS_OP_ERRORS:
            logger.warning("shared.cache redis get failed for %s", key, exc_info=True)
            return None

    def set(self, key: str, value: bytes, *, max_entries: int) -> None:
        """Store ``value`` under ``key`` and trim the namespace to ``max_entries``.

        Preconditions:
            - ``key`` is a ``str`` with no ``:``.
            - ``value`` is ``bytes``, ``bytearray``, or ``memoryview`` (copied to
              ``bytes`` immediately so a later buffer mutation cannot poison the
              store).
            - ``max_entries >= 0`` (``0`` is a no-op / disable write).
        Postconditions:
            - On success, the durable value and LRU member are written via a
              pipeline, then oldest entries past ``max_entries`` are trimmed.
            - Redis/client outages are logged and swallowed (fail-open).
            - Still raises on argument validation errors.
        """
        self._require_logical_key(key)
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("value must be bytes, bytearray, or memoryview")
        if max_entries < 0:
            raise ValueError("max_entries must be >= 0")
        if max_entries == 0:
            return
        value_bytes = bytes(value)
        set_idx = 0
        zadd_idx = 1
        try:
            pipe = self._client.pipeline()
            pipe.set(self._value_key(key), value_bytes, ex=self._cache_ttl_s)
            pipe.zadd(self._lru_key(), {key: time.time()})
            results = pipe.execute()
        except _REDIS_OP_ERRORS:
            logger.warning("shared.cache redis set failed for %s", key, exc_info=True)
            try:
                self._client.zrem(self._lru_key(), key)
            except _REDIS_OP_ERRORS:  # pragma: no cover - best-effort cleanup
                pass
            return

        value_ok = bool(results) and results[set_idx] is not False
        if not value_ok:
            try:
                self._client.zrem(self._lru_key(), key)
            except _REDIS_OP_ERRORS:  # pragma: no cover - best-effort cleanup
                pass
            return
        if len(results) <= zadd_idx or results[zadd_idx] is False:
            try:
                self._client.zadd(self._lru_key(), {key: time.time()})
            except _REDIS_OP_ERRORS:  # pragma: no cover - best-effort retry
                pass
        self._trim(max_entries)

    def delete(self, key: str) -> None:
        """Remove ``key``'s value, lock, result marker, and LRU member.

        Preconditions:
            - ``key`` is a ``str`` with no ``:``.
        Postconditions:
            - Best-effort delete of all coordination keys for ``key``. Redis
              outages are logged and swallowed (fail-open).
            - Still raises on argument validation errors.
        """
        self._require_logical_key(key)
        try:
            pipe = self._client.pipeline()
            pipe.delete(
                self._value_key(key),
                self._lock_key(key),
                self._result_key(key),
            )
            pipe.zrem(self._lru_key(), key)
            pipe.execute()
        except _REDIS_OP_ERRORS:
            logger.warning("shared.cache redis delete failed for %s", key, exc_info=True)

    def _trim(self, max_entries: int) -> None:
        """Evict oldest value keys past ``max_entries`` using the LRU ZSET.

        Evicts in bounded batches and re-checks ``zcard`` after each batch so a
        concurrent writer cannot leave the namespace far above capacity, and so a
        single ``zrange`` never pulls an unbounded key list. Continues while
        over capacity and each batch deletes at least one key; logs a warning
        if a round makes no progress. Does **not** delete ``__sf_lock`` /
        ``__sf_result`` keys — those have their own TTLs.
        """
        try:
            lru = self._lru_key()
            while True:
                overflow = int(self._client.zcard(lru)) - max_entries
                if overflow <= 0:
                    return
                batch_size = min(overflow, _TRIM_BATCH)
                oldest = self._client.zrange(lru, 0, batch_size - 1)
                if not oldest:
                    logger.warning(
                        "shared.cache redis trim made no progress for %s "
                        "(zcard overflow=%s but zrange empty)",
                        self._namespace,
                        overflow,
                    )
                    return
                keys = [k.decode("utf-8") if isinstance(k, bytes) else k for k in oldest]
                pipe = self._client.pipeline()
                for k in keys:
                    pipe.delete(self._value_key(k))
                pipe.zrem(lru, *keys)
                pipe.execute()
        except _REDIS_OP_ERRORS:  # pragma: no cover - defensive trim failure
            logger.debug("shared.cache redis trim failed", exc_info=True)

    def single_flight(
        self,
        key: str,
        compute: Callable[[], Tuple[bytes, bool]],
        *,
        max_entries: int,
    ) -> bytes:
        """Deduplicate concurrent compute for ``key`` across workers.

        Preconditions:
            - ``key`` is a ``str`` with no ``:``.
            - ``compute`` returns ``(payload_bytes, cacheable)``. When
              ``cacheable`` is False the short-lived waiter result is still
              published but no durable value is stored.
            - ``max_entries >= 0`` (``0`` skips coordination and always computes).
        Postconditions:
            - Returns the payload bytes (leader or waiter). Redis coordination
              outages fail open to a local compute. Leader ``compute``
              exceptions propagate to the leader and (when published) to waiters
              as reconstructed ``Exception`` subclasses.
            - Still raises on argument validation errors.
        """
        self._require_logical_key(key)
        if max_entries < 0:
            raise ValueError("max_entries must be >= 0")
        if max_entries == 0:
            payload, _cacheable = compute()
            return payload

        hit = self.get(key)
        if hit is not None:
            return hit

        lock_key = self._lock_key(key)
        result_key = self._result_key(key)
        got_lock = False
        lock_token = uuid.uuid4().hex.encode("ascii")
        try:
            got_lock = bool(
                self._client.eval(
                    _ACQUIRE_LOCK_LUA,
                    2,
                    lock_key,
                    result_key,
                    lock_token,
                    int(self._lock_ttl_s),
                )
            )
        except _REDIS_OP_ERRORS:
            logger.debug("shared.cache redis lock failed for %s", key, exc_info=True)
            # Fail-open: compute locally without coordinating.
            payload, cacheable = compute()
            if cacheable:
                self.set(key, payload, max_entries=max_entries)
            return payload

        if not got_lock:
            waited = self._wait_for_result(key, result_key)
            if waited is not None:
                return waited
            # Timeout / lock dropped with no publish: recompute (fail-open).
            payload, cacheable = compute()
            if cacheable:
                self.set(key, payload, max_entries=max_entries)
            return payload

        try:
            try:
                payload, cacheable = compute()
            except Exception as exc:
                self._publish_error(result_key, exc)
                raise
            try:
                # Short-lived waiter result first; durable write goes through
                # ``set()`` so LRU retry / trim / TTL stay consistent with direct sets.
                self._client.set(result_key, _RESULT_PREFIX + payload, ex=self._result_ttl_s())
                if cacheable:
                    self.set(key, payload, max_entries=max_entries)
            except _REDIS_OP_ERRORS:  # pragma: no cover - fail-open publish
                logger.debug("shared.cache redis publish failed for %s", key, exc_info=True)
            return payload
        finally:
            # Always unlock: Exception path published an error marker above;
            # BaseException (SystemExit / KeyboardInterrupt / GeneratorExit)
            # intentionally skips publishing so waiters fail open to recompute.
            self._release_lock(lock_key, lock_token)

    def _publish_error(self, result_key: str, exc: BaseException) -> None:
        """Publish a JSON error marker so waiters re-raise instead of recomputing.

        Uses JSON (module/name/args/message) rather than pickle so a compromised
        Redis cannot become an arbitrary code-execution gadget via deserialization.
        Non-JSON-safe exception args are stringified at publish time, so
        reconstructed ``exc.args`` may differ from the original (see
        ``_raise_published_error``). Waiters only reconstruct ``Exception``
        subclasses from allow-listed modules — ``SystemExit`` /
        ``KeyboardInterrupt`` markers fail open.
        """
        try:
            marker = _ERROR_PREFIX + json.dumps(
                {
                    "module": type(exc).__module__,
                    "name": type(exc).__name__,
                    "args": [a if isinstance(a, (str, int, float, bool, type(None))) else str(a) for a in exc.args],
                    "message": str(exc),
                },
                separators=(",", ":"),
            ).encode("utf-8")
            self._client.set(result_key, marker, ex=self._result_ttl_s())
        except _REDIS_OP_ERRORS:  # pragma: no cover - best-effort error publish
            logger.debug("shared.cache redis error publish failed", exc_info=True)

    @staticmethod
    def _raise_published_error(raw: bytes) -> None:
        """Re-raise a leader exception reconstructed from a published error marker.

        Preconditions:
            - ``raw`` starts with ``_ERROR_PREFIX`` followed by a JSON object.
        Postconditions:
            - Always raises: either the reconstructed ``Exception`` (when the
              module is allow-listed) or ``_LeaderComputeError`` as a fail-open
              fallback. Never raises ``SystemExit`` / ``KeyboardInterrupt`` /
              ``GeneratorExit`` from a marker (those fail open to
              ``_LeaderComputeError``).
            - Reconstructed ``exc.args`` are best-effort: non-JSON-safe original
              args are stringified at publish time, so callers must not rely on
              exact arg identity across the single-flight boundary — match on
              exception type / message instead.
        """
        try:
            meta = json.loads(raw[len(_ERROR_PREFIX) :].decode("utf-8"))
            module = str(meta["module"])
            name = str(meta["name"])
            args = list(meta.get("args") or [])
            message = str(meta.get("message") or name)
        except Exception as err:  # pragma: no cover - corrupt marker
            raise _LeaderComputeError("leader compute failed (unreadable error marker)") from err

        allowed = module == "builtins" or any(
            module == p.rstrip(".") or module.startswith(p) for p in _ALLOWED_EXC_MODULE_PREFIXES
        )
        if allowed:
            try:
                mod = importlib.import_module(module)
                cls = getattr(mod, name, None)
            except Exception:  # pragma: no cover - import failure
                cls = None
            # Exception only — never BaseException — so a poisoned marker cannot
            # terminate the worker via SystemExit / KeyboardInterrupt.
            if isinstance(cls, type) and issubclass(cls, Exception):
                ctor_args = args if args else [message]
                try:
                    raise cls(*ctor_args)
                except TypeError:
                    raise _LeaderComputeError(f"leader compute failed ({name}): {message}") from None

        raise _LeaderComputeError(f"leader compute failed ({name}): {message}")

    def _decode_published_result(self, raw: bytes) -> Optional[bytes]:
        """Decode a single-flight result-key blob into payload bytes.

        Postconditions:
            - Allow-listed ``Exception`` envelopes re-raise the reconstructed
              exception (never ``SystemExit`` / ``KeyboardInterrupt``).
            - Corrupt / non-allow-listed / non-``Exception`` error markers fail
              open to ``None`` so the waiter recomputes (``_LeaderComputeError``
              is not surfaced). A ``warning`` is logged so operators can see
              repeated leader failures that trigger expensive recomputes.
            - Result envelopes return the payload bytes after the prefix.
            - Bare (legacy) blobs are returned as-is.
        """
        if raw.startswith(_ERROR_PREFIX):
            try:
                self._raise_published_error(raw)
            except _LeaderComputeError as err:
                logger.warning(
                    "shared.cache redis leader error marker not reconstructable; "
                    "waiter will recompute (%s)",
                    err,
                )
                return None
            return None  # pragma: no cover - _raise_published_error always raises
        if raw.startswith(_RESULT_PREFIX):
            return raw[len(_RESULT_PREFIX) :]
        return raw

    def _wait_for_result(self, key: str, result_key: str) -> Optional[bytes]:
        """Poll until the leader publishes a durable value, result, or abandons.

        Postconditions:
            - Returns the leader payload bytes on success (durable value preferred;
              otherwise a short-lived result-marker payload).
            - Returns ``None`` (caller should recompute) when any of:
              the waiter deadline elapses; a Redis/client poll fails; the leader
              lock expires or is released without a durable value *or* result
              marker (crash / abandon / slow leader past lock TTL); or a
              non-reconstructable error marker is observed (see
              ``_decode_published_result``). Exit is bounded by
              ``waiter_timeout_s``: the sleep is truncated to the remaining
              deadline so the method does not overshoot by a full poll interval.
            - May raise a reconstructed leader ``Exception`` when an
              allow-listed error marker is observed (see
              ``_raise_published_error``).
            - Durable values are checked first and returned immediately. A
              result marker without a durable value is still returned (needed
              for ``cacheable=False`` publishes and the publish-before-durable
              window) but is intentionally **not** promoted to durable storage
              — waiters cannot tell whether the leader marked the payload
              cacheable.
        """
        deadline = time.monotonic() + self._waiter_timeout_s
        poll = self._waiter_poll_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None  # pragma: no cover - timeout path needs live clock control

            # Fetch durable first in its own try so a later sibling Redis failure
            # cannot discard a value the leader already published.
            try:
                durable = self._client.get(self._value_key(key))
            except _REDIS_OP_ERRORS:  # pragma: no cover - waiter poll fail-open
                logger.debug("shared.cache redis waiter poll failed for %s", key, exc_info=True)
                return None

            durable_b = self._as_bytes(durable)
            if durable_b is not None:
                try:
                    self._client.zadd(self._lru_key(), {key: time.time()})
                except _REDIS_OP_ERRORS:  # pragma: no cover - best-effort touch
                    pass
                return durable_b

            try:
                raw = self._client.get(result_key)
                lock_gone = not self._client.exists(self._lock_key(key))
            except _REDIS_OP_ERRORS:  # pragma: no cover - waiter poll fail-open
                logger.debug("shared.cache redis waiter poll failed for %s", key, exc_info=True)
                return None

            raw_b = self._as_bytes(raw)
            if raw_b is not None:
                return self._decode_published_result(raw_b)
            # Lock gone with no result yet: re-check once to close the race
            # where the leader cached + unlocked between our first reads.
            # Prefer durable; fall back to the result marker only when durable
            # is still absent (``cacheable=False`` or durable write failed).
            if lock_gone:
                try:
                    durable_b = self._as_bytes(self._client.get(self._value_key(key)))
                    if durable_b is not None:
                        try:
                            self._client.zadd(self._lru_key(), {key: time.time()})
                        except _REDIS_OP_ERRORS:  # pragma: no cover - best-effort touch
                            pass
                        return durable_b
                    raw_b = self._as_bytes(self._client.get(result_key))
                    if raw_b is not None:
                        return self._decode_published_result(raw_b)
                except _REDIS_OP_ERRORS:  # pragma: no cover - fail-open recheck
                    logger.debug(
                        "shared.cache redis waiter recheck failed for %s",
                        key,
                        exc_info=True,
                    )
                return None
            time.sleep(min(poll, remaining))

    def clear(self) -> Optional[int]:
        """Remove every key owned by this backend's namespace.

        Uses a cursor scan limited to the namespace prefix and deletes matching
        keys in batches (value keys, locks, result markers, and the LRU ZSET).
        Nested namespaces under a longer prefix are left untouched.

        Fail-open: any Redis/client error is logged at warning and swallowed so
        cache clearing never raises into callers.

        Postconditions:
            - Returns the number of Redis keys deleted on success.
            - Returns ``None`` when a Redis/client error aborts the clear
              (partial deletes may already have happened).
        """
        try:
            pattern = f"{self._prefix}*"
            batch: List[Any] = []
            deleted = 0
            for raw in self._client.scan_iter(match=pattern, count=_CLEAR_DELETE_BATCH):
                # decode_responses=False yields bytes; ownership checks need str,
                # but delete must use the original key object Redis returned.
                if not self._belongs_to_namespace(self._scan_key_as_str(raw)):
                    continue
                batch.append(raw)
                if len(batch) >= _CLEAR_DELETE_BATCH:
                    self._client.delete(*batch)
                    deleted += len(batch)
                    batch = []
            if batch:
                self._client.delete(*batch)
                deleted += len(batch)
            logger.info(
                "shared.cache redis clear removed %s keys for namespace %s",
                deleted,
                self._namespace,
            )
            return deleted
        except _REDIS_OP_ERRORS:  # pragma: no cover - fail-open clear
            logger.warning("shared.cache redis clear failed for %s", self._namespace, exc_info=True)
            return None
