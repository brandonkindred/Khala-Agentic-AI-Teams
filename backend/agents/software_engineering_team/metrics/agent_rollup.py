"""Result shape for the per-``agent_key``/per-``phase`` cost, token, and latency rollup.

Defines only the data shape the rollup computation (a later step) will fill in and
that consumers (model-tiering, cache-breakpoint adoption work) will read. No
computation lives here — grouping over ``se_agent_traces`` rows is a separate, pure
function added later, mirroring how :mod:`dora`'s ``DoraMetrics`` dataclass predates
and is filled in by ``compute_from_events``.

**Grouping keys** — three views are reported, not a subset:

- ``by_agent`` — one :class:`CallRollup` per ``agent_key``, ignoring phase.
- ``by_phase`` — one :class:`CallRollup` per ``phase``, ignoring agent_key.
- ``by_agent_phase`` — ``by_agent_phase[agent_key][phase]``, the full cross product.
  A nested ``dict[str, dict[str, CallRollup]]`` is used rather than a tuple key
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

from dataclasses import asdict, dataclass, field
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
    """

    window_days: float
    computed_at: str
    by_agent: dict[str, CallRollup] = field(default_factory=dict)
    by_phase: dict[str, CallRollup] = field(default_factory=dict)
    by_agent_phase: dict[str, dict[str, CallRollup]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the rollup as plain dicts (via ``asdict``), JSON-serializable end to end."""
        return asdict(self)


__all__ = ["CallRollup", "AgentRollupMetrics"]
