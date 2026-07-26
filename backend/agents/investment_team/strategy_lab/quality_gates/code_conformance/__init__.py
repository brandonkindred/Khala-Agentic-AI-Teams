"""Deterministic spec→code conformance gate package.

This package collects the ``code_conformance`` gate: the free-function
AST-analysis helpers live in :mod:`.ast_helpers`; the
:class:`.gate.CodeConformanceGate` itself — the ~560-line class with its
nine per-check methods (eight ``_check_*`` checks plus one ``_note_*``
info-only note) — lives in :mod:`.gate`. See :mod:`.gate` for the gate's
full behavior and scope documentation.

Callers import directly from the owning submodule (``.gate`` or
``.ast_helpers``) rather than from this package.
"""
