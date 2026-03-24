from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


def tool(*, context: bool = False) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._tool_context_enabled = context  # type: ignore[attr-defined]
        return func

    return decorator


@dataclass(slots=True)
class ToolContext:
    invocation_state: dict[str, Any]


@dataclass(slots=True)
class StubAgent:
    name: str
    state: dict[str, Any] = field(default_factory=dict)

    def invoke(self, payload: dict[str, Any], structured_output_model: type[Any]) -> Any:
        return structured_output_model.model_validate(payload)
