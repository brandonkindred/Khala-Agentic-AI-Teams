"""
Re-export from central llm_service. Blogging code should use llm_service directly.

This module is kept for backward compatibility. New code should use:
  from llm_service import get_client, LLMClient, DummyLLMClient, OllamaLLMClient
"""

from __future__ import annotations

from llm_service import (
    DummyLLMClient,
    LLMClient,
    LLMError,
    LLMJsonParseError,
    LLMTruncatedError,
    OllamaLLMClient,
    get_client,
)

__all__ = [
    "DummyLLMClient",
    "LLMClient",
    "LLMError",
    "LLMJsonParseError",
    "LLMTruncatedError",
    "OllamaLLMClient",
    "get_client",
]
