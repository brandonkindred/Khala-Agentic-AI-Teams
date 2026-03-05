"""Remediation tools for accessibility audits."""

from .suggest_fix import suggest_fix, SuggestFixInput, SuggestFixOutput
from .generate_regression_checks import (
    generate_regression_checks,
    GenerateRegressionChecksInput,
    GenerateRegressionChecksOutput,
)

__all__ = [
    "suggest_fix",
    "SuggestFixInput",
    "SuggestFixOutput",
    "generate_regression_checks",
    "GenerateRegressionChecksInput",
    "GenerateRegressionChecksOutput",
]
