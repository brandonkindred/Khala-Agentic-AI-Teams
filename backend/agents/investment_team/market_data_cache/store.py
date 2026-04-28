"""Durable, content-addressed market-data cache (issue #376).

Owns four concerns:

* **Parquet snapshots on disk** under
  ``${AGENT_CACHE}/investment_team/market_data/...`` — one file per
  ``(asset_class, symbol, frequency, provider, fetch_date)``, immutable
  once written.
* **Postgres index** ``investment_market_data_snapshots`` — looks up
  the latest snapshot covering a requested ``[start, end]`` range with
  ``fetch_ts <= as_of``.  Falls back to an in-process index when
  ``POSTGRES_HOST`` is unset; that path is enough for unit tests but
  loses cross-process reproducibility, which is the whole point of the
  cache, so production must run with Postgres.
* **Canonical SHA256 fingerprint** over OHLCV rows — emitted on
  ``BacktestResult.dataset_fingerprint`` and used as the key for the
  derived ADV cache, replacing the in-memory TTL cache previously
  hard-coded in :class:`MarketDataService`.
* **Derived ADV cache** keyed on ``(per-symbol fingerprint, lookback)``
  — eternal validity within a fingerprint, no TTL.

The store is deliberately provider-agnostic: callers pass a ``fetch_fn``
that returns ``(bars, provider_slug)``.  Provider chains, retry policy,
and the intraday-safety guard live in :class:`MarketDataService`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from shared_postgres import is_postgres_enabled
from shared_postgres.client import get_conn

from ..market_data_service import OHLCVBar, compute_adv_from_bars
from . import paths as _paths

if TYPE_CHECKING:
    import pyarrow as pa  # noqa: F401 — for forward-ref annotations only

logger = logging.getLogger(__name__)

# Lazy: built on first use by _get_parquet_schema().  Importing pyarrow at
# module load would force every consumer of investment_team.api.main to have
# pyarrow installed, even when no caller exercises the parquet write path.
_PARQUET_SCHEMA: Any = None


def _get_parquet_schema() -> Any:
    global _PARQUET_SCHEMA
    if _PARQUET_SCHEMA is None:
        import pyarrow as pa  # noqa: PLC0415 — deliberate lazy import

        _PARQUET_SCHEMA = pa.schema(
            [
                ("date", pa.string()),
                ("open", pa.float64()),
                ("high", pa.float64()),
                ("low", pa.float64()),
                ("close", pa.float64()),
                ("volume", pa.float64()),
            ]
        )
    return _PARQUET_SCHEMA


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotMeta:
    """Metadata for a single cached snapshot row.

    Mirrors the columns of ``investment_market_data_snapshots``.
    ``parquet_path`` is the absolute path on disk; ``sha256`` is the
    canonical fingerprint of the bars, suitable for use as a derived-cache
    key.
    """

    symbol: str
    asset_class: str
    frequency: str
    provider: str
    fetch_ts: datetime
    start_date: str
    end_date: str
    row_count: int
    sha256: str
    parquet_path: str
    schema_version: int = 1


FetchFn = Callable[[str, str, str, str], Tuple[List[OHLCVBar], str]]
"""``fetch_fn(symbol, asset_class, start, end) -> (bars, provider_slug)``.

Returns ``([], "")`` when every provider is exhausted; the cache treats
that as a miss and does not write a snapshot.
"""


# ---------------------------------------------------------------------------
# Canonical fingerprinting
# ---------------------------------------------------------------------------


def _hash_bars(bars: Sequence[OHLCVBar]) -> str:
    """SHA256 over a deterministic byte stream of bars.

    Bars are serialized in chronological-date order so callers may pass
    them in any order; floats use ``repr()`` so the round-trip is
    bit-exact.  Empty input is hashed to the empty digest of an empty
    string.
    """
    h = hashlib.sha256()
    for bar in sorted(bars, key=lambda b: b.date):
        h.update(
            f"{bar.date}|{repr(bar.open)}|{repr(bar.high)}|"
            f"{repr(bar.low)}|{repr(bar.close)}|{repr(bar.volume)}\n".encode()
        )
    return h.hexdigest()


def compute_dataset_fingerprint(per_symbol: Mapping[str, Sequence[OHLCVBar]]) -> str:
    """Hash a multi-symbol bars dict in a symbol-order-independent way.

    The same set of ``(symbol, bars)`` pairs always hashes the same,
    regardless of dict insertion order or per-symbol bar order — making
    this safe to use as ``BacktestResult.dataset_fingerprint``.
    """
    parts = sorted(f"{symbol}:{_hash_bars(bars)}" for symbol, bars in per_symbol.items())
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode())
        h.update(b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Parquet I/O
# ---------------------------------------------------------------------------


def _bars_to_table(bars: Sequence[OHLCVBar]) -> "pa.Table":
    import pyarrow as pa  # noqa: PLC0415 — deliberate lazy import

    return pa.Table.from_pydict(
        {
            "date": [b.date for b in bars],
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
        },
        schema=_get_parquet_schema(),
    )


def _table_to_bars(table: "pa.Table") -> List[OHLCVBar]:
    cols = {
        name: table[name].to_pylist() for name in ("date", "open", "high", "low", "close", "volume")
    }
    return [
        OHLCVBar(
            date=cols["date"][i],
            open=cols["open"][i],
            high=cols["high"][i],
            low=cols["low"][i],
            close=cols["close"][i],
            volume=cols["volume"][i],
        )
        for i in range(table.num_rows)
    ]


# ---------------------------------------------------------------------------
# Index (Postgres-backed, with in-memory fallback)
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_as_of(as_of: Optional[str]) -> datetime:
    """Normalize ``as_of`` to a UTC datetime cutoff.

    ``None`` means "now"; a bare date string means end-of-day UTC; a full
    ISO datetime is parsed verbatim.
    """
    if not as_of:
        return _now_utc()
    s = as_of.strip()
    try:
        if "T" in s or " " in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            d = date.fromisoformat(s[:10])
            dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)
    except ValueError:
        return _now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_meta(row: Mapping[str, object]) -> SnapshotMeta:
    fetch_ts = row["fetch_ts"]
    if isinstance(fetch_ts, str):
        fetch_ts = datetime.fromisoformat(fetch_ts.replace("Z", "+00:00"))
    if isinstance(fetch_ts, datetime) and fetch_ts.tzinfo is None:
        fetch_ts = fetch_ts.replace(tzinfo=timezone.utc)

    start_d = row["start_date"]
    end_d = row["end_date"]
    if isinstance(start_d, date):
        start_d = start_d.isoformat()
    if isinstance(end_d, date):
        end_d = end_d.isoformat()
    return SnapshotMeta(
        symbol=str(row["symbol"]),
        asset_class=str(row["asset_class"]),
        frequency=str(row["frequency"]),
        provider=str(row["provider"]),
        fetch_ts=fetch_ts,  # type: ignore[arg-type]
        start_date=str(start_d),
        end_date=str(end_d),
        row_count=int(row["row_count"]),
        sha256=str(row["sha256"]),
        parquet_path=str(row["parquet_path"]),
        schema_version=int(row.get("schema_version", 1)),
    )


# ---------------------------------------------------------------------------
# MarketDataCache
# ---------------------------------------------------------------------------


def _default_workers(symbol_count: int) -> int:
    raw = os.environ.get("MARKET_DATA_FETCH_WORKERS", "").strip()
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
    return max(1, min(symbol_count, 16))


class MarketDataCache:
    """Snapshot-based cache.  See module docstring."""

    def __init__(self, *, cache_root: Optional[Path] = None) -> None:
        self._cache_root: Optional[Path] = cache_root
        # In-memory index used when Postgres is disabled.  Each entry is a
        # full ``SnapshotMeta``; lookups iterate (the table is small and
        # bounded by call volume per process).
        self._memory_index: List[SnapshotMeta] = []
        self._index_lock = threading.Lock()
        # Derived ADV cache keyed on ``(symbol_fingerprint, lookback)``.
        # Stored in-process; cross-process correctness is automatic
        # because the key is content-addressed.
        self._adv_cache: Dict[Tuple[str, int], Optional[float]] = {}
        self._adv_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _resolved_root(self) -> Path:
        return self._cache_root if self._cache_root is not None else _paths.cache_root()

    def _snapshot_path(
        self, *, asset_class: str, symbol: str, frequency: str, provider: str, fetch_date: str
    ) -> Path:
        if self._cache_root is None:
            return _paths.snapshot_path(
                asset_class=asset_class,
                symbol=symbol,
                frequency=frequency,
                provider=provider,
                fetch_date=fetch_date,
            )
        return (
            self._cache_root / asset_class / symbol / frequency / provider / f"{fetch_date}.parquet"
        )

    # ------------------------------------------------------------------
    # Index queries
    # ------------------------------------------------------------------

    def _find_covering_snapshot(
        self,
        *,
        symbol: str,
        asset_class: str,
        frequency: str,
        start: str,
        end: str,
        as_of_dt: datetime,
    ) -> Optional[SnapshotMeta]:
        if is_postgres_enabled():
            try:
                from shared_postgres import dict_row  # lazy: optional dep at unit-test time

                with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        """
                        SELECT symbol, asset_class, frequency, provider, fetch_ts,
                               start_date, end_date, row_count, sha256,
                               schema_version, parquet_path
                          FROM investment_market_data_snapshots
                         WHERE symbol = %s
                           AND asset_class = %s
                           AND frequency = %s
                           AND fetch_ts <= %s
                           AND start_date <= %s
                           AND end_date >= %s
                         ORDER BY fetch_ts DESC
                         LIMIT 1
                        """,
                        (symbol, asset_class, frequency, as_of_dt, start, end),
                    )
                    row = cur.fetchone()
                if row is None:
                    return None
                return _row_to_meta(row)
            except Exception:
                logger.exception(
                    "investment_market_data_snapshots lookup failed; falling back to memory index"
                )
                # fall through to memory path

        with self._index_lock:
            candidates = [
                m
                for m in self._memory_index
                if m.symbol == symbol
                and m.asset_class == asset_class
                and m.frequency == frequency
                and m.fetch_ts <= as_of_dt
                and m.start_date <= start
                and m.end_date >= end
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda m: m.fetch_ts)

    def _record_snapshot(self, meta: SnapshotMeta) -> None:
        if is_postgres_enabled():
            try:
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO investment_market_data_snapshots
                            (symbol, asset_class, frequency, provider, fetch_ts,
                             start_date, end_date, row_count, sha256,
                             schema_version, parquet_path)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            meta.symbol,
                            meta.asset_class,
                            meta.frequency,
                            meta.provider,
                            meta.fetch_ts,
                            meta.start_date,
                            meta.end_date,
                            meta.row_count,
                            meta.sha256,
                            meta.schema_version,
                            meta.parquet_path,
                        ),
                    )
                return
            except Exception:
                logger.exception(
                    "investment_market_data_snapshots insert failed; recording in memory index only"
                )
        with self._index_lock:
            self._memory_index.append(meta)

    # ------------------------------------------------------------------
    # Snapshot read/write
    # ------------------------------------------------------------------

    def _read_snapshot(self, meta: SnapshotMeta) -> Optional[List[OHLCVBar]]:
        path = Path(meta.parquet_path)
        if not path.exists():
            logger.warning(
                "snapshot %s missing on disk (provider=%s, range=%s..%s); refetching",
                path,
                meta.provider,
                meta.start_date,
                meta.end_date,
            )
            return None
        try:
            import pyarrow.parquet as pq  # noqa: PLC0415 — deliberate lazy import

            table = pq.read_table(path)
        except Exception:
            logger.exception("failed to read parquet snapshot at %s; refetching", path)
            return None
        return _table_to_bars(table)

    def _write_snapshot(
        self,
        *,
        symbol: str,
        asset_class: str,
        frequency: str,
        provider: str,
        bars: Sequence[OHLCVBar],
        start: str,
        end: str,
    ) -> SnapshotMeta:
        fetch_ts = _now_utc()
        fetch_date = fetch_ts.date().isoformat()
        out_path = self._snapshot_path(
            asset_class=asset_class,
            symbol=symbol,
            frequency=frequency,
            provider=provider,
            fetch_date=fetch_date,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)

        table = _bars_to_table(bars)
        # Two snapshots written on the same UTC day from the same
        # provider would collide on path; suffix with a microsecond stamp
        # to keep the original immutable.
        if out_path.exists():
            stamp = fetch_ts.strftime("%Y-%m-%dT%H%M%S%f")
            out_path = out_path.with_name(f"{stamp}.parquet")
        import pyarrow.parquet as pq  # noqa: PLC0415 — deliberate lazy import

        pq.write_table(table, out_path, compression="snappy")

        meta = SnapshotMeta(
            symbol=symbol,
            asset_class=asset_class,
            frequency=frequency,
            provider=provider,
            fetch_ts=fetch_ts,
            start_date=start,
            end_date=end,
            row_count=len(bars),
            sha256=_hash_bars(bars),
            parquet_path=str(out_path),
        )
        self._record_snapshot(meta)
        return meta

    # ------------------------------------------------------------------
    # Public: get_or_fetch
    # ------------------------------------------------------------------

    def get_or_fetch(
        self,
        *,
        symbol: str,
        asset_class: str,
        frequency: str,
        start: str,
        end: str,
        fetch_fn: FetchFn,
        as_of: Optional[str] = None,
    ) -> Tuple[List[OHLCVBar], Optional[SnapshotMeta]]:
        """Return bars for ``[start, end]`` for one symbol.

        On cache hit returns ``(bars_from_parquet, meta)`` without
        invoking ``fetch_fn``.  On miss invokes ``fetch_fn`` once, writes
        a new snapshot, and indexes it.  Empty fetches do not produce a
        snapshot — ``meta`` is None and the bar list is empty.
        """
        as_of_dt = _parse_as_of(as_of)
        existing = self._find_covering_snapshot(
            symbol=symbol,
            asset_class=asset_class,
            frequency=frequency,
            start=start,
            end=end,
            as_of_dt=as_of_dt,
        )
        if existing is not None:
            cached = self._read_snapshot(existing)
            if cached is not None:
                trimmed = [b for b in cached if start <= b.date <= end]
                return trimmed, existing

        bars, provider = fetch_fn(symbol, asset_class, start, end)
        if not bars or not provider:
            return [], None
        meta = self._write_snapshot(
            symbol=symbol,
            asset_class=asset_class,
            frequency=frequency,
            provider=provider,
            bars=bars,
            start=start,
            end=end,
        )
        return list(bars), meta

    def get_or_fetch_multi(
        self,
        *,
        symbols: Sequence[str],
        asset_class: str,
        frequency: str,
        start: str,
        end: str,
        fetch_fn: FetchFn,
        as_of: Optional[str] = None,
    ) -> Dict[str, Tuple[List[OHLCVBar], Optional[SnapshotMeta]]]:
        """Parallel wrapper around :meth:`get_or_fetch`.

        Workers come from ``MARKET_DATA_FETCH_WORKERS`` (default
        ``min(len(symbols), 16)``).  Per-symbol failures are logged and
        omitted from the result; the caller decides what to do with
        partial coverage.
        """
        result: Dict[str, Tuple[List[OHLCVBar], Optional[SnapshotMeta]]] = {}
        if not symbols:
            return result
        workers = _default_workers(len(symbols))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self.get_or_fetch,
                    symbol=sym,
                    asset_class=asset_class,
                    frequency=frequency,
                    start=start,
                    end=end,
                    fetch_fn=fetch_fn,
                    as_of=as_of,
                ): sym
                for sym in symbols
            }
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    bars, meta = fut.result()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("cache fetch failed for %s: %s", sym, exc)
                    continue
                if bars:
                    result[sym] = (bars, meta)
        return result

    # ------------------------------------------------------------------
    # Snapshot from already-materialized bars (used by streaming wrapper)
    # ------------------------------------------------------------------

    def record_bars_snapshot(
        self,
        *,
        symbol: str,
        asset_class: str,
        frequency: str,
        provider: str,
        bars: Sequence[OHLCVBar],
        start: str,
        end: str,
    ) -> Optional[SnapshotMeta]:
        """Persist bars that the caller already fetched (e.g. via a stream).

        Returns the new ``SnapshotMeta`` or None when ``bars`` is empty.
        """
        if not bars:
            return None
        return self._write_snapshot(
            symbol=symbol,
            asset_class=asset_class,
            frequency=frequency,
            provider=provider,
            bars=bars,
            start=start,
            end=end,
        )

    def lookup_snapshot(
        self,
        *,
        symbol: str,
        asset_class: str,
        frequency: str,
        start: str,
        end: str,
        as_of: Optional[str] = None,
    ) -> Optional[SnapshotMeta]:
        return self._find_covering_snapshot(
            symbol=symbol,
            asset_class=asset_class,
            frequency=frequency,
            start=start,
            end=end,
            as_of_dt=_parse_as_of(as_of),
        )

    def read_snapshot(self, meta: SnapshotMeta) -> Optional[List[OHLCVBar]]:
        return self._read_snapshot(meta)

    # ------------------------------------------------------------------
    # Derived ADV cache
    # ------------------------------------------------------------------

    def derive_adv(
        self,
        *,
        fingerprint: str,
        lookback: int,
        compute: Callable[[], Optional[float]],
    ) -> Optional[float]:
        """Memoize ``compute()`` under ``(fingerprint, lookback)``.

        The fingerprint must uniquely identify the bar window (typically
        a per-symbol snapshot's ``sha256``); under that key the result is
        eternally valid.
        """
        key = (fingerprint, int(lookback))
        with self._adv_lock:
            if key in self._adv_cache:
                return self._adv_cache[key]
        value = compute()
        with self._adv_lock:
            self._adv_cache[key] = value
        return value

    def adv_for_bars(
        self,
        *,
        bars: Sequence[OHLCVBar],
        lookback: int,
        fingerprint: Optional[str] = None,
    ) -> Optional[float]:
        """Convenience: hash the window and route through ``derive_adv``.

        Caches on the canonical hash of the supplied bars so repeated
        calls with byte-equal input share the result without recomputing.
        """
        fp = fingerprint or _hash_bars(bars)
        return self.derive_adv(
            fingerprint=fp,
            lookback=lookback,
            compute=lambda: compute_adv_from_bars(bars, lookback=lookback),
        )


# ---------------------------------------------------------------------------
# Module-level shared instance
# ---------------------------------------------------------------------------


_DEFAULT_CACHE: Optional[MarketDataCache] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_cache() -> MarketDataCache:
    """Return the process-wide cache instance, lazily constructed.

    Tests construct their own ``MarketDataCache(cache_root=tmp_path)``
    and inject it into the consumer; production code uses this default
    so the cache root resolves once via ``paths.cache_root()``.
    """
    global _DEFAULT_CACHE
    with _DEFAULT_LOCK:
        if _DEFAULT_CACHE is None:
            _DEFAULT_CACHE = MarketDataCache()
        return _DEFAULT_CACHE


def reset_default_cache() -> None:
    """Test helper — discard the module-level cache so the next call rebuilds."""
    global _DEFAULT_CACHE
    with _DEFAULT_LOCK:
        _DEFAULT_CACHE = None


__all__ = [
    "MarketDataCache",
    "SnapshotMeta",
    "FetchFn",
    "compute_dataset_fingerprint",
    "get_default_cache",
    "reset_default_cache",
]
