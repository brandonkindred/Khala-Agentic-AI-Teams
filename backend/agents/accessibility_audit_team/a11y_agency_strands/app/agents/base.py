from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


def a11y_phase(*, context: bool = False) -> Callable:
    """Temporary decorator replacing the former local @tool.

    Phase 3 will replace this with ``from strands import tool`` and migrate
    all consumers to proper strands.Agent instances.
    """
    def decorator(func: Callable) -> Callable:
        func._a11y_phase_context_enabled = context  # type: ignore[attr-defined]
        return func

    return decorator


@dataclass(slots=True)
class ToolContext:
    invocation_state: dict[str, Any]
