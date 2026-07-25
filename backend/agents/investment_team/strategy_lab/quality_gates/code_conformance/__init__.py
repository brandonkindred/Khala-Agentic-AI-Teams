"""Deterministic spec→code conformance gate package.

This package collects the ``code_conformance`` gate: the free-function
AST-analysis helpers live in :mod:`.ast_helpers`; the
:class:`.gate.CodeConformanceGate` itself — the ~560-line class with its
nine per-check methods (eight ``_check_*`` checks plus one ``_note_*``
info-only note) — lives in :mod:`.gate`. See :mod:`.gate` for the gate's
full behavior and scope documentation.
"""

from .ast_helpers import (
    _BOLLINGER_BASE_BANDS,  # noqa: F401 (re-exported for downstream imports)
    _BOLLINGER_DERIVED_BANDS,  # noqa: F401 (re-exported for downstream imports)
    _POSITION_SNAPSHOT_ATTRS,  # noqa: F401 (re-exported for downstream imports)
)
from .gate import GATE, CodeConformanceGate

__all__ = [
    "CodeConformanceGate",
    "GATE",
    "_BOLLINGER_BASE_BANDS",
    "_BOLLINGER_DERIVED_BANDS",
    "_POSITION_SNAPSHOT_ATTRS",
]
