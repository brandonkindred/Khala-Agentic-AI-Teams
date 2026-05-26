"""Error types for the SPEC-007 guardrail pipeline."""

from __future__ import annotations


class GuardrailError(Exception):
    """Base for guardrail failures."""


class GuardrailNotImplementedError(GuardrailError, NotImplementedError):
    """W1 scaffolding stub — replaced by W2 ``checker.check_recommendation``."""


class InteractionDataError(GuardrailError):
    """Raised when ``interactions.yaml`` fails schema validation."""
