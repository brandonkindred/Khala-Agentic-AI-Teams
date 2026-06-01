"""Per-attempt memoization of strategy backtests.

The orchestrator re-executes ``run_strategy_code`` for the *same* code
against the *same* hoisted ``market_data`` and ``BacktestConfig`` in several
places — most notably the trade-alignment loop, where a proposed fix that
turns out to equal the current code, a determinism re-check, or an
audit/recovery re-backtest all replay identical inputs. Backtests are
deterministic in their inputs, so replaying them is wasted wall time.

:class:`BacktestCache` keys finished :class:`StrategyRunResult` objects on
``(code_hash, market_data_fingerprint, config_hash, spec_hash)`` and returns
the stored result on a hit. The spec is keyed too because
``run_strategy_code(..., strategy=spec)`` feeds risk_limits / rules / sizing /
target_symbols into the engine, so a risk-only refinement that leaves the
source unchanged must still re-execute. It is scoped to a *single* design
attempt: the orchestrator
constructs a fresh cache per ``_run_design_attempt`` and discards it when the
attempt ends, so a cached entry can never cross a market-data snapshot.

Reused building blocks:
  * :func:`..phases.hash_code` — SHA-256 of the strategy source.
  * :func:`..market_data_cache.store.compute_dataset_fingerprint` — canonical
    SHA-256 over every OHLCV bar (already used for
    ``BacktestResult.dataset_fingerprint``).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..market_data_cache.store import compute_dataset_fingerprint
from ..trading_service.modes.sandbox_compat import StrategyRunResult, run_strategy_code
from .phases import hash_code, hash_spec

# Marker for "no strategy supplied" — ``run_strategy_code`` then derives a
# minimal spec purely from ``code`` (already in the key), so a constant is
# correct here.
_NO_SPEC = "no-spec"


def _spec_hash(strategy: Any) -> str:
    """Hash the parts of ``strategy`` that steer the backtest.

    ``run_strategy_code(..., strategy=spec)`` feeds ``risk_limits``, the
    structured entry/exit rules, sizing, and target symbols into the engine,
    so two runs with identical *code* but a different spec (e.g. a refinement
    that tightens ``risk_limits`` without touching the source) must not alias.
    Delegates to :func:`hash_spec`, which canonicalises the whole spec
    *excluding* ``strategy_code`` (the code is keyed independently).

    Preconditions:
      - ``strategy`` is ``None``, a ``StrategySpec`` (exposes ``model_dump``),
        or any object (best-effort ``repr`` fallback for non-spec inputs).
    Postconditions:
      - Returns a stable string component for the cache key.
    """
    if strategy is None:
        return _NO_SPEC
    if hasattr(strategy, "model_dump"):
        return hash_spec(strategy)
    # Defensive: non-spec object (not expected on the orchestrator path).
    return hashlib.sha256(repr(strategy).encode("utf-8")).hexdigest()


def _config_hash(config: Any) -> str:
    """SHA-256 of the canonical-JSON serialisation of ``config``.

    Hashing the *whole* ``BacktestConfig`` (rather than a hand-picked subset
    of cost fields) is deliberately conservative: over-keying only lowers the
    hit rate, it can never return a result computed under different
    assumptions. ``config`` is fixed for the lifetime of a design attempt, so
    every call within an attempt produces the same component.

    Preconditions:
      - ``config`` exposes ``model_dump`` (a Pydantic ``BacktestConfig``).
    Postconditions:
      - Returns a 64-character lowercase hex digest, stable for any config
        with the same field values (keys sorted, no whitespace).
    Invariants:
      - Pure: no side effects, no I/O.
    """
    payload = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BacktestCache:
    """Memoize ``run_strategy_code`` results within a single design attempt.

    Invariants:
      - The cache is single-attempt scoped: every stored result was produced
        against one of the (typically one) market-data objects fingerprinted
        here. Because the attempt holds those objects alive for the cache's
        lifetime, the ``id()``-keyed fingerprint memo cannot collide with a
        reused object id.
      - ``hits + misses`` equals the number of :meth:`get_or_run` calls.
    """

    def __init__(self) -> None:
        self._results: Dict[str, StrategyRunResult] = {}
        # ``id(market_data) -> fingerprint`` so the O(total_bars) hash is paid
        # once per distinct data object instead of on every lookup.
        self._fingerprint_by_id: Dict[int, str] = {}
        # Hold a reference to each fingerprinted object so its ``id()`` cannot
        # be recycled by the allocator mid-attempt (which would alias a stale
        # fingerprint onto a different dataset).
        self._fingerprinted_refs: List[Any] = []
        self.hits: int = 0
        self.misses: int = 0

    def _market_data_fingerprint(self, market_data: Dict[str, List[Any]]) -> str:
        key = id(market_data)
        fingerprint = self._fingerprint_by_id.get(key)
        if fingerprint is None:
            fingerprint = compute_dataset_fingerprint(market_data)
            self._fingerprint_by_id[key] = fingerprint
            self._fingerprinted_refs.append(market_data)
        return fingerprint

    def _key(self, code: str, market_data: Dict[str, List[Any]], config: Any, strategy: Any) -> str:
        digest = hashlib.sha256()
        digest.update(hash_code(code).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self._market_data_fingerprint(market_data).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(_config_hash(config).encode("utf-8"))
        digest.update(b"\x00")
        # The spec (minus its code, hashed above) also steers the backtest —
        # e.g. a refinement that tightens risk_limits without changing the
        # source must invalidate the entry.
        digest.update(_spec_hash(strategy).encode("utf-8"))
        return digest.hexdigest()

    def get_or_run(
        self,
        code: str,
        market_data: Dict[str, List[Any]],
        config: Any,
        *,
        strategy: Any = None,
        runner: Optional[Callable[..., StrategyRunResult]] = None,
    ) -> Tuple[StrategyRunResult, bool]:
        """Return a backtest result for ``code``, running it only on a miss.

        ``runner`` defaults to :func:`run_strategy_code`; the orchestrator
        passes its own module-level ``run_strategy_code`` reference so test
        monkeypatches of ``orchestrator.run_strategy_code`` continue to apply.

        Preconditions:
          - ``code`` is a non-empty ``str``.
          - ``market_data`` is the hoisted per-symbol OHLCV dict for the
            attempt; ``config`` is the attempt's ``BacktestConfig``.
        Postconditions:
          - Returns ``(result, hit)``. On a hit, ``result`` is the object
            stored by the first run with the same ``(code, market_data,
            config, strategy)`` key and no execution happened. On a miss,
            ``runner`` was invoked exactly once and its result stored. For
            deterministic ``runner`` (the contract of ``run_strategy_code``) a
            hit is observationally identical to a fresh run.
          - ``hits``/``misses`` are incremented to reflect the outcome.
        """
        assert isinstance(code, str) and code, "code must be a non-empty string"
        run = runner if runner is not None else run_strategy_code
        key = self._key(code, market_data, config, strategy)
        cached = self._results.get(key)
        if cached is not None:
            self.hits += 1
            return cached, True
        result = run(code, market_data, config, strategy=strategy)
        self._results[key] = result
        self.misses += 1
        return result, False
