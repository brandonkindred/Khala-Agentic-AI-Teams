from __future__ import annotations

from studiogrid.runtime.errors import PermissionError


class ToolFactory:
    def __init__(self, tool_map: dict[str, object]) -> None:
        self.tool_map = tool_map

    def build_tools(self, allowed_tools: list[str], permissions: list[str]) -> list[object]:
        del permissions
        unknown = [tool for tool in allowed_tools if tool not in self.tool_map]
        if unknown:
            raise PermissionError(f"Unknown or forbidden tools requested: {unknown}")
        return [self.tool_map[name] for name in allowed_tools]
