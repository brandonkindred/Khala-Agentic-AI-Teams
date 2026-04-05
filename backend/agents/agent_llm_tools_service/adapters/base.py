"""Protocol for tool catalog adapters."""

from __future__ import annotations

from typing import Any, List, Protocol

from agent_llm_tools_service.models import (
    OperationDetail,
    ToolDetail,
    ToolDocumentation,
    ToolSummary,
)


class LlmToolAdapter(Protocol):
    """Registers one logical tool (e.g. git) and its OpenAI function operations."""

    @property
    def tool_id(self) -> str: ...

    def summarize(self) -> ToolSummary: ...

    def documentation(self) -> ToolDocumentation: ...

    def detail(self) -> ToolDetail: ...

    def list_operations(self) -> List[OperationDetail]: ...

    def openai_tool_definitions(self) -> List[dict[str, Any]]: ...
