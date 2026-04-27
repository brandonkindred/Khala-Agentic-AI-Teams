"""Preflight market-data integrity validator (issue #375).

Single public entry point :func:`validate_market_data` runs before every
backtest start and at paper-trading session warm-up.  It detects the
classes of corruption that silently distort backtest results:

* missing trading days / gaps vs. the asset-class calendar
* timestamps inconsistent with the requested ``expected_frequency``
* duplicate timestamps
* NaN / negative / zero prices
* OHLC invariant violations (``H >= max(O, C, L)``, ``L <= min(O, C, H)``)
* zero-volume bars (stuck feed)
* volume z-score outliers (spike bars)
* cross-symbol date misalignment (different first/last/length)

Two modes:

* ``strict`` — raises :class:`DataIntegrityError` whenever the aggregate
  severity is ``"fail"``.
* ``warn``  — always returns the populated report; callers decide what
  to do with the advisories.

The narrow :mod:`investment_team.execution.intraday_guard` continues to
own intraday-provider safety; this module sits alongside and runs both.

No new third-party dependency: weekend / holiday inference uses pandas
``BusinessDay`` plus a small explicit US holiday set.  Crypto runs on a
24/7 calendar.  Regional / non-US calendars are intentionally out of
scope for this PR (future opt-in via ``pandas_market_calendars``).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Literal, Optional, Sequence

from pydantic import BaseModel, Field

from ..market_data_service import OHLCVBar

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frequencies
# ---------------------------------------------------------------------------

# Canonical bar-duration table.  Anything not listed here is treated as an
# unknown frequency and the inferred-vs-expected check is skipped (a
# warning is emitted instead so the run is not gated on a typo).
_FREQUENCY_SECONDS: Dict[str, float] = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
}

# Tolerance band for frequency inference.  The median delta must fall
# within ±_FREQUENCY_TOL_PCT of the canonical seconds-per-bar to count
# as a match — e.g. a 1d run with a couple of weekend-skip 3d gaps still
# infers "1d" because the median is on the daily bucket.
_FREQUENCY_TOL_PCT = 0.10

# US equity holidays we observe out-of-the-box.  The list is intentionally
# small: this PR's gap detector is forgiving (>0.5% missing OR >3 bars,
# whichever is larger), so missing one minor holiday in a long backtest
# does not flip the run to ``fail``.  Extend or replace with
# ``pandas_market_calendars`` later if precise calendars become a need.
# Inclusive year window covered by the holiday set below.  Used to gate
# gap detection on equity-style classes — runs that touch a year outside
# this window degrade to "skip gaps with a note" rather than risk
# false-failing on legitimate market holidays we haven't catalogued.
_US_HOLIDAYS_YEAR_MIN = 2018
_US_HOLIDAYS_YEAR_MAX = 2030


_US_HOLIDAYS_2018_2030: frozenset[str] = frozenset(
    {
        # New Year's Day (observed)
        "2018-01-01",
        "2019-01-01",
        "2020-01-01",
        "2021-01-01",
        "2022-01-03",
        "2023-01-02",
        "2024-01-01",
        "2025-01-01",
        "2026-01-01",
        "2027-01-01",
        "2028-01-03",
        "2029-01-01",
        "2030-01-01",
        # MLK Day (3rd Mon Jan)
        "2018-01-15",
        "2019-01-21",
        "2020-01-20",
        "2021-01-18",
        "2022-01-17",
        "2023-01-16",
        "2024-01-15",
        "2025-01-20",
        "2026-01-19",
        "2027-01-18",
        "2028-01-17",
        "2029-01-15",
        "2030-01-21",
        # Presidents' Day (3rd Mon Feb)
        "2018-02-19",
        "2019-02-18",
        "2020-02-17",
        "2021-02-15",
        "2022-02-21",
        "2023-02-20",
        "2024-02-19",
        "2025-02-17",
        "2026-02-16",
        "2027-02-15",
        "2028-02-21",
        "2029-02-19",
        "2030-02-18",
        # Good Friday (manually listed)
        "2018-03-30",
        "2019-04-19",
        "2020-04-10",
        "2021-04-02",
        "2022-04-15",
        "2023-04-07",
        "2024-03-29",
        "2025-04-18",
        "2026-04-03",
        "2027-03-26",
        "2028-04-14",
        "2029-03-30",
        "2030-04-19",
        # Memorial Day (last Mon May)
        "2018-05-28",
        "2019-05-27",
        "2020-05-25",
        "2021-05-31",
        "2022-05-30",
        "2023-05-29",
        "2024-05-27",
        "2025-05-26",
        "2026-05-25",
        "2027-05-31",
        "2028-05-29",
        "2029-05-28",
        "2030-05-27",
        # Juneteenth (from 2022)
        "2022-06-20",
        "2023-06-19",
        "2024-06-19",
        "2025-06-19",
        "2026-06-19",
        "2027-06-18",
        "2028-06-19",
        "2029-06-19",
        "2030-06-19",
        # Independence Day (observed)
        "2018-07-04",
        "2019-07-04",
        "2020-07-03",
        "2021-07-05",
        "2022-07-04",
        "2023-07-04",
        "2024-07-04",
        "2025-07-04",
        "2026-07-03",
        "2027-07-05",
        "2028-07-04",
        "2029-07-04",
        "2030-07-04",
        # Labor Day (1st Mon Sep)
        "2018-09-03",
        "2019-09-02",
        "2020-09-07",
        "2021-09-06",
        "2022-09-05",
        "2023-09-04",
        "2024-09-02",
        "2025-09-01",
        "2026-09-07",
        "2027-09-06",
        "2028-09-04",
        "2029-09-03",
        "2030-09-02",
        # Thanksgiving (4th Thu Nov)
        "2018-11-22",
        "2019-11-28",
        "2020-11-26",
        "2021-11-25",
        "2022-11-24",
        "2023-11-23",
        "2024-11-28",
        "2025-11-27",
        "2026-11-26",
        "2027-11-25",
        "2028-11-23",
        "2029-11-22",
        "2030-11-28",
        # Christmas (observed)
        "2018-12-25",
        "2019-12-25",
        "2020-12-25",
        "2021-12-24",
        "2022-12-26",
        "2023-12-25",
        "2024-12-25",
        "2025-12-25",
        "2026-12-25",
        "2027-12-24",
        "2028-12-25",
        "2029-12-25",
        "2030-12-25",
    }
)


# Asset classes that observe US equity-style closures.  ``crypto`` is the
# notable opt-out (24/7); FX has its own quirks but for the purposes of
# weekend gaps it lines up with US weekdays well enough for an MVP.
_BUSINESS_DAY_ASSET_CLASSES = frozenset(
    {"stocks", "equities", "options", "commodities", "forex", "futures"}
)


# Lightweight synonyms for the asset-class label.  Differs from
# :func:`investment_team.strategy_lab_context.normalize_asset_class` in
# one important way: unknown classes are returned unchanged (rather than
# defaulting to ``"stocks"``), so a typo or genuinely exotic class falls
# back to the continuous calendar instead of being silently treated as
# US equities.
_ASSET_CLASS_ALIASES: Dict[str, str] = {
    "equities": "stocks",
    "equity": "stocks",
    "stock": "stocks",
    "fx": "forex",
    "commodity": "commodities",
}


def _normalize_asset_class(asset_class: Optional[str]) -> str:
    if not asset_class:
        return ""
    x = asset_class.lower().strip()
    return _ASSET_CLASS_ALIASES.get(x, x)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SymbolDataQualityReport(BaseModel):
    """Per-symbol counts and inferred frequency."""

    symbol: str
    bar_count: int
    inferred_frequency: str
    expected_frequency: str
    gaps: int = 0
    duplicate_timestamps: int = 0
    off_frequency_bars: int = 0
    nan_or_negative_prices: int = 0
    ohlc_violations: int = 0
    zero_volume_bars: int = 0
    volume_outliers: int = 0
    first_ts: str = ""
    last_ts: str = ""
    issues: List[str] = Field(default_factory=list)


class DataQualityReport(BaseModel):
    """Aggregate report across all symbols in a single fetch."""

    severity: Literal["ok", "warn", "fail"] = "ok"
    expected_frequency: str
    asset_class: str
    cross_symbol_alignment_misses: int = 0
    per_symbol: Dict[str, SymbolDataQualityReport] = Field(default_factory=dict)
    fail_threshold: Dict[str, float] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)


class DataIntegrityError(RuntimeError):
    """Raised by :func:`validate_market_data` in strict mode when severity == 'fail'.

    Carries the structured :class:`DataQualityReport` so callers (the
    backtest API, audit log) can surface specifics without re-parsing
    the message.
    """

    def __init__(self, *, report: DataQualityReport) -> None:
        sym_summary = (
            ", ".join(
                f"{sym}={','.join(r.issues) or 'misaligned'}"
                for sym, r in report.per_symbol.items()
                if r.issues
            )
            or "cross_symbol_alignment_miss"
        )
        super().__init__(f"Market data failed preflight integrity checks: {sym_summary}")
        self.report = report


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_market_data(
    *,
    bars_by_symbol: Dict[str, List[OHLCVBar]],
    expected_frequency: str,
    asset_class: str,
    mode: Literal["strict", "warn"] = "strict",
    z_threshold: float = 6.0,
    gap_tolerance_bars: int = 0,
) -> DataQualityReport:
    """Validate every series in ``bars_by_symbol`` and return a structured report.

    Pure function: no I/O, no side effects beyond a single ``logger.warning``
    call when ``severity != "ok"``.

    ``strict`` mode raises :class:`DataIntegrityError` whenever any rule
    flagged in the per-symbol report rolls up to ``severity == "fail"``.
    ``warn`` mode always returns; callers are responsible for surfacing
    advisories on the run record.
    """
    asset_class_norm = _normalize_asset_class(asset_class)
    expected_freq_norm = (expected_frequency or "").lower()

    per_symbol: Dict[str, SymbolDataQualityReport] = {}
    cross_alignment_misses = 0
    notes: List[str] = []

    if not bars_by_symbol:
        report = DataQualityReport(
            severity="fail",
            expected_frequency=expected_freq_norm,
            asset_class=asset_class_norm,
            notes=["no_symbols_supplied"],
            fail_threshold=_default_fail_thresholds(),
        )
        if mode == "strict":
            raise DataIntegrityError(report=report)
        logger.warning("data_quality: no symbols supplied")
        return report

    # Per-symbol checks.
    for symbol, bars in bars_by_symbol.items():
        per_symbol[symbol] = _validate_symbol(
            symbol=symbol,
            bars=bars,
            expected_frequency=expected_freq_norm,
            asset_class=asset_class_norm,
            z_threshold=z_threshold,
            gap_tolerance_bars=gap_tolerance_bars,
        )

    # Cross-symbol alignment: report a miss for every symbol whose first/last
    # bar timestamp differs from the modal first/last.  We track at most one
    # miss per symbol so the count caps at len(bars_by_symbol).
    if len(bars_by_symbol) > 1:
        firsts = [r.first_ts for r in per_symbol.values() if r.first_ts]
        lasts = [r.last_ts for r in per_symbol.values() if r.last_ts]
        if firsts and lasts:
            modal_first = max(set(firsts), key=firsts.count)
            modal_last = max(set(lasts), key=lasts.count)
            for sym, rep in per_symbol.items():
                if rep.first_ts and (rep.first_ts != modal_first or rep.last_ts != modal_last):
                    cross_alignment_misses += 1
                    rep.issues.append("cross_symbol_alignment_miss")

    severity = _aggregate_severity(per_symbol, cross_alignment_misses)
    report = DataQualityReport(
        severity=severity,
        expected_frequency=expected_freq_norm,
        asset_class=asset_class_norm,
        cross_symbol_alignment_misses=cross_alignment_misses,
        per_symbol=per_symbol,
        fail_threshold=_default_fail_thresholds(),
        notes=notes,
    )

    if severity != "ok":
        logger.warning(
            "data_quality: severity=%s asset_class=%s symbols=%d misses=%d",
            severity,
            asset_class_norm,
            len(per_symbol),
            cross_alignment_misses,
        )

    if mode == "strict" and severity == "fail":
        raise DataIntegrityError(report=report)

    return report


# ---------------------------------------------------------------------------
# Per-symbol validation
# ---------------------------------------------------------------------------


def _validate_symbol(
    *,
    symbol: str,
    bars: Sequence[OHLCVBar],
    expected_frequency: str,
    asset_class: str,
    z_threshold: float,
    gap_tolerance_bars: int,
) -> SymbolDataQualityReport:
    """Run every rule against a single symbol's bars and return its report."""
    report = SymbolDataQualityReport(
        symbol=symbol,
        bar_count=len(bars),
        inferred_frequency="unknown",
        expected_frequency=expected_frequency,
    )

    if not bars:
        report.issues.append("empty_series")
        return report

    report.first_ts = bars[0].date
    report.last_ts = bars[-1].date

    # Price / OHLC / volume rules — single pass over bars.
    seen_ts: set[str] = set()
    volumes: List[float] = []
    for bar in bars:
        if bar.date in seen_ts:
            report.duplicate_timestamps += 1
        else:
            seen_ts.add(bar.date)
        if _has_nan_or_negative_price(bar):
            report.nan_or_negative_prices += 1
        if _ohlc_violation(bar):
            report.ohlc_violations += 1
        if bar.volume == 0:
            report.zero_volume_bars += 1
        if math.isfinite(bar.volume):
            volumes.append(bar.volume)

    # Volume z-score outliers (single NumPy-free pass — keep the module dep-light).
    report.volume_outliers = _count_volume_outliers(volumes, z_threshold)

    # Frequency inference + off-frequency bar count.
    inferred, off_freq = _infer_frequency(bars)
    report.inferred_frequency = inferred
    report.off_frequency_bars = off_freq

    # Gap detection vs. expected calendar.  Only attempt when we have at
    # least 2 bars and a known expected frequency — single-bar paper-trade
    # warm-ups would otherwise spuriously fail.
    if len(bars) >= 2 and expected_frequency in _FREQUENCY_SECONDS:
        gap_count, gap_note = _count_gaps(
            bars=bars,
            expected_frequency=expected_frequency,
            asset_class=asset_class,
            tolerance=gap_tolerance_bars,
        )
        report.gaps = gap_count
        if gap_note is not None:
            report.issues.append(gap_note)

    # Roll up the rules into a flat ``issues`` list so callers can render a
    # one-line summary without re-implementing the severity logic.
    if report.duplicate_timestamps > 0:
        report.issues.append("duplicate_timestamps")
    if report.nan_or_negative_prices > 0:
        report.issues.append("nan_or_negative_prices")
    if report.ohlc_violations > 0:
        report.issues.append("ohlc_violations")
    if report.gaps > _gap_fail_count(len(bars)):
        report.issues.append("gap_above_threshold")
    if (
        expected_frequency in _FREQUENCY_SECONDS
        and inferred in _FREQUENCY_SECONDS
        and inferred != expected_frequency
    ):
        report.issues.append("frequency_mismatch")
    if report.zero_volume_bars > 0:
        report.issues.append("zero_volume_bars")
    if report.volume_outliers > 0:
        report.issues.append("volume_outliers")

    return report


# ---------------------------------------------------------------------------
# Rule helpers
# ---------------------------------------------------------------------------


def _has_nan_or_negative_price(bar: OHLCVBar) -> bool:
    for value in (bar.open, bar.high, bar.low, bar.close):
        if not math.isfinite(value):
            return True
        if value <= 0:
            return True
    return False


def _ohlc_violation(bar: OHLCVBar) -> bool:
    o, h, ll, c = bar.open, bar.high, bar.low, bar.close
    if not all(math.isfinite(v) for v in (o, h, ll, c)):
        # Already counted by the NaN check; do not double-count.
        return False
    if h < ll:
        return True
    if h < max(o, c):
        return True
    if ll > min(o, c):
        return True
    return False


def _count_volume_outliers(volumes: Sequence[float], z_threshold: float) -> int:
    if len(volumes) < 4:
        return 0
    mean = sum(volumes) / len(volumes)
    variance = sum((v - mean) ** 2 for v in volumes) / len(volumes)
    std = math.sqrt(variance)
    if std == 0:
        return 0
    count = 0
    for v in volumes:
        if abs(v - mean) / std > z_threshold:
            count += 1
    return count


def _infer_frequency(bars: Sequence[OHLCVBar]) -> tuple[str, int]:
    """Return ``(inferred_frequency, off_frequency_bar_count)``.

    ``off_frequency`` counts deltas that fall outside the ±tolerance band
    around the canonical frequency-bucket the median maps to (or zero if
    the median itself is unrecognised).
    """
    if len(bars) < 2:
        return ("unknown", 0)
    deltas: List[float] = []
    for prev, curr in zip(bars, bars[1:]):
        prev_dt = _parse_ts(prev.date)
        curr_dt = _parse_ts(curr.date)
        if prev_dt is None or curr_dt is None:
            continue
        delta = (curr_dt - prev_dt).total_seconds()
        if delta > 0:
            deltas.append(delta)
    if not deltas:
        return ("unknown", 0)
    deltas.sort()
    median = deltas[len(deltas) // 2]
    inferred = _bucket_to_frequency(median)
    if inferred not in _FREQUENCY_SECONDS:
        return (inferred, 0)
    expected_seconds = _FREQUENCY_SECONDS[inferred]
    off_count = 0
    # Allow integer multiples up to 5x for weekend / holiday skips before
    # we count a delta as "off-frequency" — a Friday→Monday daily run has
    # a 3x delta on every weekend.
    upper_multiple_pct = 5 * (1 + _FREQUENCY_TOL_PCT)
    for d in deltas:
        ratio = d / expected_seconds
        if ratio < (1 - _FREQUENCY_TOL_PCT):
            off_count += 1
        elif ratio > upper_multiple_pct:
            off_count += 1
    return (inferred, off_count)


def _bucket_to_frequency(seconds: float) -> str:
    """Snap a delta-in-seconds to the nearest canonical frequency bucket."""
    best_label = "unknown"
    best_dist = math.inf
    for label, ref in _FREQUENCY_SECONDS.items():
        # Compare in log-space so 1m vs 5m and 1d vs 1w are treated symmetrically.
        dist = abs(math.log(max(seconds, 1)) - math.log(ref))
        if dist < best_dist:
            best_dist = dist
            best_label = label
    # Require the snap to actually be within the tolerance band, otherwise
    # call it unknown so the caller flags it explicitly.
    if best_label in _FREQUENCY_SECONDS:
        ref = _FREQUENCY_SECONDS[best_label]
        if not ((1 - _FREQUENCY_TOL_PCT) <= seconds / ref <= (1 + _FREQUENCY_TOL_PCT)):
            return "unknown"
    return best_label


def _count_gaps(
    *,
    bars: Sequence[OHLCVBar],
    expected_frequency: str,
    asset_class: str,
    tolerance: int,
) -> tuple[int, Optional[str]]:
    """Count missing bars between bars[0] and bars[-1] vs. an expected calendar.

    Returns ``(missing_count, note)``.  ``note`` is non-None only when the
    requested range falls outside our hardcoded US-equity holiday window
    (``_US_HOLIDAYS_YEAR_MIN`` … ``_US_HOLIDAYS_YEAR_MAX``).  In that
    case the calendar can't reliably distinguish "true holiday" from
    "missing bar," so we skip gap detection (return 0) and surface a
    ``calendar_window_unsupported`` issue on the per-symbol report
    rather than risk a false ``DataIntegrityError`` on otherwise clean
    historical data (e.g. a 2017 backtest).

    For sub-daily frequencies we expect every ``expected_frequency`` step
    to have a bar (no calendar — even crypto markets pause briefly, but
    treating those as gaps is overly aggressive at minute granularity, so
    we tolerate small misses).  For daily frequencies we use a
    business-day calendar with a small US holiday list; crypto and other
    24/7 classes use a continuous calendar.
    """
    if expected_frequency not in _FREQUENCY_SECONDS:
        return (0, None)
    expected_seconds = _FREQUENCY_SECONDS[expected_frequency]

    first = _parse_ts(bars[0].date)
    last = _parse_ts(bars[-1].date)
    if first is None or last is None or last <= first:
        return (0, None)

    # If the asset class observes US-equity-style closures and any part
    # of the range is outside our holiday-set coverage, skip gap
    # detection rather than risk false-failing on legitimate holidays
    # we haven't catalogued.
    is_daily = expected_frequency == "1d"
    if (
        is_daily
        and asset_class in _BUSINESS_DAY_ASSET_CLASSES
        and not _holiday_window_covers(first, last)
    ):
        return (0, "calendar_window_unsupported")

    # Build the expected timestamp set.
    expected_timestamps = _expected_timestamps(
        first=first,
        last=last,
        expected_frequency=expected_frequency,
        asset_class=asset_class,
    )
    if not expected_timestamps:
        return (0, None)

    actual = {_normalize_ts(b.date, expected_seconds) for b in bars}
    missing = sum(1 for ts in expected_timestamps if ts not in actual)
    return (max(missing - tolerance, 0), None)


def _holiday_window_covers(first: datetime, last: datetime) -> bool:
    """True iff every year in [first, last] is in the holiday set's range."""
    return first.year >= _US_HOLIDAYS_YEAR_MIN and last.year <= _US_HOLIDAYS_YEAR_MAX


def _expected_timestamps(
    *,
    first: datetime,
    last: datetime,
    expected_frequency: str,
    asset_class: str,
) -> List[str]:
    expected_seconds = _FREQUENCY_SECONDS[expected_frequency]
    is_daily = expected_frequency == "1d"
    use_business_calendar = is_daily and asset_class in _BUSINESS_DAY_ASSET_CLASSES
    out: List[str] = []
    cur = first
    while cur <= last:
        if use_business_calendar:
            iso = cur.date().isoformat()
            # Mon-Fri only, skip US holidays.
            if cur.weekday() < 5 and iso not in _US_HOLIDAYS_2018_2030:
                out.append(_normalize_ts(iso, expected_seconds))
            cur = cur + timedelta(days=1)
        else:
            out.append(_normalize_ts(cur.isoformat(), expected_seconds))
            cur = cur + timedelta(seconds=expected_seconds)
    return out


def _normalize_ts(ts: str, expected_seconds: float) -> str:
    """Snap a timestamp to a canonical form for set-membership checks.

    Daily bars commonly arrive as ``"2024-01-02"`` while intraday bars
    arrive as ISO-8601 with a time component.  We strip to the date-only
    form for daily frequencies so a 'YYYY-MM-DD' actual bar matches the
    'YYYY-MM-DD' expected bar regardless of whether one carries a time.

    For sub-daily frequencies we re-parse and re-emit in a single
    canonical form so ``"...11:59:00Z"`` and ``"...11:59:00+00:00"`` —
    both legitimate outputs from various provider adapters — collapse
    to the same key.
    """
    if expected_seconds >= _FREQUENCY_SECONDS["1d"]:
        return ts[:10]
    parsed = _parse_ts(ts)
    if parsed is None:
        return ts
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_ts(ts: str) -> Optional[datetime]:
    """Tolerant ISO-8601 parser that accepts both ``YYYY-MM-DD`` and full ISO."""
    if not ts:
        return None
    candidate = ts.strip()
    # Accept trailing ``Z`` (UTC) — datetime.fromisoformat in py3.10 doesn't.
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            dt = datetime.strptime(candidate[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Severity rules
# ---------------------------------------------------------------------------


def _default_fail_thresholds() -> Dict[str, float]:
    return {
        "ohlc_violations": 0,
        "nan_or_negative_prices": 0,
        "duplicate_timestamps": 0,
        "gap_pct_threshold": 0.005,
        "gap_min_count": 3,
        "frequency_mismatch": 0,
        "cross_symbol_alignment_misses": 0,
    }


def _gap_fail_count(bar_count: int) -> int:
    """Symbols may have up to ``max(3, 0.5%)`` missing bars before failing."""
    return max(3, int(0.005 * bar_count))


def _aggregate_severity(
    per_symbol: Dict[str, SymbolDataQualityReport],
    cross_misses: int,
) -> Literal["ok", "warn", "fail"]:
    has_fail = False
    has_warn = False
    if cross_misses > 0:
        has_fail = True
    for rep in per_symbol.values():
        # Any of these immediately fails the run.
        if (
            rep.ohlc_violations > 0
            or rep.nan_or_negative_prices > 0
            or rep.duplicate_timestamps > 0
        ):
            has_fail = True
        # Frequency mismatch is fatal: silently treating 15m data as daily
        # would produce hopelessly wrong returns.
        if (
            rep.expected_frequency in _FREQUENCY_SECONDS
            and rep.inferred_frequency in _FREQUENCY_SECONDS
            and rep.inferred_frequency != rep.expected_frequency
        ):
            has_fail = True
        if rep.gaps > _gap_fail_count(rep.bar_count):
            has_fail = True
        # Soft signals — surface but don't fail the run.
        if rep.zero_volume_bars > 0 or rep.volume_outliers > 0:
            has_warn = True
        if 0 < rep.gaps <= _gap_fail_count(rep.bar_count):
            has_warn = True
    if has_fail:
        return "fail"
    if has_warn:
        return "warn"
    return "ok"


# ---------------------------------------------------------------------------
# Live-stream gap monitor (paper trading)
# ---------------------------------------------------------------------------


class LiveGapMonitor:
    """Stateful per-symbol watchdog for paper-trading streams.

    Records the last live-bar timestamp per symbol and emits a structured
    warning when the gap between two consecutive live bars exceeds
    ``threshold_multiple`` × the bar frequency.  Returns the warning
    string (or ``None``) so the caller can append to
    :class:`PaperTradingSession.warnings`.

    The monitor is safe to feed bars in any order: out-of-order or
    duplicate bars (delta ≤ 0 vs. the recorded last) are dropped without
    mutating state, so a single late bar can't make the next in-order
    bar look like an artificial large gap.  ``_translate`` in
    :mod:`paper_trade` separately enforces ``ts >= cutover_ts`` against
    a single global cut-over timestamp; this monitor handles per-symbol
    monotonicity on top of that.
    """

    def __init__(
        self,
        *,
        bar_frequency: str,
        threshold_multiple: float = 5.0,
    ) -> None:
        self._frequency = bar_frequency
        self._frequency_seconds = _FREQUENCY_SECONDS.get(bar_frequency, 0.0)
        self._threshold = threshold_multiple
        self._last_ts: Dict[str, datetime] = {}

    def observe(self, symbol: str, ts: str) -> Optional[str]:
        if self._frequency_seconds <= 0:
            return None
        cur = _parse_ts(ts)
        if cur is None:
            return None
        prev = self._last_ts.get(symbol)
        if prev is None:
            # First observation for this symbol — record state and return.
            self._last_ts[symbol] = cur
            return None
        delta = (cur - prev).total_seconds()
        if delta <= 0:
            # Out-of-order or duplicate.  Do *not* update state — keeping
            # the last in-order timestamp ensures the next forward-moving
            # bar is still compared against a real prev rather than this
            # stale one (which would otherwise produce a false large-gap
            # warning).
            return None
        # In-order bar: advance state regardless of whether the gap rule
        # fires (so consecutive over-threshold gaps each produce a single
        # warning rather than the same one chained against an old prev).
        self._last_ts[symbol] = cur
        if delta <= self._threshold * self._frequency_seconds:
            return None
        return f"data_quality:live_gap:{symbol}"


__all__ = [
    "DataIntegrityError",
    "DataQualityReport",
    "LiveGapMonitor",
    "SymbolDataQualityReport",
    "validate_market_data",
]
