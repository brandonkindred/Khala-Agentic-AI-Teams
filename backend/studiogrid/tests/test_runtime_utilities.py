from __future__ import annotations

import pytest
from studiogrid.runtime.errors import PermissionError, SchemaValidationError
from studiogrid.runtime.router import PhaseRouter
from studiogrid.runtime.tool_factory import ToolFactory
from studiogrid.runtime.validators.schema_validator import validate_envelope, validate_payload


def test_phase_router_returns_tasks_unchanged() -> None:
    router = PhaseRouter()
    tasks = [{"task_id": "t1"}, {"task_id": "t2"}]

    assert router.route("DESIGN", tasks) == tasks


def test_tool_factory_builds_requested_tools_in_order() -> None:
    tools = {"figma": object(), "contrast_check": object()}
    factory = ToolFactory(tools)

    built = factory.build_tools(["contrast_check", "figma"], permissions=["*"])

    assert built == [tools["contrast_check"], tools["figma"]]


def test_tool_factory_rejects_unknown_tool() -> None:
    factory = ToolFactory({"figma": object()})

    with pytest.raises(PermissionError, match="Unknown or forbidden"):
        factory.build_tools(["figma", "slack_notify"], permissions=["*"])


def test_validate_envelope_rejects_non_dict() -> None:
    with pytest.raises(SchemaValidationError, match="Envelope must be an object"):
        validate_envelope("not-a-dict")  # type: ignore[arg-type]


def test_validate_payload_for_review_and_decision_require_fields() -> None:
    validate_payload("REVIEW", {"gate": "g1", "passed": True, "required_fixes": []})
    validate_payload("DECISION", {"title": "Pick", "options": []})

    with pytest.raises(SchemaValidationError, match="Missing required fields"):
        validate_payload("DECISION", {"title": "Pick"})
