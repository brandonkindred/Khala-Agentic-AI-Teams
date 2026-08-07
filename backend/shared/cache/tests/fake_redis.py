"""Test helpers for ``shared.cache`` (FakeRedis and friends).

Not imported by production code — only by unit/integration tests.
"""

from __future__ import annotations

import fnmatch
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple


class _FakePipeline:
    def __init__(self, client: FakeRedis) -> None:
        self._client = client
        self._ops: List[Tuple[str, tuple, dict]] = []

    def set(self, *args: Any, **kwargs: Any) -> _FakePipeline:
        self._ops.append(("set", args, kwargs))
        return self

    def get(self, *args: Any, **kwargs: Any) -> _FakePipeline:
        self._ops.append(("get", args, kwargs))
        return self

    def zadd(self, *args: Any, **kwargs: Any) -> _FakePipeline:
        self._ops.append(("zadd", args, kwargs))
        return self

    def delete(self, *args: Any, **kwargs: Any) -> _FakePipeline:
        self._ops.append(("delete", args, kwargs))
        return self

    def zrem(self, *args: Any, **kwargs: Any) -> _FakePipeline:
        self._ops.append(("zrem", args, kwargs))
        return self

    def execute(self) -> list:
        ops, self._ops = self._ops, []
        results = []
        for name, args, kwargs in ops:
            results.append(getattr(self._client, name)(*args, **kwargs))
        return results


class FakeRedis:
    """Minimal redis-py stand-in shared by multiple RedisBackend instances.

    Honors ``ex`` TTLs via ``_now`` (defaults to ``time.time``; tests may
    replace it with a controllable clock).
    """

    def __init__(self) -> None:
        self._kv: Dict[str, bytes] = {}
        self._expiry: Dict[str, float] = {}
        self._zsets: Dict[str, Dict[str, float]] = {}
        self.fail_ops: set[str] = set()
        self._now = time.time
        self.close_calls = 0

    def _purge_if_expired(self, key: str) -> bool:
        exp = self._expiry.get(key)
        if exp is not None and self._now() >= exp:
            self._kv.pop(key, None)
            self._expiry.pop(key, None)
            return True
        return False

    def pipeline(self) -> _FakePipeline:
        if "pipeline" in self.fail_ops:
            raise ConnectionError("pipeline down")
        return _FakePipeline(self)

    def get(self, key: str) -> Optional[bytes]:
        if "get" in self.fail_ops:
            raise ConnectionError("get down")
        if self._purge_if_expired(key):
            return None
        return self._kv.get(key)

    def set(
        self,
        key: str,
        value: bytes | str,
        ex: Optional[int] = None,
        nx: bool = False,
        **_kwargs: Any,
    ) -> Optional[bool]:
        if "set" in self.fail_ops:
            raise ConnectionError("set down")
        self._purge_if_expired(key)
        if nx and key in self._kv:
            return None
        if isinstance(value, str):
            value = value.encode("utf-8")
        self._kv[key] = value
        if ex is not None:
            self._expiry[key] = self._now() + float(ex)
        else:
            self._expiry.pop(key, None)
        return True

    def set_raw(self, key: str, value: bytes | str) -> None:
        """Store ``value`` as-is (no str→bytes coercion). Test-only helper."""
        self._kv[key] = value  # type: ignore[assignment]
        self._expiry.pop(key, None)

    def delete(self, *keys: str) -> int:
        if "delete" in self.fail_ops:
            raise ConnectionError("delete down")
        deleted: set[str] = set()
        for key in keys:
            if isinstance(key, (bytes, bytearray)):
                key = key.decode("utf-8")
            if key in self._kv or key in self._expiry:
                self._kv.pop(key, None)
                self._expiry.pop(key, None)
                deleted.add(key)
            if key in self._zsets:
                del self._zsets[key]
                deleted.add(key)
        return len(deleted)

    def exists(self, key: str) -> int:
        if "exists" in self.fail_ops:
            raise ConnectionError("exists down")
        if isinstance(key, (bytes, bytearray)):
            key = key.decode("utf-8")
        if self._purge_if_expired(key):
            return 0
        return 1 if key in self._kv or key in self._zsets else 0

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        """Minimal Redis EVAL stand-in for lock acquire/release (not Python eval)."""
        if "eval" in self.fail_ops:
            raise ConnectionError("eval down")
        script_l = script.lower()
        # Acquire: SET NX + DEL result (two keys).
        if "nx" in script_l and numkeys >= 2:
            lock_key = keys_and_args[0]
            result_key = keys_and_args[1]
            token = keys_and_args[2]
            ttl = int(keys_and_args[3])
            if isinstance(lock_key, (bytes, bytearray)):
                lock_key = lock_key.decode("utf-8")
            if isinstance(result_key, (bytes, bytearray)):
                result_key = result_key.decode("utf-8")
            acquired = self.set(lock_key, token, nx=True, ex=ttl)
            if not acquired:
                return 0
            self.delete(result_key)
            return 1
        # Release: compare-and-delete.
        _ = numkeys
        key = keys_and_args[0]
        token = keys_and_args[1]
        if isinstance(key, (bytes, bytearray)):
            key = key.decode("utf-8")
        if isinstance(token, str):
            token = token.encode("utf-8")
        current = self.get(key)
        if current == token:
            return self.delete(key)
        return 0

    def zadd(self, name: str, mapping: Dict[str, float]) -> int:
        if "zadd" in self.fail_ops:
            raise ConnectionError("zadd down")
        z = self._zsets.setdefault(name, {})
        added = 0
        for k, score in mapping.items():
            if k not in z:
                added += 1
            z[k] = float(score)
        return added

    def zcard(self, name: str) -> int:
        if "zcard" in self.fail_ops:
            raise ConnectionError("zcard down")
        return len(self._zsets.get(name, {}))

    def zrange(self, name: str, start: int, end: int) -> List[str]:
        if "zrange" in self.fail_ops:
            raise ConnectionError("zrange down")
        items = sorted(self._zsets.get(name, {}).items(), key=lambda kv: (kv[1], kv[0]))
        n = len(items)
        if start < 0:
            start = max(0, n + start)
        if end < 0:
            end = max(-1, n + end)
        if end < start or start >= n:
            return []
        return [k for k, _ in items[start : end + 1]]

    def zrem(self, name: str, *keys: str) -> int:
        if "zrem" in self.fail_ops:
            raise ConnectionError("zrem down")
        z = self._zsets.get(name, {})
        n = 0
        for k in keys:
            if k in z:
                del z[k]
                n += 1
        return n

    def scan_iter(self, match: str = "*", count: int = 10) -> Iterable[str | bytes]:
        if "scan_iter" in self.fail_ops:
            raise ConnectionError("scan down")
        _ = count  # redis hint; FakeRedis yields all matches in one pass
        yield_bytes = getattr(self, "scan_yield_bytes", False)
        for key in list(self._kv.keys()):
            if self._purge_if_expired(key):
                continue
            if fnmatch.fnmatch(key, match):
                yield key.encode("utf-8") if yield_bytes else key
        for zname in list(self._zsets.keys()):
            if fnmatch.fnmatch(zname, match):
                yield zname.encode("utf-8") if yield_bytes else zname

    def ping(self) -> bool:
        if "ping" in self.fail_ops:
            raise ConnectionError("ping down")
        return True

    def close(self) -> None:
        self.close_calls += 1
