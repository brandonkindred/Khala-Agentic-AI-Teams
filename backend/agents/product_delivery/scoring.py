"""WSJF and RICE scoring — pure functions, no I/O.

Both formulas are well-established product-prioritisation heuristics:

* **WSJF** (SAFe): ``Cost of Delay / Job Size`` where Cost of Delay is
  ``user_business_value + time_criticality + risk_reduction_or_opportunity_enablement``.
  Higher is better. Job size of zero is treated as 1 to avoid division by
  zero; callers should guard against missing estimates upstream.

* **RICE** (Intercom): ``(reach * impact * confidence) / effort``.
  ``confidence`` is a 0..1 multiplier (60% → 0.6). Effort of zero is
  treated as 1 for the same reason as WSJF.

Each function returns a finite ``float`` rounded to four decimals so the
persisted ``DOUBLE PRECISION`` column is stable across re-grooms. If
arithmetic on caller-supplied finite inputs would overflow (e.g. very
large ``reach * impact`` in RICE), the result is clamped to a finite
sentinel (``sys.float_info.max``) before rounding rather than returned
as ``inf`` — non-finite scores break Starlette's JSON encoder when
serialised back through ``/backlog`` or ``/groom``.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass

_FINITE_MAX = sys.float_info.max


def _finite_round(value: float) -> float:
    """Round to 4 dp; clamp ``±inf`` to ``±sys.float_info.max`` first.

    Always returns a finite float, so persistence and JSON serialisation
    downstream can't trip on a non-finite score.
    """
    if not math.isfinite(value):
        # NaN propagates through arithmetic too; treat it like +inf so
        # the caller still gets a deterministic finite number.
        if math.isnan(value) or value > 0:
            value = _FINITE_MAX
        else:
            value = -_FINITE_MAX
    return round(value, 4)


@dataclass(frozen=True)
class WSJFInputs:
    user_business_value: float
    time_criticality: float
    risk_reduction_or_opportunity_enablement: float
    job_size: float


@dataclass(frozen=True)
class RICEInputs:
    reach: float
    impact: float
    confidence: float
    effort: float


def wsjf_score(inputs: WSJFInputs) -> float:
    """Cost of Delay divided by Job Size. Higher is better."""
    cost_of_delay = (
        max(0.0, inputs.user_business_value)
        + max(0.0, inputs.time_criticality)
        + max(0.0, inputs.risk_reduction_or_opportunity_enablement)
    )
    job_size = inputs.job_size if inputs.job_size > 0 else 1.0
    return _finite_round(cost_of_delay / job_size)


def rice_score(inputs: RICEInputs) -> float:
    """(reach * impact * confidence) / effort. Higher is better.

    ``confidence`` is expected as a 0..1 multiplier; values outside that
    range are clamped so a stray "60" doesn't blow up the score by 100x.
    """
    confidence = min(1.0, max(0.0, inputs.confidence))
    effort = inputs.effort if inputs.effort > 0 else 1.0
    return _finite_round((max(0.0, inputs.reach) * max(0.0, inputs.impact) * confidence) / effort)
