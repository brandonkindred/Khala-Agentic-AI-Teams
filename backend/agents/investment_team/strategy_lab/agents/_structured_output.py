"""Shared "is provider-enforced structured output available" seam.

``design.py``, ``refinement.py``, and ``design_review.py`` each need to know,
before invoking the LLM, whether the active provider supports
provider-enforced schema-conformant decoding — the two-call chain
``provider_supports_structured_output(resolve_provider())``. That chain is
extracted here so it lives in one place instead of being retyped
identically across the three call sites.

Each of the three agent modules re-exports this function under its own
module-level name:

    from ._structured_output import structured_output_available as _structured_output_available

rather than calling ``_structured_output.structured_output_available()``
directly at each call site. This preserves the per-module test seam the
original duplication existed for: tests patch
``design_mod._structured_output_available``,
``refinement_mod._structured_output_available``, and
``design_review_mod._structured_output_available`` independently — including
cases where design.py's self-review path and design_review.py's own agent
must be forced onto different branches within the same test. Because
``monkeypatch.setattr(module, "_structured_output_available", fn)`` rebinds
the name directly in the target module's namespace, patching one module's
re-exported name never affects another's.

Preconditions: none.
Postconditions: synchronous, no network call, never raises.
"""

from __future__ import annotations

from llm_service import provider_supports_structured_output
from llm_service.config import resolve_provider


def structured_output_available() -> bool:
    """Whether the active LLM provider supports provider-enforced schema-conformant decoding.

    Preconditions: none.
    Postconditions: synchronous, no network call, never raises.
    """
    return provider_supports_structured_output(resolve_provider())
