"""
Resolve ``module.path:ClassName`` schema refs into JSON Schema dicts.

Kept in a separate module so the loader has no hard dependency on the ability
to import arbitrary team code. Import failures raise
:class:`SchemaResolutionError` and are translated to 404s at the API layer.
"""

from __future__ import annotations

import importlib
from typing import Any

from pydantic import BaseModel, TypeAdapter


class SchemaResolutionError(Exception):
    """Raised when a schema_ref cannot be imported or converted to JSON schema."""


def resolve_schema(schema_ref: str) -> dict[str, Any]:
    """Import ``schema_ref`` and return its JSON Schema.

    ``schema_ref`` format: ``module.path:Symbol`` where ``Symbol`` is a Pydantic
    model class (preferred) or any type for which ``TypeAdapter`` can produce
    a schema.
    """
    if ":" not in schema_ref:
        raise SchemaResolutionError(
            f"Malformed schema_ref {schema_ref!r}; expected 'module.path:Symbol'."
        )
    module_path, symbol = schema_ref.split(":", 1)
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        raise SchemaResolutionError(f"Cannot import module {module_path!r}: {exc}") from exc
    if not hasattr(module, symbol):
        raise SchemaResolutionError(f"Module {module_path!r} has no attribute {symbol!r}.")
    target = getattr(module, symbol)
    try:
        if isinstance(target, type) and issubclass(target, BaseModel):
            return target.model_json_schema()
        return TypeAdapter(target).json_schema()
    except Exception as exc:
        raise SchemaResolutionError(
            f"Could not build JSON schema for {schema_ref!r}: {exc}"
        ) from exc
