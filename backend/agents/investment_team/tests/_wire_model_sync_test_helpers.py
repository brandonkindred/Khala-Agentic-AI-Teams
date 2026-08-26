"""Reusable drift-guard for wire-model <-> persisted-model field pairs
(see #7279 / #7266).

Not a test module — the leading underscore keeps pytest from collecting it.
Imported from ``test_strategy_lab_*.py`` files that need to assert a wire
schema hasn't grown a field its persisted counterpart doesn't know about.
"""

from __future__ import annotations

from typing import Iterable

from pydantic import BaseModel


def assert_wire_fields_subset(
    wire_model: type[BaseModel],
    persisted_model: type[BaseModel],
    exclusions: Iterable[str] = (),
) -> None:
    """Assert every field on ``wire_model`` also exists on ``persisted_model``.

    Guards against wire-model field drift: a field added to a wire schema
    without being threaded onto its persisted counterpart would silently
    fail to round-trip once emitted. One-directional subset check, not
    equality — persisted-only fields (audit metadata, internal versioning)
    are never flagged.

    Preconditions:
        - ``wire_model`` and ``persisted_model`` are pydantic ``BaseModel``
          subclasses (not instances).
        - ``exclusions`` is an iterable of field names known to be
          legitimately wire-only; a name that matches nothing is a no-op.
    Postconditions:
        - Returns ``None`` when every field of ``wire_model``, minus
          ``exclusions``, is present on ``persisted_model``.
        - Otherwise raises ``AssertionError`` naming the offending field(s)
          (sorted) and both model class names.
    """
    offending = sorted(
        set(wire_model.model_fields) - set(persisted_model.model_fields) - set(exclusions)
    )
    assert not offending, (
        f"{wire_model.__name__} declares field(s) {offending} that "
        f"{persisted_model.__name__} does not have. Either add the field to "
        f"{persisted_model.__name__} or add it to `exclusions=(...)` with a reason."
    )
