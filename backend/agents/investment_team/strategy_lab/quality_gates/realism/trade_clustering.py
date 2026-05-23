"""Trade clustering realism gate.

A strategy whose entire trade ledger fires inside a single fold or
calendar quarter is not really a "5-year strategy" — it's a "this one
vol spike paid off" anecdote that will not recur. The realism cycle
catches this by combining two deterministic signals:

* **Calendar-quarter concentration** — the share of trades arriving in
  any single ``YYYY-Qn`` bucket. A 70%+ share in a single quarter is
  the obvious cluster.
* **Lag-1 autocorrelation of inter-arrival times** — significant
  positive autocorrelation means trades arrive in bursts (one trade
  predicts the next will come soon after), which is the statistical
  fingerprint of clustering even when the calendar-quarter view doesn't
  cross the 70% threshold.

Severity:

* **critical** — both signals fire (concentrated quarter AND
  significant lag-1 autocorrelation). The strategy is verifiably
  cluster-dependent.
* **warning** — only one signal fires. Borderline pattern worth
  flagging but not vetoing outright.
* **info** — neither signal fires (well-spread arrivals).

Skipped when fewer than 10 trades are present — sample is too small
for either signal to be informative.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import ClassVar, List, Optional, Sequence

from ....models import TradeRecord
from ..models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "trade_clustering_realism"

# Minimum trade count for either signal to be evaluated. Below this,
# the autocorrelation estimate is too noisy and the quarter-share
# breakdown trivially clusters.
_MIN_TRADES_FOR_EVAL = 10

# Calendar-quarter dominance threshold. 70% mirrors the issue spec.
_QUARTER_DOMINANCE_THRESHOLD = 0.70

# Lag-1 Ljung-Box critical value at p=0.05, df=1, hard-coded so the
# module doesn't pull in scipy. ``Q = n * (n+2) * rho^2 / (n-1)``; reject
# the independence null when ``Q > 3.84``.
_LB_CRITICAL_LAG1: float = 3.84


class TradeClusteringGate(GateResultsMixin):
    """Verification-phase gate over trade-arrival temporal distribution.

    Contract:
      Pre: ``trades`` is a list of :class:`TradeRecord`. Each trade's
      ``entry_date`` is the canonical ``YYYY-MM-DD`` string the
      simulator emits (intraday timestamps in ISO form are also
      tolerated and truncated to the calendar date for clustering math).
      Post: returns exactly one :class:`QualityGateResult` tagged with
      the caller's ``phase``. ``critical``/``warning`` flag clustering;
      ``info`` covers the no-clustering and skipped cases.
      Invariants: deterministic; no I/O; never returns an empty list.
    """

    GATE: ClassVar[str] = GATE

    def check(
        self,
        trades: List[TradeRecord],
        *,
        phase: StrategyLabPhase = "verification",
    ) -> List[QualityGateResult]:
        with self._using_phase(phase):
            if len(trades) < _MIN_TRADES_FOR_EVAL:
                return [
                    self._info(
                        f"Trade clustering check skipped: {len(trades)} trade(s) "
                        f"below the {_MIN_TRADES_FOR_EVAL}-trade minimum sample."
                    )
                ]

            entry_dates = _entry_dates(trades)
            if len(entry_dates) < _MIN_TRADES_FOR_EVAL:
                return [
                    self._info(
                        "Trade clustering check skipped: insufficient parseable "
                        "entry_date strings on the ledger."
                    )
                ]

            max_quarter_share, dominant_quarter = _max_calendar_quarter_share(entry_dates)
            quarter_clusters = max_quarter_share >= _QUARTER_DOMINANCE_THRESHOLD

            lag1_rho = _lag1_autocorrelation(entry_dates)
            lb_q = _ljung_box_q_lag1(lag1_rho, len(entry_dates))
            autocorr_clusters = lb_q is not None and lb_q > _LB_CRITICAL_LAG1

            return [
                self._verdict(
                    quarter_clusters=quarter_clusters,
                    autocorr_clusters=autocorr_clusters,
                    max_quarter_share=max_quarter_share,
                    dominant_quarter=dominant_quarter,
                    lag1_rho=lag1_rho,
                    lb_q=lb_q,
                    total=len(entry_dates),
                )
            ]

    def _verdict(
        self,
        *,
        quarter_clusters: bool,
        autocorr_clusters: bool,
        max_quarter_share: float,
        dominant_quarter: Optional[str],
        lag1_rho: Optional[float],
        lb_q: Optional[float],
        total: int,
    ) -> QualityGateResult:
        quarter_fragment = (
            f"{max_quarter_share:.0%} of {total} trades clustered in {dominant_quarter}"
            if dominant_quarter is not None
            else f"max-quarter share {max_quarter_share:.0%}"
        )
        autocorr_fragment = (
            f"lag-1 autocorrelation {lag1_rho:+.2f} (Ljung-Box Q={lb_q:.2f})"
            if lag1_rho is not None and lb_q is not None
            else "lag-1 autocorrelation unavailable"
        )

        if quarter_clusters and autocorr_clusters:
            return self._critical(
                "Trade arrivals cluster in time: "
                f"{quarter_fragment}; {autocorr_fragment}. The strategy's "
                "edge is concentrated in a narrow window and is unlikely to "
                "recur outside it."
            )
        if quarter_clusters:
            return self._warning(
                "Trade arrivals concentrate by calendar quarter: "
                f"{quarter_fragment}. Inter-arrival autocorrelation is not "
                "elevated, so the run may still be representative; review the "
                "regime conditions in that quarter."
            )
        if autocorr_clusters:
            return self._warning(
                "Trade arrivals show significant burst pattern: "
                f"{autocorr_fragment}. No single calendar quarter dominates, "
                "but inter-arrival times are correlated."
            )
        return self._info(f"Trade arrivals well-spread: {quarter_fragment}; {autocorr_fragment}.")


def _entry_dates(trades: Sequence[TradeRecord]) -> List[date]:
    """Convert each trade's ``entry_date`` to a :class:`date`.

    Preconditions:
      - ``trades`` is iterable; each entry has an ``entry_date`` string.
    Postconditions:
      - Returns dates sorted chronologically.
      - Trades whose ``entry_date`` is empty or unparseable are skipped.
    """
    out: List[date] = []
    for t in trades:
        d = _parse_date(t.entry_date)
        if d is not None:
            out.append(d)
    out.sort()
    return out


def _parse_date(value: str) -> Optional[date]:
    """Parse ``YYYY-MM-DD`` or a full ISO datetime; return ``None`` on bad
    input."""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(value).date()
        except (TypeError, ValueError):
            return None


def _calendar_quarter(d: date) -> str:
    """Return ``YYYY-Qn`` (e.g., ``"2024-Q2"``) for a calendar date."""
    quarter = (d.month - 1) // 3 + 1
    return f"{d.year}-Q{quarter}"


def _max_calendar_quarter_share(dates: Sequence[date]) -> tuple:
    """Return the highest share of trades in any single calendar quarter,
    along with the dominant quarter label.

    Postconditions:
      - Returns ``(share, label)``; ``label`` is ``None`` when the input
        is empty.
      - ``share`` is in ``[0.0, 1.0]``.
    """
    if not dates:
        return 0.0, None
    counts: dict = {}
    for d in dates:
        q = _calendar_quarter(d)
        counts[q] = counts.get(q, 0) + 1
    dominant = max(counts.items(), key=lambda kv: kv[1])
    return dominant[1] / len(dates), dominant[0]


def _lag1_autocorrelation(dates: Sequence[date]) -> Optional[float]:
    """Sample lag-1 autocorrelation of inter-arrival days.

    Preconditions:
      - ``dates`` is chronologically sorted; ``len(dates) >= 3`` is
        required to compute at least two inter-arrival intervals AND a
        lag-1 pair.
    Postconditions:
      - Returns ``None`` when the series is too short, when every
        interval is identical (variance zero — autocorrelation
        undefined), or when fewer than 2 paired observations remain.
      - Otherwise returns ``rho`` in ``[-1, 1]``.
    """
    if len(dates) < 3:
        return None
    intervals = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    if len(intervals) < 2:
        return None
    mean = sum(intervals) / len(intervals)
    centered = [x - mean for x in intervals]
    denominator = sum(c * c for c in centered)
    if denominator == 0:
        return None
    numerator = sum(centered[i] * centered[i - 1] for i in range(1, len(centered)))
    return numerator / denominator


def _ljung_box_q_lag1(rho: Optional[float], n: int) -> Optional[float]:
    """Ljung-Box Q statistic at lag 1 only.

    Preconditions:
      - ``n`` is the count of inter-arrival observations (one less than
        the number of dates); ``rho`` is the lag-1 autocorrelation of
        those observations.
    Postconditions:
      - Returns ``None`` when ``rho`` is ``None`` or ``n <= 1``.
      - Otherwise returns ``Q = n * (n + 2) * rho^2 / (n - 1)``. Reject
        independence at p=0.05 when ``Q > 3.84``.
    """
    if rho is None or n <= 1:
        return None
    return n * (n + 2) * (rho * rho) / (n - 1)


__all__ = ["GATE", "TradeClusteringGate"]
