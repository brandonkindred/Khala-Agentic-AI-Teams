"""Team-level configuration constants for AI Agent Development."""

from __future__ import annotations

REQUIRED_ARTIFACT_HINTS: tuple[str, ...] = (
    "blueprint",
    "evaluation",
    "safety",
    "runbook",
    "mcp",
)

ARTIFACT_GATE_DESCRIPTION_PREFIX = "Missing expected artifact category: "

PLACEHOLDER_ARTIFACT_DIR = "ai_system"
