"""SPEC-007 guardrail pipeline.

Behaviour is gated by the ``NUTRITION_GUARDRAIL`` env var (off by
default), mirroring the SPEC-006 ``NUTRITION_RESTRICTION_RESOLVER``
pattern.
"""

from __future__ import annotations

import os

from .checker import check_recommendation
from .errors import GuardrailError, GuardrailNotImplementedError, InteractionDataError
from .interactions import (
    EMPTY_POLICY,
    InteractionPolicy,
    get_interaction_policies,
    load_interactions,
)
from .version import GUARDRAIL_VERSION
from .violations import GuardrailResult, Severity, Violation, ViolationReason

_FLAG = "NUTRITION_GUARDRAIL"


def is_guardrail_enabled() -> bool:
    """Read at every call site so env can flip without restart."""
    return os.environ.get(_FLAG, "0") == "1"


__all__ = [
    "EMPTY_POLICY",
    "GUARDRAIL_VERSION",
    "GuardrailError",
    "GuardrailNotImplementedError",
    "GuardrailResult",
    "InteractionDataError",
    "InteractionPolicy",
    "Severity",
    "Violation",
    "ViolationReason",
    "check_recommendation",
    "get_interaction_policies",
    "is_guardrail_enabled",
    "load_interactions",
]
