"""
Re-export from central llm_service. Blogging code should use llm_service directly.

New code should prefer get_strands_model() for Strands Agent integration.
"""

from __future__ import annotations

from llm_service import (
    DummyLLMClient,
    LLMClient,
    LLMError,
    LLMJsonParseError,
    LLMTruncatedError,
    OllamaLLMClient,
    get_strands_model,
)

__all__ = [
    "DummyLLMClient",
    "LLMClient",
    "LLMError",
    "LLMJsonParseError",
    "LLMTruncatedError",
    "OllamaLLMClient",
    "get_strands_model",
]
