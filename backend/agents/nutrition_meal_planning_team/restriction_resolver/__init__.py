"""SPEC-006 profile-side restriction resolver.

Public entry point: :func:`resolve_restrictions`.
"""

from .resolver import FUZZY_THRESHOLD, resolve_restrictions

__all__ = ["FUZZY_THRESHOLD", "resolve_restrictions"]
