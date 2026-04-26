"""WSJF and RICE scoring — pure functions, no I/O.

Both formulas are well-established product-prioritisation heuristics:

* **WSJF** (SAFe): ``Cost of Delay / Job Size`` where Cost of Delay is
  ``user_business_value + time_criticality + risk_reduction_or_opportunity_enablement``.
  Higher is better. Job size of zero is treated as 1 to avoid division by
  zero; callers should guard against missing estimates upstream.

* **RICE** (Intercom): ``(reach * impact * confidence) / effort``.
  ``confidence`` is a 0..1 multiplier (60% → 0.6). Effort of zero is
  treated as 1 for the same reason as WSJF.

Each function returns a ``float`` rounded to four decimals so the
persisted ``DOUBLE PRECISION`` column is stable across re-grooms.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    return round(cost_of_delay / job_size, 4)


def rice_score(inputs: RICEInputs) -> float:
    """(reach * impact * confidence) / effort. Higher is better.

    ``confidence`` is expected as a 0..1 multiplier; values outside that
    range are clamped so a stray "60" doesn't blow up the score by 100x.
    """
    confidence = min(1.0, max(0.0, inputs.confidence))
    effort = inputs.effort if inputs.effort > 0 else 1.0
    return round(
        (max(0.0, inputs.reach) * max(0.0, inputs.impact) * confidence) / effort,
        4,
    )
