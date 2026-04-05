"""LlmToolsService — list tools, get detail, list operations."""

from __future__ import annotations

from typing import Dict, List, Optional

from agent_llm_tools_service.adapters.base import LlmToolAdapter
from agent_llm_tools_service.models import (
    OperationDetail,
    ToolDetail,
    ToolDocumentation,
    ToolSummary,
)
from agent_llm_tools_service.registry import get_default_registry


class LlmToolNotFoundError(KeyError):
    """Raised when ``tool_id`` is not registered."""


class LlmToolsService:
    """Discovery API for LLM-invokable tools (metadata only; no remote execution)."""

    def __init__(self, registry: Optional[Dict[str, LlmToolAdapter]] = None) -> None:
        self._registry = registry if registry is not None else get_default_registry()

    def list_tools(self) -> List[ToolSummary]:
        return [a.summarize() for a in self._registry.values()]

    def get_tool(self, tool_id: str) -> ToolDetail:
        adapter = self._registry.get(tool_id)
        if adapter is None:
            raise LlmToolNotFoundError(tool_id)
        return adapter.detail()

    def get_documentation(self, tool_id: str) -> ToolDocumentation:
        """Tool-wide documentation links and man hints (same object embedded in ``ToolDetail``)."""
        adapter = self._registry.get(tool_id)
        if adapter is None:
            raise LlmToolNotFoundError(tool_id)
        return adapter.documentation()

    def list_operations(self, tool_id: str) -> List[OperationDetail]:
        adapter = self._registry.get(tool_id)
        if adapter is None:
            raise LlmToolNotFoundError(tool_id)
        return adapter.list_operations()
