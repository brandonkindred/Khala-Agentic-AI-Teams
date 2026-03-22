"""Remediation tools for accessibility audits."""

from .generate_regression_checks import (
    GenerateRegressionChecksInput,
    GenerateRegressionChecksOutput,
    generate_regression_checks,
)
from .suggest_fix import SuggestFixInput, SuggestFixOutput, suggest_fix

__all__ = [
    "suggest_fix",
    "SuggestFixInput",
    "SuggestFixOutput",
    "generate_regression_checks",
    "GenerateRegressionChecksInput",
    "GenerateRegressionChecksOutput",
]
