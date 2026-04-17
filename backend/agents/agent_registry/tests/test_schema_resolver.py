"""Unit tests for the schema_ref resolver."""

from __future__ import annotations

import pytest

from agent_registry.schema_resolver import SchemaResolutionError, resolve_schema


def test_resolve_pydantic_model_returns_json_schema() -> None:
    # Use a well-known model from the registry's own package.
    schema = resolve_schema("agent_registry.models:AgentSummary")
    assert schema["type"] == "object"
    assert "id" in schema["properties"]
    assert "team" in schema["properties"]


def test_resolve_missing_module_raises() -> None:
    with pytest.raises(SchemaResolutionError):
        resolve_schema("does.not.exist:Nothing")


def test_resolve_missing_symbol_raises() -> None:
    with pytest.raises(SchemaResolutionError):
        resolve_schema("agent_registry.models:NoSuchSymbol")


def test_malformed_ref_raises() -> None:
    with pytest.raises(SchemaResolutionError):
        resolve_schema("no_colon_here")


def test_non_pydantic_target_falls_back_to_type_adapter() -> None:
    # Any typed value works via TypeAdapter. Reuse a stdlib type ref via dynamic
    # module to keep the test hermetic.
    import sys
    import types

    mod = types.ModuleType("_agent_registry_test_mod")
    mod.IntList = list[int]
    sys.modules["_agent_registry_test_mod"] = mod
    try:
        schema = resolve_schema("_agent_registry_test_mod:IntList")
        assert schema["type"] == "array"
    finally:
        del sys.modules["_agent_registry_test_mod"]
