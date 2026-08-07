"""Shared AST name / attribute extraction helpers."""

from __future__ import annotations

import ast
from typing import Optional


def name_or_attr(node: Optional[ast.AST]) -> Optional[str]:
    """Return ``Name.id`` or ``Attribute.attr``, else ``None``.

    Preconditions:
      - ``node`` is an AST node or ``None``.
    Postconditions:
      - Returns the identifier string for ``ast.Name`` / ``ast.Attribute``.
      - Returns ``None`` for every other node type and for ``None``.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def call_name(node: ast.Call) -> str:
    """Extract the callable name from ``node`` without case folding.

    Preconditions:
      - ``node`` is an ``ast.Call``.
    Postconditions:
      - Returns ``Name.id`` or ``Attribute.attr`` of ``node.func``.
      - Returns ``""`` when ``node.func`` is neither (matches legacy
        ``code_safety_ast._get_call_name``).
    """
    return name_or_attr(node.func) or ""


def func_name(func: ast.expr) -> Optional[str]:
    """Extract and lowercase a callable expression name.

    Preconditions:
      - ``func`` is an ``ast.expr`` (typically ``Call.func``).
    Postconditions:
      - Returns lowercased ``Name.id`` / ``Attribute.attr``.
      - Returns ``None`` when neither (matches legacy
        ``coverage_probe.subcond_builder._func_name``).
    """
    raw = name_or_attr(func)
    return raw.lower() if raw is not None else None
