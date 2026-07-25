"""Deterministic spec→code conformance gate package (issue #541).

This package collects the ``code_conformance`` gate: the free-function
AST-analysis helpers live in :mod:`.ast_helpers`; the
:class:`.gate.CodeConformanceGate` itself — the ~560-line class with its
nine ``_check_*`` methods — lives in :mod:`.gate`. See :mod:`.gate` for the
gate's full behavior and scope documentation.
"""

from __future__ import annotations

from .ast_helpers import (
    _BOLLINGER_BASE_BANDS,  # noqa: F401 (re-exported for downstream imports)
    _BOLLINGER_DERIVED_BANDS,  # noqa: F401 (re-exported for downstream imports)
    _POSITION_SNAPSHOT_ATTRS,  # noqa: F401 (re-exported for downstream imports)
)
from .gate import GATE, CodeConformanceGate

__all__ = ["CodeConformanceGate", "GATE"]
