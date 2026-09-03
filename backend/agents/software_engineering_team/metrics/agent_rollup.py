"""Result shape for the per-``agent_key``/per-``phase`` cost, token, and latency rollup.

Defines the data shape the rollup computation fills in and that consumers
(model-tiering, cache-breakpoint adoption work) will read. Grouping over
``se_agent_traces`` rows is done by the pure :func:`compute_from_traces`, mirroring how
:mod:`dora` keeps its ``DoraMetrics`` dataclass and pure ``compute_from_events`` in one
module.

**Grouping keys** — three views are reported, not a subset:

- ``by_agent`` — one :class:`CallRollup` per ``agent_key``, ignoring phase.
- ``by_phase`` — one :class:`CallRollup` per ``phase``, ignoring agent_key.
- ``by_agent_phase`` — ``by_agent_phase[agent_key][phase]``, one entry per distinct
  (agent_key, phase) pair observed; combinations that never co-occur are absent, not
  an empty ``CallRollup``. A nested ``dict[str, dict[str, CallRollup]]`` is used rather than a tuple key
  (breaks ``asdict()``/JSON serialization) or a composite string key like
  ``"agent::phase"`` (risks delimiter collisions with real identifiers). Nesting is
  a direct extension of ``dora.py``'s existing ``dict[str, ...]`` idiom and stays
  JSON-serializable end to end via ``asdict()``.

All three are needed: per-agent and per-phase views answer "who/what is expensive"
on their own, but a given agent's token and cache profile can differ materially
across phases, so tiering or cache-breakpoint decisions need the pair.

**Cache-read ratio** — per :class:`CallRollup`, defined as::

    cache_read_ratio = cache_read_tokens / (cache_read_tokens + cache_creation_tokens + input_tokens)

summed across every call in the group *before* dividing (never averaged per-call,
which would equal-weight a 10-token and a 100,000-token call and misrepresent the
group). ``input_tokens`` here is Anthropic's fresh, non-cached prompt tokens —
already a distinct bucket from ``cache_read_tokens``/``cache_creation_tokens`` in the
provider's usage object (see ``llm_service/clients/claude.py``), so the three sum to
the group's total prompt-side tokens processed with no double-counting.

Rejected alternatives, for the record:

- Dividing by total tokens (input + output) is wrong: output tokens are never
  cache-eligible and would dilute the ratio.
- Excluding ``cache_creation_tokens`` from the denominator overstates the ratio: a
  cache-creation token is prompt content that was *not* already cached — a genuine
  miss at call time, not a hit.

**``None`` vs. ``0`` — one governing rule.** Counts and sums (``call_count``,
``total_cost_usd``, every ``total_*_tokens`` field) are never ``Optional``: ``0``/
``0.0`` is unambiguous whether summed over zero rows or over rows that happen to sum
to zero. Derived statistics that are undefined without samples (``cache_read_ratio``,
``latency_ms_median``, ``latency_ms_p95``) are ``Optional[float] = None``, used both
when ``call_count == 0`` (no group) and when ``call_count > 0`` but that statistic's
own sample set is empty (e.g. the ratio's denominator is 0 — no prompt-side tokens
processed at all). A ``0.0`` ratio, by contrast, means calls existed, tokens were
processed, and genuinely none were served from cache.

**Latency percentiles** — ``latency_ms_median`` follows :func:`dora._median`'s
pure-Python, no-numpy, "empty sample → ``None``" convention. ``latency_ms_p95`` is
this repo's first percentile precedent (no existing convention to match), defined by
nearest-rank on the sorted sample ``ordered`` of length ``n > 0``::

    rank = max(1, min(n, ceil(0.95 * n)))
    p95 = ordered[rank - 1]

No interpolation between neighboring ranks. At ``n == 1`` this returns the single
sample (matching median's ``n == 1`` behavior); at ``n == 2`` it returns the larger
of the two, since p95 of two samples is intuitively "the worse one." An empty sample
(``n == 0``) yields ``None``, exactly like the median.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class CallRollup:
    """One grouping bucket's metrics. See the module docstring for exact definitions.

    ``call_count == 0`` marks an empty group: every other field is then ``0``/``0.0``
    or ``None`` per the module docstring's rule. ``total_cache_read_tokens`` and
    ``total_cache_creation_tokens`` are carried alongside the derived
    ``cache_read_ratio`` so the ratio is auditable without recomputing the sums;
    ``latency_ms_sample_count`` is carried alongside the percentiles for the same
    reason.

    These invariants are documented, not enforced here: this dataclass has no
    ``__post_init__`` validation. The future pure computation step is this shape's
    single producer and is responsible for upholding them (and asserting them in
    its tests) — e.g. ``cache_read_ratio`` in ``[0, 1]`` when not ``None``, and
    ``latency_ms_sample_count == 0`` implying both percentiles are ``None``.
    """

    call_count: int = 0
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    cache_read_ratio: Optional[float] = None
    latency_ms_median: Optional[float] = None
    latency_ms_p95: Optional[float] = None
    latency_ms_sample_count: int = 0


@dataclass
class AgentRollupMetrics:
    """Per-``agent_key``/per-``phase`` rollup over a time window. See module docstring.

    ``computed_at`` is an ISO 8601 UTC timestamp (e.g. ``"2026-09-02T00:00:00+00:00"``);
    ``window_days`` is the length, in days, of the rolling window ending at
    ``computed_at`` over which traces were aggregated.

    Like :class:`CallRollup`, these contracts are documented, not enforced: the
    future producer is responsible for emitting ``computed_at`` in ISO 8601 UTC
    and ``window_days > 0``.
    """

    window_days: float
    computed_at: str
    by_agent: dict[str, CallRollup] = field(default_factory=dict)
    by_phase: dict[str, CallRollup] = field(default_factory=dict)
    by_agent_phase: dict[str, dict[str, CallRollup]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the rollup as plain dicts (via ``asdict``), JSON-serializable end to end."""
        return asdict(self)


def _median(values: list[float]) -> Optional[float]:
    """Median of ``values``; ``None`` for an empty list.

    Duplicates :func:`dora._median`'s algorithm rather than importing it — that helper
    is module-private, and this module stays self-contained like ``dora.py`` is.

    Preconditions:
        - Every element of ``values`` is finite (no NaN/inf).
    Postconditions:
        - Returns ``None`` iff ``values`` is empty.
        - Otherwise returns the sorted-list midpoint, averaging the two middle
          elements when ``len(values)`` is even.
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _p95(values: list[float]) -> Optional[float]:
    """95th percentile of ``values`` by nearest-rank; ``None`` for an empty list.

    Implements this module's documented convention: no interpolation between
    neighboring ranks.

    Preconditions:
        - Every element of ``values`` is finite (no NaN/inf).
    Postconditions:
        - Returns ``None`` iff ``values`` is empty.
        - Otherwise returns ``sorted(values)[rank - 1]`` where
          ``rank = max(1, min(n, ceil(0.95 * n)))`` and ``n = len(values)`` — the single
          sample at ``n == 1``, the larger of two at ``n == 2``.
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    rank = max(1, min(n, math.ceil(0.95 * n)))
    return ordered[rank - 1]


def _rollup_for_group(rows: list[dict[str, Any]]) -> CallRollup:
    """Aggregate one grouping bucket's rows into a :class:`CallRollup`.

    Preconditions:
        - Every row carries ``cost_usd``, ``input_tokens``, ``output_tokens``,
          ``cache_read_tokens``, ``cache_creation_tokens``, and ``latency_ms`` —
          the numeric ``se_agent_traces`` columns — with non-negative values.
    Postconditions:
        - ``call_count == len(rows)``; every row contributes exactly one latency
          sample, so ``latency_ms_sample_count == call_count``.
        - ``cache_read_ratio`` is ``None`` iff the summed cache-read + cache-creation +
          input tokens are all zero; otherwise it is the ratio defined in the module
          docstring, rounded to 4 decimal places, in ``[0, 1]``.
        - ``latency_ms_median``/``latency_ms_p95`` follow :func:`_median`/:func:`_p95`.
    """
    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_creation = 0
    latencies: list[float] = []

    for row in rows:
        total_cost += float(row.get("cost_usd") or 0.0)
        total_input += int(row.get("input_tokens") or 0)
        total_output += int(row.get("output_tokens") or 0)
        total_cache_read += int(row.get("cache_read_tokens") or 0)
        total_cache_creation += int(row.get("cache_creation_tokens") or 0)
        latencies.append(float(row.get("latency_ms") or 0))

    denom = total_cache_read + total_cache_creation + total_input
    cache_read_ratio = round(total_cache_read / denom, 4) if denom > 0 else None

    return CallRollup(
        call_count=len(rows),
        total_cost_usd=round(total_cost, 6),
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        total_cache_read_tokens=total_cache_read,
        total_cache_creation_tokens=total_cache_creation,
        cache_read_ratio=cache_read_ratio,
        latency_ms_median=_median(latencies),
        latency_ms_p95=_p95(latencies),
        latency_ms_sample_count=len(latencies),
    )


def compute_from_traces(rows: list[dict[str, Any]], window_days: float) -> AgentRollupMetrics:
    """Compute the per-``agent_key``/per-``phase`` rollup from a list of trace rows.

    A pure computation over already-fetched ``se_agent_traces``-shaped row dicts — it
    does not mutate ``rows`` and has no database dependency, mirroring
    :func:`dora.compute_from_events`. The sole non-deterministic output is
    ``computed_at``, stamped from the UTC wall clock at call time; every other field is
    a deterministic function of ``rows`` and ``window_days``. ``window_days`` is not
    used to filter rows (callers are expected to have already scoped ``rows`` to the
    window); it is only carried onto the returned :class:`AgentRollupMetrics`.

    Preconditions:
        - ``window_days > 0``.
        - Each element of ``rows`` is a dict shaped like an ``se_agent_traces`` record:
          ``agent_key``/``phase`` (strings, ``""`` permitted and treated as a real
          group) plus the numeric fields :func:`_rollup_for_group` requires.
    Postconditions:
        - Raises ``ValueError`` if ``window_days <= 0``; never raises for an empty
          ``rows`` list, returning an ``AgentRollupMetrics`` with empty
          ``by_agent``/``by_phase``/``by_agent_phase`` dicts instead.
        - ``by_agent`` has one entry per distinct ``agent_key`` seen in ``rows``;
          ``by_phase`` one per distinct ``phase``; ``by_agent_phase`` has one entry per
          distinct (agent_key, phase) pair seen — combinations that never co-occur are
          absent, not an empty ``CallRollup``. No observed group is dropped, including
          the ``""`` group.
        - Keys within each of the three dicts (and within each inner ``by_agent_phase``
          dict) are sorted ascending, so the result is deterministic for a fixed row
          *set* regardless of input row order — not just for a fixed row sequence.
        - Every ``CallRollup`` it produces satisfies the invariants documented on that
          class and this module (``None`` vs. ``0`` rule, ratio/percentile math).
    """
    if window_days <= 0:
        raise ValueError("window_days must be > 0")

    by_agent_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_phase_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_agent_phase_rows: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for row in rows:
        agent_key = row.get("agent_key") or ""
        phase = row.get("phase") or ""
        by_agent_rows[agent_key].append(row)
        by_phase_rows[phase].append(row)
        by_agent_phase_rows[agent_key][phase].append(row)

    metrics = AgentRollupMetrics(
        window_days=window_days,
        computed_at=datetime.now(tz=timezone.utc).isoformat(),
    )
    metrics.by_agent = {k: _rollup_for_group(by_agent_rows[k]) for k in sorted(by_agent_rows)}
    metrics.by_phase = {k: _rollup_for_group(by_phase_rows[k]) for k in sorted(by_phase_rows)}
    metrics.by_agent_phase = {
        agent_key: {
            phase: _rollup_for_group(by_agent_phase_rows[agent_key][phase])
            for phase in sorted(by_agent_phase_rows[agent_key])
        }
        for agent_key in sorted(by_agent_phase_rows)
    }
    return metrics


__all__ = ["CallRollup", "AgentRollupMetrics", "compute_from_traces"]
