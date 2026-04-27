"""Unit tests for the WSJF and RICE scoring functions."""

from __future__ import annotations

import pytest

from product_delivery.scoring import (
    RICEInputs,
    WSJFInputs,
    rice_score,
    wsjf_score,
)

# ---------------------------------------------------------------------------
# WSJF
# ---------------------------------------------------------------------------


def test_wsjf_basic_division() -> None:
    score = wsjf_score(
        WSJFInputs(
            user_business_value=8,
            time_criticality=5,
            risk_reduction_or_opportunity_enablement=2,
            job_size=5,
        )
    )
    # Cost of delay = 15, job size 5 → 3.0
    assert score == pytest.approx(3.0)


def test_wsjf_zero_job_size_treated_as_one() -> None:
    score = wsjf_score(
        WSJFInputs(
            user_business_value=4,
            time_criticality=2,
            risk_reduction_or_opportunity_enablement=1,
            job_size=0,
        )
    )
    assert score == pytest.approx(7.0)


def test_wsjf_negative_components_floor_at_zero() -> None:
    score = wsjf_score(
        WSJFInputs(
            user_business_value=-3,
            time_criticality=4,
            risk_reduction_or_opportunity_enablement=1,
            job_size=5,
        )
    )
    # value clamps to 0 → cost of delay 5 / 5 = 1.0
    assert score == pytest.approx(1.0)


def test_wsjf_rounded_to_four_dp() -> None:
    score = wsjf_score(
        WSJFInputs(
            user_business_value=1,
            time_criticality=1,
            risk_reduction_or_opportunity_enablement=1,
            job_size=3,
        )
    )
    # 3 / 3 = 1.0 — but check rounding contract holds for an awkward case
    score2 = wsjf_score(
        WSJFInputs(
            user_business_value=1,
            time_criticality=1,
            risk_reduction_or_opportunity_enablement=0,
            job_size=3,
        )
    )
    assert score == pytest.approx(1.0)
    assert score2 == pytest.approx(round(2 / 3, 4))


# ---------------------------------------------------------------------------
# RICE
# ---------------------------------------------------------------------------


def test_rice_basic_formula() -> None:
    # (1000 * 2 * 0.8) / 4 = 400.0
    score = rice_score(RICEInputs(reach=1000, impact=2, confidence=0.8, effort=4))
    assert score == pytest.approx(400.0)


def test_rice_confidence_clamped_above_one() -> None:
    score = rice_score(RICEInputs(reach=100, impact=1, confidence=60, effort=5))
    # confidence clamps to 1.0 → (100 * 1 * 1) / 5 = 20.0 (not 1200)
    assert score == pytest.approx(20.0)


def test_rice_zero_effort_treated_as_one() -> None:
    score = rice_score(RICEInputs(reach=10, impact=1, confidence=1, effort=0))
    assert score == pytest.approx(10.0)


def test_rice_negative_reach_floors_at_zero() -> None:
    score = rice_score(RICEInputs(reach=-50, impact=1, confidence=1, effort=2))
    assert score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Finite-output guarantees
# ---------------------------------------------------------------------------


def test_wsjf_overflow_clamps_to_finite_max() -> None:
    """Caller-supplied finite inputs that overflow during arithmetic must
    not return ``inf`` — Starlette's JSON encoder rejects non-finite
    floats and would raise during response serialisation otherwise.
    """
    import math
    import sys

    # cost_of_delay = 3 × max ≈ ±inf when summed; result must be finite.
    huge = sys.float_info.max
    score = wsjf_score(
        WSJFInputs(
            user_business_value=huge,
            time_criticality=huge,
            risk_reduction_or_opportunity_enablement=huge,
            job_size=1.0,
        )
    )
    assert math.isfinite(score)
    assert score == pytest.approx(sys.float_info.max)


def test_rice_overflow_clamps_to_finite_max() -> None:
    import math
    import sys

    huge = sys.float_info.max
    score = rice_score(RICEInputs(reach=huge, impact=huge, confidence=1.0, effort=1.0))
    assert math.isfinite(score)
    assert score == pytest.approx(sys.float_info.max)
