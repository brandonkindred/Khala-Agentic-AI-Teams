"""Unit tests for the Agent Registry Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_platform.registry.models import IOSchema


def test_inline_schema_accepts_a_well_formed_json_schema() -> None:
    IOSchema(inline_schema={"type": "object", "properties": {"q": {"type": "string"}}})


def test_inline_schema_accepts_an_empty_dict() -> None:
    # {} is a valid (unconstrained) JSON Schema.
    IOSchema(inline_schema={})


def test_inline_schema_accepts_none() -> None:
    IOSchema(inline_schema=None)


def test_inline_schema_rejects_a_malformed_json_schema() -> None:
    with pytest.raises(ValidationError, match="inline_schema is not a valid JSON Schema"):
        IOSchema(inline_schema={"type": "not-a-real-json-schema-type"})
