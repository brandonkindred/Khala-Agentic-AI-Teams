from __future__ import annotations

from pathlib import Path

from studiogrid.runtime.orchestrator import Orchestrator
from studiogrid.runtime.registry_loader import RegistryLoader
from studiogrid.runtime.router import PhaseRouter
from studiogrid.runtime.storage.postgres_store import PostgresStore
from studiogrid.runtime.storage.s3_store import S3Store
from studiogrid.runtime.strands_runtime import StrandsAgentExecutor
from studiogrid.runtime.tool_factory import ToolFactory
from studiogrid.tools import asset_export_tool, contrast_check_tool, figma_tool, notify_tool, token_export_tool

_STORE = PostgresStore()
_S3 = S3Store()


def build_orchestrator() -> Orchestrator:
    root = Path(__file__).resolve().parents[1]
    registry = RegistryLoader(root)
    tool_factory = ToolFactory(
        {
            "figma_tool": figma_tool.run,
            "asset_export_tool": asset_export_tool.run,
            "token_export_tool": token_export_tool.run,
            "contrast_check_tool": contrast_check_tool.run,
            "notify_tool": notify_tool.run,
        }
    )
    executor = StrandsAgentExecutor(registry=registry, tool_factory=tool_factory)
    return Orchestrator(
        store=_STORE,
        s3=_S3,
        registry=executor,
        validators={},
        router=PhaseRouter(),
        policies={},
    )
