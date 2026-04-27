"""Issue #376 — canonical SHA256 dataset fingerprint and derived ADV cache.

Covers:

* ``compute_dataset_fingerprint`` — deterministic across symbol-set
  insertion order and per-symbol bar order.
* Tampering invariance — a single bar mutation flips the fingerprint.
* ``BacktestResult.dataset_fingerprint`` — populated and stable across
  re-runs of the same backtest config.
* Derived ADV cache — same fingerprint + lookback returns a cached
  result without recomputing.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from investment_team.market_data_cache import (
    MarketDataCache,
    compute_dataset_fingerprint,
)
from investment_team.market_data_service import OHLCVBar


def _bars(closes: List[float]) -> List[OHLCVBar]:
    out: List[OHLCVBar] = []
    for i, c in enumerate(closes):
        out.append(
            OHLCVBar(
                date=f"2024-01-{i + 1:02d}",
                open=c,
                high=c + 1.0,
                low=c - 1.0,
                close=c,
                volume=1_000_000.0 + i,
            )
        )
    return out


# ---------------------------------------------------------------------------
# compute_dataset_fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_is_symbol_order_independent() -> None:
    a = _bars([100.0, 101.0, 102.0])
    b = _bars([200.0, 201.0, 202.0])
    fp1 = compute_dataset_fingerprint({"AAA": a, "BBB": b})
    fp2 = compute_dataset_fingerprint({"BBB": b, "AAA": a})
    assert fp1 == fp2


def test_fingerprint_is_bar_order_independent_when_dates_unique() -> None:
    bars = _bars([100.0, 101.0, 102.0])
    reversed_bars = list(reversed(bars))
    assert compute_dataset_fingerprint({"AAA": bars}) == compute_dataset_fingerprint(
        {"AAA": reversed_bars}
    )


def test_fingerprint_changes_on_single_bar_mutation() -> None:
    bars = _bars([100.0, 101.0, 102.0])
    fp_baseline = compute_dataset_fingerprint({"AAA": bars})
    mutated = list(bars)
    mutated[1] = OHLCVBar(
        date=mutated[1].date,
        open=mutated[1].open,
        high=mutated[1].high,
        low=mutated[1].low,
        close=mutated[1].close + 1e-9,  # one ULP-ish tweak
        volume=mutated[1].volume,
    )
    fp_mutated = compute_dataset_fingerprint({"AAA": mutated})
    assert fp_baseline != fp_mutated


def test_empty_input_is_a_stable_value() -> None:
    fp1 = compute_dataset_fingerprint({})
    fp2 = compute_dataset_fingerprint({})
    assert fp1 == fp2


# ---------------------------------------------------------------------------
# BacktestResult.dataset_fingerprint via the legacy pre-fetched path
# ---------------------------------------------------------------------------


def test_backtest_result_fingerprint_stable_across_runs(tmp_path: Path) -> None:
    """A pre-fetched market_data dict yields an identical fingerprint on rerun.

    Using the legacy data path (which hashes the dict directly) keeps
    this test independent of the provider stream — it isolates the
    fingerprint contract from the cache backing store.
    """
    import textwrap

    from investment_team.models import BacktestConfig, StrategySpec
    from investment_team.trading_service.modes.backtest import run_backtest

    bars_a = _bars([100.0 + i * 0.1 for i in range(60)])
    bars_b = _bars([50.0 + i * 0.05 for i in range(60)])

    market_data = {"AAA": bars_a, "BBB": bars_b}
    spec = StrategySpec(
        strategy_id="fp-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="trivial",
        signal_definition="hold-forever",
        entry_rules=["hold"],
        exit_rules=["never"],
        sizing_rules=["full"],
        strategy_code=textwrap.dedent(
            """
            def on_bar(bar, ctx):
                return ()
            """
        ),
    )
    cfg = BacktestConfig(start_date="2024-01-01", end_date="2024-03-01")

    r1 = run_backtest(strategy=spec, config=cfg, market_data=market_data)
    r2 = run_backtest(strategy=spec, config=cfg, market_data=market_data)
    assert r1.result.dataset_fingerprint is not None
    assert r1.result.dataset_fingerprint == r2.result.dataset_fingerprint


# ---------------------------------------------------------------------------
# Derived ADV cache
# ---------------------------------------------------------------------------


def test_derived_adv_caches_on_fingerprint_lookback(tmp_path: Path) -> None:
    cache = MarketDataCache(cache_root=tmp_path)
    # The fingerprint key is opaque to ``derive_adv``; the test only
    # cares that two calls with the same key share the cached result.
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return 100_000_000.0

    fp = "deadbeef" * 8
    v1 = cache.derive_adv(fingerprint=fp, lookback=20, compute=compute)
    v2 = cache.derive_adv(fingerprint=fp, lookback=20, compute=compute)
    assert v1 == 100_000_000.0
    assert v2 == 100_000_000.0
    assert calls["n"] == 1, "compute must run only on the first call"


def test_adv_for_bars_uses_canonical_hash(tmp_path: Path) -> None:
    cache = MarketDataCache(cache_root=tmp_path)
    bars = [
        OHLCVBar(
            date=f"2024-01-{i + 1:02d}",
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=1_000_000.0,
        )
        for i in range(20)
    ]
    adv1 = cache.adv_for_bars(bars=bars, lookback=20)
    adv2 = cache.adv_for_bars(bars=bars, lookback=20)
    assert adv1 == 100_000_000.0
    assert adv1 == adv2

    # Tweaking one bar's volume must produce a different cache key
    # (independent recomputation).
    tweaked = list(bars)
    tweaked[-1] = OHLCVBar(
        date=tweaked[-1].date,
        open=tweaked[-1].open,
        high=tweaked[-1].high,
        low=tweaked[-1].low,
        close=tweaked[-1].close,
        volume=2_000_000.0,
    )
    adv_tweaked = cache.adv_for_bars(bars=tweaked, lookback=20)
    assert adv_tweaked != adv1


# ---------------------------------------------------------------------------
# No-postgres guard
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
